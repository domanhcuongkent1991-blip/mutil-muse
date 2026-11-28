"""Dot 3 (28/11): sao luu + canh bao kiem tra cu."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pc" / "center"))
import backup_runner


class TestDot3(unittest.TestCase):
    def test_backup_va_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            center = root / "data" / "center"
            center.mkdir(parents=True)
            (center / "events.jsonl").write_text("{}\n")
            result = backup_runner.backup(root, backup_dir=root / "bk")
            self.assertTrue(result["ok"])
            check = backup_runner.verify(result["dest"])
            self.assertTrue(check["ok"])


if __name__ == "__main__":
    unittest.main()
