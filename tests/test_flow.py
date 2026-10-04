import json, sys, tempfile, threading, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class DroneFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.db = Path(self.tmp.name) / "test.db"; self.svc = DroneAirspaceService(self.db)
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100, start=None, hours=1, operator="OP1"):
        start = start or self.start
        return self.svc.create_plan("op-user", "operator", operator, {"callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(start), "ends_at": iso(start + timedelta(hours=hours)),
            "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def submit(self, p): return self.svc.submit(p["id"], "op-user", "operator", "OP1", {})["plan"]

    def approve_normal(self, p, offline=None, reviewer="reviewer"):
        return self.svc.approve(p["id"], reviewer, "airspace_reviewer", {"expected_revision": p["revision"], "offline_id": offline or f"off-{p['callsign']}", "reason": "常规审核通过"})

    def rescue(self, p, offline, mission="应急救援", commander="cmd-1"):
        return self.svc.rescue(p["id"], commander, "commander", {"expected_revision": p["revision"], "offline_id": offline, "mission": mission})

    def notifications(self, kind=None):
        notes = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        return [n for n in notes if kind is None or n["kind"] == kind]

    # ------------------------------------------------------------ 原有流程
    def test_full_approval_change_and_offline_reconciliation(self):
        plan = self.plan(); submitted = self.submit(plan)
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertTrue(check["approvable"])
        approved = self.approve_normal(submitted, "offline-1")
        self.assertEqual(approved["plan"]["status"], "approved")
        self.assertEqual(approved["plan"]["occupancy"]["kind"], "normal")
        duplicate = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "补传"})
        self.assertTrue(duplicate["idempotent"])
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": submitted["revision"], "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["status"], "draft"); self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["occupancy"]["status"], "invalidated")
        notifications = self.notifications()
        self.assertEqual(notifications[0]["kind"], "approval_invalidated")
        # 全航路同一时段只有一份有效占用
        active = [o for o in self.svc.occupancies("airspace_reviewer")["occupancies"] if o["status"] == "active"]
        self.assertEqual(active, [])

    def test_restriction_emergency_override_and_conflicts(self):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "临时禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.2, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        plan = self.plan("D101"); self.submit(plan)
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertFalse(check["approvable"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-2", "reason": "常规审核"})
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        override = self.svc.approve(plan["id"], "commander", "commander", {"expected_revision": 1, "offline_id": "offline-3", "reason": "紧急任务", "override_reason": "应急救援授权"})
        self.assertEqual(override["plan"]["status"], "approved")
        self.assertEqual(override["plan"]["occupancy"]["kind"], "rescue")
        conflicting = self.plan("D102", route=[[116.11, 39.81], [116.15, 39.84]])
        self.submit(conflicting)
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-4", "reason": "复核"})
        self.assertIn(ctx.exception.code, {"hard_constraint_violation", "airspace_conflict"})
        details = ctx.exception.details["blocking_conflicts"]
        self.assertTrue(any(c.get("code") == "adjacent_traffic" for c in details))
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 99, "offline_id": "offline-5", "reason": "过期审核"})
        self.assertEqual(ctx.exception.code, "revision_conflict")

    # ------------------------------------------------------------ 抢占
    def test_rescue_preempts_unchanged_plan_and_keeps_basis(self):
        p = self.plan("D200"); self.submit(p); self.approve_normal(p, "off-n")
        r = self.plan("D201", route=[[116.12, 39.82], [116.2, 39.86]])
        self.submit(r)
        result = self.rescue(r, "off-rescue", mission="搜救失踪人员")
        self.assertEqual(result["preempted"], [{"plan_id": p["id"], "occupancy_id": 1, "callsign": "D200"}])
        victim = self.svc.get_plan(p["id"], "operator", "OP1")
        self.assertEqual(victim["status"], "awaiting_reschedule")
        occ = victim["occupancy"]
        self.assertEqual(occ["status"], "preempted")
        self.assertEqual(occ["plan_revision"], 1)  # 版本未变才允许被挤
        # 原依据保留
        self.assertEqual(occ["basis"]["approval_id"], 1)
        self.assertEqual(occ["basis"]["offline_id"], "off-n")
        # 看得到被谁挤掉
        self.assertEqual(occ["displaced_by"]["callsign"], "D201")
        self.assertEqual(occ["displaced_by"]["rescue_authorization"]["commander"], "cmd-1")
        self.assertEqual(occ["displaced_by"]["rescue_authorization"]["mission"], "搜救失踪人员")
        note = self.notifications("preempted")[0]
        self.assertIn("搜救失踪人员", note["message"]); self.assertIn("待重排", note["message"])
        active = [o for o in self.svc.occupancies("airspace_reviewer")["occupancies"] if o["status"] == "active"]
        self.assertEqual([(o["callsign"], o["kind"]) for o in active], [("D201", "rescue")])

    def test_rescue_cannot_preempt_plan_that_changed_version(self):
        # 普通计划改版后其占用已立即失效，救援不需要挤掉任何东西
        p = self.plan("D210"); self.submit(p); self.approve_normal(p, "off-n")
        self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.05, 39.78], [116.09, 39.82]]})
        r = self.plan("D211"); self.submit(r)
        result = self.rescue(r, "off-rescue")
        self.assertEqual(result["preempted"], [])

    def test_concurrent_commanders_only_first_settles(self):
        p = self.plan("D300"); self.submit(p); self.approve_normal(p, "off-n")
        r1 = self.plan("D301", route=[[116.11, 39.81], [116.2, 39.85]]); self.submit(r1)
        r2 = self.plan("D302", route=[[116.12, 39.82], [116.21, 39.86]]); self.submit(r2)
        outcomes = {}
        barrier = threading.Barrier(2)
        def run(key, plan, offline, commander):
            barrier.wait()
            try: outcomes[key] = ("ok", self.rescue(plan, offline, commander=commander))
            except ApiError as exc: outcomes[key] = ("err", exc)
        t1 = threading.Thread(target=run, args=("a", r1, "off-r1", "cmd-A"))
        t2 = threading.Thread(target=run, args=("b", r2, "off-r2", "cmd-B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        winner_key = next(k for k, v in outcomes.items() if v[0] == "ok")
        loser_key = "b" if winner_key == "a" else "a"
        kind, err = outcomes[loser_key]
        self.assertEqual(kind, "err"); self.assertEqual(err.code, "rescue_conflict")
        current = err.details["current_occupancy"]
        self.assertEqual(current["callsign"], {"a": "D301", "b": "D302"}[winner_key])
        self.assertIn(current["commander"], {"cmd-A", "cmd-B"})
        active_rescue = [o for o in self.svc.occupancies("commander")["occupancies"] if o["status"] == "active" and o["kind"] == "rescue"]
        self.assertEqual(len(active_rescue), 1)
        self.assertEqual(len(self.notifications("preempted")), 1)
        # 失败方没有留下任何半成品
        loser_id = r2["id"] if loser_key == "b" else r1["id"]
        loser_plan = self.svc.get_plan(loser_id, "airspace_reviewer")
        self.assertEqual(loser_plan["status"], "submitted")

    # ------------------------------------------------------------ 补位
    def test_backfill_after_rescue_revoke_by_takeoff_time_and_revision(self):
        # 簇 X：D400 被救援挤掉
        px = self.plan("D400", start=self.start + timedelta(hours=3)); self.submit(px); self.approve_normal(px, "off-x")
        rx = self.plan("D401", route=[[116.12, 39.82], [116.2, 39.86]], start=self.start + timedelta(hours=3))
        self.submit(rx); grant = self.rescue(rx, "off-rx")
        auth_id = grant["authorization_id"]
        self.assertEqual(self.svc.get_plan(px["id"], "airspace_reviewer")["status"], "awaiting_reschedule")
        # 簇 Y（不相交空域）：D500 被临时限制压成待重排，起飞时间比 D400 更早
        py = self.plan("D500", route=[[121.1, 31.1], [121.3, 31.3]], start=self.start + timedelta(hours=1))
        self.submit(py); self.approve_normal(py, "off-y")
        restriction = self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "上海管控", "kind": "no_fly",
            "min_lon": 121.0, "min_lat": 31.0, "max_lon": 121.4, "max_lat": 31.4, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=5)), "reason": "活动"})
        self.assertEqual(self.svc.get_plan(py["id"], "airspace_reviewer")["status"], "awaiting_reschedule")
        # 同时解除限制并撤销授权：按起飞时间补位，D500（更早）先结算
        release = self.svc.release_restriction(restriction["id"], "reviewer", "airspace_reviewer", {"reason": "活动结束"})
        revoke = self.svc.revoke_rescue(auth_id, "cmd-1", "commander", {"reason": "救援结束"})
        ordered = release["backfilled"] + revoke["backfilled"]
        self.assertEqual([b["callsign"] for b in ordered], ["D500", "D400"])
        for pid in (px["id"], py["id"]):
            view = self.svc.get_plan(pid, "airspace_reviewer")
            self.assertEqual(view["status"], "approved")
            self.assertEqual(view["occupancy"]["status"], "active")
        # 补位沿用原依据，没有产生新审批
        view = self.svc.get_plan(px["id"], "airspace_reviewer")
        self.assertEqual(view["occupancy"]["basis"]["offline_id"], "off-x")
        self.assertEqual(view["occupancy"]["cycle"], 2)
        self.assertEqual(len(self.notifications("backfilled")), 2)
        # 救援机退回待审
        self.assertEqual(self.svc.get_plan(rx["id"], "airspace_reviewer")["status"], "submitted")

    def test_backfill_skips_plan_whose_revision_changed(self):
        p = self.plan("D410"); self.submit(p); self.approve_normal(p, "off-n")
        r = self.plan("D411", route=[[116.12, 39.82], [116.2, 39.86]]); self.submit(r)
        grant = self.rescue(r, "off-r")
        # 被挤后运营方改了计划：依据版本不再匹配，不能自动补位
        self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.1, 39.81], [116.3, 39.91]]})
        revoke = self.svc.revoke_rescue(grant["authorization_id"], "cmd-1", "commander", {"reason": "结束"})
        self.assertEqual(revoke["backfilled"], [])
        self.assertEqual(self.svc.get_plan(p["id"], "operator", "OP1")["status"], "draft")

    def test_restriction_lift_reopens_invalidated_occupancy(self):
        p = self.plan("D420"); self.submit(p); self.approve_normal(p, "off-n")
        restriction = self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "临时管控", "kind": "temporary_limit",
            "min_lon": 116.05, "min_lat": 39.75, "max_lon": 116.25, "max_lat": 39.95, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=3)), "reason": "安保"})
        view = self.svc.get_plan(p["id"], "airspace_reviewer")
        self.assertEqual(view["status"], "awaiting_reschedule")
        self.assertEqual(view["occupancy"]["status"], "invalidated")
        result = self.svc.release_restriction(restriction["id"], "reviewer", "airspace_reviewer", {"reason": "管控结束"})
        self.assertEqual([b["callsign"] for b in result["backfilled"]], ["D420"])
        view = self.svc.get_plan(p["id"], "airspace_reviewer")
        self.assertEqual(view["status"], "approved"); self.assertEqual(view["occupancy"]["cycle"], 2)

    def test_started_occupancy_survives_new_restriction(self):
        p = self.plan("D430", start=utcnow() + timedelta(minutes=10))
        self.submit(p); self.approve_normal(p, "off-n")
        # 已开始（starts_at 已过）的占用不受新限制影响——用对账场景外的直接语义：未来占用才失效
        future_p = self.plan("D431", start=utcnow() + timedelta(hours=8))
        self.submit(future_p); self.approve_normal(future_p, "off-f")
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "管控", "kind": "no_fly",
            "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.4, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(utcnow() + timedelta(hours=7)), "ends_at": iso(utcnow() + timedelta(hours=10)), "reason": "安保"})
        # D430 起飞在限制生效之前，占用仍有效；D431 在限制窗口内，被立即失效
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer")["occupancy"]["status"], "active")
        self.assertEqual(self.svc.get_plan(future_p["id"], "airspace_reviewer")["status"], "awaiting_reschedule")

    def test_change_invalidates_pending_occupancy_immediately(self):
        p = self.plan("D440"); self.submit(p); self.approve_normal(p, "off-n")
        self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.2, 39.85], [116.4, 39.95]]})
        active = [o for o in self.svc.occupancies("airspace_reviewer")["occupancies"] if o["status"] == "active"]
        self.assertEqual(active, [])
        # 反复对账不会让旧版本占用复活，也不会重复通知
        self.svc.reconcile(); self.svc.reconcile()
        self.assertEqual(len(self.notifications("approval_invalidated")), 1)

    # ------------------------------------------------------------ 幂等/重启/部分失败
    def test_rescue_retry_and_restart_never_duplicates(self):
        p = self.plan("D500"); self.submit(p); self.approve_normal(p, "off-n")
        r = self.plan("D501", route=[[116.12, 39.82], [116.2, 39.86]]); self.submit(r)
        first = self.rescue(r, "off-dup")
        again = self.rescue(r, "off-dup")
        self.assertTrue(again["idempotent"]); self.assertEqual(again["authorization_id"], first["authorization_id"])
        occs = self.svc.occupancies("airspace_reviewer")["occupancies"]
        self.assertEqual(len(occs), 2)  # 普通一条 + 救援一条，无重复
        self.assertEqual(len(self.notifications("preempted")), 1)
        # 模拟关掉进程再启动：新实例打开同一库并执行启动对账
        svc2 = DroneAirspaceService(self.db); svc2.startup()
        occs2 = svc2.occupancies("airspace_reviewer")["occupancies"]
        self.assertEqual([(o["id"], o["status"]) for o in occs2], [(o["id"], o["status"]) for o in occs])
        notes2 = svc2.notifications("op-user", "operator", "OP1")["notifications"]
        notes1 = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        self.assertEqual(len(notes2), len(notes1))

    def test_restart_backfills_when_revoke_committed_but_settlement_pending(self):
        p = self.plan("D510"); self.submit(p); self.approve_normal(p, "off-n")
        r = self.plan("D511", route=[[116.12, 39.82], [116.2, 39.86]]); self.submit(r)
        grant = self.rescue(r, "off-r")
        # 直接构造“撤销已落库但补位未跑”的半成品状态（模拟结算中途进程被杀）
        conn = self.svc.repo.conn
        conn.execute("UPDATE rescue_authorizations SET status='revoked',revoke_reason='x',revoked_at=? WHERE id=?", (iso(), grant["authorization_id"]))
        conn.execute("UPDATE occupancies SET status='revoked' WHERE id=?", (grant["rescue_occupancy_id"],))
        conn.execute("UPDATE flight_plans SET status='submitted' WHERE id=?", (r["id"],))
        svc2 = DroneAirspaceService(self.db); report = svc2.startup() or svc2.reconcile()
        view = svc2.get_plan(p["id"], "airspace_reviewer")
        self.assertEqual(view["status"], "approved"); self.assertEqual(view["occupancy"]["status"], "active")
        # 再重启一次不重复补位/通知
        before = len(svc2.notifications("op-user", "operator", "OP1")["notifications"])
        DroneAirspaceService(self.db).startup()
        after = len(svc2.notifications("op-user", "operator", "OP1")["notifications"])
        self.assertEqual(before, after)

    def test_partial_write_failure_retries_only_missing_delivery(self):
        # 直接登记两个结算事件，投递第一个时失败一次：已完成的保留，未写入的之后只补一次
        p = self.plan("D900")
        conn = self.svc.repo.conn
        for key, msg in (("evt:A", "通知甲"), ("evt:B", "通知乙")):
            payload = json.dumps({"notify": {"kind": "probe", "message": msg}}, ensure_ascii=False)
            conn.execute("INSERT INTO outbox(event_key,event_type,plan_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                         (key, "t", p["id"], payload, iso()))
        original = DroneAirspaceService._deliver_notification
        calls = {"n": 0}
        def flaky(self_, c, event, note):
            if event["event_key"] == "evt:A" and calls["n"] == 0:
                calls["n"] += 1; raise RuntimeError("disk full")
            return original(self_, c, event, note)
        DroneAirspaceService._deliver_notification = flaky
        try:
            self.svc.drain_outbox()
        finally:
            DroneAirspaceService._deliver_notification = original
        pending = conn.execute("SELECT event_key FROM outbox WHERE notified_at IS NULL").fetchall()
        self.assertEqual([r["event_key"] for r in pending], ["evt:A"])
        # 甲失败不阻断乙：乙已经写入，审计也照常推进
        notes = conn.execute("SELECT kind FROM notifications WHERE plan_id=? AND kind='probe'", (p["id"],)).fetchall()
        self.assertEqual([r["kind"] for r in notes], ["probe"])
        self.svc.drain_outbox()
        notes = conn.execute("SELECT event_key FROM notifications WHERE kind='probe' ORDER BY event_key").fetchall()
        self.assertEqual([r["event_key"] for r in notes], ["evt:A", "evt:B"])
        self.svc.drain_outbox()  # 再跑不重复
        self.assertEqual(len(conn.execute("SELECT * FROM notifications WHERE kind='probe'").fetchall()), 2)

    # ------------------------------------------------------------ 其他关闭路径
    def test_cancel_and_expire_release_occupancy_and_backfill(self):
        p = self.plan("D600"); self.submit(p); self.approve_normal(p, "off-n")
        r = self.plan("D601", route=[[116.12, 39.82], [116.2, 39.86]]); self.submit(r)
        grant = self.rescue(r, "off-r")
        # 救援取消自身飞行：占用关闭，待重排计划补位
        out = self.svc.cancel(r["id"], "cmd-1", "commander", "", {"reason": "任务取消"})
        self.assertEqual([b["callsign"] for b in out["backfilled"]], ["D600"])
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer")["status"], "approved")

    def test_queue_visible_with_basis_in_state(self):
        p = self.plan("D700"); self.submit(p); self.approve_normal(p, "off-n")
        r = self.plan("D701", route=[[116.12, 39.82], [116.2, 39.86]]); self.submit(r)
        self.rescue(r, "off-r")
        state = self.svc.state("airspace_reviewer", "")
        queue = state["awaiting_reschedule"]
        self.assertEqual([q["callsign"] for q in queue], ["D700"])
        self.assertEqual(queue[0]["occupancy"]["displaced_by"]["callsign"], "D701")
        op_state = self.svc.state("operator", "OP1")
        mine = [x for x in op_state["plans"] if x["id"] == p["id"]][0]
        self.assertEqual(mine["occupancy"]["displaced_by"]["callsign"], "D701")


if __name__ == "__main__": unittest.main()
