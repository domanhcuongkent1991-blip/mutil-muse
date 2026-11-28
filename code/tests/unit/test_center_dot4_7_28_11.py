"""Dot 4-7 (28/11): canh bao ket, do brief, ap goi, so danh thuc."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "muse-side"))
import apply_update
import center_brief_measure
import center_stuck_check
import center_wake_log


class TestDot47(unittest.TestCase):
    def test_stuck_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "center_outbox").mkdir()
            (root / "center_outbox" / "a.jsonl").write_text("{}\n" * 5)
            result = center_stuck_check.check(root, "a")
            self.assertEqual(result["alerts"][0]["pending"], 5)

    def test_measure_va_wake_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = center_brief_measure.record(tmp, "chat", "abc")
            self.assertEqual(row["chars"], 3)
            center_wake_log.record(tmp, "a", "chung", "mention")
            line = (Path(tmp) / "center_wake_log.jsonl").read_text()
            self.assertEqual(json.loads(line)["reason"], "mention")

    def test_apply_update_chan_lech_ma_bam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "x.py"
            src.write_text("x = 1\n")
            good = hashlib.sha256(src.read_bytes()).hexdigest()
            dst = root / "sub" / "x.py"
            result = apply_update.apply([(src, dst, good)], root / "bk")
            self.assertTrue(result["ok"])
            bad = apply_update.apply([(src, dst, "0" * 64)], root / "bk")
            self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
