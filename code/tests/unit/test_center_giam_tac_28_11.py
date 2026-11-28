#!/usr/bin/env python3
"""Test goi giam tac 28/11/2026 (chu nhan duyet 12:30).

Bon diem:
1. Brief worker gon: khong in toan van body dai + moi tin gan viec;
   giu thong tin chinh (file_id, sha256, duong dan dich) + lenh mau.
2. Dispatcher uu tien danh thuc viec truong nhom giao truoc viec
   nhieu, trong cung so lan danh thuc that bai.
3. Outbox gap 429/5xx thi cho lui (backoff) thay vi thu lai moi
   chu ky ~10 giay.
4. center_leader co duong chay that qua CLI de truong nhom tao
   viec con co gian nhip + chong trung client_key.

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
for _p in (str(TREE_ROOT / "muse-side"), str(TREE_ROOT), str(RUNTIME_OPS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import center_dispatcher  # noqa: E402
import center_leader  # noqa: E402
import center_outbox  # noqa: E402

BRIEF = RUNTIME_OPS / "center_worker_brief.py"
TASK_ID = "t-abcdef000001"
ACCOUNT = "muse-1992-a"
SHA = "e5d239a7b74a37e933ad67ee3d521a934489e84b7b7bda3a61f20e508d210260"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestBriefSlim(unittest.TestCase):
    def test_brief_caps_body_and_messages_keeps_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = ("Ap file f-1425540c0129 sha256 %s vao "
                    "muse-side/center_chat_brief.py. " % SHA
                    + "x" * 3000 + "TAILMARKER")
            _write_json(root / "center_tasks" / ("%s.json" % TASK_ID), {
                "seq": 10,
                "record": {
                    "task_id": TASK_ID, "title": "Viec ap file",
                    "kind": "khac", "assigned_from": "muse-chinh",
                    "assigned_to": [ACCOUNT], "status": "moi",
                    "created_at": "2026-11-28T11:40:00+07:00",
                    "updated_at": "2026-11-28T11:40:00+07:00",
                    "body": body, "history": [],
                },
            })
            lines = []
            for i in range(8):
                lines.append(json.dumps({
                    "seq": 11 + i, "at": "2026-11-28T11:41:00+07:00",
                    "record": {"from_account": "muse-chinh",
                               "kind": "hoi", "task_id": TASK_ID,
                               "body": "y" * 800 + "MSG%d" % i},
                }, ensure_ascii=False))
            (root / "center_messages.jsonl").write_text(
                "\n".join(lines), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(BRIEF), "--root", str(root),
                 "--account-id", ACCOUNT,
                 "--center-task-id", TASK_ID],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout
            self.assertIn(SHA, out)
            self.assertIn("f-1425540c0129", out)
            self.assertIn("ack-start", out)
            self.assertNotIn("TAILMARKER", out)
            self.assertNotIn("MSG0", out)
            # Phan co dinh (lenh mau + gui file) ~2.500 ky tu; tong
            # phai nho hon ro ret ban cu (~11.000 ky tu cung du lieu).
            self.assertLess(len(out), 8000)


class TestWakePriority(unittest.TestCase):
    def test_leader_task_first_within_same_fail_count(self):
        candidates = [
            {"task_id": "t-noise", "record": {
                "assigned_from": "muse-1996",
                "created_at": "2026-11-28T11:00:00+07:00"}},
            {"task_id": "t-leader", "record": {
                "assigned_from": "muse-chinh",
                "created_at": "2026-11-28T11:40:00+07:00"}},
            {"task_id": "t-failed", "record": {
                "assigned_from": "muse-chinh",
                "created_at": "2026-11-28T10:00:00+07:00"}},
        ]
        ordered = center_dispatcher.sort_wake_candidates(
            candidates, {"t-failed": 2}, ACCOUNT)
        self.assertEqual([c["task_id"] for c in ordered],
                         ["t-leader", "t-noise", "t-failed"])


class TestOutboxBackoff(unittest.TestCase):
    def test_429_waits_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            config = {"account_id": "muse-chinh",
                      "receiver_endpoint": "https://example.invalid",
                      "push_secret": "test-secret"}
            center_outbox.enqueue_message(
                root, "muse-chinh", "hoi", "tin thu",
                to_account=["muse-1992-a"], room="chung")
            calls = []
            state = {"status": 429}

            def fake_post(url, secret, payload, timeout, account_id):
                calls.append(url)
                if state["status"] == 200:
                    return 200, {"ok": True}, None
                return state["status"], {}, None

            clock = {"now": 1000.0}
            orig_now = center_outbox.now_epoch
            center_outbox.now_epoch = lambda: clock["now"]
            try:
                first = center_outbox.flush(
                    root, config=config, http_post_fn=fake_post,
                    log_fn=lambda _m: None)
                self.assertEqual(first["error"], "http_429")
                self.assertEqual(len(calls), 1)
                second = center_outbox.flush(
                    root, config=config, http_post_fn=fake_post,
                    log_fn=lambda _m: None)
                self.assertEqual(second["error"], "backoff_wait")
                self.assertEqual(len(calls), 1)
                clock["now"] += 11
                state["status"] = 200
                third = center_outbox.flush(
                    root, config=config, http_post_fn=fake_post,
                    log_fn=lambda _m: None)
                self.assertEqual(third["sent"], 1)
                self.assertEqual(len(calls), 2)
            finally:
                center_outbox.now_epoch = orig_now


class TestLeaderCli(unittest.TestCase):
    def test_cli_creates_child_with_stable_client_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "body.txt"
            body_file.write_text("Noi dung viec con", encoding="utf-8")
            seen = []

            def fake_post(config, payload):
                seen.append(payload)
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
                     "--assignee", "muse-1992-a",
                     "--title", "Viec con",
                     "--body-file", str(body_file)],
                    post_fn=fake_post, config_loader=fake_loader)
            self.assertEqual(rc, 0)
            self.assertIn("t-child000001", out.getvalue())
            self.assertEqual(
                seen[0]["client_key"],
                center_leader.child_client_key("me-001", "muse-1992-a"))
            self.assertEqual(seen[0]["assigned_to"], ["muse-1992-a"])


if __name__ == "__main__":
    unittest.main()
