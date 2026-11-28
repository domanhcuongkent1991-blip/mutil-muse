"""Test chặng T3l (v0.6.0): thảo luận tài khoản.

Gộp từ 3 file thành phần (schema v4 + Luật 2; logic thảo luận phía Muse;
giao diện + gateway) để tránh đụng nhau khi code song song; nội dung test
giữ nguyên, chỉ gộp lại.

Bao phủ (theo PLAN T3l, 18 tiêu chí):
- schema tin nhắn v4 (``_as_to_account_list``, ``message_schema_version``
  trả 4, ``SCHEMA_VERSION`` giữ = 1, fingerprint ổn định qua nâng cấp,
  trần 5 ở cả 3 tầng, flush GIỮ dòng v4 khi receiver cũ trả 400)
  + Luật 2 (3 trường hợp frag-05 §3.2);
- đếm lượt 5, khóa vòng (phòng, người mở, tập người tham gia), W7,
  reset khi chủ nhân xen tin, chỉ chủ nhân mở vòng mới, tin nhiều người
  nhận song song + đếm chung, brief mang thông tin vòng;
- màu tài khoản, công tắc thảo luận, ô lọc theo người gửi, ô "Gửi cho:"
  nhiều người (v4), cờ ``discussion_enabled`` trong /health, vòng đã
  dừng, file khung chung.

unittest, stdlib-only. Mọi giá trị secret trong test đều là mẫu giả
dạng rõ ràng ("TEST-FAKE-..."), không secret thật, không nội dung
tin thật.
"""

import ipaddress
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import io
import time
from contextlib import redirect_stdout
import re
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "muse-side"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT.parent / "multibot-runtime" / "ops"))

import center_fetcher
import center_outbox
from pc.center import center_store
from pc.center.center_store import (  # noqa: E402
    MAX_MENTIONS,
    SCHEMA_VERSION,
    CenterStore,
    _as_to_account_list,
    _fingerprint_message,
    message_schema_version,
)
from pc.gateway import gateway_main as gateway
from pc.sync_receiver import receiver_main as receiver
import center_chat_brief
import center_discussion
import center_dispatcher
import center_plan_cycle
from pc.gateway import gateway_main
from pc.ui import ui_app, ui_auth
from pc.ui import ui_pages, ui_store
from pc.ui.ui_static import APP_CSS, APP_JS

SECRETS = {
    "muse-chinh": ("TEST-FAKE-CENTER-FETCH-CHINH",
                   "TEST-FAKE-CENTER-PUSH-CHINH"),
    "muse-phu": ("TEST-FAKE-CENTER-FETCH-PHU",
                 "TEST-FAKE-CENTER-PUSH-PHU"),
    "muse-ba": ("TEST-FAKE-CENTER-FETCH-BA",
                "TEST-FAKE-CENTER-PUSH-BA"),
    "muse-bon": ("TEST-FAKE-CENTER-FETCH-BON",
                 "TEST-FAKE-CENTER-PUSH-BON"),
    "muse-nam": ("TEST-FAKE-CENTER-FETCH-NAM",
                 "TEST-FAKE-CENTER-PUSH-NAM"),
    "muse-sau": ("TEST-FAKE-CENTER-FETCH-SAU",
                 "TEST-FAKE-CENTER-PUSH-SAU"),
    "muse-tat": ("TEST-FAKE-CENTER-FETCH-TAT",
                 "TEST-FAKE-CENTER-PUSH-TAT"),
    "muse-ngung": ("TEST-FAKE-CENTER-FETCH-NGUNG",
                   "TEST-FAKE-CENTER-PUSH-NGUNG"),
}

ACCOUNT_FLAGS = {
    "muse-chinh": (True, None),
    "muse-phu": (True, None),
    "muse-ba": (True, None),
    "muse-bon": (True, None),
    "muse-nam": (True, None),
    "muse-sau": (True, None),
    "muse-tat": (True, False),
    "muse-ngung": (False, None),
}

ACCOUNT_NAMES = {
    "muse-chinh": "Muse chinh",
    "muse-phu": "Muse phu",
    "muse-ba": "Muse ba",
    "muse-bon": "Muse bon",
    "muse-nam": "Muse nam",
    "muse-sau": "Muse sau",
    "muse-tat": "Muse tat thao luan",
    "muse-ngung": "Muse ngung",
}

ME = "muse-chinh"

OTHER = "muse-phu"

def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

def _write_accounts(root):
    accounts = {}
    for account_id, (participating, discussion) in ACCOUNT_FLAGS.items():
        record = {"display_name": ACCOUNT_NAMES[account_id],
                  "center_participating": participating}
        if discussion is not None:
            record["center_discussion_enabled"] = discussion
        accounts[account_id] = record
    _write_json(root / "ui-config" / "accounts.json",
                {"version": 1, "accounts": accounts})

def _serve(make_server_fn):
    server = make_server_fn()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return server, port, thread

def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

