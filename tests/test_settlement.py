import sys, tempfile, threading, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100, start=None):
        return self.svc.create_plan("op-user", "operator", "OP1", {
            "callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.1, 39.8], [116.3, 39.9]],
            "starts_at": iso(start or self.start), "ends_at": iso((start or self.start) + timedelta(hours=1)),
            "max_altitude": altitude, "population_risk": risk,
            "emergency_plan": "返回起降点", "region": "BJ"})

    def approve(self, pid, revision, offline, role="airspace_reviewer", actor="reviewer", override=None):
        body = {"expected_revision": revision, "offline_id": offline, "reason": "ok"}
        if override: body["override_reason"] = override
        return self.svc.approve(pid, actor, role, body)

    def ordinary(self, callsign):
        p = self.plan(callsign); self.svc.submit(p["id"], "op-user", "operator", "OP1", {})
        self.approve(p["id"], 1, f"o-{callsign}")
        return p

    def rescue(self, callsign, offline):
        p = self.plan(callsign); self.svc.submit(p["id"], "op-user", "operator", "OP1", {})
        return self.approve(p["id"], 1, offline, role="commander", actor="commander", override="应急救援")

    def occs(self, plan_id=None, status=None):
        return self.svc.list_occupations("airspace_reviewer", "", plan_id=plan_id, status=status)["occupations"]

    def test_ordinary_approve_creates_active_occupation(self):
        p = self.ordinary("D100")
        occ = self.occs(plan_id=p["id"])
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0]["status"], "active")
        self.assertEqual(occ[0]["basis_kind"], "approved")

    def test_rescue_preempts_ordinary_and_retains_basis(self):
        ordinary = self.ordinary("D200")
        rescue = self.rescue("D201", "o-rescue-1")
        o_occ = self.occs(plan_id=ordinary["id"])
        self.assertEqual(len(o_occ), 1)
        self.assertEqual(o_occ[0]["status"], "displaced")
        self.assertEqual(o_occ[0]["displaced_reason"], "rescue_preempted")
        self.assertIsNotNone(o_occ[0]["displaced_by"])
        self.assertIn("displaced_by_rescue", o_occ[0]["original_basis"])
        self.assertEqual(o_occ[0]["original_basis"]["displaced_by_rescue"]["rescue_plan_id"], rescue["plan"]["id"])
        r_occ = self.occs(plan_id=rescue["plan"]["id"])
        self.assertEqual(r_occ[0]["status"], "active")
        # 被挤掉的计划能看到被谁挤掉
        self.assertEqual(o_occ[0]["displaced_by"], r_occ[0]["id"])

    def test_revoke_rescue_reinstates_displaced(self):
        ordinary = self.ordinary("D300")
        rescue = self.rescue("D301", "o-rescue-2")
        self.assertEqual(self.occs(plan_id=ordinary["id"])[0]["status"], "displaced")
        self.svc.revoke_rescue("commander", "commander", {"offline_id": "o-rescue-2"})
        self.assertEqual(self.occs(plan_id=rescue["plan"]["id"])[0]["status"], "invalid")
        self.assertEqual(self.occs(plan_id=ordinary["id"])[0]["status"], "active")

    def test_rebalance_orders_by_takeoff_then_version(self):
        # 构造同一航路时段的两份待重排占用，验证按起飞时间补位
        ordinary = self.ordinary("D400")
        later = self.plan("D401", start=self.start + timedelta(hours=3))
        rescue = self.rescue("D402", "o-rescue-3")
        # later 与 rescue 时段不重叠，不会被挤掉；手动构造一份同航路同时段的待重排占用
        with self.svc.repo.tx() as conn:
            bbox = (116.1, 39.8, 116.3, 39.9)
            later_start = self.start + timedelta(minutes=30)  # 与 ordinary 航路时段重叠，起飞更晚
            conn.execute("""INSERT INTO occupations(plan_id,plan_revision,slot_min_lon,slot_min_lat,slot_max_lon,slot_max_lat,
                            starts_at,ends_at,basis_kind,status,displaced_reason,original_basis_json,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?, 'approved', 'displaced', 'rescue_preempted', '{}',?,?)""",
                         (later["id"], 1, bbox[0], bbox[1], bbox[2], bbox[3], iso(later_start), iso(later_start + timedelta(hours=1)), iso(), iso()))
        self.svc.revoke_rescue("commander", "commander", {"offline_id": "o-rescue-3"})
        # ordinary 起飞更早，优先补位；later 起飞晚，仍待重排
        self.assertEqual(self.occs(plan_id=ordinary["id"])[0]["status"], "active")
        self.assertEqual(self.occs(plan_id=later["id"])[0]["status"], "displaced")

    def test_restriction_displaces_and_revoke_reinstates(self):
        p = self.ordinary("D500")
        r = self.svc.create_restriction("reviewer", "airspace_reviewer", {
            "name": "临时禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7,
            "max_lon": 116.2, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)),
            "reason": "活动"})
        self.assertEqual(self.occs(plan_id=p["id"])[0]["status"], "displaced")
        self.assertEqual(self.occs(plan_id=p["id"])[0]["displaced_reason"], "restriction")
        self.svc.revoke_restriction(r["id"], "reviewer", "airspace_reviewer")
        self.assertEqual(self.occs(plan_id=p["id"])[0]["status"], "active")

    def test_plan_change_invalidates_future_occupation(self):
        p = self.ordinary("D600")
        self.assertEqual(self.occs(plan_id=p["id"])[0]["status"], "active")
        self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "max_altitude": 110})
        self.assertEqual(self.occs(plan_id=p["id"])[0]["status"], "invalid")

    def test_settlement_idempotent_no_duplicate(self):
        ordinary = self.ordinary("D700")
        rescue = self.rescue("D701", "o-rescue-4")
        rid = rescue["plan"]["id"]
        # 同一 offline_id 重复结算：幂等，不重复占位/通知
        r1 = self.svc.settle_rescue(rid, "commander", "commander", "o-rescue-4", "救援")
        r2 = self.svc.settle_rescue(rid, "commander", "commander", "o-rescue-4", "救援")
        self.assertEqual(r1["settlement_id"], r2["settlement_id"])
        self.assertEqual(len(self.occs(plan_id=rid)), 1)
        self.assertEqual(len(self.occs(plan_id=ordinary["id"])), 1)

    def test_two_commanders_first_wins_second_sees_current(self):
        self.ordinary("D800")
        r1 = self.rescue("D801", "o-rescue-5")   # 先结算：占用生效
        r2 = self.rescue("D802", "o-rescue-6")   # 后结算：看到当前占用，不抢占
        r1_id, r2_id = r1["plan"]["id"], r2["plan"]["id"]
        self.assertEqual(self.occs(plan_id=r1_id)[0]["status"], "active")
        self.assertEqual(self.occs(plan_id=r2_id)[0]["status"], "displaced")
        self.assertEqual(self.occs(plan_id=r2_id)[0]["displaced_by"], self.occs(plan_id=r1_id)[0]["id"])
        # 后到的指挥官明确看到当前占用
        res = self.svc.settle_rescue(r2_id, "commander", "commander", "set-8", "再抢占")
        self.assertFalse(res["won_slot"])
        self.assertEqual(res["detail"]["current_rescue_occupation"], self.occs(plan_id=r1_id)[0]["id"])

    def test_concurrent_commanders_serialized(self):
        self.ordinary("D900")
        r1 = self.rescue("D901", "o-rescue-7")
        r2 = self.rescue("D902", "o-rescue-8")
        r1_id, r2_id = r1["plan"]["id"], r2["plan"]["id"]
        barrier = threading.Barrier(2)
        results = {}

        def settle(name, pid, offline):
            barrier.wait()
            results[name] = self.svc.settle_rescue(pid, "commander", "commander", offline, "并发抢占")

        t1 = threading.Thread(target=settle, args=("r1", r1_id, "c-1"))
        t2 = threading.Thread(target=settle, args=("r2", r2_id, "c-2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        # 先结算的一方生效，后到的看到当前占用；不重复占位
        self.assertEqual(len(self.occs(plan_id=r1_id)), 1)
        self.assertEqual(len(self.occs(plan_id=r2_id)), 1)
        self.assertEqual(self.occs(plan_id=r1_id)[0]["status"], "active")
        self.assertEqual(self.occs(plan_id=r2_id)[0]["status"], "displaced")
        self.assertFalse(results["r2"]["won_slot"])

    def test_resume_keeps_completed_and_retries_missing(self):
        ordinary = self.ordinary("D1000")
        rescue = self.rescue("D1001", "o-rescue-9")
        # 模拟崩溃：结算单已建，救援通知已落盘，挤占通知还没落盘
        with self.svc.repo.tx() as conn:
            cur = conn.execute("INSERT INTO settlements(offline_id,kind,status,trigger_plan_id,detail_json,created_at,updated_at) VALUES(?,?, 'in_progress',?,?,?,?)",
                               ("crash-1", "rescue_preemption", rescue["plan"]["id"], "{}", iso(), iso()))
            sid = cur.lastrowid
            conn.execute("INSERT INTO settlement_steps(settlement_id,seq,kind,ref_key,status,created_at,updated_at) VALUES(?,?,?,?, 'done',?,?)",
                         (sid, 0, "notification", f"notify:{rescue['plan']['id']}:rescue_settled", iso(), iso()))
            conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)",
                         (rescue["plan"]["id"], "rescue_settled", "已落盘", iso()))
            conn.execute("INSERT INTO settlement_steps(settlement_id,seq,kind,ref_key,status,created_at,updated_at) VALUES(?,?,?,?, 'pending',?,?)",
                         (sid, 1, "notification", f"notify:{ordinary['id']}:displaced", iso(), iso()))
        before = len(self.svc.notifications("commander", "commander", "")["notifications"])
        self.svc.resume_settlements()
        after = len(self.svc.notifications("commander", "commander", "")["notifications"])
        self.assertEqual(after - before, 1)  # 只补写未落盘的挤占通知
        self.svc.resume_settlements()  # 再次恢复不重复写
        self.assertEqual(len(self.svc.notifications("commander", "commander", "")["notifications"]), after)
        with self.svc.repo.tx() as conn:
            self.assertEqual(conn.execute("SELECT status FROM settlements WHERE id=?", (sid,)).fetchone()["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
