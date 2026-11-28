#!/usr/bin/env python3
"""Test cai tien 28/11/2026: claim hai pha, bo kiem tra suc khoe
PC, gian nhip tao viec con truong nhom.

unittest, stdlib-only, thoi gian gia lap. Du lieu gia.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "muse-side"))
sys.path.insert(0, str(REPO_ROOT))

import center_claims  # noqa: E402
import center_dispatcher  # noqa: E402
import center_leader  # noqa: E402
from pc.center import center_health_check  # noqa: E402

ACC = "muse-phu"
T0 = 1_800_000_000  # moc gia lap


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class TestClaimTwoPhase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reserved_expires_after_ack_wait_not_lease(self):
        self.assertTrue(center_claims.claim(
            self.root, ACC, "t-000000000001", "center_dispatcher:1",
            now=T0, wake_id="w-1"))
        self.assertEqual(
            center_claims.claim_phase(self.root, ACC, "t-000000000001"),
            "reserved")
        # Trong han cho ack: chua het han, van dem cap.
        self.assertFalse(center_claims.is_expired(
            self.root, ACC, "t-000000000001", now=T0 + 100))
        self.assertEqual(center_claims.count_running(
            self.root, ACC, now=T0 + 100), 1)
        # Qua han cho ack (300s) nhung chua du lease 900s: het han.
        later = T0 + center_claims.ACK_WAIT_SECONDS + 1
        self.assertTrue(center_claims.is_reserved_expired(
            self.root, ACC, "t-000000000001", now=later))
        self.assertTrue(center_claims.is_expired(
            self.root, ACC, "t-000000000001", now=later))
        self.assertEqual(center_claims.count_running(
            self.root, ACC, now=later), 0)

    def test_ack_start_moves_to_running_and_keeps_lease(self):
        center_claims.claim(self.root, ACC, "t-000000000002",
                            "center_dispatcher:1", now=T0,
                            wake_id="w-2")
        # Wake_id sai -> khong ack duoc.
        self.assertFalse(center_claims.ack_start(
            self.root, ACC, "t-000000000002", wake_id="w-cu",
            now=T0 + 10))
        self.assertTrue(center_claims.ack_start(
            self.root, ACC, "t-000000000002", wake_id="w-2",
            now=T0 + 10))
        self.assertEqual(
            center_claims.claim_phase(self.root, ACC, "t-000000000002"),
            "running")
        # Qua han cho ack van con han nho heartbeat + lease.
        self.assertFalse(center_claims.is_expired(
            self.root, ACC, "t-000000000002",
            now=T0 + center_claims.ACK_WAIT_SECONDS + 60))
        # Heartbeat dung wake_id gia han; sai wake_id bi tu choi.
        self.assertTrue(center_claims.heartbeat(
            self.root, ACC, "t-000000000002", now=T0 + 400,
            wake_id="w-2"))
        self.assertFalse(center_claims.heartbeat(
            self.root, ACC, "t-000000000002", now=T0 + 400,
            wake_id="w-cu"))
        # Mat heartbeat qua lease -> het han nhu cu.
        self.assertTrue(center_claims.is_expired(
            self.root, ACC, "t-000000000002",
            now=T0 + 400 + center_claims.LEASE_SECONDS + 1))

    def test_legacy_claim_without_phase(self):
        # Claim cu (khong phase/wake_id): chua heartbeat -> reserved.
        path = center_claims.claim_path(self.root, ACC, "t-000000000003")
        path.write_text(json.dumps({
            "task_id": "t-000000000003", "account_id": ACC,
            "holder": "center_dispatcher:1", "claimed_at": T0,
        }), encoding="utf-8")
        import os
        os.utime(path, (T0, T0))
        self.assertEqual(
            center_claims.claim_phase(self.root, ACC, "t-000000000003"),
            "reserved")
        self.assertTrue(center_claims.is_expired(
            self.root, ACC, "t-000000000003",
            now=T0 + center_claims.ACK_WAIT_SECONDS + 1))


class TestDispatcherSweep(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def _write_task(self, task_id, created_epoch):
        record = {
            "schema_version": 1, "task_id": task_id,
            "title": "Viec thu", "body": "Noi dung", "kind": "khac",
            "created_by": "owner", "assigned_from": "owner",
            "assigned_to": [ACC], "status": "moi",
            "due_at": None, "created_at": _iso(created_epoch),
            "updated_at": _iso(created_epoch), "seen_at": None,
            "history": [],
        }
        path = self.root / "center_tasks" / ("%s.json" % task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"seq": 1, "task_id": task_id, "record": record}),
            encoding="utf-8")

    def _cycle(self, now, woken):
        return center_dispatcher.run_cycle_for_account(
            self.root, ACC,
            deps={"worker_fn": lambda a, t, c: woken.append(
                t["task_id"]),
                  "log_fn": self.logs.append,
                  "alert_fn": lambda *a: None},
            now=now)

    def test_reserved_no_ack_frees_cap_for_third_task(self):
        # Tai lap su co 28/11: 2 viec duoc giu cho nhung worker
        # khong ack; sau han cho ack, viec thu 3 phai duoc danh
        # thuc thay vi ket cap 15 phut.
        for index, task_id in enumerate(
                ("t-000000000001", "t-000000000002", "t-000000000003")):
            self._write_task(task_id, T0 + index)
        woken = []
        self._cycle(T0 + 10, woken)   # giu cho viec 1
        self._cycle(T0 + 20, woken)   # giu cho viec 2 -> cap day
        summary = self._cycle(T0 + 30, woken)
        self.assertTrue(summary["cap_blocked"])
        self.assertEqual(woken, ["t-000000000001", "t-000000000002"])
        # Qua han cho ack: sweep giai phong ca 2; cac chu ky sau
        # danh thuc lai theo thu tu cu nhat va viec 3 duoc wake
        # trong cung cua so han cho ack, khong ket 15 phut.
        later = T0 + center_claims.ACK_WAIT_SECONDS + 30
        summary = self._cycle(later, woken)
        self.assertEqual(summary["reserved_swept"], 2)
        self.assertFalse(summary["cap_blocked"])
        self._cycle(later + 10, woken)
        self._cycle(later + 20, woken)
        self.assertIn("t-000000000003", woken)

    def test_three_no_ack_defers_task(self):
        self._write_task("t-000000000001", T0)
        self._write_task("t-000000000002", T0 + 1)
        woken = []
        now = T0 + 10
        # Lap cac luot giu cho khong ack toi khi viec 1 du 3 lan
        # that bai -> tam lui, viec 2 di tiep.
        summary = {}
        for _round in range(8):
            summary = self._cycle(now, woken)
            if "t-000000000001" in summary.get("deferred_tasks", []):
                break
            now += center_claims.ACK_WAIT_SECONDS + 10
        self.assertIn("t-000000000001", summary["deferred_tasks"])
        self.assertIn("t-000000000002", woken)


class TestHealthCheck(unittest.TestCase):
    def _fetch_state(self, last_fetch_epoch):
        return {"center": {ACC: {"last_fetch_at": _iso(last_fetch_epoch),
                                 "fetch_count": 5}}}

    def test_no_fetch_levels_and_boot_grace(self):
        now = T0 + 10000
        # Keo tin 1 gio truoc -> vang; 3 gio -> do.
        alerts = center_health_check.evaluate(
            self._fetch_state(now - 3600), [], [ACC], now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "yellow")
        alerts = center_health_check.evaluate(
            self._fetch_state(now - 3 * 3600), [], [ACC], now)
        self.assertEqual(alerts[0]["level"], "red")
        # Trong an han 30 phut sau khi PC bat: khong canh bao.
        alerts = center_health_check.evaluate(
            self._fetch_state(now - 3 * 3600), [], [ACC], now,
            boot_at=now - 600)
        self.assertEqual(alerts, [])

    def test_unclaimed_task(self):
        now = T0 + 10000
        task = {
            "task_id": "t-000000000001", "status": "moi",
            "assigned_to": [ACC], "reported_by": [], "history": [],
            "created_at": _iso(now - 3 * 3600),
        }
        alerts = center_health_check.evaluate(
            {}, [task], [ACC], now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "task_unclaimed")
        self.assertEqual(alerts[0]["level"], "red")
        # Viec chung chua ai bao xong: khong bao S4 (S1 da phu).
        shared = dict(task, assigned_to=[ACC, "muse-khac"])
        alerts = center_health_check.evaluate(
            {}, [shared], [ACC], now)
        self.assertEqual(alerts, [])
        # Nguoi khac da bao xong, ACC chua: bao dung ACC.
        shared = dict(task, assigned_to=[ACC, "muse-khac"],
                      reported_by=["muse-khac"])
        alerts = center_health_check.evaluate(
            {}, [shared], [ACC], now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["account_id"], ACC)


class TestLeaderPacing(unittest.TestCase):
    def test_retry_429_reuses_client_key(self):
        calls = []
        sleeps = []

        def post_fn(config, payload):
            calls.append(dict(payload))
            if len(calls) < 3:
                return 429, {"error": "rate_limited"}, None
            return 200, {"task_id": "t-000000000001",
                         "duplicate": False}, None

        result = center_leader.create_child_task(
            {"account_id": "muse-chinh"}, "EXP-1", "Tieu de",
            "Noi dung", ACC, post_fn=post_fn,
            sleep_fn=sleeps.append)
        self.assertEqual(result["task_id"], "t-000000000001")
        self.assertEqual(result["attempts"], 3)
        keys = {call["client_key"] for call in calls}
        self.assertEqual(len(keys), 1)  # thu lai cung khoa chong trung
        self.assertEqual(sleeps, [5, 10])  # backoff tang dan

    def test_pacing_between_children_and_error_isolated(self):
        sleeps = []

        def post_fn(config, payload):
            if payload["assigned_to"] == ["muse-hong"]:
                return 400, {"error": "bad_assigned_to"}, None
            return 200, {"task_id": "t-000000000002",
                         "duplicate": True}, None

        children = [
            {"title": "A", "body": "x", "assignee": ACC},
            {"title": "B", "body": "x", "assignee": "muse-hong"},
            {"title": "C", "body": "x", "assignee": "muse-khac"},
        ]
        results = center_leader.create_child_tasks(
            {"account_id": "muse-chinh"}, "EXP-1", children,
            post_fn=post_fn, sleep_fn=sleeps.append, pace_seconds=3)
        self.assertEqual(sleeps, [3, 3])  # gian nhip giua cac viec
        self.assertEqual(results[0]["task_id"], "t-000000000002")
        self.assertIn("error", results[1])
        self.assertEqual(results[2]["task_id"], "t-000000000002")


if __name__ == "__main__":
    unittest.main()