def _http(port, method, path, payload=None, secret=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    body = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["Authorization"] = "Bearer %s" % secret
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        data = exc.read().decode("utf-8")
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = {"raw": data}
        return exc.code, parsed

class StoreFixture(unittest.TestCase):
    """Fixture chỉ-store (không server): test trực tiếp CenterStore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _write_accounts(self.root)
        self.store = CenterStore(self.root)
        self._msg_n = 0

    def _msg_id(self):
        self._msg_n += 1
        return "m-%012x" % self._msg_n

    def _append(self, from_account, body, to_account=None,
                room="chung", kind="hoi", client_key=None):
        return self.store.append_message(
            from_account, kind, to_account, None, body, self._msg_id(),
            room=room, client_key=client_key)

class TestAsToAccountList(StoreFixture):
    """_as_to_account_list: None/chuỗi/danh sách/kiểu lạ."""

    def test_none_to_none(self):
        self.assertIsNone(_as_to_account_list(None))

    def test_string_to_single_list(self):
        self.assertEqual(_as_to_account_list("muse-phu"), ["muse-phu"])

    def test_list_kept(self):
        self.assertEqual(_as_to_account_list(["muse-phu", "muse-ba"]),
                         ["muse-phu", "muse-ba"])
        self.assertEqual(_as_to_account_list([]), [])

    def test_other_types_raise_value_error(self):
        for bad in (123, 1.5, True, {"a": 1}, ("muse-phu",), b"muse-phu"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _as_to_account_list(bad)

class TestMessageSchemaVersion(StoreFixture):
    """message_schema_version: 4 cho danh sách, giữ 1/2/3 cũ."""

    def test_old_rules_kept_without_to_account(self):
        self.assertEqual(message_schema_version("chung", None, False), 1)
        self.assertEqual(message_schema_version("chung", None, True), 2)
        self.assertEqual(
            message_schema_version("dm:muse-phu", None, False), 3)
        self.assertEqual(
            message_schema_version("chung", ["muse-phu"], False), 3)
        # Quy tắc 3 thắng quy tắc 2.
        self.assertEqual(
            message_schema_version("dm:muse-phu", None, True), 3)

    def test_string_to_account_keeps_old_rules(self):
        # Chuỗi cũ (v1-v3) không làm tăng version.
        self.assertEqual(
            message_schema_version("chung", None, False, "muse-phu"), 1)
        self.assertEqual(
            message_schema_version("chung", None, True, "muse-phu"), 2)

    def test_list_to_account_returns_4(self):
        # Kể cả danh sách 1 người.
        self.assertEqual(
            message_schema_version("chung", None, False, ["muse-phu"]),
            4)
        self.assertEqual(
            message_schema_version(
                "chung", None, False, ["muse-phu", "muse-ba"]), 4)

    def test_rule_4_beats_rule_3(self):
        self.assertEqual(
            message_schema_version(
                "dm:muse-phu", ["muse-phu"], True, ["muse-phu"]), 4)

    def test_schema_version_constant_stays_1(self):
        # D1(h): tin hub tự sinh không bao giờ có to_account là
        # danh sách nên hằng giữ nguyên = 1.
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(center_store.SCHEMA_VERSION, 1)

class TestStoreAppendV4(StoreFixture):
    """append_message: chấp nhận danh sách / từ chối >5 / id sai."""

    def test_list_stored_as_list_with_version_4(self):
        message, duplicate = self._append(
            ME, "Hoi ca hai", to_account=["muse-phu", "muse-ba"])
        self.assertFalse(duplicate)
        self.assertEqual(message["to_account"], ["muse-phu", "muse-ba"])
        self.assertEqual(message["schema_version"], 4)

    def test_single_person_list_is_still_version_4(self):
        message, _dup = self._append(
            ME, "Hoi mot nguoi", to_account=["muse-phu"])
        self.assertEqual(message["to_account"], ["muse-phu"])
        self.assertEqual(message["schema_version"], 4)

    def test_string_kept_as_string_with_old_version(self):
        # Tương thích ngược (test T3i ghim: chuỗi vẫn là chuỗi).
        message, _dup = self._append(
            "owner", "Hoi rieng phu", to_account="muse-phu")
        self.assertEqual(message["to_account"], "muse-phu")
        self.assertEqual(message["schema_version"], 1)

    def test_more_than_5_rejected(self):
        with self.assertRaises(center_store.CenterError) as ctx:
            self._append(ME, "Hoi sau nguoi", to_account=[
                "muse-phu", "muse-ba", "muse-bon",
                "muse-nam", "muse-sau", "muse-chinh"])
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertEqual(ctx.exception.error, "bad_to_account")
        self.assertEqual(self.store.read_events(), [])

    def test_exactly_5_accepted(self):
        message, _dup = self._append(ME, "Hoi nam nguoi", to_account=[
            "muse-phu", "muse-ba", "muse-bon", "muse-nam", "muse-sau"])
        self.assertEqual(message["schema_version"], 4)
        self.assertEqual(len(message["to_account"]), 5)

    def test_bad_id_in_list_rejected(self):
        for bad in (["Muse-Phu"], ["muse phu"], ["muse-phu", ""],
                    ["muse-phu", 123]):
            # "owner" khớp mẫu account_id nên store chấp nhận ở
            # tầng này (kiểm tra tồn tại là việc của receiver/UI).
            with self.assertRaises(center_store.CenterError) as ctx:
                self._append(ME, "Hoi", to_account=bad)
            self.assertEqual(ctx.exception.http_status, 400)
            self.assertEqual(ctx.exception.error, "bad_to_account")

    def test_wrong_type_rejected(self):
        for bad in (123, {"a": 1}, ("muse-phu",)):
            with self.assertRaises(center_store.CenterError) as ctx:
                self._append(ME, "Hoi", to_account=bad)
            self.assertEqual(ctx.exception.http_status, 400)
            self.assertEqual(ctx.exception.error, "bad_to_account")

class TestFingerprintStableAcrossUpgrade(StoreFixture):
    """Fingerprint ổn định qua nâng cấp: chuỗi cũ vs danh sách
    1 người có cùng fingerprint, không 409 oan."""

    def test_fingerprint_equal_string_vs_single_list(self):
        base = {"kind": "hoi", "body": "Cung noi dung",
                "room": "chung", "task_id": None, "file_id": None}
        old = dict(base, to_account="muse-phu")
        new = dict(base, to_account=["muse-phu"])
        self.assertEqual(_fingerprint_message(old),
                         _fingerprint_message(new))
        # Danh sách khác người -> fingerprint khác.
        other = dict(base, to_account=["muse-ba"])
        self.assertNotEqual(_fingerprint_message(old),
                            _fingerprint_message(other))
        # Danh sách rỗng giữ nguyên là [] (không ép về None) —
        # khác None nên fingerprint khác (trường hợp biên: gửi
        # chung viết dạng [] hay null là 2 cách viết khác nhau).
        self.assertEqual(_as_to_account_list([]), [])

    def test_resend_same_client_key_string_then_list_no_409(self):
        message, duplicate = self._append(
            ME, "Cung noi dung", to_account="muse-phu",
            client_key="ck-upgrade-1")
        self.assertFalse(duplicate)
        message2, duplicate2 = self.store.append_message(
            ME, "hoi", ["muse-phu"], None, "Cung noi dung",
            self._msg_id(), client_key="ck-upgrade-1")
        self.assertTrue(duplicate2)
        self.assertEqual(message2["msg_id"], message["msg_id"])
        self.assertEqual(len(self.store.read_events()), 1)

    def test_resend_same_client_key_list_then_string_no_409(self):
        message, duplicate = self._append(
            ME, "Cung noi dung", to_account=["muse-phu"],
            client_key="ck-upgrade-2")
        self.assertFalse(duplicate)
        message2, duplicate2 = self.store.append_message(
            ME, "hoi", "muse-phu", None, "Cung noi dung",
            self._msg_id(), client_key="ck-upgrade-2")
        self.assertTrue(duplicate2)
        self.assertEqual(message2["msg_id"], message["msg_id"])

    def test_resend_same_client_key_different_content_still_409(self):
        self._append(ME, "Noi dung goc", to_account="muse-phu",
                     client_key="ck-upgrade-3")
        with self.assertRaises(center_store.CenterError) as ctx:
            self.store.append_message(
                ME, "hoi", ["muse-phu"], None, "Noi dung khac",
                self._msg_id(), client_key="ck-upgrade-3")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.error, "client_key_conflict")

class TestLaw2(StoreFixture):
    """Luật 2 — 3 trường hợp bảng frag-05 §3.2 (+ NS-26)."""

    def test_owner_tag_without_to_account_creates_task(self):
        # Trường hợp 1 (hồi quy P2, PLAN 15): tag của chủ nhân ở
        # khung chung, không ghi to_account -> sinh 1 việc chung.
        message, _dup = self._append(
            "owner", "@muse-phu xem giup viec nay", to_account=None)
        self.assertEqual(message["mentions"], ["muse-phu"])
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        task = next(iter(tasks.values()))
        self.assertEqual(task["assigned_to"], ["muse-phu"])
        self.assertEqual(task["source_msg_id"], message["msg_id"])
        # Tin giao-viec hub tự sinh giữ SCHEMA_VERSION = 1.
        gv_rows = [row for row in self.store.read_events()
                   if (row.get("record") or {}).get("kind") == "giao-viec"]
        self.assertEqual(len(gv_rows), 1)
        self.assertEqual(gv_rows[0]["record"]["schema_version"], 1)
        self.assertIsNone(gv_rows[0]["record"].get("to_account"))

    def test_owner_tag_with_to_account_creates_no_task(self):
        # D14 (QUYET-DINH-QUA-DEM-20-11.md, sau audit NS-L2): Luat 2
        # mo rong cho ca tin chu nhan — chu nhan dung o "Gui cho:"
        # (ghi ro to_account) roi go @ten trong noi dung thi tag
        # chi nhac ten, KHONG sinh viec. Duong sinh viec P2 chi
        # con khi chu nhan go @ten ma KHONG dung "Gui cho:"
        # (xem test_owner_tag_without_to_account_creates_task).
        message, _dup = self._append(
            "owner", "@muse-phu xem giup viec nay",
            to_account=["muse-phu"])
        self.assertEqual(message["mentions"], ["muse-phu"])
        self.assertEqual(message["schema_version"], 4)
        self.assertEqual(self.store.list_tasks(), {})
        self.assertEqual(len(self.store.read_events()), 1)

    def test_muse_tag_with_to_account_list_creates_no_task(self):
        # Trường hợp 2 (Luật 2): tin thảo luận của tài khoản Muse
        # đã ghi rõ người nhận -> tag chỉ nhắc tên.
        message, _dup = self._append(
            ME, "@muse-phu, y cau la gi?", to_account=["muse-phu"])
        self.assertEqual(message["mentions"], ["muse-phu"])
        self.assertEqual(message["schema_version"], 4)
        self.assertEqual(self.store.list_tasks(), {})
        self.assertEqual(len(self.store.read_events()), 1)

    def test_muse_tag_with_to_account_string_creates_no_task(self):
        # Luật 2 áp cho "ghi rõ to_account" ở cả 2 dạng lưu
        # (chuỗi cũ hay danh sách v4).
        self._append(ME, "@muse-phu, y cau la gi?",
                     to_account="muse-phu")
        self.assertEqual(self.store.list_tasks(), {})

    def test_muse_tag_for_other_than_to_account_creates_no_task(self):
        # Luật 2 áp cho TOÀN BỘ tin, kể cả tag nhắc người không
        # nằm trong to_account.
        self._append(ME, "@muse-ba, y cau la gi?",
                     to_account=["muse-phu"])
        self.assertEqual(self.store.list_tasks(), {})

    def test_muse_tag_without_to_account_creates_no_task(self):
        # Quyet 28/11/2026: tag cua tai khoan Muse chi nhac ten,
        # ke ca khi khong ghi to_account; chi tag chu nhan moi
        # sinh viec.
        message, _dup = self._append(
            ME, "@muse-phu xem giup viec nay", to_account=None)
        self.assertEqual(message["mentions"], ["muse-phu"])
        self.assertEqual(self.store.list_tasks(), {})
        self.assertEqual(len(self.store.read_events()), 1)

    def test_tag_in_dm_creates_no_task(self):
        # Trường hợp 3: tag ở phòng riêng chỉ nhắc tên.
        message, _dup = self._append(
            ME, "@muse-phu xem rieng", to_account=None,
            room="dm:muse-phu")
        self.assertEqual(message["mentions"], ["muse-phu"])
        self.assertEqual(self.store.list_tasks(), {})

    def test_resend_law2_message_creates_no_repair_task(self):
        # Gửi lại cùng msg_id: đường bù CHẶN-1 cũng tôn trọng
        # Luật 2 — không tạo việc bù cho tin thảo luận.
        _msg, _dup = self._append(
            ME, "@muse-phu, y cau la gi?", to_account=["muse-phu"])
        msg_id = self.store.read_events()[0].get("msg_id")
        _msg2, duplicate2 = self.store.append_message(
            ME, "hoi", ["muse-phu"], None, "@muse-phu, y cau la gi?",
            msg_id)
        self.assertTrue(duplicate2)
        self.assertEqual(self.store.list_tasks(), {})

    def test_rebuild_does_not_repair_law2_message(self):
        self._append(ME, "@muse-phu, y cau la gi?",
                     to_account=["muse-phu"])
        result = self.store.rebuild_tasks_state()
        self.assertEqual(result.get("tag_repaired"), 0)
        self.assertEqual(self.store.list_tasks(), {})

    def test_rebuild_does_not_repair_muse_tag_message(self):
        self._append(ME, "@muse-phu xem giup viec nay",
                     to_account=None)
        result = self.store.rebuild_tasks_state()
        self.assertEqual(result.get("tag_repaired"), 0)
        self.assertEqual(self.store.list_tasks(), {})

class HttpFixture(unittest.TestCase):
    """Fixture HTTP thật: gateway + receiver trên localhost."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "ui-config").mkdir(parents=True)
        (self.root / "data" / "bot-a").mkdir(parents=True)
        (self.root / "registry.json").write_text(json.dumps({
            "version": 1,
            "bots": {"bot-a": {"bot_id": "bot-a",
                               "platform": "zalo"}}}))
        (self.root / "secrets" / "bot-a.sync_secret").write_text(
            "TEST-FAKE-SYNC-BOT-A\n")
        _write_accounts(self.root)
        for account, (fetch, push) in SECRETS.items():
            (self.root / "secrets"
             / ("center-%s.fetch_secret" % account)).write_text(
                fetch + "\n")
            (self.root / "secrets"
             / ("center-%s.push_secret" % account)).write_text(
                push + "\n")
        self.networks = [ipaddress.ip_network("127.0.0.0/8"),
                         ipaddress.ip_network("::1/128")]
        self.gw_ctx = gateway.GatewayContext(
            self.root, self.networks, log_fn=lambda msg: None)
        self.gw_server, self.gw_port, self.gw_thread = _serve(
            lambda: gateway.make_server(self.gw_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.gw_server, self.gw_thread)
        self.rc_ctx = receiver.ReceiverContext(
            self.root, self.networks, log_fn=lambda msg: None, min_gap=0)
        self.rc_server, self.rc_port, self.rc_thread = _serve(
            lambda: receiver.make_server(self.rc_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.rc_server, self.rc_thread)
        self.store = CenterStore(self.root)
        self._msg_n = 0

    def _msg_id(self):
        self._msg_n += 1
        return "m-%012x" % self._msg_n

    def _push(self, account, secret, to_account):
        return _http(self.rc_port, "POST",
                     "/v1/center/%s/messages" % account, payload={
                         "msg_id": self._msg_id(), "kind": "hoi",
                         "to_account": to_account, "task_id": None,
                         "body": "Hoi thao luan", "room": "chung"},
                     secret=secret)

    def _event_count(self):
        return len(self.store.read_events())

class TestReceiverV4(HttpFixture):
    """Receiver chấp nhận v4 / trả 403 đúng 2 mã / trần 5."""

    def test_accepts_list_and_stores_version_4(self):
        status, body = self._push(ME, SECRETS[ME][1],
                                  ["muse-phu", "muse-ba"])
        self.assertEqual(status, 200, body)
        record = self.store.read_events()[0]["record"]
        self.assertEqual(record["to_account"], ["muse-phu", "muse-ba"])
        self.assertEqual(record["schema_version"], 4)

    def test_accepts_single_person_list(self):
        status, body = self._push(ME, SECRETS[ME][1], ["muse-phu"])
        self.assertEqual(status, 200, body)
        record = self.store.read_events()[0]["record"]
        self.assertEqual(record["to_account"], ["muse-phu"])
        self.assertEqual(record["schema_version"], 4)

    def test_accepts_old_string(self):
        status, body = self._push(ME, SECRETS[ME][1], "muse-phu")
        self.assertEqual(status, 200, body)
        record = self.store.read_events()[0]["record"]
        self.assertEqual(record["to_account"], "muse-phu")
        self.assertEqual(record["schema_version"], 1)

    def test_unknown_target_400(self):
        status, body = self._push(ME, SECRETS[ME][1],
                                  ["muse-khong-co"])
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "bad_to_account")
        self.assertEqual(self._event_count(), 0)

    def test_owner_as_target_400(self):
        # "owner" khớp mẫu nhưng không phải tài khoản trong
        # accounts.json -> 400 (receiver kiểm tra tồn tại).
        status, body = self._push(ME, SECRETS[ME][1], ["owner"])
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "bad_to_account")
        self.assertEqual(self._event_count(), 0)

    def test_one_bad_one_good_400_atomic(self):
        status, body = self._push(ME, SECRETS[ME][1],
                                  ["muse-phu", "muse-khong-co"])
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "bad_to_account")
        self.assertEqual(self._event_count(), 0)

    def test_suspended_target_403_not_participating(self):
        status, body = self._push(ME, SECRETS[ME][1], ["muse-ngung"])
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"),
                         "to_account_not_participating")
        self.assertEqual(self._event_count(), 0)

    def test_suspended_plus_active_403_atomic(self):
        status, body = self._push(ME, SECRETS[ME][1],
                                  ["muse-phu", "muse-ngung"])
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"),
                         "to_account_not_participating")
        self.assertEqual(self._event_count(), 0)

    def test_discussion_disabled_target_403(self):
        status, body = self._push(ME, SECRETS[ME][1], ["muse-tat"])
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "discussion_disabled")
        self.assertEqual(self._event_count(), 0)

    def test_sender_discussion_disabled_403(self):
        # Tài khoản tự tắt thảo luận mà vẫn ghi to_account thì
        # hub từ chối ngay (frag-05 §4.2).
        status, body = self._push("muse-tat", SECRETS["muse-tat"][1],
                                  ["muse-phu"])
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "discussion_disabled")
        self.assertEqual(self._event_count(), 0)

    def test_sender_discussion_disabled_without_to_account_ok(self):
        # Không ghi to_account thì không phải tin thảo luận —
        # công tắc thảo luận không chặn.
        status, body = self._push("muse-tat", SECRETS["muse-tat"][1],
                                  None)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._event_count(), 1)

    def test_more_than_5_400(self):
        status, body = self._push(ME, SECRETS[ME][1], [
            "muse-phu", "muse-ba", "muse-bon",
            "muse-nam", "muse-sau", "muse-tat"])
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "bad_to_account")
        self.assertEqual(self._event_count(), 0)

    def test_wrong_type_400(self):
        status, body = self._push(ME, SECRETS[ME][1], 123)
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "bad_to_account")
        self.assertEqual(self._event_count(), 0)

