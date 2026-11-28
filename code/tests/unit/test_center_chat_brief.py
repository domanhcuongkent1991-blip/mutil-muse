#!/usr/bin/env python3
"""Test brief chat trung tam — muc GUI FILE (B1, ngay 21/11).

Khoa lai muc "GUI FILE" trong center_chat_brief.build_brief: co mat
trong brief; chua dung 2 lenh mau (upload lay file_id + gui tin kem
file_id); gioi han 50 MB/file; fallback text-only khi tai len that
bai; upload va gui tin di lien trong cung luot worker; rang buoc
kind hoi/bao-cao + phong rieng phai la tin hoi; toi da 1 file/luot.
Cung khoa hanh vi _format_message hien thi file dinh kem cua tin
(da co san truoc B1 — khong sua them).

unittest, stdlib-only, du lieu gia, khong secret that, khong mang.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "muse-side"))
sys.path.insert(0, str(REPO_ROOT))

import center_chat_brief  # noqa: E402

ME = "muse-chinh"
NOW = time.time()


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _msg_record(kind="hoi", sender="owner", room="chung",
                msg_id="m-000000000001", created_at=None, file_meta=None):
    record = {
        "schema_version": 1,
        "msg_id": msg_id,
        "from_account": sender,
        "to_account": None,
        "kind": kind,
        "task_id": None,
        "body": "Tin thu",
        "created_at": created_at or _iso(NOW),
        "report_role": None,
        "room": room,
        "mentions": None,
    }
    if file_meta is not None:
        record["file"] = file_meta
    return record


def _muse_root(tmp, messages=()):
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    for seq, record in messages:
        _append_jsonl(root / "center_messages.jsonl", {
            "seq": seq, "at": record.get("created_at"),
            "msg_id": record.get("msg_id"), "record": record})
    return root


class GuiFileBriefTest(unittest.TestCase):
    """Muc GUI FILE trong brief chat trung tam (B1)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-brief-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "muse"

    def _brief(self, messages=((1, _msg_record()),)):
        root = _muse_root(self.root, messages=messages)
        return center_chat_brief.build_brief(
            root, ME, "chung", [seq for seq, _rec in messages],
            now=NOW)

    def test_brief_contains_gui_file_section(self):
        brief = self._brief()
        self.assertIn("GỬI FILE", brief)

    def test_gui_file_has_exactly_two_sample_commands(self):
        brief = self._brief()
        upload = ("center_files.py --root <root> --account-id <id> "
                  "upload <đường dẫn file>")
        send = ("center_outbox.py --root <root> --account-id <id> "
                "--message-kind hoi --body-file "
                "<file nội dung> --file-id <f-...> "
                "--to-account <id-nguoi-nhan> --room <phòng>")
        self.assertIn(upload, brief)
        self.assertIn(send, brief)
        # Moi lenh mau chi xuat hien dung 1 lan trong toan brief.
        self.assertEqual(brief.count(upload), 1)
        self.assertEqual(brief.count(send), 1)
        # Lenh upload noi ro lay file_id tu meta JSON.
        self.assertIn("file_id/file_name/file_size/sha256", brief)
        # Tin chi co file thi bo --body-file.
        self.assertIn("tin chỉ có file thì bỏ --body-file", brief)

    def test_gui_file_mentions_50mb_limit(self):
        brief = self._brief()
        self.assertIn("50 MB", brief)

    def test_gui_file_mentions_text_only_fallback(self):
        brief = self._brief()
        self.assertIn("text-only", brief)
        self.assertIn("không gửi được file", brief)

    def test_gui_file_upload_and_send_go_together(self):
        brief = self._brief()
        self.assertIn("đi liền", brief)
        self.assertIn("7 ngày", brief)

    def test_gui_file_kind_and_room_constraints(self):
        brief = self._brief()
        self.assertIn("hoi hoặc bao-cao", brief)
        self.assertIn("phòng riêng phải là tin hoi", brief)
        self.assertIn("bao-cao bắt buộc ở khung chung", brief)

    def test_gui_file_max_one_file_per_reply(self):
        brief = self._brief()
        self.assertIn("tối đa 1 file/lượt trả lời", brief)

    def test_format_message_shows_attached_file(self):
        file_meta = {
            "file_id": "f-0123456789ab",
            "file_name": "bao-cao-thu.pdf",
            "file_size": 12345,
            "sha256": "aabbccddeeff00112233445566778899",
        }
        record = _msg_record(file_meta=file_meta)
        line = center_chat_brief._format_message(
            {"seq": 1, "at": record["created_at"], "record": record},
            {}, full_body=True)
        self.assertIn("[file:", line)
        self.assertIn("bao-cao-thu.pdf", line)
        self.assertIn("12345 bytes", line)
        self.assertIn("aabbccddeeff", line)  # 12 ky tu dau sha256


if __name__ == "__main__":
    unittest.main()
