#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only).

抢占结算模型：
- occupancies 是航路 × 时段占用台账，同一航路同一时段只允许一份 active 占用。
- 普通批准产生 normal 占用；指挥官的应急救援授权产生 rescue 占用，并把版本未变的
  normal 占用挤成 preempted，计划进入 awaiting_reschedule，原批准依据随占用保留。
- 限制解除、授权撤销、取消、到期都会触发补位（按起飞时间、再按版本）。
- 限制新设或计划一变，尚未生效的 normal 占用立即 invalidated。
- 所有结算通知/审计先写 outbox，再按 event_key 幂等投递；进程重启只补没写入的部分，
  并通过启动对账修复“占用台账与事实不一致”的半成品状态，绝不重复占位或通知。
"""
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
OCC_ACTIVE = "active"
OCC_ENDED = {"preempted", "invalidated", "revoked", "canceled", "completed", "expired"}


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
        self.path = str(path)
        self._local = threading.local()
        self.conn  # 初始化主连接并建表

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn(); self._local.conn = conn
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        ready = getattr(self._local, "schema_ready", False)
        if ready: return
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            released_at TEXT, release_reason TEXT, created_at TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS rescue_authorizations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            commander TEXT NOT NULL, mission TEXT NOT NULL, approval_id INTEGER NOT NULL REFERENCES approvals(id),
            offline_id TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'active', revoke_reason TEXT, created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS occupancies(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            plan_revision INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'normal', status TEXT NOT NULL DEFAULT 'active',
            cycle INTEGER NOT NULL DEFAULT 1, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, bbox_json TEXT NOT NULL,
            basis_json TEXT NOT NULL, rescue_authorization_id INTEGER REFERENCES rescue_authorizations(id),
            displaced_by_occupancy_id INTEGER REFERENCES occupancies(id),
            displaced_key TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_occ_status ON occupancies(status);
        CREATE INDEX IF NOT EXISTS idx_occ_plan ON occupancies(plan_id);
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, event_key TEXT UNIQUE, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL, event_type TEXT NOT NULL,
            plan_id INTEGER, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
            notified_at TEXT, audited_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, event_key TEXT UNIQUE, created_at TEXT NOT NULL
        );
        """)
        self._local.schema_ready = True

    @contextmanager
    def tx(self):
        conn = self.conn  # 确保连接已建表（避免在空快照上开启事务）
        conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception:
            try: self.conn.execute("ROLLBACK")
            except sqlite3.Error: pass
            raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any], event_key: str | None = None) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,event_key,created_at) VALUES(?,?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), event_key, iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str, event_key: str | None = None) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,event_key,created_at) VALUES(?,?,?,?,?)", (plan_id, kind, message, event_key, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path, run_startup: bool = True):
        self.repo = Repository(path)
        if run_startup: self.startup()

    # ---------------------------------------------------------------- 身份
    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    # ---------------------------------------------------------------- 发件箱（幂等投递：失败只重试未写入部分）
    @staticmethod
    def _enqueue(conn: sqlite3.Connection, event_key: str, event_type: str, plan_id: int | None, payload: dict[str, Any]) -> None:
        conn.execute("INSERT OR IGNORE INTO outbox(event_key,event_type,plan_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (event_key, event_type, plan_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), iso()))

    def _deliver_notification(self, conn: sqlite3.Connection, event: sqlite3.Row, note: dict[str, Any]) -> None:
        conn.execute("INSERT OR IGNORE INTO notifications(plan_id,kind,message,event_key,created_at) VALUES(?,?,?,?,?)",
                     (event["plan_id"], note["kind"], note["message"], event["event_key"], iso()))

    def _deliver_audit(self, conn: sqlite3.Connection, event: sqlite3.Row, audit: dict[str, Any]) -> None:
        conn.execute("INSERT OR IGNORE INTO audit_log(plan_id,actor,role,action,detail_json,event_key,created_at) VALUES(?,?,?,?,?,?,?)",
                     (event["plan_id"], audit["actor"], audit["role"], audit["action"],
                      json.dumps(audit.get("detail", {}), ensure_ascii=False, sort_keys=True), event["event_key"], iso()))

    def drain_outbox(self, limit: int = 200) -> int:
        """把已提交的结算事件逐条幂等投递；每个写入独立事务，完成的保留，只重试没写入的。"""
        delivered = 0
        rows = self.repo.conn.execute("SELECT * FROM outbox WHERE notified_at IS NULL OR audited_at IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if row["notified_at"] is None:
                try:
                    with self.repo.tx() as conn:
                        if payload.get("notify"):
                            self._deliver_notification(conn, row, payload["notify"]); delivered += 1
                        conn.execute("UPDATE outbox SET notified_at=? WHERE id=?", (iso(), row["id"]))
                except Exception as exc:  # 本事件通知没写入：保留，下轮只重试它，不阻断其他事件
                    print(f"outbox notification retry pending for {row['event_key']}: {exc!r}")
            if row["audited_at"] is None:
                try:
                    with self.repo.tx() as conn:
                        if payload.get("audit"): self._deliver_audit(conn, row, payload["audit"])
                        conn.execute("UPDATE outbox SET audited_at=? WHERE id=?", (iso(), row["id"]))
                except Exception as exc:
                    print(f"outbox audit retry pending for {row['event_key']}: {exc!r}")
        return delivered

    def startup(self) -> dict[str, Any] | None:
        """进程重启后：修复半成品结算状态并补投递，不重复占位或通知。"""
        report = self.reconcile(); self.drain_outbox(); return report

    # ---------------------------------------------------------------- 空域限制
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
            rid = cur.lastrowid
            row = conn.execute("SELECT * FROM restrictions WHERE id=?", (rid,)).fetchone()
            affected = self._apply_restriction_locked(conn, row)
            Repository.audit(conn, rid, actor, role, "restriction_created", {"name": name, "kind": kind, "affected_plan_ids": affected})
        self.drain_outbox()
        return dict(row)

    def release_restriction(self, restriction_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以解除限制")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "解除原因必填")
        with self.repo.tx() as conn:
            row = conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone()
            if not row: raise ApiError(404, "restriction_not_found", "空域限制不存在")
            if row["status"] != "active":
                backfilled = self._backfill_locked(conn, reason=f"restriction:{restriction_id}:released")
                return {"restriction_id": restriction_id, "idempotent": True, "backfilled": backfilled}
            conn.execute("UPDATE restrictions SET status='released',released_at=?,release_reason=? WHERE id=?", (iso(), reason, restriction_id))
            Repository.audit(conn, None, actor, role, "restriction_released", {"restriction_id": restriction_id, "reason": reason})
            backfilled = self._backfill_locked(conn, reason=f"restriction:{restriction_id}:released")
        self.drain_outbox()
        return {"restriction_id": restriction_id, "idempotent": False, "backfilled": backfilled}

    def _active_restriction_blockers(self, conn: sqlite3.Connection, bbox: tuple[float, float, float, float], start: datetime, end: datetime, altitude: float) -> list[sqlite3.Row]:
        blockers = []
        for r in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (r["min_lon"], r["min_lat"], r["max_lon"], r["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(r["starts_at"]), parse_time(r["ends_at"])): continue
            if altitude > r["min_altitude"] and r["max_altitude"] > 0: blockers.append(r)
        return blockers

    def _apply_restriction_locked(self, conn: sqlite3.Connection, restriction: sqlite3.Row) -> list[int]:
        """限制一变：还没生效的 normal 占用立即失效（已生效占用继续完成，救援不受常规限制影响）。"""
        now = iso(); rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
        affected: list[int] = []
        for occ in conn.execute("SELECT * FROM occupancies WHERE status='active' AND kind='normal'"):
            plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
            if not plan or plan["revision"] != occ["plan_revision"]: continue
            if occ["starts_at"] <= now: continue  # 已经生效，不再抢占
            bbox = tuple(json.loads(occ["bbox_json"]))
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(parse_time(occ["starts_at"]), parse_time(occ["ends_at"]), parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            if not (plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0): continue
            conn.execute("UPDATE occupancies SET status='invalidated',displaced_key=?,updated_at=? WHERE id=?",
                         (f"restriction:{restriction['id']}", now, occ["id"]))
            conn.execute("UPDATE flight_plans SET status='awaiting_reschedule',updated_at=? WHERE id=?", (now, plan["id"]))
            self._enqueue(conn, f"plan:{plan['id']}:invalidated:occ{occ['id']}:restriction{restriction['id']}", "occupancy_invalidated", plan["id"], {
                "notify": {"kind": "occupancy_invalidated", "message": f"飞行计划 {plan['callsign']} 的空域占用因限制“{restriction['name']}”未生效即失效，进入待重排"},
                "audit": {"actor": "system", "role": "system", "action": "occupancy_invalidated",
                          "detail": {"occupancy_id": occ["id"], "restriction_id": restriction["id"], "restriction_name": restriction["name"]}}})
            affected.append(plan["id"])
        return affected

    # ---------------------------------------------------------------- 计划
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
            return self._plan_view(conn, plan_id, role, operator)

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _active_occupancy_conflicts(self, conn: sqlite3.Connection, plan: sqlite3.Row, *, kind: str) -> list[dict[str, Any]]:
        """同一航路同一时段的当前有效占用（相邻判断沿用 0.002 缓冲）。"""
        bbox = route_bbox(self._route(plan)); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        conflicts: list[dict[str, Any]] = []
        rows = conn.execute("""SELECT o.* FROM occupancies o
                               WHERE o.status='active' AND o.plan_id!=? AND o.starts_at<? AND o.ends_at>?""",
                            (plan["id"], iso(end), iso(start))).fetchall()
        for occ in rows:
            other = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
            if not boxes_overlap(bbox, route_bbox(self._route(other)), 0.002): continue
            auth = None
            if occ["rescue_authorization_id"]:
                ar = conn.execute("SELECT * FROM rescue_authorizations WHERE id=?", (occ["rescue_authorization_id"],)).fetchone()
                auth = {"id": ar["id"], "commander": ar["commander"], "mission": ar["mission"], "offline_id": ar["offline_id"]} if ar else None
            conflicts.append({"occupancy_id": occ["id"], "plan_id": occ["plan_id"], "callsign": other["callsign"],
                              "operator_id": other["operator_id"], "kind": occ["kind"], "starts_at": occ["starts_at"], "ends_at": occ["ends_at"],
                              "commander": auth["commander"] if auth else None, "mission": auth["mission"] if auth else None,
                              "rescue_authorization": auth})
        return conflicts

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        bbox = route_bbox(self._route(plan)); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        restrictions = self._active_restriction_blockers(conn, bbox, start, end, plan["max_altitude"])
        for r in restrictions:
            blocking.append({"code": "airspace_restriction", "restriction_id": r["id"], "name": r["name"], "kind": r["kind"], "reason": r["reason"]})
        current = self._active_occupancy_conflicts(conn, plan, kind="normal")
        if current:
            blocking.append({"code": "adjacent_traffic", "plans": [{"plan_id": c["plan_id"], "callsign": c["callsign"], "operator_id": c["operator_id"],
                                                                    "status": "approved", "starts_at": c["starts_at"], "ends_at": c["ends_at"]} for c in current],
                             "current_occupancies": current, "message": "同一航路同一时段已有有效占用"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    # ---------------------------------------------------------------- 占用视图
    @staticmethod
    def _basis(approval: sqlite3.Row) -> dict[str, Any]:
        return {"approval_id": approval["id"], "plan_revision": approval["plan_revision"], "reviewer": approval["reviewer"],
                "decision": approval["decision"], "reason": approval["reason"], "offline_id": approval["offline_id"],
                "override_kind": approval["override_kind"], "created_at": approval["created_at"]}

    def _occupancy_view(self, conn: sqlite3.Connection, occ: sqlite3.Row) -> dict[str, Any]:
        item = dict(occ)
        item["bbox"] = json.loads(item.pop("bbox_json")); item["basis"] = json.loads(item.pop("basis_json"))
        plan = conn.execute("SELECT callsign,operator_id FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
        item["callsign"] = plan["callsign"]; item["operator_id"] = plan["operator_id"]
        if occ["displaced_by_occupancy_id"]:
            by = conn.execute("SELECT o.*, p.callsign FROM occupancies o JOIN flight_plans p ON p.id=o.plan_id WHERE o.id=?",
                              (occ["displaced_by_occupancy_id"],)).fetchone()
            if by:
                info: dict[str, Any] = {"occupancy_id": by["id"], "plan_id": by["plan_id"], "callsign": by["callsign"], "kind": by["kind"]}
                if by["rescue_authorization_id"]:
                    ar = conn.execute("SELECT * FROM rescue_authorizations WHERE id=?", (by["rescue_authorization_id"],)).fetchone()
                    info["commander"] = ar["commander"]; info["mission"] = ar["mission"]
                    info["rescue_authorization"] = {"id": ar["id"], "commander": ar["commander"], "mission": ar["mission"],
                                                    "plan_revision": ar["plan_revision"], "offline_id": ar["offline_id"],
                                                    "created_at": ar["created_at"]}
                item["displaced_by"] = info
        return item

    def _plan_view(self, conn: sqlite3.Connection, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        occ = conn.execute("SELECT * FROM occupancies WHERE plan_id=? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
        result["occupancy"] = self._occupancy_view(conn, occ) if occ else None
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}:
            result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
            result["rescue_authorizations"] = [dict(r) for r in conn.execute("SELECT * FROM rescue_authorizations WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        with self.repo.tx() as conn:
            return self._plan_view(conn, plan_id, role, operator)

    # ---------------------------------------------------------------- 提交
    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self._plan_view(conn, plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            view = self._plan_view(conn, plan_id, role, operator)
        self.drain_outbox()
        return {"plan": view, "idempotent": False}

    # ---------------------------------------------------------------- 普通批准
    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        if role == "commander" and override:
            body = dict(body); body["mission"] = override
            return self.rescue(plan_id, actor, "commander", body)
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self._plan_view(conn, plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved":
                occ = conn.execute("SELECT * FROM occupancies WHERE plan_id=? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
                return {"plan": self._plan_view(conn, plan_id, role, ""), "idempotent": True,
                        "approval_id": conn.execute("SELECT id FROM approvals WHERE plan_id=? AND decision='approved' ORDER BY id LIMIT 1", (plan_id,)).fetchone()["id"]}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"]: raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at)
                                  VALUES(?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, iso()))
            approval = conn.execute("SELECT * FROM approvals WHERE id=?", (cur.lastrowid,)).fetchone()
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            occ_id = self._insert_occupancy(conn, plan, approval)
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id, "occupancy_id": occ_id})
            self._enqueue(conn, f"plan:{plan_id}:approved:occ{occ_id}", "approved", plan_id,
                          {"notify": {"kind": "approved", "message": f"飞行计划 {plan['callsign']} 已批准"}})
            view = self._plan_view(conn, plan_id, role, "")
        self.drain_outbox()
        return {"plan": view, "idempotent": False, "approval_id": cur.lastrowid, "occupancy_id": occ_id, "override_kind": None}

    # ---------------------------------------------------------------- 应急救援授权（抢占结算）
    def rescue(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "commander": raise ApiError(403, "rescue_forbidden", "只有指挥官可以下达应急救援授权")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        mission, reason = str(body.get("mission", "")).strip(), str(body.get("reason", "")).strip() or str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not mission: raise ApiError(400, "rescue_details_required", "expected_revision、offline_id 和 mission 必填")
        with self.repo.tx() as conn:
            prior_auth = conn.execute("SELECT * FROM rescue_authorizations WHERE offline_id=?", (offline_id,)).fetchone()
            prior_appr = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior_auth:
                if prior_auth["plan_id"] != plan_id or prior_auth["plan_revision"] != expected:
                    raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
                occ = conn.execute("SELECT * FROM occupancies WHERE rescue_authorization_id=? ORDER BY id DESC LIMIT 1", (prior_auth["id"],)).fetchone()
                return {"plan": self._plan_view(conn, plan_id, role, ""), "idempotent": True, "authorization_id": prior_auth["id"],
                        "rescue_occupancy_id": occ["id"], "preempted": []}
            plan = self._plan_row(conn, plan_id)
            if plan["status"] not in {"submitted", "approved"}: raise ApiError(409, "invalid_transition", "当前状态的计划不能下达救援授权")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，授权不能套用")
            result = self._grant_rescue_locked(conn, plan, actor, offline_id, mission, reason)
        self.drain_outbox()
        return result

    def _insert_occupancy(self, conn: sqlite3.Connection, plan: sqlite3.Row, approval: sqlite3.Row, *,
                          kind: str = "normal", cycle: int = 1, rescue_authorization_id: int | None = None) -> int:
        cur = conn.execute("""INSERT INTO occupancies(plan_id,plan_revision,kind,status,cycle,starts_at,ends_at,bbox_json,basis_json,rescue_authorization_id,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (plan["id"], plan["revision"], kind, "active", cycle, plan["starts_at"], plan["ends_at"],
                            json.dumps(route_bbox(self._route(plan))), json.dumps(self._basis(approval), ensure_ascii=False),
                            rescue_authorization_id, iso(), iso()))
        return cur.lastrowid

    def _grant_rescue_locked(self, conn: sqlite3.Connection, plan: sqlite3.Row, actor: str, offline_id: str, mission: str, reason: str) -> dict[str, Any]:
        now = iso()
        report = self._conflict_report(conn, plan)
        if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "救援授权也不能违反载荷与高度硬限制", report)
        conflicts = self._active_occupancy_conflicts(conn, plan, kind="rescue")
        rescue_conflict = next((c for c in conflicts if c["kind"] == "rescue"), None)
        if rescue_conflict:
            raise ApiError(409, "rescue_conflict", f"同一航路同一时段已有救援占用（{rescue_conflict['callsign']}），后到的抢占不生效",
                           {"plan_id": plan["id"], "current_occupancy": rescue_conflict})
        cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                              VALUES(?,?,?,?,?,?,?,?)""", (plan["id"], plan["revision"], actor, "approved", reason or mission, offline_id, "emergency_authority", now))
        approval = conn.execute("SELECT * FROM approvals WHERE id=?", (cur.lastrowid,)).fetchone()
        auth_cur = conn.execute("""INSERT INTO rescue_authorizations(plan_id,plan_revision,commander,mission,approval_id,offline_id,created_at)
                                   VALUES(?,?,?,?,?,?,?)""", (plan["id"], plan["revision"], actor, mission, approval["id"], offline_id, now))
        auth_id = auth_cur.lastrowid
        conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (now, plan["id"]))
        rescue_occ_id = self._insert_occupancy(conn, plan, approval, kind="rescue", rescue_authorization_id=auth_id)
        preempted: list[dict[str, Any]] = []
        # 救援挤掉版本未变的普通占用（同一航路同一时段只保留一份有效占用）
        for c in conflicts:
            occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (c["occupancy_id"],)).fetchone()
            victim = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
            if occ["kind"] != "normal" or victim["revision"] != occ["plan_revision"]: continue
            conn.execute("UPDATE occupancies SET status='preempted',displaced_by_occupancy_id=?,displaced_key=?,updated_at=? WHERE id=?",
                         (rescue_occ_id, f"rescue:{auth_id}", now, occ["id"]))
            conn.execute("UPDATE flight_plans SET status='awaiting_reschedule',updated_at=? WHERE id=?", (now, victim["id"]))
            preempted.append({"plan_id": victim["id"], "occupancy_id": occ["id"], "callsign": victim["callsign"]})
            self._enqueue(conn, f"plan:{victim['id']}:preempted:occ{occ['id']}:by{rescue_occ_id}", "preempted", victim["id"], {
                "notify": {"kind": "preempted", "message": f"飞行计划 {victim['callsign']} 被救援授权“{mission}”（指挥官 {actor}）挤掉，进入待重排并保留原批准依据"},
                "audit": {"actor": actor, "role": "commander", "action": "plan_preempted",
                          "detail": {"plan_id": victim["id"], "occupancy_id": occ["id"], "rescue_authorization_id": auth_id, "mission": mission}}})
        self._enqueue(conn, f"plan:{plan['id']}:rescue:auth{auth_id}", "rescue_granted", plan["id"], {
            "audit": {"actor": actor, "role": "commander", "action": "rescue_authorized",
                      "detail": {"plan_id": plan["id"], "authorization_id": auth_id, "mission": mission, "preempted": preempted}}})
        view = self._plan_view(conn, plan["id"], "commander", "")
        return {"plan": view, "idempotent": False, "authorization_id": auth_id, "approval_id": approval["id"],
                "rescue_occupancy_id": rescue_occ_id, "preempted": preempted, "override_kind": "emergency_authority"}

    def revoke_rescue(self, authorization_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"commander", "airspace_reviewer"}: raise ApiError(403, "rescue_forbidden", "只有指挥官或空域审核员可以撤销救援授权")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "撤销原因必填")
        with self.repo.tx() as conn:
            auth = conn.execute("SELECT * FROM rescue_authorizations WHERE id=?", (authorization_id,)).fetchone()
            if not auth: raise ApiError(404, "rescue_not_found", "救援授权不存在")
            if auth["status"] != "active":
                backfilled = self._backfill_locked(conn, reason=f"rescue:{authorization_id}:revoked")
                return {"authorization_id": authorization_id, "idempotent": True, "backfilled": backfilled}
            now = iso()
            conn.execute("UPDATE rescue_authorizations SET status='revoked',revoke_reason=?,revoked_at=? WHERE id=?", (reason, now, authorization_id))
            rescue_occs = conn.execute("SELECT * FROM occupancies WHERE rescue_authorization_id=? AND status='active'", (authorization_id,)).fetchall()
            released: list[int] = []
            for occ in rescue_occs:
                conn.execute("UPDATE occupancies SET status='revoked',updated_at=? WHERE id=?", (now, occ["id"]))
                plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
                if plan and plan["revision"] == auth["plan_revision"] and plan["status"] == "approved":
                    conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (now, plan["id"]))
                released.append(occ["plan_id"])
            self._enqueue(conn, f"rescue:{authorization_id}:revoked", "rescue_revoked", auth["plan_id"], {
                "audit": {"actor": actor, "role": role, "action": "rescue_revoked",
                          "detail": {"authorization_id": authorization_id, "reason": reason, "plan_ids": released}}})
            backfilled = self._backfill_locked(conn, reason=f"rescue:{authorization_id}:revoked")
        self.drain_outbox()
        return {"authorization_id": authorization_id, "idempotent": False, "backfilled": backfilled}

    # ---------------------------------------------------------------- 拒绝
    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self._plan_view(conn, plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            view = self._plan_view(conn, plan_id, role, "")
        self.drain_outbox()
        return {"plan": view, "idempotent": False}

    # ---------------------------------------------------------------- 变更：决定依据一变，未生效占用立即失效
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
            revision = expected + 1; now = iso()
            was_approved = plan["status"] == "approved"; was_queue = plan["status"] == "awaiting_reschedule"
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, now, plan_id))
            # 计划版本一变：所有未结束占用立即失效
            stale = conn.execute("SELECT * FROM occupancies WHERE plan_id=? AND status='active'", (plan_id,)).fetchall()
            released_clusters: list[dict[str, Any]] = []
            for occ in stale:
                conn.execute("UPDATE occupancies SET status='invalidated',displaced_key=?,updated_at=? WHERE id=?", (f"plan:revision:{revision}", now, occ["id"]))
                released_clusters.append({"occupancy_id": occ["id"], "kind": occ["kind"]})
                self._enqueue(conn, f"plan:{plan_id}:invalidated:occ{occ['id']}:rev{revision}", "occupancy_invalidated", plan_id, {
                    "notify": {"kind": "approval_invalidated", "message": f"飞行计划 {plan['callsign']} 已修改，原空域占用立即失效，需重新提交审核"},
                    "audit": {"actor": actor, "role": role, "action": "occupancy_invalidated", "detail": {"occupancy_id": occ["id"], "from_revision": expected, "to_revision": revision}}})
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"]})
            if not stale:
                if was_approved:
                    Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
                else:
                    Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            backfilled = self._backfill_locked(conn, reason=f"plan:{plan_id}:rev{revision}") if released_clusters else []
            view = self._plan_view(conn, plan_id, role, operator)
        self.drain_outbox()
        return view

    # ---------------------------------------------------------------- 取消
    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self._plan_view(conn, plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            now = iso()
            occs = conn.execute("SELECT * FROM occupancies WHERE plan_id=? AND status='active'", (plan_id,)).fetchall()
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (now, plan_id))
            for occ in occs:
                conn.execute("UPDATE occupancies SET status='canceled',updated_at=? WHERE id=?", (now, occ["id"]))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason, "occupancy_ids": [o["id"] for o in occs]})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            backfilled = self._backfill_locked(conn, reason=f"plan:{plan_id}:canceled") if occs else []
            view = self._plan_view(conn, plan_id, role, operator)
        self.drain_outbox()
        return {"plan": view, "idempotent": False, "backfilled": backfilled}

    # ---------------------------------------------------------------- 到期
    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status IN ('approved','awaiting_reschedule') AND ends_at<=?", (now,)))
            expired: list[int] = []
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                for occ in conn.execute("SELECT * FROM occupancies WHERE plan_id=? AND status='active'", (row["id"],)):
                    conn.execute("UPDATE occupancies SET status=?,updated_at=? WHERE id=?", ("completed" if occ["kind"] == "rescue" else "expired", now, occ["id"]))
                expired.append(row["id"])
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
            backfilled = self._backfill_locked(conn, reason="expire-run") if expired else []
        self.drain_outbox()
        return {"expired": len(rows), "backfilled": backfilled}

    # ---------------------------------------------------------------- 补位结算
    def _backfill_locked(self, conn: sqlite3.Connection, *, reason: str) -> list[dict[str, Any]]:
        """按起飞时间、再按版本，把待重排计划补回空位；依据版本未变且当前无冲突才补位。"""
        backfilled: list[dict[str, Any]] = []; now = iso()
        rows = conn.execute("""SELECT p.*, o.id AS occ_id, o.plan_revision AS occ_plan_revision, o.cycle AS occ_cycle,
                                      o.basis_json AS occ_basis, o.rescue_authorization_id AS occ_rescue
                               FROM flight_plans p JOIN occupancies o ON o.plan_id=p.id
                               WHERE p.status='awaiting_reschedule' AND o.status IN ('preempted','invalidated')
                               ORDER BY p.starts_at ASC, p.revision ASC, o.id ASC""").fetchall()
        latest_per_plan: dict[int, sqlite3.Row] = {}
        for row in rows: latest_per_plan[row["id"]] = row
        for row in sorted(latest_per_plan.values(), key=lambda r: (r["starts_at"], r["revision"], r["occ_id"])):
            if row["revision"] != row["occ_plan_revision"]: continue  # 依据所指版本已变，不自动补位
            plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (row["id"],)).fetchone()
            if plan["status"] != "awaiting_reschedule": continue
            start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
            if end <= utcnow(): continue
            bbox = route_bbox(self._route(plan))
            if self._active_restriction_blockers(conn, bbox, start, end, plan["max_altitude"]): continue
            occupied = False
            for occ in conn.execute("SELECT * FROM occupancies WHERE status='active' AND starts_at<? AND ends_at>?", (plan["ends_at"], plan["starts_at"])):
                other = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
                if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002): occupied = True; break
            if occupied: continue
            basis = json.loads(row["occ_basis"]); cycle = (row["occ_cycle"] or 1) + 1
            new_id = conn.execute("""INSERT INTO occupancies(plan_id,plan_revision,kind,status,cycle,starts_at,ends_at,bbox_json,basis_json,rescue_authorization_id,created_at,updated_at)
                                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (plan["id"], plan["revision"], "normal", "active", cycle, plan["starts_at"], plan["ends_at"],
                                   json.dumps(bbox), json.dumps(basis, ensure_ascii=False), None, now, now)).lastrowid
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (now, plan["id"]))
            info = {"plan_id": plan["id"], "occupancy_id": new_id, "callsign": plan["callsign"], "cycle": cycle,
                    "based_on_approval_offline_id": basis.get("offline_id")}
            backfilled.append(info)
            self._enqueue(conn, f"plan:{plan['id']}:backfilled:occ{new_id}", "backfilled", plan["id"], {
                "notify": {"kind": "backfilled", "message": f"飞行计划 {plan['callsign']} 已按原批准依据补位（第 {cycle} 次占用），空域恢复有效"},
                "audit": {"actor": "system", "role": "system", "action": "plan_backfilled",
                          "detail": {**info, "reason": reason, "previous_occupancy_id": row["occ_id"]}}})
        return backfilled

    # ---------------------------------------------------------------- 启动对账（幂等，可重复执行）
    def reconcile(self) -> dict[str, Any]:
        with self.repo.tx() as conn:
            now = iso()
            # 1) active 占用对应计划版本已变 → 立即失效
            for occ in conn.execute("SELECT * FROM occupancies WHERE status='active'").fetchall():
                plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
                if not plan or plan["revision"] != occ["plan_revision"]:
                    conn.execute("UPDATE occupancies SET status='invalidated',displaced_key=?,updated_at=? WHERE id=?", ("reconcile:revision", now, occ["id"]))
                    if plan and plan["status"] == "approved":
                        conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=?", (now, plan["id"]))
                    continue
                if plan["status"] == "canceled":
                    conn.execute("UPDATE occupancies SET status='canceled',updated_at=? WHERE id=?", (now, occ["id"]))
                    continue
                if plan["ends_at"] <= now:
                    conn.execute("UPDATE occupancies SET status=?,updated_at=? WHERE id=?",
                                 ("completed" if occ["kind"] == "rescue" else "expired", now, occ["id"]))
                    if plan["status"] == "approved": conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, plan["id"]))
            # 2) 撤销已落库但占用仍 active 的半成品（授权撤销后进程被杀）→ 关闭并回退待审
            for occ in conn.execute("""SELECT o.* FROM occupancies o JOIN rescue_authorizations a ON a.id=o.rescue_authorization_id
                                      WHERE o.status='active' AND a.status='revoked'""").fetchall():
                conn.execute("UPDATE occupancies SET status='revoked',updated_at=? WHERE id=?", (now, occ["id"]))
                plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (occ["plan_id"],)).fetchone()
                if plan and plan["status"] == "approved": conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (now, plan["id"]))
            # 3) 同航路同时段多份 active（理论上不该发生）：保留 rescue 优先、最早创建的一份，其余失效
            actives = conn.execute("SELECT * FROM occupancies WHERE status='active' ORDER BY created_at,id").fetchall()
            plans_by_id = {r["id"]: r for r in conn.execute("SELECT * FROM flight_plans")}
            kept: list[sqlite3.Row] = []
            for occ in actives:
                plan = plans_by_id[occ["plan_id"]]
                clash = next((k for k in kept if boxes_overlap(route_bbox(self._route(plan)), route_bbox(self._route(plans_by_id[k["plan_id"]])), 0.002)
                              and times_overlap(parse_time(occ["starts_at"]), parse_time(occ["ends_at"]), parse_time(k["starts_at"]), parse_time(k["ends_at"]))), None)
                if clash is None: kept.append(occ)
                elif occ["kind"] == "rescue" and clash["kind"] != "rescue":
                    conn.execute("UPDATE occupancies SET status='preempted',displaced_by_occupancy_id=?,updated_at=? WHERE id=?", (occ["id"], now, clash["id"]))
                    vp = plans_by_id[clash["plan_id"]]
                    if vp["status"] == "approved": conn.execute("UPDATE flight_plans SET status='awaiting_reschedule',updated_at=? WHERE id=?", (now, vp["id"]))
                    kept = [occ if k["id"] == clash["id"] else k for k in kept]
                else:
                    conn.execute("UPDATE occupancies SET status='invalidated',displaced_key='reconcile:duplicate',updated_at=? WHERE id=?", (now, occ["id"]))
                    if plan["status"] == "approved": conn.execute("UPDATE flight_plans SET status='awaiting_reschedule',updated_at=? WHERE id=?", (now, plan["id"]))
            backfilled = self._backfill_locked(conn, reason="reconcile")
        self.drain_outbox()
        return {"backfilled": backfilled}

    # ---------------------------------------------------------------- 查询
    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def occupancies(self, role: str, operator: str = "") -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("SELECT o.* FROM occupancies o JOIN flight_plans p ON p.id=o.plan_id WHERE p.operator_id=? ORDER BY o.id DESC", (operator,))
            occs = [self._occupancy_view(self.repo.conn, r) for r in rows]
            return {"occupancies": occs, "awaiting_reschedule": []}
        if role not in {"airspace_reviewer", "commander", "auditor"}: raise ApiError(403, "occupancies_forbidden", "当前角色不能读取占用台账")
        occs = [self._occupancy_view(self.repo.conn, r) for r in self.repo.conn.execute("SELECT * FROM occupancies ORDER BY id DESC")]
        queue = []
        with self.repo.tx() as conn:
            for r in conn.execute("SELECT id FROM flight_plans WHERE status='awaiting_reschedule' ORDER BY starts_at,id"):
                queue.append(self._plan_view(conn, r["id"], role, ""))
        return {"occupancies": occs, "awaiting_reschedule": queue}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        with self.repo.tx() as c:
            for row in rows: plans.append(self._plan_view(c, row["id"], role, operator))
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        result: dict[str, Any] = {"plans": plans, "restrictions": restrictions, "server_time": iso()}
        if role in {"airspace_reviewer", "commander", "auditor"}:
            result["occupancies"] = [self._occupancy_view(conn, r) for r in conn.execute("SELECT * FROM occupancies ORDER BY id DESC")]
            result["awaiting_reschedule"] = [p for p in plans if p["status"] == "awaiting_reschedule"]
            result["rescue_authorizations"] = [dict(r) for r in conn.execute("SELECT * FROM rescue_authorizations ORDER BY id DESC")]
        return result


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
        if path == "/api/occupancies": return 200, self.service.occupancies(role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if path == "/api/reconcile":
            if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "reconcile_forbidden", "当前角色不能执行对账")
            return 200, self.service.reconcile()
        if len(parts) == 4 and parts[:2] == ["api", "rescue"] and parts[2].isdigit() and parts[3] == "revoke":
            return 200, self.service.revoke_rescue(int(parts[2]), actor, role, body)
        if len(parts) == 5 and parts[:2] == ["api", "restrictions"] and parts[2].isdigit() and parts[3] == "release":
            return 200, self.service.release_restriction(int(parts[2]), actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
                "rescue": lambda: self.service.rescue(pid, actor, role, body),
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
    handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