class OutboxFixture(unittest.TestCase):
    """Fixture phía Muse: outbox + fetcher (không server)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.muse_root = Path(self.tmp.name) / "muse"
        self.muse_root.mkdir()
        self.logs = []
        self.config = {
            "account_id": ME,
            "receiver_endpoint": "http://127.0.0.1:9",
            "push_secret": SECRETS[ME][1],
            "via_proxy": False,
        }

    def _flush(self, http_post_fn):
        return center_outbox.flush(
            self.muse_root, config=dict(self.config),
            log_fn=self.logs.append,
            alert_fn=lambda *args: None,
            http_post_fn=http_post_fn)

    def _cursor(self):
        return center_outbox.load_cursor(
            center_outbox._cursor_path(self.muse_root, ME))

class TestOutboxV4(OutboxFixture):
    """Outbox validate + enqueue danh sách; flush giữ dòng v4."""

    def test_validate_accepts_list_string_none(self):
        base = {"kind": "hoi", "msg_id": "m-000000000001",
                "body": "Hoi", "to_account": ["muse-phu", "muse-ba"]}
        self.assertEqual(center_outbox.validate_message_row(base), [])
        self.assertEqual(center_outbox.validate_message_row(
            dict(base, to_account=["muse-phu"])), [])
        self.assertEqual(center_outbox.validate_message_row(
            dict(base, to_account="muse-phu")), [])
        self.assertEqual(center_outbox.validate_message_row(
            dict(base, to_account=None)), [])

    def test_validate_rejects_over_5_bad_element_wrong_type(self):
        base = {"kind": "hoi", "msg_id": "m-000000000001",
                "body": "Hoi"}
        self.assertTrue(center_outbox.validate_message_row(dict(
            base, to_account=["muse-phu", "muse-ba", "muse-bon",
                              "muse-nam", "muse-sau", "muse-tat"])))
        self.assertTrue(center_outbox.validate_message_row(
            dict(base, to_account=["muse-phu", "Muse-Ba"])))
        self.assertTrue(center_outbox.validate_message_row(
            dict(base, to_account=["muse-phu", 123])))
        self.assertTrue(center_outbox.validate_message_row(
            dict(base, to_account=123)))
        self.assertTrue(center_outbox.validate_message_row(
            dict(base, to_account="Muse-Phu")))

    def test_enqueue_accepts_list_and_rejects_over_5(self):
        row = center_outbox.enqueue_message(
            self.muse_root, ME, "hoi", "Hoi ca hai",
            to_account=["muse-phu", "muse-ba"])
        self.assertEqual(row["to_account"], ["muse-phu", "muse-ba"])
        rows, total, _skipped = center_outbox.read_outbox(self.muse_root)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0][1]["to_account"],
                         ["muse-phu", "muse-ba"])
        with self.assertRaises(ValueError):
            center_outbox.enqueue_message(
                self.muse_root, ME, "hoi", "Hoi sau nguoi",
                to_account=["muse-phu", "muse-ba", "muse-bon",
                            "muse-nam", "muse-sau", "muse-tat"])

    def test_flush_holds_v4_row_on_400_bad_to_account_then_retries(self):
        # D1(f): receiver cũ trả 400 bad_to_account cho tin v4 ->
        # GIỮ dòng lại (không tiến con trỏ), thử lại chu kỳ sau.
        center_outbox.enqueue_message(
            self.muse_root, ME, "hoi", "Hoi thao luan",
            to_account=["muse-phu"])
        calls = {"n": 0}

        def fake_old_receiver(url, secret, payload, timeout, label):
            calls["n"] += 1
            return 400, {"error": "bad_to_account"}, None

        result = self._flush(fake_old_receiver)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["cursor_after"], 0)
        self.assertEqual(self._cursor()["line"], 0)
        self.assertEqual(result["error"],
                         "http_400_bad_to_account_v4_retry")

        # Receiver "nâng cấp": lần flush sau thành công.
        def fake_new_receiver(url, secret, payload, timeout, label):
            calls["n"] += 1
            self.assertEqual(payload["to_account"], ["muse-phu"])
            return 200, {"duplicate": False}, None

        result = self._flush(fake_new_receiver)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(self._cursor()["line"], 1)
        self.assertEqual(calls["n"], 2)

    def test_flush_drops_string_row_on_400_bad_to_account(self):
        # Tin v1-v3 (chuỗi) bị 400 bad_to_account vẫn là lỗi vĩnh
        # viễn như cũ — chỉ riêng dòng v4 mới được giữ lại.
        center_outbox.enqueue_message(
            self.muse_root, ME, "hoi", "Hoi cu", to_account="muse-phu")

        def fake(url, secret, payload, timeout, label):
            return 400, {"error": "bad_to_account"}, None

        result = self._flush(fake)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(self._cursor()["line"], 1)

    def test_flush_drops_v4_row_on_other_400(self):
        # Chỉ riêng 400 bad_to_account mới giữ dòng v4; các lỗi
        # 400 khác vẫn vĩnh viễn.
        center_outbox.enqueue_message(
            self.muse_root, ME, "hoi", "Hoi thao luan",
            to_account=["muse-phu"])

        def fake(url, secret, payload, timeout, label):
            return 400, {"error": "bad_body"}, None

        result = self._flush(fake)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)

    def test_flush_drops_v4_row_on_400_without_json_body(self):
        # 400 không có body JSON thì không xác định được mã lỗi —
        # giữ hành vi cũ (vĩnh viễn).
        center_outbox.enqueue_message(
            self.muse_root, ME, "hoi", "Hoi thao luan",
            to_account=["muse-phu"])

        def fake(url, secret, payload, timeout, label):
            return 400, None, None

        result = self._flush(fake)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)

class TestFetcherV4(HttpFixture):
    """Fetcher sao chép tin v4 nguyên vẹn (D1(e))."""

    def test_append_messages_copies_v4_record_verbatim(self):
        record = {
            "schema_version": 4, "msg_id": "m-000000000099",
            "from_account": ME, "to_account": ["muse-phu", "muse-ba"],
            "kind": "hoi", "task_id": None,
            "body": "@muse-phu, @muse-ba xem giup",
            "file": None, "created_at": "2026-11-20T10:00:00+07:00",
            "report_role": None, "room": "chung",
            "mentions": ["muse-phu", "muse-ba"],
        }
        muse_root = Path(self.tmp.name) / "muse"
        muse_root.mkdir()
        added = center_fetcher._append_messages(muse_root, [{
            "kind": "message", "seq": 7,
            "at": "2026-11-20T10:00:00+07:00",
            "msg_id": "m-000000000099", "record": record}],
            log_fn=lambda msg: None)
        self.assertEqual(added, 1)
        rows, _skipped = center_fetcher.read_jsonl(
            muse_root / "center_messages.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seq"], 7)
        self.assertEqual(rows[0]["record"], record)
        self.assertEqual(rows[0]["record"]["to_account"],
                         ["muse-phu", "muse-ba"])
        self.assertEqual(rows[0]["record"]["schema_version"], 4)

    def test_end_to_end_v4_through_receiver_gateway_fetcher(self):
        # Tin v4 đi hết: receiver ghi -> gateway trả feed ->
        # fetcher sao chép nguyên vẹn, không kiểm tra kiểu
        # to_account.
        status, body = self._push(ME, SECRETS[ME][1],
                                  ["muse-phu", "muse-ba"])
        self.assertEqual(status, 200, body)
        hub_record = self.store.read_events()[0]["record"]
        self.assertEqual(hub_record["schema_version"], 4)
        status, feed = _http(
            self.gw_port, "GET",
            "/v1/center/%s/feed?since=0&limit=100" % OTHER,
            secret=SECRETS[OTHER][0])
        self.assertEqual(status, 200, feed)
        events = [event for event in feed["events"]
                  if event.get("kind") == "message"]
        self.assertEqual(len(events), 1)
        muse_root = Path(self.tmp.name) / "muse"
        muse_root.mkdir()
        added = center_fetcher._append_messages(
            muse_root, events, log_fn=lambda msg: None)
        self.assertEqual(added, 1)
        rows, _skipped = center_fetcher.read_jsonl(
            muse_root / "center_messages.jsonl")
        self.assertEqual(rows[0]["record"], hub_record)

    def test_end_to_end_v4_through_run_cycle(self):
        status, body = self._push(ME, SECRETS[ME][1], ["muse-phu"])
        self.assertEqual(status, 200, body)
        hub_record = self.store.read_events()[0]["record"]
        muse_root = Path(self.tmp.name) / "muse"
        muse_root.mkdir()
        _write_json(muse_root / "center-config" / ("%s.json" % OTHER), {
            "endpoint": "http://127.0.0.1:%d" % self.gw_port,
            "receiver_endpoint": "http://127.0.0.1:%d" % self.rc_port,
            "fetch_secret": SECRETS[OTHER][0],
            "push_secret": SECRETS[OTHER][1],
            "via_proxy": False,
        })
        result = center_fetcher.run_cycle(
            muse_root, log_fn=lambda msg: None,
            alert_fn=lambda *args: None)
        self.assertFalse(result["skipped"])
        self.assertIsNone(
            result["accounts"][OTHER]["error"],
            result["accounts"][OTHER])
        rows, _skipped = center_fetcher.read_jsonl(
            muse_root / "center_messages.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record"], hub_record)
        self.assertEqual(rows[0]["record"]["to_account"], ["muse-phu"])

A = "muse-chinh"

B = "muse-phu"

C = "muse-ba"

NOW = time.time()

def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

def _append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")

def _msg_record(kind="hoi", sender="owner", room="chung",
                to_account=None, mentions=None, msg_id="m-000000000001",
                created_at=None, source=None, task_id=None,
                body="Tin thu"):
    record = {
        "schema_version": 1,
        "msg_id": msg_id,
        "from_account": sender,
        "to_account": to_account,
        "kind": kind,
        "task_id": task_id,
        "body": body,
        "created_at": created_at or _iso(NOW),
        "report_role": None,
        "room": room,
        "mentions": mentions,
    }
    if source is not None:
        record["source"] = source
    return record

def _muse_root(tmp, messages=(), tasks=(), chat_state=None,
               config_accounts=()):
    """Thư mục gốc Muse tạm: center_messages.jsonl + center_tasks/ +
    center_chat_state/ (tùy chọn) + center-config/ (chỉ tên file,
    không secret — planner chỉ cần tên để phát hiện tài khoản)."""
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    for seq, record in messages:
        _append_jsonl(root / "center_messages.jsonl", {
            "seq": seq, "at": record.get("created_at"),
            "msg_id": record.get("msg_id"), "record": record})
    for task_id, record in tasks:
        _write_json(root / "center_tasks" / ("%s.json" % task_id), {
            "task_id": task_id, "seq": 1, "record": record})
    if chat_state is not None:
        _write_json(root / "center_chat_state" / ("%s.json" % A),
                    chat_state)
    for account_id in config_accounts:
        _write_json(root / "center-config" / ("%s.json" % account_id),
                    {})
    return root

def _round_messages(n=5, room="chung"):
    """n tin thảo luận A<->B xen kẽ trong phòng, seq 1..n. Tin lẻ
    A -> B, tin chẵn B -> A (tin mở vòng seq 1 là A -> B)."""
    out = []
    for seq in range(1, n + 1):
        if seq % 2 == 1:
            sender, to = A, B
        else:
            sender, to = B, A
        out.append((seq, _msg_record(
            sender=sender, room=room, to_account=to,
            msg_id="m-%012d" % seq,
            body="Tin thao luan %d" % seq)))
    return out

class DiscussionKeyTest(unittest.TestCase):
    """Khóa vòng D9: "room|opener|p1,p2,..." (participants đã sắp
    xếp, gồm người mở + to_account của tin mở vòng)."""

    def test_key_sorts_participants_and_includes_opener(self):
        self.assertEqual(
            center_discussion.discussion_key(
                "chung", A, [B, A]),
            "chung|muse-chinh|muse-chinh,muse-phu")
        self.assertEqual(
            center_discussion.discussion_key(
                "chung", A, [C, B, A]),
            "chung|muse-chinh|muse-ba,muse-chinh,muse-phu")

    def test_key_requires_opener_in_participants(self):
        with self.assertRaises(ValueError):
            center_discussion.discussion_key("chung", A, [B])
        with self.assertRaises(ValueError):
            center_discussion.discussion_key("chung", A, [])

    def test_parse_round_trip(self):
        key = center_discussion.discussion_key("chung", A, [B, A])
        self.assertEqual(center_discussion.parse_discussion_key(key),
                         ("chung", A, ("muse-chinh", "muse-phu")))
        self.assertEqual(center_discussion.round_accounts(key),
                         {A, B})
        self.assertIsNone(center_discussion.parse_discussion_key("x"))
        self.assertIsNone(
            center_discussion.parse_discussion_key("chung||muse-phu"))

    def test_is_discussion_message(self):
        self.assertTrue(center_discussion.is_discussion_message(
            _msg_record(sender=A, to_account=B)))
        self.assertTrue(center_discussion.is_discussion_message(
            _msg_record(sender=A, to_account=[B, C])))
        # Khong ghi to_account -> khong phai tin thao luan.
        self.assertFalse(center_discussion.is_discussion_message(
            _msg_record(sender=A)))
        # Tin cua chu nhan khong tinh vao luot.
        self.assertFalse(center_discussion.is_discussion_message(
            _msg_record(sender="owner", to_account=B)))
        # Tin bao-cao / tin hub tu sinh khong tinh vao luot.
        self.assertFalse(center_discussion.is_discussion_message(
            _msg_record(kind="bao-cao", sender=A, to_account=B)))
        self.assertFalse(center_discussion.is_discussion_message(
            _msg_record(sender=A, to_account=B, source="hub")))

class CountRoundsTest(unittest.TestCase):
    """Đếm lượt: 1 tin hoi = 1 lượt; chạm 5 thì dừng; tin thứ 6
    không làm count tăng thêm; tính lại idempotent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _root(self, messages, chat_state=None):
        return _muse_root(Path(self.tmp.name) / "muse",
                          messages=messages, chat_state=chat_state)

    def _rounds(self, root, account_id):
        return center_dispatcher.read_chat_state(
            root, account_id).get("discussion_rounds") or {}

    def test_one_message_is_one_turn(self):
        root = self._root(_round_messages(1))
        rounds = center_discussion.refresh_discussion_rounds(root, B)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds, {key: {
            "count": 1, "state": "mo", "opened_seq": 1,
            "last_seq": 1}})

    def test_five_messages_close_the_round(self):
        root = self._root(_round_messages(5))
        rounds = center_discussion.refresh_discussion_rounds(root, B)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds[key]["count"], 5)
        self.assertEqual(rounds[key]["state"], "dong")
        self.assertEqual(rounds[key]["opened_seq"], 1)
        self.assertEqual(rounds[key]["last_seq"], 5)

    def test_sixth_message_keeps_count_at_five(self):
        root = self._root(_round_messages(6))
        rounds = center_discussion.refresh_discussion_rounds(root, B)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds[key]["count"], 5)
        self.assertEqual(rounds[key]["state"], "dong")
        self.assertEqual(rounds[key]["last_seq"], 6)

    def test_non_discussion_messages_do_not_count(self):
        messages = [
            (1, _msg_record(sender=A, to_account=B,
                            msg_id="m-000000000001")),
            (2, _msg_record(kind="bao-cao", sender=A, to_account=B,
                            msg_id="m-000000000002")),
            (3, _msg_record(sender=A, msg_id="m-000000000003")),
            (4, _msg_record(sender=A, to_account=B, source="hub",
                            msg_id="m-000000000004")),
            (5, _msg_record(sender=B, to_account=A,
                            msg_id="m-000000000005")),
        ]
        root = self._root(messages)
        rounds = center_discussion.refresh_discussion_rounds(root, B)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds[key]["count"], 2)
        self.assertEqual(rounds[key]["state"], "mo")

    def test_refresh_is_idempotent_and_preserves_markers(self):
        root = self._root(
            _round_messages(3),
            chat_state={"account_id": A, "rooms": {
                "chung": {"replied_up_to_seq": 2,
                          "updated_at": _iso(self.now)}},
                "discussion_rounds": {}})
        first = center_discussion.refresh_discussion_rounds(root, A)
        path = root / "center_chat_state" / ("%s.json" % A)
        content = path.read_text(encoding="utf-8")
        second = center_discussion.refresh_discussion_rounds(root, A)
        self.assertEqual(first, second)
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        state = center_dispatcher.read_chat_state(root, A)
        self.assertEqual(
            state["rooms"]["chung"]["replied_up_to_seq"], 2)

    def test_refresh_writes_nothing_without_rounds(self):
        root = self._root([
            (3, _msg_record(room="dm:%s" % A, sender="owner")),
        ])
        self.assertEqual(
            center_discussion.refresh_discussion_rounds(root, A), {})
        self.assertFalse((root / "center_chat_state").exists())

