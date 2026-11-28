"""Dot 2 (28/11): nen du lieu hub HUB-01/02/03."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pc" / "center"))
import center_store


class TestDot2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = center_store.CenterStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_events(self, rows):
        self.store.center_dir.mkdir(parents=True, exist_ok=True)
        with open(self.store.events_path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_seq_khong_lui_sau_event(self):
        self._write_events([{"seq": 7, "kind": "message", "at": "2026-11-28T00:00:00+00:00"}])
        self.store._write_seq_locked(3, 0)
        with self.store._locked():
            seq, _f = self.store._read_seq_locked()
        self.assertEqual(seq, 7)

    def test_prune_khong_cat_viec_dang_mo(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        self._write_events([
            {"seq": 1, "kind": "task", "task_id": "t-open", "at": old},
            {"seq": 2, "kind": "message", "task_id": "t-open", "at": old},
            {"seq": 3, "kind": "message", "at": old},
        ])
        self.store._write_tasks_locked(
            {"t-open": {"status": "dang-lam", "updated_at": old}})
        self.store._write_seq_locked(3, 0)
        result = self.store.prune(cursors={"a": 3})
        events = self.store._read_events_locked()
        self.assertEqual([e["seq"] for e in events], [1, 2])
        self.assertEqual(result["cut"], 1)


if __name__ == "__main__":
    unittest.main()
