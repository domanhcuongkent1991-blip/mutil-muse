#!/usr/bin/env python3
"""Test goi gop harness mong + sua tin lap 28/11/2026.

- So ke hoach viec me-con ben (center_plan): ghi viec con, bang
  chung, tong ket; ma ke hoach sai mau bi tu choi.
- Leader CLI ghi so ke hoach sau khi tao viec con thanh cong.
- Claim phong chat co wake_id: ack sai luot bi tu choi.
- CLI heartbeat gia han lease cho worker viec dai.
- Brief worker/chat: lenh tra loi ghi ro --to-account; brief
  worker co lenh heartbeat dinh ky.

unittest, stdlib-only, khong mang, du lieu gia.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TREE_ROOT = Path("/home/hatch/workspace/multibot-code")
RUNTIME_OPS = Path("/home/hatch/workspace/multibot-runtime/ops")
for _p in (str(TREE_ROOT / "muse-side"), str(TREE_ROOT),
           str(RUNTIME_OPS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import center_claims  # noqa: E402
import center_leader  # noqa: E402
import center_plan  # noqa: E402

BRIEF = RUNTIME_OPS / "center_worker_brief.py"
TASK_ID = "t-abcdef000001"
ACCOUNT = "muse-1992-a"


class TestPlanLedger(unittest.TestCase):
    def test_record_child_evidence_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            center_plan.record_child(tmp, "me-001", TASK_ID, ACCOUNT)
            center_plan.record_evidence(
                tmp, "me-001", TASK_ID,
                {"sha256": "ab" * 32, "tests": "4/4"},
                status="confirmed")
            plan = center_plan.load_plan(tmp, "me-001")
            self.assertEqual(plan["children"][0]["task_id"], TASK_ID)
            summary = center_plan.plan_summary(tmp, "me-001")
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["confirmed"], 1)
            self.assertEqual(summary["verified"], 1)

    def test_invalid_parent_code_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                center_plan.record_child(tmp, "../x", TASK_ID, ACCOUNT)

    def test_evidence_requires_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            center_plan.record_child(tmp, "me-001", TASK_ID, ACCOUNT)
            with self.assertRaises(ValueError):
                center_plan.record_evidence(tmp, "me-001", TASK_ID, {})


class TestLeaderRecordsPlan(unittest.TestCase):
    def test_cli_records_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "body.txt"
            body_file.write_text("Noi dung", encoding="utf-8")

            def fake_post(config, payload):
                return 200, {"task_id": "t-child000001"}, None

            def fake_loader(root, log_fn=None):
                return {"muse-chinh": {
                    "account_id": "muse-chinh",
                    "receiver_endpoint": "https://example.invalid",
                    "push_secret": "test-secret"}}

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = center_leader.run_cli(
                    ["--root", tmp, "--account-id", "muse-chinh",
                     "--parent-code", "me-001",
                     "--assignee", ACCOUNT, "--title", "Viec con",
                     "--body-file", str(body_file)],
                    post_fn=fake_post, config_loader=fake_loader)
            self.assertEqual(rc, 0)
            plan = center_plan.load_plan(tmp, "me-001")
            self.assertEqual(plan["children"][0]["task_id"],
                             "t-child000001")
            self.assertIn("plan_recorded", out.getvalue())


class TestChatClaimWake(unittest.TestCase):
    def test_room_claim_wake_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = center_claims.claim_room(
                tmp, ACCOUNT, "chung", "center_dispatcher:100",
                now=1000.0, wake_id="w-chat-1")
            self.assertTrue(ok)
            key = center_claims.chat_room_key("chung")
            self.assertFalse(center_claims.ack_start(
                tmp, ACCOUNT, key, wake_id="w-chat-cu", now=1001.0))
            self.assertTrue(center_claims.ack_start(
                tmp, ACCOUNT, key, wake_id="w-chat-1", now=1002.0))


class TestHeartbeatCli(unittest.TestCase):
    def test_heartbeat_cli_extends(self):
        with tempfile.TemporaryDirectory() as tmp:
            center_claims.claim(tmp, ACCOUNT, TASK_ID,
                                "center_dispatcher:100", now=1000.0,
                                wake_id="w-1")
            center_claims.ack_start(tmp, ACCOUNT, TASK_ID,
                                    wake_id="w-1", now=1001.0)
            rc = center_claims.main(
                ["heartbeat", "--root", tmp, "--account-id", ACCOUNT,
                 "--task-id", TASK_ID, "--wake-id", "w-1"])
            self.assertEqual(rc, 0)
            self.assertTrue(center_claims.heartbeat_path(
                tmp, ACCOUNT, TASK_ID).exists())


class TestBriefSamples(unittest.TestCase):
    def test_worker_brief_has_to_account_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "center_tasks"
            task_dir.mkdir(parents=True)
            (task_dir / ("%s.json" % TASK_ID)).write_text(json.dumps({
                "seq": 10,
                "record": {"task_id": TASK_ID, "title": "Viec thu",
                           "kind": "khac",
                           "assigned_from": "muse-chinh",
                           "assigned_to": [ACCOUNT],
                           "status": "moi", "body": "Noi dung",
                           "history": []},
            }), encoding="utf-8")
            (root / "center_messages.jsonl").write_text(
                "", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(BRIEF), "--root", str(root),
                 "--account-id", ACCOUNT,
                 "--center-task-id", TASK_ID],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--to-account", proc.stdout)
            self.assertIn("heartbeat", proc.stdout)


if __name__ == "__main__":
    unittest.main()