class WakeRoundLimitTest(unittest.TestCase):
    """W7: vòng chạm 5 thì dừng — tin thứ 5 và thứ 6 đều không
    đánh thức ai; ở 4 lượt thì vẫn đánh thức."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _plan(self, root, account_id):
        return center_dispatcher.plan_chat_for_account(
            root, account_id,
            deps={"log_fn": lambda msg: None}, now=self.now)

    def test_four_turns_still_wake(self):
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=_round_messages(4))
        plan_b = self._plan(root, B)
        self.assertIsNotNone(plan_b)
        self.assertEqual(plan_b["seqs"], [1, 3])
        plan_a = self._plan(root, A)
        self.assertIsNotNone(plan_a)
        self.assertEqual(plan_a["seqs"], [2, 4])

    def test_fifth_and_sixth_messages_wake_nobody(self):
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=_round_messages(5))
        self.assertIsNone(self._plan(root, B))
        self.assertIsNone(self._plan(root, A))
        root = _muse_root(Path(self.tmp.name) / "muse2",
                          messages=_round_messages(6))
        self.assertIsNone(self._plan(root, B))
        self.assertIsNone(self._plan(root, A))

    def test_w7_only_blocks_the_message_own_round(self):
        # Vong A-B da dong (5 tin); vong A-C moi mo (1 tin) thi C
        # van duoc danh thuc — D9: 2 cap khong lam nhau dung oan.
        messages = _round_messages(5) + [
            (6, _msg_record(sender=A, to_account=C,
                            msg_id="m-000000000006")),
        ]
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=messages)
        plan_c = self._plan(root, C)
        self.assertIsNotNone(plan_c)
        self.assertEqual(plan_c["seqs"], [6])
        self.assertIsNone(self._plan(root, B))

    def test_w7_check_chat_wake_new_format_keys(self):
        record = _msg_record(sender=B, to_account=A)
        closed = {"chung|%s|%s,%s" % (A, A, B): {
            "count": 5, "state": "dong", "opened_seq": 1,
            "last_seq": 9}}
        ctx = {"replied_up_to_seq": 0, "source_msg_ids": set(),
               "discussion_rounds": closed, "participating": True,
               "discussion_enabled": True}
        self.assertIn("W7", center_dispatcher.check_chat_wake(
            record, 10, A, ctx))
        other_pair_closed = {"chung|%s|%s,%s" % (A, A, C): {
            "count": 5, "state": "dong", "opened_seq": 1,
            "last_seq": 9}}
        ctx["discussion_rounds"] = other_pair_closed
        self.assertNotIn("W7", center_dispatcher.check_chat_wake(
            record, 10, A, ctx))

class OwnerResetTest(unittest.TestCase):
    """Chủ nhân xen tin → trả lời gộp + bộ đếm về 0; chỉ chủ nhân
    mở vòng mới."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _plan(self, root, account_id):
        return center_dispatcher.plan_chat_for_account(
            root, account_id,
            deps={"log_fn": lambda msg: None}, now=self.now)

    def _rounds(self, root, account_id):
        return center_dispatcher.read_chat_state(
            root, account_id).get("discussion_rounds") or {}

    def test_owner_message_resets_count_and_groups_reply(self):
        messages = _round_messages(3) + [
            (4, _msg_record(sender="owner", mentions=[B],
                            msg_id="m-000000000004",
                            body="Chu nhan xen tin")),
        ]
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=messages)
        rounds = center_discussion.refresh_discussion_rounds(root, B)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds[key]["count"], 0)
        self.assertEqual(rounds[key]["state"], "mo")
        # B tra loi GOP: tin thao luan chua tra loi + tin chu nhan.
        plan = self._plan(root, B)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["seqs"], [1, 3, 4])

    def test_account_cannot_reopen_closed_round(self):
        # Vong da dong; A nhan tiep (seq 6) khong mo vong moi,
        # khong danh thuc ai.
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=_round_messages(6))
        self.assertIsNone(self._plan(root, B))
        self.assertIsNone(self._plan(root, A))

    def test_owner_reopens_round_then_account_continues(self):
        messages = _round_messages(5) + [
            (6, _msg_record(sender=B, to_account=A,
                            msg_id="m-000000000006")),
            (7, _msg_record(sender="owner", mentions=[B],
                            msg_id="m-000000000007",
                            body="Chu nhan mo vong moi")),
        ]
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=messages)
        # Tin thu 6 cua tai khoan sau khi vong dong: khong danh
        # thuc ai (kiem bang ke hoach cua A truoc khi co tin chu
        # nhan — dung ban sao chi co 6 tin).
        root6 = _muse_root(Path(self.tmp.name) / "muse6",
                           messages=_round_messages(6))
        self.assertIsNone(self._plan(root6, A))
        # Chu nhan nhan tin goi ten B -> B duoc danh thuc.
        plan_b = self._plan(root, B)
        self.assertIsNotNone(plan_b)
        self.assertIn(7, plan_b["seqs"])
        # B tra loi (seq 8, goi A) la luot 1 cua vong moi.
        messages = messages + [
            (8, _msg_record(sender=B, to_account=A,
                            msg_id="m-000000000008")),
        ]
        root = _muse_root(Path(self.tmp.name) / "muse8",
                          messages=messages)
        rounds = center_discussion.refresh_discussion_rounds(root, A)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds[key]["count"], 1)
        self.assertEqual(rounds[key]["state"], "mo")
        self.assertEqual(rounds[key]["opened_seq"], 8)
        plan_a = self._plan(root, A)
        self.assertIsNotNone(plan_a)
        self.assertIn(8, plan_a["seqs"])

