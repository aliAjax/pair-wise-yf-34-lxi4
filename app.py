#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path); self._local = threading.local()
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS occupations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            slot_min_lon REAL NOT NULL, slot_min_lat REAL NOT NULL, slot_max_lon REAL NOT NULL, slot_max_lat REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
            basis_kind TEXT NOT NULL, restriction_id INTEGER REFERENCES restrictions(id), authorization_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active', displaced_by INTEGER REFERENCES occupations(id), displaced_reason TEXT,
            original_basis_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_occ_plan ON occupations(plan_id);
        CREATE INDEX IF NOT EXISTS idx_occ_status ON occupations(status);
        CREATE INDEX IF NOT EXISTS idx_occ_slot ON occupations(starts_at,ends_at,status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_occ_one_active ON occupations(plan_id) WHERE status='active';
        CREATE TABLE IF NOT EXISTS settlements(
            id INTEGER PRIMARY KEY AUTOINCREMENT, offline_id TEXT UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'in_progress',
            trigger_plan_id INTEGER, trigger_restriction_id INTEGER, detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settlement_steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT, settlement_id INTEGER NOT NULL REFERENCES settlements(id), seq INTEGER NOT NULL,
            kind TEXT NOT NULL, ref_key TEXT NOT NULL, ref_id INTEGER, status TEXT NOT NULL DEFAULT 'pending',
            detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(settlement_id,seq), UNIQUE(settlement_id,ref_key)
        );
        CREATE INDEX IF NOT EXISTS idx_steps_pending ON settlement_steps(status);
        """)

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        cached = getattr(self._local, "conn", None)
        if cached is None:
            cached = self._open_conn(); self._local.conn = cached
        return cached

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            restriction = conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone()
            displaced = self._displace_by_restriction(conn, restriction)
            if displaced:
                Repository.audit(conn, None, actor, role, "restriction_displaced_occupations", {"restriction_id": restriction["id"], "displaced": displaced})
                for item in displaced:
                    Repository.notify(conn, item["plan_id"], "displaced", f"飞行计划 {item['callsign']} 航路时段因临时限制被挤占，已进入待重排并保留原依据")
            result = dict(restriction)
            result["displaced_occupations"] = displaced
            return result

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    # ---- 占用（航路时段）结算 ----
    @staticmethod
    def _slot_bbox(plan: sqlite3.Row) -> tuple[float, float, float, float]:
        return route_bbox(DroneAirspaceService._route(plan))

    @staticmethod
    def _occ_overlaps(occ: sqlite3.Row, bbox: tuple[float, float, float, float], start: datetime, end: datetime) -> bool:
        obox = (occ["slot_min_lon"], occ["slot_min_lat"], occ["slot_max_lon"], occ["slot_max_lat"])
        return boxes_overlap(bbox, obox, 0.002) and times_overlap(start, end, parse_time(occ["starts_at"]), parse_time(occ["ends_at"]))

    @staticmethod
    def _restriction_blocks(restriction: sqlite3.Row, bbox: tuple[float, float, float, float], start: datetime, end: datetime, altitude: float) -> bool:
        rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
        if not boxes_overlap(bbox, rbox): return False
        if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): return False
        return altitude > restriction["min_altitude"] and restriction["max_altitude"] > 0

    def _insert_occupation(self, conn: sqlite3.Connection, plan: sqlite3.Row, *, basis_kind: str,
                            restriction_id: int | None = None, authorization_id: int | None = None,
                            status: str = "active", displaced_by: int | None = None, displaced_reason: str | None = None,
                            original_basis: dict[str, Any] | None = None) -> int:
        bbox = self._slot_bbox(plan)
        cur = conn.execute("""INSERT INTO occupations(plan_id,plan_revision,slot_min_lon,slot_min_lat,slot_max_lon,slot_max_lat,
                            starts_at,ends_at,basis_kind,restriction_id,authorization_id,status,displaced_by,displaced_reason,
                            original_basis_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (plan["id"], plan["revision"], bbox[0], bbox[1], bbox[2], bbox[3], plan["starts_at"], plan["ends_at"],
                            basis_kind, restriction_id, authorization_id, status, displaced_by, displaced_reason,
                            json.dumps(original_basis or {}, ensure_ascii=False), iso(), iso()))
        return int(cur.lastrowid)

    def _active_occupation_for_plan(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM occupations WHERE plan_id=? AND status='active' ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()

    def _create_ordinary_occupation(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> None:
        """常规批准计划占用航路时段；同一计划只保留一份有效占用。"""
        if self._active_occupation_for_plan(conn, plan["id"]): return
        self._insert_occupation(conn, plan, basis_kind="approved", original_basis={"plan_revision": plan["revision"]})

    def _invalidate_plan_occupations(self, conn: sqlite3.Connection, plan_id: int, new_revision: int, *, future_only: bool = True) -> int:
        """计划版本变化后，依据旧版本、尚未生效的占用立即失效。"""
        now = iso()
        rows = list(conn.execute("SELECT id FROM occupations WHERE plan_id=? AND plan_revision<? AND status IN ('active','displaced')", (plan_id, new_revision)))
        changed = 0
        for row in rows:
            occ = conn.execute("SELECT * FROM occupations WHERE id=?", (row["id"],)).fetchone()
            if future_only and parse_time(occ["starts_at"]) <= utcnow(): continue
            conn.execute("UPDATE occupations SET status='invalid', displaced_by=NULL, displaced_reason=NULL, updated_at=? WHERE id=?", (now, row["id"]))
            changed += 1
        return changed

    def _displace_by_restriction(self, conn: sqlite3.Connection, restriction: sqlite3.Row) -> list[dict[str, Any]]:
        """新限制压住尚未生效的有效占用：挤掉并保留依据，待限制解除后补位。"""
        displaced: list[dict[str, Any]] = []
        for occ in conn.execute("SELECT * FROM occupations WHERE status='active' ORDER BY id"):
            plan = self._plan_row(conn, occ["plan_id"])
            if parse_time(occ["starts_at"]) <= utcnow(): continue
            bbox = (occ["slot_min_lon"], occ["slot_min_lat"], occ["slot_max_lon"], occ["slot_max_lat"])
            if not self._restriction_blocks(restriction, bbox, parse_time(occ["starts_at"]), parse_time(occ["ends_at"]), plan["max_altitude"]): continue
            basis = json.loads(occ["original_basis_json"] or "{}")
            basis["displaced_by_restriction"] = {"restriction_id": restriction["id"], "name": restriction["name"], "revision_at": plan["revision"]}
            conn.execute("UPDATE occupations SET status='displaced', displaced_reason='restriction', original_basis_json=?, updated_at=? WHERE id=?",
                         (json.dumps(basis, ensure_ascii=False), iso(), occ["id"]))
            displaced.append({"occupation_id": occ["id"], "plan_id": occ["plan_id"], "callsign": plan["callsign"]})
        return displaced

    def _rebalance(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """限制解除或授权撤销后，按起飞时间、版本为待重排占用重新补位。"""
        now = utcnow(); reinstated: list[dict[str, Any]] = []
        rows = list(conn.execute("""SELECT o.* FROM occupations o JOIN flight_plans p ON p.id=o.plan_id
                                    WHERE o.status='displaced' ORDER BY o.starts_at ASC, o.plan_revision DESC, o.id ASC"""))
        for occ in rows:
            plan = self._plan_row(conn, occ["plan_id"])
            if plan["revision"] != occ["plan_revision"]: continue  # 依据版本已变，不能补位
            if parse_time(occ["starts_at"]) <= now: continue      # 已到起飞时间，不再补位
            if self._active_occupation_for_plan(conn, occ["plan_id"]): continue
            bbox = (occ["slot_min_lon"], occ["slot_min_lat"], occ["slot_max_lon"], occ["slot_max_lat"])
            start, end = parse_time(occ["starts_at"]), parse_time(occ["ends_at"])
            blocked = False
            for other in conn.execute("SELECT * FROM occupations WHERE status='active' AND id!=? AND plan_id!=?", (occ["id"], occ["plan_id"])):
                if self._occ_overlaps(other, bbox, start, end): blocked = True; break
            if not blocked:
                for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
                    if self._restriction_blocks(restriction, bbox, start, end, plan["max_altitude"]): blocked = True; break
            if blocked: continue
            conn.execute("UPDATE occupations SET status='active', displaced_by=NULL, displaced_reason=NULL, updated_at=? WHERE id=?", (iso(), occ["id"]))
            reinstated.append({"occupation_id": occ["id"], "plan_id": occ["plan_id"], "callsign": plan["callsign"]})
        return reinstated

    def _settlement_result(self, conn: sqlite3.Connection, settlement: sqlite3.Row) -> dict[str, Any]:
        steps = [dict(r) for r in conn.execute("SELECT * FROM settlement_steps WHERE settlement_id=? ORDER BY seq", (settlement["id"],))]
        return {"settlement_id": settlement["id"], "offline_id": settlement["offline_id"], "kind": settlement["kind"],
                "status": settlement["status"], "detail": json.loads(settlement["detail_json"] or "{}"),
                "steps": steps}

    def _execute_pending_steps(self, settlement_id: int) -> None:
        """逐笔提交通知步骤：已完成的保留，只重试未写入的；同事务内写通知+置位，崩溃不产生重复。"""
        while True:
            with self.repo.tx() as conn:
                step = conn.execute("SELECT * FROM settlement_steps WHERE settlement_id=? AND status IN ('pending','failed') ORDER BY seq LIMIT 1",
                                    (settlement_id,)).fetchone()
                if not step: break
                try:
                    if step["kind"] == "notification":
                        _, plan_id_s, kind = step["ref_key"].split(":", 2)
                        plan = self._plan_row(conn, int(plan_id_s))
                        messages = {
                            "rescue_settled": f"飞行计划 {plan['callsign']} 应急救援授权已结算，占用航路时段",
                            "displaced": f"飞行计划 {plan['callsign']} 航路时段被应急救援任务挤占，已进入待重排并保留原依据",
                            "reinstated": f"飞行计划 {plan['callsign']} 航路时段已重新补位",
                        }
                        Repository.notify(conn, int(plan_id_s), kind, messages.get(kind, f"飞行计划 {plan['callsign']} 结算通知"))
                    conn.execute("UPDATE settlement_steps SET status='done', detail_json=?, updated_at=? WHERE id=?",
                                 (step["detail_json"], iso(), step["id"]))
                except Exception as exc:
                    conn.execute("UPDATE settlement_steps SET status='failed', detail_json=?, updated_at=? WHERE id=?",
                                 (json.dumps({"error": str(exc)}, ensure_ascii=False), iso(), step["id"]))
                    raise
        with self.repo.tx() as conn:
            pending = conn.execute("SELECT COUNT(*) AS c FROM settlement_steps WHERE settlement_id=? AND status!='done'", (settlement_id,)).fetchone()["c"]
            if pending == 0:
                conn.execute("UPDATE settlements SET status='completed', updated_at=? WHERE id=?", (iso(), settlement_id))

    def resume_settlements(self) -> dict[str, Any]:
        """进程重启后恢复未完成的结算：只补写未落盘的步骤，不重复占位或通知。"""
        resumed = 0
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM settlements WHERE status='in_progress' ORDER BY id"))
        for row in rows:
            self._execute_pending_steps(row["id"]); resumed += 1
        return {"resumed": resumed}

    def list_occupations(self, role: str, operator: str, plan_id: int | None = None, status: str | None = None) -> dict[str, Any]:
        clauses, params = [], []
        if plan_id is not None: clauses.append("o.plan_id=?"); params.append(plan_id)
        if status: clauses.append("o.status=?"); params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = []
        for occ in self.repo.conn.execute(f"SELECT o.*, p.operator_id, p.callsign FROM occupations o JOIN flight_plans p ON p.id=o.plan_id{where} ORDER BY o.id DESC", params):
            item = dict(occ)
            if role == "operator" and item["operator_id"] != operator: continue
            item["original_basis"] = json.loads(item.pop("original_basis_json") or "{}")
            rows.append(item)
        return {"occupations": rows}

    def settle_rescue(self, plan_id: int, actor: str, role: str, offline_id: str, reason: str) -> dict[str, Any]:
        """应急救援抢占结算：同一航路同一时段只保留一份有效占用；救援挤掉版本未变的普通计划。

        指挥官并发抢占时由 BEGIN IMMEDIATE 串行化：先结算者生效，后到者看到当前占用。
        占用的写入在同一事务内完成（原子），通知步骤逐笔落盘并可恢复，崩溃不重复占位/通知。
        """
        if role != "commander": raise ApiError(403, "rescue_forbidden", "只有指挥官可以执行应急救援抢占结算")
        offline_id = (offline_id or "").strip()
        if not offline_id: raise ApiError(400, "offline_id_required", "应急救援结算必须提供 offline_id 用于幂等")
        with self.repo.tx() as conn:
            existing = conn.execute("SELECT * FROM settlements WHERE offline_id=?", (offline_id,)).fetchone()
            if existing:
                if existing["status"] == "completed": return self._settlement_result(conn, existing)
                settlement = existing  # 崩溃恢复：继续补写未落盘步骤
            else:
                plan = self._plan_row(conn, plan_id)
                if plan["status"] != "approved": raise ApiError(409, "plan_not_approved", "只有已批准计划可以执行应急救援结算")
                bbox = self._slot_bbox(plan); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
                overlaps = [o for o in conn.execute("SELECT * FROM occupations WHERE status='active' ORDER BY id")
                            if o["plan_id"] != plan_id and self._occ_overlaps(o, bbox, start, end)]
                rescue_overlap = [o for o in overlaps if o["basis_kind"] == "rescue"]
                ordinary_overlap = [o for o in overlaps if o["basis_kind"] == "approved"]
                won_slot = not rescue_overlap
                cur = conn.execute("""INSERT INTO settlements(offline_id,kind,status,trigger_plan_id,detail_json,created_at,updated_at)
                                      VALUES(?,?, 'in_progress',?,?,?,?)""",
                                   (offline_id, "rescue_preemption", plan_id,
                                    json.dumps({"reason": reason, "won_slot": won_slot,
                                                "displaced_plan_ids": [o["plan_id"] for o in ordinary_overlap],
                                                "current_rescue_occupation": rescue_overlap[0]["id"] if rescue_overlap else None},
                                               ensure_ascii=False), iso(), iso()))
                sid = int(cur.lastrowid)
                seq = 0
                existing_occ = conn.execute("SELECT * FROM occupations WHERE plan_id=? AND basis_kind='rescue' ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
                if existing_occ:
                    rescue_occ_id = existing_occ["id"]  # 已存在救援占用（有效或待重排），复用不重复占位
                else:
                    rescue_occ_id = self._insert_occupation(
                        conn, plan, basis_kind="rescue",
                        status="active" if won_slot else "displaced",
                        displaced_by=rescue_overlap[0]["id"] if rescue_overlap else None,
                        displaced_reason="slot_occupied_by_rescue" if rescue_overlap else None,
                        original_basis={"offline_id": offline_id, "reason": reason, "actor": actor, "plan_revision": plan["revision"]})
                conn.execute("INSERT INTO settlement_steps(settlement_id,seq,kind,ref_key,ref_id,status,created_at,updated_at) VALUES(?,?,?,?,?, 'done',?,?)",
                             (sid, seq, "rescue_occupation", f"rescue:{plan_id}", rescue_occ_id, iso(), iso())); seq += 1
                if won_slot:
                    for o in ordinary_overlap:
                        basis = json.loads(o["original_basis_json"] or "{}")
                        basis["displaced_by_rescue"] = {"settlement_id": sid, "rescue_plan_id": plan_id, "rescue_offline_id": offline_id}
                        conn.execute("UPDATE occupations SET status='displaced', displaced_by=?, displaced_reason='rescue_preempted', original_basis_json=?, updated_at=? WHERE id=?",
                                     (rescue_occ_id, json.dumps(basis, ensure_ascii=False), iso(), o["id"]))
                        conn.execute("INSERT INTO settlement_steps(settlement_id,seq,kind,ref_key,ref_id,status,created_at,updated_at) VALUES(?,?,?,?,?, 'done',?,?)",
                                     (sid, seq, "displace", f"displace:{o['plan_id']}:{o['id']}", o["id"], iso(), iso())); seq += 1
                # 通知步骤逐笔落盘（事务外执行），崩溃只补未写的
                conn.execute("INSERT INTO settlement_steps(settlement_id,seq,kind,ref_key,ref_id,status,created_at,updated_at) VALUES(?,?,?,?,?, 'pending',?,?)",
                             (sid, seq, "notification", f"notify:{plan_id}:rescue_settled", None, iso(), iso())); seq += 1
                for o in ordinary_overlap if won_slot else []:
                    conn.execute("INSERT INTO settlement_steps(settlement_id,seq,kind,ref_key,ref_id,status,created_at,updated_at) VALUES(?,?,?,?,?, 'pending',?,?)",
                                 (sid, seq, "notification", f"notify:{o['plan_id']}:displaced", None, iso(), iso())); seq += 1
                if not won_slot:
                    Repository.audit(conn, plan_id, actor, role, "rescue_preempted_by_rescue",
                                     {"current_occupation_id": rescue_overlap[0]["id"], "current_plan_id": rescue_overlap[0]["plan_id"]})
                else:
                    Repository.audit(conn, plan_id, actor, role, "rescue_settled",
                                     {"displaced_plan_ids": [o["plan_id"] for o in ordinary_overlap]})
                settlement = conn.execute("SELECT * FROM settlements WHERE id=?", (sid,)).fetchone()
            result = self._settlement_result(conn, settlement)
        self._execute_pending_steps(settlement["id"])
        with self.repo.tx() as conn:
            settlement = conn.execute("SELECT * FROM settlements WHERE id=?", (settlement["id"],)).fetchone()
            result = self._settlement_result(conn, settlement)
        result["won_slot"] = json.loads(settlement["detail_json"] or "{}").get("won_slot")
        return result

    def revoke_rescue(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """撤销应急授权：救援占用失效，被挤掉的普通计划按起飞时间和版本补位。"""
        if role != "commander": raise ApiError(403, "rescue_forbidden", "只有指挥官可以撤销应急授权")
        offline_id = str(body.get("offline_id", "")).strip()
        if not offline_id: raise ApiError(400, "offline_id_required", "必须提供应急授权的 offline_id")
        with self.repo.tx() as conn:
            approval = conn.execute("SELECT * FROM approvals WHERE offline_id=? AND decision='approved' AND override_kind='emergency_authority'", (offline_id,)).fetchone()
            if not approval: raise ApiError(404, "rescue_not_found", "未找到该应急授权")
            plan = self._plan_row(conn, approval["plan_id"])
            conn.execute("UPDATE occupations SET status='invalid', updated_at=? WHERE plan_id=? AND basis_kind='rescue' AND status='active'", (iso(), plan["id"]))
            reinstated = self._rebalance(conn)
            Repository.audit(conn, plan["id"], actor, role, "rescue_revoked", {"offline_id": offline_id, "reinstated": reinstated})
            Repository.notify(conn, plan["id"], "rescue_revoked", f"飞行计划 {plan['callsign']} 应急授权已撤销")
            for item in reinstated:
                Repository.notify(conn, item["plan_id"], "reinstated", f"飞行计划 {item['callsign']} 航路时段已重新补位")
        return {"revoked": True, "plan_id": plan["id"], "reinstated": reinstated}

    def revoke_restriction(self, restriction_id: int, actor: str, role: str) -> dict[str, Any]:
        """解除限制：被挤掉的占用按起飞时间和版本补位。"""
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以解除限制")
        with self.repo.tx() as conn:
            restriction = conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone()
            if not restriction: raise ApiError(404, "restriction_not_found", "限制不存在")
            if restriction["status"] != "active": return {"revoked": False, "idempotent": True, "reinstated": []}
            conn.execute("UPDATE restrictions SET status='revoked' WHERE id=?", (restriction_id,))
            reinstated = self._rebalance(conn)
            Repository.audit(conn, None, actor, role, "restriction_revoked", {"restriction_id": restriction_id, "reinstated": reinstated})
            for item in reinstated:
                Repository.notify(conn, item["plan_id"], "reinstated", f"飞行计划 {item['callsign']} 航路时段已重新补位")
        return {"revoked": True, "restriction_id": restriction_id, "reinstated": reinstated}

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        occ = conn.execute("SELECT * FROM occupations WHERE plan_id=? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
        if occ:
            result["occupation"] = {"id": occ["id"], "status": occ["status"], "basis_kind": occ["basis_kind"],
                                    "displaced_by": occ["displaced_by"], "displaced_reason": occ["displaced_reason"]}
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                  VALUES(?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, iso()))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            else: self._create_ordinary_occupation(conn, plan)
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            approval_id = cur.lastrowid
        result = {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": approval_id, "override_kind": override_kind}
        if override_kind:
            result["rescue_settlement"] = self.settle_rescue(plan_id, actor, role, offline_id, override or reason)
        return result

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"]})
            invalidated = self._invalidate_plan_occupations(conn, plan_id, revision)
            if invalidated: Repository.audit(conn, plan_id, actor, role, "occupations_invalidated_by_change", {"to_revision": revision, "count": invalidated})
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            conn.execute("UPDATE occupations SET status='invalid', displaced_by=NULL, displaced_reason=NULL, updated_at=? WHERE plan_id=? AND status!='invalid'", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(rows)}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        return {"plans": plans, "restrictions": restrictions, "server_time": iso()}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        if path == "/api/occupations":
            query = urlparse(self.path).query
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            plan_id = int(params["plan_id"]) if params.get("plan_id", "").isdigit() else None
            return 200, self.service.list_occupations(role, operator, plan_id=plan_id, status=params.get("status"))
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if path == "/api/rescue/settle":
            return 200, self.service.settle_rescue(int(body["plan_id"]), actor, role, str(body.get("offline_id", "")), str(body.get("reason", "")))
        if path == "/api/rescue/revoke": return 200, self.service.revoke_rescue(actor, role, body)
        if path == "/api/settlements/resume": return 200, self.service.resume_settlements()
        if len(parts) == 4 and parts[:2] == ["api", "restrictions"] and parts[2].isdigit() and parts[3] == "revoke":
            return 200, self.service.revoke_restriction(int(parts[2]), actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path)
    service.resume_settlements()  # 重启后恢复未落盘的结算步骤，不重复占位或通知
    handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
