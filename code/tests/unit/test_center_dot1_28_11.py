"""Dot 1 (28/11): 3 loi CHAN tu gia lap kiem thu."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "muse-side"))
import center_claims
import center_outbox


class TestDot1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedupe_key_khong_append_trung(self):
        r1 = center_outbox.enqueue_message(
            self.root, "muse-chinh", "hoi", "tra loi", room="chung",
            dedupe_key="chat:muse-chinh:chung:10")
        r2 = center_outbox.enqueue_message(
            self.root, "muse-chinh", "hoi", "tra loi", room="chung",
            dedupe_key="chat:muse-chinh:chung:10")
        self.assertEqual(r1["msg_id"], r2["msg_id"])
        rows, _t, _s = center_outbox.read_outbox(self.root)
        self.assertEqual(len(rows), 1)

    def test_heartbeat_bo_wake_khong_gia_han(self):
        center_claims.claim(self.root, "muse-chinh", "t-1", "h",
                            wake_id="w-1")
        self.assertFalse(center_claims.heartbeat(
            self.root, "muse-chinh", "t-1"))
        self.assertTrue(center_claims.heartbeat(
            self.root, "muse-chinh", "t-1", wake_id="w-1"))

    def test_release_wake_cu_khong_xoa_claim_moi(self):
        center_claims.claim(self.root, "muse-chinh", "t-1", "h",
                            wake_id="w-2")
        self.assertFalse(center_claims.release(
            self.root, "muse-chinh", "t-1", wake_id="w-1"))
        self.assertIsNotNone(center_claims.holder_of(
            self.root, "muse-chinh", "t-1"))
        self.assertTrue(center_claims.release(
            self.root, "muse-chinh", "t-1", wake_id="w-2"))

    def test_flush_dong_sai_schema_vao_dead_letter(self):
        path = center_outbox.outbox_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "hoi", "body": "x"}) + "\n")
            fh.write(json.dumps({"kind": "hoi", "body": "y",
                                 "msg_id": "m-1"}) + "\n")
        cfg = {"account_id": "muse-chinh",
               "receiver_endpoint": "http://x", "push_secret": "s"}
        stats = center_outbox.flush(
            self.root, config=cfg,
            http_post_fn=lambda *a: (200, "{}", None),
            log_fn=lambda m: None)
        self.assertEqual(stats["error"], "invalid_schema")
        self.assertEqual(stats["cursor_after"], 1)
        dead = (self.root / "center_outbox_dead.jsonl").read_text()
        self.assertIn("invalid_schema", dead)


if __name__ == "__main__":
    unittest.main()