class SelfReplyAndToAccountTest(unittest.TestCase):
    """A không tự trả lời tin của chính mình; B trả lời ghi
    to_account chứa A → A được đánh thức lại, B không ghi
    to_account → A không bị đánh thức lại."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _plan(self, root, account_id):
        return center_dispatcher.plan_chat_for_account(
            root, account_id,
            deps={"log_fn": lambda msg: None}, now=self.now)

    def test_no_self_reply(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (1, _msg_record(sender=A, to_account=B,
                            msg_id="m-000000000001")),
        ])
        self.assertIsNone(self._plan(root, A))
        self.assertIsNotNone(self._plan(root, B))

    def test_reply_with_to_account_rewakes(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (1, _msg_record(sender=A, to_account=B,
                            msg_id="m-000000000001")),
            (2, _msg_record(sender=B, to_account=A,
                            msg_id="m-000000000002")),
        ])
        plan_a = self._plan(root, A)
        self.assertIsNotNone(plan_a)
        self.assertEqual(plan_a["seqs"], [2])

    def test_reply_without_to_account_wakes_nobody(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (1, _msg_record(sender=A, to_account=B,
                            msg_id="m-000000000001")),
            (2, _msg_record(sender=B, to_account=None,
                            msg_id="m-000000000002")),
        ])
        self.assertIsNone(self._plan(root, A))
        # Tin tra loi khong ghi to_account cung khong tinh vao luot.
        rounds = center_discussion.refresh_discussion_rounds(root, A)
        key = "chung|%s|%s,%s" % (A, A, B)
        self.assertEqual(rounds[key]["count"], 1)

class MultiRecipientTest(unittest.TestCase):
    """Tin nhiều người nhận: tất cả tài khoản được tag đều có plan
    riêng chứa tin đó (song song), đếm chung 1 lượt; tài khoản
    không được gọi tên có 0 worker chat được đánh thức."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _plan(self, root, account_id):
        return center_dispatcher.plan_chat_for_account(
            root, account_id,
            deps={"log_fn": lambda msg: None}, now=self.now)

    def test_two_recipients_both_planned_shared_count(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (1, _msg_record(sender=A, to_account=[B, C],
                            msg_id="m-000000000001")),
        ])
        plan_b = self._plan(root, B)
        plan_c = self._plan(root, C)
        self.assertIsNotNone(plan_b)
        self.assertIsNotNone(plan_c)
        self.assertEqual(plan_b["seqs"], [1])
        self.assertEqual(plan_c["seqs"], [1])
        # 1 tin = 1 luot, dem chung cho ca 2 (khong dem 2 lan).
        key = center_discussion.discussion_key(
            "chung", A, [A, B, C])
        for account_id in (B, C):
            rounds = center_discussion.refresh_discussion_rounds(
                root, account_id)
            self.assertEqual(rounds[key]["count"], 1)
            self.assertEqual(rounds[key]["state"], "mo")
        # Nguoi gui khong tu tra loi tin cua minh.
        self.assertIsNone(self._plan(root, A))

    def test_run_planning_emits_both_recipients(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (1, _msg_record(sender=A, to_account=[B, C],
                            msg_id="m-000000000001")),
        ], config_accounts=[B, C])
        _plan, chats, _stuck, _summary = center_plan_cycle.run_planning(
            root)
        self.assertEqual(
            sorted(chat["account_id"] for chat in chats),
            sorted([B, C]))
        for chat in chats:
            self.assertEqual(chat["seqs"], [1])
            self.assertEqual(chat["room"], "chung")
            rounds = chat["discussion"]["rounds"]
            self.assertEqual(len(rounds), 1)
            self.assertEqual(rounds[0]["count"], 1)
            self.assertEqual(rounds[0]["state"], "mo")
            self.assertEqual(rounds[0]["remaining"], 4)

    def test_three_accounts_untagged_gets_zero_wakes(self):
        messages = [
            (1, _msg_record(sender=A, to_account=[B],
                            msg_id="m-000000000001")),
        ]
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=messages)
        self.assertIsNotNone(self._plan(root, B))
        self.assertIsNone(self._plan(root, C))
        self.assertIsNone(self._plan(root, A))
        # Dung ban sao rieng cho run_planning (plan o tren da giu
        # claim phong cua B — chu ky moi moi lap lai duoc).
        root2 = _muse_root(Path(self.tmp.name) / "muse2",
                           messages=messages,
                           config_accounts=[A, B, C])
        _plan, chats, _stuck, _summary = center_plan_cycle.run_planning(
            root2)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["account_id"], B)

    def test_unrelated_rooms_still_one_chat_per_cycle(self):
        # T3h giu nguyen: 2 phong khong lien quan thi chi phong co
        # tin cu nhat duoc phat trong 1 chu ky.
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (1, _msg_record(room="dm:%s" % B, sender="owner",
                            msg_id="m-000000000001")),
            (2, _msg_record(sender=A, to_account=C,
                            msg_id="m-000000000002")),
        ], config_accounts=[B, C])
        _plan, chats, _stuck, _summary = center_plan_cycle.run_planning(
            root)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["account_id"], B)
        self.assertEqual(chats[0]["room"], "dm:%s" % B)

class DiscussionBriefTest(unittest.TestCase):
    """Brief worker chat chứa thông tin vòng: số lượt đã dùng/tổng
    5, trạng thái vòng, ai mở vòng + hướng dẫn ghi to_account."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        self.root = Path(self.tmp.name) / "muse"

    def test_brief_shows_open_round(self):
        root = _muse_root(self.root, messages=_round_messages(3))
        brief = center_chat_brief.build_brief(
            root, B, "chung", [3], now=self.now)
        self.assertIn("VÒNG THẢO LUẬN", brief)
        self.assertIn("3/5", brief)
        self.assertIn("còn 2 lượt", brief)
        self.assertIn("đang mở", brief)
        self.assertIn("người mở vòng: %s" % A, brief)
        self.assertIn("muse-phu", brief)
        self.assertIn(
            "Muốn hỏi tiếp ai thì ghi rõ gửi cho ai (to_account)",
            brief)
        self.assertIn(
            "không ghi to_account thì không đánh thức ai", brief)

    def test_brief_shows_closed_round(self):
        root = _muse_root(self.root, messages=_round_messages(5))
        brief = center_chat_brief.build_brief(
            root, B, "chung", [5], now=self.now)
        self.assertIn("5/5", brief)
        self.assertIn("ĐÃ DỪNG", brief)
        self.assertIn("chờ chủ nhân nhắn tin gọi tên", brief)

    def test_brief_without_round(self):
        root = _muse_root(self.root, messages=[
            (1, _msg_record(room="dm:%s" % B, sender="owner",
                            msg_id="m-000000000001")),
        ])
        brief = center_chat_brief.build_brief(
            root, B, "dm:%s" % B, [1], now=self.now)
        self.assertIn("VÒNG THẢO LUẬN", brief)
        self.assertIn("Không có vòng thảo luận nào đang mở", brief)

    def test_brief_shows_split_message_costs_turns(self):
        root = _muse_root(self.root, messages=_round_messages(3))
        brief = center_chat_brief.build_brief(
            root, B, "chung", [3], now=self.now)
        self.assertIn("mỗi tin tách tính 1 lượt", brief)

class PlannerDiscussionFieldTest(unittest.TestCase):
    """Output lập kế hoạch mang trường discussion tối thiểu."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3l-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def test_run_planning_chat_has_discussion_field(self):
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=_round_messages(2))
        _plan, chats, _stuck, _summary = center_plan_cycle.run_planning(
            root, account_id=B)
        self.assertEqual(len(chats), 1)
        rounds = chats[0]["discussion"]["rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["count"], 2)
        self.assertEqual(rounds[0]["opener"], A)
        self.assertEqual(rounds[0]["participants"], [A, B])

    def test_main_dry_run_one_line_json_with_discussion(self):
        root = _muse_root(Path(self.tmp.name) / "muse",
                          messages=_round_messages(2))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = center_plan_cycle.main([
                "--root", str(root), "--account-id", B, "--dry-run"])
        self.assertEqual(code, 0)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(len(data["center_chats"]), 1)
        self.assertIn("discussion", data["center_chats"][0])

PASSWORD = "Test-Pass-123"

FETCH_CHINH = "TEST-FAKE-CENTER-FETCH-CHINH"

FETCH_PHU = "TEST-FAKE-CENTER-FETCH-PHU"

def _build_multipart(fields, files, boundary="----TESTL"):
    """Dựng body multipart/form-data (mẫu test T3i)."""
    out = b""
    for name, value in fields:
        out += b"--" + boundary.encode() + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n'
                % name).encode()
        out += value.encode("utf-8") + b"\r\n"
    for field, filename, data in files:
        out += b"--" + boundary.encode() + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"; '
                'filename="%s"\r\n\r\n' % (field, filename)).encode()
        out += data + b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return out, "multipart/form-data; boundary=" + boundary

class T3lUiFixture(unittest.TestCase):
    """Fixture UI + 3 tài khoản trung tâm (2 tham gia, 1 chưa)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "secrets").mkdir()
        (self.root / "data" / "bot-a").mkdir(parents=True)
        (self.root / "ui-config").mkdir()
        self.now = time.time()
        (self.root / "registry.json").write_text(json.dumps({
            "version": 1,
            "bots": {"bot-a": {"bot_id": "bot-a", "platform": "zalo",
                               "display_name": "Bot A", "enabled": True,
                               "paused": False,
                               "awaiting_muse_sync": False,
                               "token_pending_verification": False}}}),
            encoding="utf-8")
        self._write_accounts({"muse-chinh": True, "muse-phu": True,
                              "muse-ngoai": False})
        for account_id, fetch_secret in (("muse-chinh", FETCH_CHINH),
                                         ("muse-phu", FETCH_PHU)):
            (self.root / "secrets" /
             ("center-%s.fetch_secret" % account_id)).write_text(
                fetch_secret + "\n", encoding="utf-8")
            (self.root / "secrets" /
             ("center-%s.push_secret" % account_id)).write_text(
                "TEST-FAKE-CENTER-PUSH-%s\n" % account_id.upper(),
                encoding="utf-8")
        hashed = ui_auth.hash_password(PASSWORD, n=2 ** 10, r=8, p=1)
        (self.root / "ui-config" / "password.hash").write_text(
            hashed + "\n", encoding="utf-8")
        self.app = ui_app.UiApp(
            self.root,
            unit_runner=lambda a, b: (0, ""),
            unit_status_fn=lambda bot_id: "active",
            tailscale_fn=lambda: {"up": True, "ip": "100.64.0.5"},
            log_fn=lambda bot_id, limit=50: ["log ok"],
            verify_fn=lambda p, u, t: (True, {"id": "1", "name": "Bot"}),
            gateway_status_fn=lambda: "active")
        self.center = center_store.CenterStore(self.root)

    def _write_accounts(self, flags, extras=None):
        names = {"muse-chinh": "Muse chính", "muse-phu": "Muse phụ",
                 "muse-ngoai": "Muse ngoài"}
        accounts = {}
        for account_id, participating in flags.items():
            record = {"display_name": names.get(account_id, account_id),
                      "center_participating": participating}
            if extras and account_id in extras:
                record.update(extras[account_id])
            accounts[account_id] = record
        (self.root / "ui-config" / "accounts.json").write_text(
            json.dumps({"version": 1, "accounts": accounts}),
            encoding="utf-8")

    # -- request helpers (mẫu test T3i) ----------------------------------
    def request(self, method, path, form=None, headers=None, query=None,
                raw_body=None):
        body = raw_body if raw_body is not None else b""
        hdrs = dict(headers or {})
        if form is not None:
            body = urlencode(form, doseq=True).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        return ui_app.Request(method, path, query=query or {}, headers=hdrs,
                              body=body, client_ip="127.0.0.1")

    def login(self, password=PASSWORD):
        resp = self.app.handle(self.request("GET", "/login"))
        csrf = re.search(rb'name="login_csrf" value="([^"]+)"',
                         resp.body).group(1).decode()
        cookie = resp.headers["Set-Cookie"].split(";")[0]
        resp = self.app.handle(self.request(
            "POST", "/login",
            form={"login_csrf": csrf, "password": password},
            headers={"Cookie": cookie}))
        self.assertEqual(resp.status, 302, resp.body[:200])
        cookie = resp.headers["Set-Cookie"].split(";")[0]
        resp = self.app.handle(self.request(
            "GET", "/", headers={"Cookie": cookie}))
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"',
                         resp.body).group(1).decode()
        return cookie, csrf

    def get(self, path, cookie, query=None):
        return self.app.handle(self.request(
            "GET", path, query=query or {},
            headers={"Cookie": cookie}))

    def post(self, path, cookie, csrf, form):
        form = dict(form)
        form["csrf_token"] = csrf
        return self.app.handle(self.request(
            "POST", path, form=form, headers={"Cookie": cookie}))

    def post_multipart(self, path, cookie, csrf, fields, files):
        fields = list(fields) + [("csrf_token", csrf)]
        body, content_type = _build_multipart(fields, files)
        return self.app.handle(self.request(
            "POST", path, raw_body=body,
            headers={"Cookie": cookie,
                     "Content-Type": content_type}))

    def _audit_rows(self):
        path = self.root / "ui-config" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line]

    def _append_discussion(self, sender, targets, body, seq_no):
        """Ghi 1 tin hoi thảo luận qua store (to_account danh sách v4)."""
        msg, _dup = self.center.append_message(
            sender, "hoi", list(targets), None, body,
            "m-%012d" % seq_no, room="chung")
        return msg

    def _five_turn_round(self):
        """Vòng A<->B đủ 5 lượt: chinh mở, phu đáp, chinh đáp..."""
        turns = [("muse-chinh", ["muse-phu"], "chinh hoi phu 1"),
                 ("muse-phu", ["muse-chinh"], "phu dap chinh 1"),
                 ("muse-chinh", ["muse-phu"], "chinh hoi phu 2"),
                 ("muse-phu", ["muse-chinh"], "phu dap chinh 2"),
                 ("muse-chinh", ["muse-phu"], "chinh hoi phu 3")]
        for index, (sender, targets, body) in enumerate(turns, start=1):
            self._append_discussion(sender, targets, body, index)

class TestChatColors(T3lUiFixture):
    def test_default_colors_fixed_accounts(self):
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertEqual(accounts["muse-chinh"]["chat_color"], "#7c3aed")
        self.assertEqual(accounts["muse-phu"]["chat_color"], "#16a34a")
        colors = ui_store.center_chat_colors(self.root)
        self.assertEqual(colors["owner"], "#57534e")
        self.assertEqual(colors["muse-chinh"], "#7c3aed")

    def test_extra_accounts_get_colors_in_join_order(self):
        self._write_accounts({"muse-chinh": True, "muse-phu": True,
                              "muse-ba": True, "muse-bon": True,
                              "muse-nam": True, "muse-sau": True})
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertEqual(accounts["muse-ba"]["chat_color"], "#c2410c")
        self.assertEqual(accounts["muse-bon"]["chat_color"], "#be185d")
        self.assertEqual(accounts["muse-nam"]["chat_color"], "#475569")
        # Hết 3 màu thêm thì quay vòng lại từ #c2410c.
        self.assertEqual(accounts["muse-sau"]["chat_color"], "#c2410c")

    def test_hand_set_color_kept_invalid_falls_back(self):
        self._write_accounts(
            {"muse-chinh": True, "muse-phu": True},
            extras={"muse-chinh": {"chat_color": "#123456"},
                    "muse-phu": {"chat_color": "tim"}})
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertEqual(accounts["muse-chinh"]["chat_color"], "#123456")
        self.assertEqual(accounts["muse-phu"]["chat_color"], "#16a34a")

    def test_discussion_enabled_defaults_true(self):
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertTrue(accounts["muse-chinh"]["center_discussion_enabled"])
        self.assertTrue(accounts["muse-phu"]["center_discussion_enabled"])
        self._write_accounts(
            {"muse-chinh": True},
            extras={"muse-chinh": {"center_discussion_enabled": False}})
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertFalse(
            accounts["muse-chinh"]["center_discussion_enabled"])
        self._write_accounts(
            {"muse-chinh": True},
            extras={"muse-chinh": {"center_discussion_enabled": "yes"}})
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertTrue(
            accounts["muse-chinh"]["center_discussion_enabled"])

    def test_bubble_carries_sender_color_and_from_account(self):
        # SUA UI 21/11 (P0 CSP): màu người gửi KHÔNG còn đi qua
        # style="background:..." (CSP style-src 'self' chặn) mà qua
        # data-color + JS paintColorDots tô qua CSSOM — hành vi giữ
        # nguyên (bong bóng vẫn mang đúng màu người gửi).
        colors = ui_store.center_chat_colors(self.root)
        names = {"muse-chinh": "Muse chính", "owner": "Chủ nhân"}
        html = ui_pages.center_message_bubble_html(
            {"kind": "hoi", "from_account": "muse-chinh",
             "body": "xin chao", "created_at": "2026-11-20T10:00:00+07:00",
             "seq": 1},
            names, None, colors)
        self.assertIn('data-from-account="muse-chinh"', html)
        self.assertIn('data-color="#7c3aed"', html)
        html_owner = ui_pages.center_message_bubble_html(
            {"kind": "hoi", "from_account": "owner",
             "body": "chu nhan hoi", "created_at":
             "2026-11-20T10:00:00+07:00", "seq": 2},
            names, None, colors)
        self.assertIn('data-from-account="owner"', html_owner)
        self.assertIn('data-color="#57534e"', html_owner)

    def test_typing_dot_stays_green(self):
        # Chấm typing xanh lá của T3i KHÔNG đổi màu theo tài khoản
        # (T3k đã đưa màu vào token --typing, giá trị vẫn #16a34a).
        self.assertIn(".room-typing-dot", APP_CSS)
        self.assertIn("background: var(--typing)", APP_CSS)
        self.assertIn("--typing: #16a34a", APP_CSS)
        self.assertIn(".msg-color-dot", APP_CSS)

class TestDiscussionToggle(T3lUiFixture):
    def test_toggle_off_then_on_with_audit(self):
        cookie, csrf = self.login()
        resp = self.post(
            "/accounts/muse-phu/center/discussion", cookie, csrf,
            {"enabled": "0"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertFalse(
            accounts["muse-phu"]["center_discussion_enabled"])
        rows = [row for row in self._audit_rows()
                if row.get("action") == "center_discussion_toggle"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["before"]["center_discussion_enabled"],
                         True)
        self.assertEqual(rows[0]["after"]["center_discussion_enabled"],
                         False)
        resp = self.post(
            "/accounts/muse-phu/center/discussion", cookie, csrf,
            {"enabled": "1"})
        self.assertEqual(resp.status, 302)
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertTrue(
            accounts["muse-phu"]["center_discussion_enabled"])

    def test_toggle_requires_csrf(self):
        cookie, csrf = self.login()
        resp = self.app.handle(self.request(
            "POST", "/accounts/muse-phu/center/discussion",
            form={"enabled": "0"}, headers={"Cookie": cookie}))
        self.assertEqual(resp.status, 403)
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertTrue(
            accounts["muse-phu"]["center_discussion_enabled"])

    def test_toggle_bad_value_rejected(self):
        cookie, csrf = self.login()
        resp = self.post(
            "/accounts/muse-phu/center/discussion", cookie, csrf,
            {"enabled": "2"})
        self.assertEqual(resp.status, 200)
        self.assertIn("không hợp lệ", resp.body.decode())
        accounts = ui_store.read_accounts(self.root)["accounts"]
        self.assertTrue(
            accounts["muse-phu"]["center_discussion_enabled"])

    def test_toggle_unknown_account_404(self):
        cookie, csrf = self.login()
        resp = self.post(
            "/accounts/muse-khong-co/center/discussion", cookie, csrf,
            {"enabled": "0"})
        self.assertEqual(resp.status, 404)

    def test_accounts_page_shows_toggle_and_color(self):
        cookie, _csrf = self.login()
        html = self.get("/accounts", cookie).body.decode()
        self.assertIn("Tắt thảo luận", html)
        self.assertIn("Thảo luận: Đang bật", html)
        self.assertIn("#7c3aed", html)
        self.assertIn("/accounts/muse-phu/center/discussion", html)

class TestSenderFilter(T3lUiFixture):
    def test_chat_tab_has_sender_filter(self):
        cookie, _csrf = self.login()
        html = self.get("/", cookie, query={"tab": ["chat"]}).body.decode()
        self.assertIn('id="center-sender-filter"', html)
        self.assertIn('<option value="">Tất cả</option>', html)
        self.assertIn('<option value="muse-phu">Muse phụ</option>', html)
        self.assertIn('<option value="owner">Chủ nhân</option>', html)

    def test_filter_js_only_hides_shows_no_fetch(self):
        self.assertIn("applySenderFilter", APP_JS)
        self.assertIn('getElementById("center-sender-filter")', APP_JS)
        self.assertIn('addEventListener("change", applySenderFilter)',
                      APP_JS)
        self.assertIn('getAttribute("data-from-account")', APP_JS)
        self.assertIn('bubble.style.display', APP_JS)
        # Bộ lọc chỉ ẩn/hiện: hàm lọc không gọi fetch; tin mới về
        # (polling/tải tin cũ) chạy lại bộ lọc để không lọt tin sai.
        # 4 điểm gọi: lần đầu + tải tin cũ + nhánh delta pollTick +
        # nhánh D5 (phòng rỗng) mới thêm.
        self.assertEqual(APP_JS.count("applySenderFilter();"), 4)

    def test_filter_css_wraps_at_360px(self):
        self.assertIn(".center-sender-filter", APP_CSS)
        self.assertIn("flex-wrap: wrap", APP_CSS)
        # (Gop UI checkbox, 22/11) Nhom checkbox thay <select multiple>.
        self.assertIn(".check-group", APP_CSS)
        self.assertNotIn("#center-hoi-to[multiple]", APP_CSS)

class TestSendToMany(T3lUiFixture):
    def test_send_to_form_is_checkboxes(self):
        # (Gop UI checkbox, 22/11) Nhom checkbox thay <select multiple>.
        cookie, _csrf = self.login()
        html = self.get("/", cookie, query={"tab": ["chat"]}).body.decode()
        self.assertIn('type="checkbox" name="to_account"', html)
        self.assertNotIn("giữ Ctrl", html)
        self.assertNotIn('id="center-hoi-to"', html)

    def test_send_to_many_accounts_list(self):
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "hỏi cả hai", "room": "chung",
            "to_account": ["muse-chinh", "muse-phu"]})
        self.assertEqual(resp.status, 302, resp.body[:200])
        events = self.center.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["record"]["to_account"],
                         ["muse-chinh", "muse-phu"])

    def test_send_to_one_is_single_item_list(self):
        # Schema v4: kể cả chỉ chọn 1 người, to_account vẫn là danh
        # sách (test T3i cũ ghim chuỗi v3 — xem bàn giao T3l).
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "hỏi riêng phụ", "room": "chung",
            "to_account": "muse-phu"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        events = self.center.read_events()
        self.assertEqual(events[0]["record"]["to_account"], ["muse-phu"])

    def test_send_to_all_empty_is_none(self):
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "hỏi tất cả", "room": "chung", "to_account": ""})
        self.assertEqual(resp.status, 302)
        events = self.center.read_events()
        self.assertIsNone(events[0]["record"]["to_account"])

    def test_send_to_dedupes_repeated_choice(self):
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "hỏi trùng", "room": "chung",
            "to_account": ["muse-phu", "muse-phu"]})
        self.assertEqual(resp.status, 302)
        events = self.center.read_events()
        self.assertEqual(events[0]["record"]["to_account"], ["muse-phu"])

    def test_send_to_owner_400(self):
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "thử", "room": "chung",
            "to_account": ["muse-phu", "owner"]})
        self.assertEqual(resp.status, 400)
        self.assertIn("Không gửi cho chính chủ nhân được",
                      resp.body.decode())
        self.assertEqual(self.center.read_events(), [])

    def test_send_to_unknown_400(self):
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "thử", "room": "chung",
            "to_account": ["muse-phu", "muse-khong-co"]})
        self.assertEqual(resp.status, 400)
        self.assertIn("Tài khoản nhận không tồn tại",
                      resp.body.decode())
        self.assertEqual(self.center.read_events(), [])

    def test_send_to_more_than_five_400(self):
        self._write_accounts({"muse-chinh": True, "muse-phu": True,
                              "muse-ba": True, "muse-bon": True,
                              "muse-nam": True, "muse-sau": True,
                              "muse-bay": True})
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "thử", "room": "chung",
            "to_account": ["muse-chinh", "muse-phu", "muse-ba",
                           "muse-bon", "muse-nam", "muse-sau"]})
        self.assertEqual(resp.status, 400)
        self.assertIn("tối đa 5", resp.body.decode())
        self.assertEqual(self.center.read_events(), [])

    def test_send_to_five_ok(self):
        self._write_accounts({"muse-chinh": True, "muse-phu": True,
                              "muse-ba": True, "muse-bon": True,
                              "muse-nam": True})
        cookie, csrf = self.login()
        resp = self.post("/center/messages", cookie, csrf, {
            "body": "hỏi 5 người", "room": "chung",
            "to_account": ["muse-chinh", "muse-phu", "muse-ba",
                           "muse-bon", "muse-nam"]})
        self.assertEqual(resp.status, 302, resp.body[:200])
        events = self.center.read_events()
        self.assertEqual(len(events[0]["record"]["to_account"]), 5)

class TestDiscussionRounds(T3lUiFixture):
    def test_round_counts_to_five_then_stops(self):
        self._five_turn_round()
        rounds = ui_store.center_discussion_rounds(self.root)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["room"], "chung")
        self.assertEqual(rounds[0]["opener"], "muse-chinh")
        self.assertEqual(rounds[0]["participants"],
                         ["muse-chinh", "muse-phu"])
        self.assertEqual(rounds[0]["count"], 5)
        self.assertEqual(rounds[0]["state"], "dong")
        stopped = ui_store.center_discussion_stopped_rounds(self.root)
        self.assertEqual(len(stopped), 1)

    def test_reply_names_opener_counts_same_round(self):
        self._append_discussion("muse-chinh", ["muse-phu"], "mo vong", 1)
        self._append_discussion("muse-phu", ["muse-chinh"], "dap", 2)
        rounds = ui_store.center_discussion_rounds(self.root)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["opener"], "muse-chinh")
        self.assertEqual(rounds[0]["count"], 2)
        self.assertEqual(rounds[0]["state"], "mo")

    def test_two_pairs_do_not_stop_each_other(self):
        self._five_turn_round()
        self._append_discussion("muse-ngoai", ["muse-ba"], "cap khac 1",
                                101)
        self._append_discussion("muse-ba", ["muse-ngoai"], "cap khac 2",
                                102)
        rounds = ui_store.center_discussion_rounds(self.root)
        self.assertEqual(len(rounds), 2)
        stopped = [item for item in rounds if item["state"] == "dong"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["participants"],
                         ["muse-chinh", "muse-phu"])

    def test_owner_message_resets_rounds(self):
        self._five_turn_round()
        self.assertEqual(
            len(ui_store.center_discussion_stopped_rounds(self.root)), 1)
        self._append_discussion("owner", ["muse-phu"], "chu nhan xen", 6)
        # G-L5: chu nhan xen tin -> vong GIU LAI, count ve 0, state "mo"
        # (khong xoa khoi danh sach); last_seq giu 5 vi tin chu nhan
        # khong tinh la 1 luot.
        self.assertEqual(ui_store.center_discussion_rounds(self.root), [{
            "room": "chung", "opener": "muse-chinh",
            "participants": ["muse-chinh", "muse-phu"], "count": 0,
            "state": "mo", "opened_seq": 1, "last_seq": 5}])

    def test_non_discussion_messages_not_counted(self):
        self.center.append_message(
            "muse-chinh", "hoi", None, None, "hoi chung khong goi ai",
            "m-%012d" % 1, room="chung")
        self.center.append_message(
            "muse-chinh", "bao-cao", ["muse-phu"], None, "bao cao",
            "m-%012d" % 2, room="chung")
        self.assertEqual(ui_store.center_discussion_rounds(self.root), [])

    def test_chat_tab_shows_stopped_round_line(self):
        self._five_turn_round()
        cookie, _csrf = self.login()
        html = self.get("/", cookie, query={"tab": ["chat"]}).body.decode()
        self.assertIn("Vòng thảo luận đã dừng", html)
        self.assertIn("đủ 5 lượt", html)

    def test_alerts_tab_shows_stopped_info_line(self):
        self._five_turn_round()
        cookie, _csrf = self.login()
        html = self.get("/", cookie,
                        query={"tab": ["alerts"]}).body.decode()
        self.assertIn("Trung tâm — vòng thảo luận đã dừng", html)
        self.assertIn("Vòng thảo luận đã dừng", html)
        self.assertIn("đủ 5 lượt, mức thông tin", html)

    def test_no_stopped_line_before_five(self):
        self._append_discussion("muse-chinh", ["muse-phu"], "mo vong", 1)
        cookie, _csrf = self.login()
        html = self.get("/", cookie, query={"tab": ["chat"]}).body.decode()
        self.assertNotIn("Vòng thảo luận đã dừng", html)
        html = self.get("/", cookie,
                        query={"tab": ["alerts"]}).body.decode()
        self.assertNotIn("Trung tâm — vòng thảo luận đã dừng", html)

class TestCenterHealthFlag(T3lUiFixture):
    """Gateway GET /v1/center/<account>/health -> discussion_enabled."""

    def setUp(self):
        super().setUp()
        ctx = gateway_main.GatewayContext(
            self.root,
            gateway_main.parse_cidrs(
                gateway_main.DEFAULT_ALLOW_CIDRS),
            log_fn=lambda *args: None)
        self.server = gateway_main.make_server(ctx, "127.0.0.1", 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _health(self, account_id, secret):
        url = ("http://127.0.0.1:%d/v1/center/%s/health"
               % (self.port, account_id))
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + secret})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_flag_default_true(self):
        status, body = self._health("muse-phu", FETCH_PHU)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["discussion_enabled"])

    def test_health_flag_false_after_toggle(self):
        self._write_accounts(
            {"muse-chinh": True, "muse-phu": True},
            extras={"muse-chinh": {"center_discussion_enabled": False}})
        status, body = self._health("muse-chinh", FETCH_CHINH)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["discussion_enabled"])
        status, body = self._health("muse-phu", FETCH_PHU)
        self.assertTrue(body["discussion_enabled"])

class TestCommonRoomFile(T3lUiFixture):
    """File khung chung: mọi tài khoản đều tải được (quyền xem file =
    quyền xem tin)."""

    def setUp(self):
        super().setUp()
        ctx = gateway_main.GatewayContext(
            self.root,
            gateway_main.parse_cidrs(
                gateway_main.DEFAULT_ALLOW_CIDRS),
            log_fn=lambda *args: None)
        self.server = gateway_main.make_server(ctx, "127.0.0.1", 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _gateway_file(self, account_id, file_id, secret):
        url = ("http://127.0.0.1:%d/v1/center/%s/files/%s"
               % (self.port, account_id, file_id))
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + secret})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_common_room_file_downloadable_by_every_account(self):
        meta = self.center.save_file(
            "owner", "ket-qua.txt", b"noi dung gia")
        file_id = meta["file_id"]
        self.center.append_message(
            "owner", "hoi", None, None, "gui file chung",
            "m-%012d" % 1, file_id=file_id, room="chung")
        # Chủ nhân tải qua UI.
        cookie, _csrf = self.login()
        resp = self.get("/center/files/%s" % file_id, cookie)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"noi dung gia")
        # Cả 2 tài khoản tham gia tải qua gateway (khung chung).
        for account_id, secret in (("muse-chinh", FETCH_CHINH),
                                   ("muse-phu", FETCH_PHU)):
            status, data = self._gateway_file(account_id, file_id, secret)
            self.assertEqual(status, 200, (account_id, data[:100]))
            self.assertEqual(data, b"noi dung gia")

if __name__ == "__main__":
    unittest.main()