"""Test chặng T3e: tag sinh việc + việc chung (v0.4.0).

Thay the tests/unit/test_center_t3d.py (luat cu v0.3.0 "tag chi
nhac ten" da HUY). Phan con dung cua T3d duoc giu lai: parse
mentions, highlight `.msg-mention`, huy hieu "@", CSS.

Bao phu 9 tieu chi PLAN T3e:
(1) schema viec v2 + tag khung chung sinh viec;
(2) viec chung: moi nguoi bao xong rieng, du tat ca moi chuyen
    trang thai, UI "x/y da bao xong", ghep noi theo tung nguoi
    (CHANG-2);
(3) @owner / phong rieng / tai khoan da ngung / tag chinh minh /
    mention khong ton tai -> chi nhac ten, khong tao viec;
(4) tu choi Q-V040-1;
(5) due_at null -> khong qua han, UI "chua co han";
(6) brief cau ranh gioi moi NGUYEN VAN + muc "VIEC MOI TU TAG";
    dispatcher claim theo ∈;
(7) qua MAX_MENTIONS -> 400, Q-V030-1 khong doi;
(8) kiem 360px bang trinh duyet that — CHUA LAM o day (cho ap
    goi len PC);
(9) cong chan: T3e xong moi sang T4.
"""
import io
import ipaddress
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "muse-side"))
sys.path.insert(
    0, str(Path("/home/hatch/workspace/multibot-runtime") / "ops"))

import center_worker_brief  # noqa: E402
import center_dispatcher  # noqa: E402
import center_overdue  # noqa: E402
from pc.center.center_store import (  # noqa: E402
    CenterStore, MAX_MENTIONS, parse_mentions, ROOM_CHUNG,
)
from pc.center import center_store as store_module  # noqa: E402
from pc.gateway import gateway_main as gateway  # noqa: E402
from pc.sync_receiver import receiver_main as receiver  # noqa: E402
# (Gop UI checkbox, 22/11) pc.ui KHONG co trong cay ban giao (chi co
# tren kho nguon day du) nen import mem: thieu thi bo qua cac test UI
# (TestUiJoint) thay vi lam hong ca file test. Cay day du van chay
# het nhu cu.
try:
    from pc.ui import ui_app as ui_app_module  # noqa: E402
    from pc.ui import ui_pages as ui_pages_module  # noqa: E402
    from pc.ui.ui_static import APP_CSS  # noqa: E402
    UI_AVAILABLE = True
except ImportError:
    ui_app_module = None
    ui_pages_module = None
    APP_CSS = ""
    UI_AVAILABLE = False

PASSWORD = "mat-khau-thu"

# Secret MAU GIA DANG ("TEST-FAKE-..."), khong bao gio la secret that.
FETCH_CHINH = "TEST-FAKE-CENTER-FETCH-CHINH"
PUSH_CHINH = "TEST-FAKE-CENTER-PUSH-CHINH"
FETCH_PHU = "TEST-FAKE-CENTER-FETCH-PHU"
PUSH_PHU = "TEST-FAKE-CENTER-PUSH-PHU"
FETCH_BA = "TEST-FAKE-CENTER-FETCH-BA"
PUSH_BA = "TEST-FAKE-CENTER-PUSH-BA"

# 7 tai khoan: 6 tham gia + 1 da ngung (muse-ngung) de test loc.
ACCOUNT_NAMES = {
    "muse-chinh": "Muse chinh",
    "muse-phu": "Muse phu",
    "muse-ba": "Muse ba",
    "muse-bon": "Muse bon",
    "muse-nam": "Muse nam",
    "muse-sau": "Muse sau",
    "muse-ngung": "Muse ngung",
}
ACCOUNT_FLAGS = {
    "muse-chinh": True, "muse-phu": True, "muse-ba": True,
    "muse-bon": True, "muse-nam": True, "muse-sau": True,
    "muse-ngung": False,
}
ACCOUNT_SECRETS = {
    "muse-chinh": (FETCH_CHINH, PUSH_CHINH),
    "muse-phu": (FETCH_PHU, PUSH_PHU),
    "muse-ba": (FETCH_BA, PUSH_BA),
    "muse-bon": ("TEST-FAKE-CENTER-FETCH-BON",
                 "TEST-FAKE-CENTER-PUSH-BON"),
    "muse-nam": ("TEST-FAKE-CENTER-FETCH-NAM",
                 "TEST-FAKE-CENTER-PUSH-NAM"),
    "muse-sau": ("TEST-FAKE-CENTER-FETCH-SAU",
                 "TEST-FAKE-CENTER-PUSH-SAU"),
    "muse-ngung": ("TEST-FAKE-CENTER-FETCH-NGUNG",
                   "TEST-FAKE-CENTER-PUSH-NGUNG"),
}


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _write_accounts(root, flags=None):
    flags = ACCOUNT_FLAGS if flags is None else flags
    _write_json(root / "ui-config" / "accounts.json", {
        "version": 1, "accounts": {
            account_id: {
                "display_name": ACCOUNT_NAMES[account_id],
                "center_participating": participating}
            for account_id, participating in flags.items()}})


def _build_http_root(tmp):
    root = Path(tmp)
    (root / "secrets").mkdir(parents=True)
    (root / "ui-config").mkdir(parents=True)
    (root / "data" / "bot-a").mkdir(parents=True)
    (root / "registry.json").write_text(json.dumps({
        "version": 1,
        "bots": {"bot-a": {"bot_id": "bot-a", "platform": "zalo"}}}))
    (root / "secrets" / "bot-a.sync_secret").write_text(
        "TEST-FAKE-SYNC-BOT-A\n")
    _write_accounts(root)
    for account, (fetch, push) in ACCOUNT_SECRETS.items():
        (root / "secrets" / ("center-%s.fetch_secret" % account)
         ).write_text(fetch + "\n")
        (root / "secrets" / ("center-%s.push_secret" % account)
         ).write_text(push + "\n")
    return root


def _build_multipart(fields, files=None):
    """Body multipart/form-data (fields: dict str->str; files:
    dict str->(filename, bytes, content_type)). Dung cho UI."""
    boundary = "----t3e-boundary"
    parts = []
    for key, value in fields.items():
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s'
             % (boundary, key, value)).encode("utf-8"))
    for key, (filename, content, ctype) in (files or {}).items():
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"; '
             'filename="%s"\r\nContent-Type: %s\r\n\r\n'
             % (boundary, key, filename, ctype)).encode("utf-8")
            + content)
    parts.append(('--%s--' % boundary).encode("utf-8"))
    body = b"\r\n".join(parts) + b"\r\n"
    headers = {"Content-Type":
               "multipart/form-data; boundary=%s" % boundary}
    return body, headers


def _http_raw(port, method, path, body=None, headers=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _http_json(port, method, path, payload=None, secret=None, raw=None,
               content_type="application/json"):
    body = raw if raw is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None
        else None)
    headers = {"Content-Type": content_type}
    if secret is not None:
        headers["Authorization"] = "Bearer %s" % secret
    status, data, resp_headers = _http_raw(port, method, path, body,
                                           headers)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = {"raw": data}
    return status, parsed, resp_headers


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


def _event_count(store, kind=None):
    rows = store.read_events()
    if kind is None:
        return len(rows)
    return sum(1 for row in rows if row.get("kind") == kind)


class T3eStoreFixture(unittest.TestCase):
    """Fixture chi-store (khong server): test truc tiep CenterStore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _write_accounts(self.root)
        self.store = CenterStore(self.root)

    def _event_count(self, kind):
        return _event_count(self.store, kind)


class T3eHttpFixture(unittest.TestCase):
    """Fixture HTTP that: gateway + receiver tren localhost."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _build_http_root(self.tmp.name)
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

    def rc_msg(self, account, payload, secret):
        return _http_json(self.rc_port, "POST",
                          "/v1/center/%s/messages" % account,
                          payload=payload, secret=secret)

    def rc_create(self, account, payload, secret):
        return _http_json(self.rc_port, "POST",
                          "/v1/center/%s/tasks" % account,
                          payload=payload, secret=secret)

    def rc_status(self, account, task_id, payload, secret):
        return _http_json(
            self.rc_port, "POST",
            "/v1/center/%s/tasks/%s/status" % (account, task_id),
            payload=payload, secret=secret)

    def gw_feed(self, account, secret, query=""):
        return _http_json(
            self.gw_port, "GET",
            "/v1/center/%s/feed%s" % (account, query), secret=secret)

    def feed_messages(self, feed):
        return [e["record"] for e in feed["events"]
                if e.get("kind") == "message"]

    def _event_count(self, kind):
        return _event_count(self.store, kind)


class T3eUiFixture(unittest.TestCase):
    """Fixture UI (theo mau T3cUiFixture/T3dUiFixture)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "secrets").mkdir()
        (self.root / "data" / "bot-a").mkdir(parents=True)
        (self.root / "ui-config").mkdir()
        (self.root / "registry.json").write_text(json.dumps({
            "version": 1,
            "bots": {"bot-a": {"bot_id": "bot-a", "platform": "zalo",
                               "display_name": "Bot A", "enabled": True,
                               "paused": False,
                               "awaiting_muse_sync": False,
                               "token_pending_verification": False}}}),
            encoding="utf-8")
        _write_accounts(self.root)
        for account_id in ACCOUNT_NAMES:
            for kind in ("fetch", "push"):
                path = (self.root / "secrets" /
                        ("center-%s.%s_secret" % (account_id, kind)))
                path.write_text("TEST-FAKE-SECRET-%s\n" % kind,
                                encoding="utf-8")
        from pc.ui import ui_auth
        hashed = ui_auth.hash_password(PASSWORD, n=2 ** 10, r=8, p=1)
        (self.root / "ui-config" / "password.hash").write_text(
            hashed + "\n", encoding="utf-8")
        self.app = ui_app_module.UiApp(
            self.root,
            unit_runner=lambda a, b: (0, ""),
            unit_status_fn=lambda bot_id: "active",
            tailscale_fn=lambda: {"up": True, "ip": "100.64.0.5"},
            log_fn=lambda bot_id, limit=50: ["log ok"],
            verify_fn=lambda p, u, t: (True, {"id": "1", "name": "Bot"}),
            gateway_status_fn=lambda: "active")
        self.center = CenterStore(self.root)
        self.session_cookie, self.csrf_token = self._login()

    def request(self, method, path, form=None, headers=None,
                query=None, raw_body=None):
        body = raw_body if raw_body is not None else b""
        hdrs = dict(headers or {})
        if form is not None:
            body = urlencode(form).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        return ui_app_module.Request(method, path, query=query or {},
                                     headers=hdrs, body=body,
                                     client_ip="127.0.0.1")

    def _login(self, password=PASSWORD):
        resp = self.app.handle(self.request("GET", "/login"))
        csrf = re.search(rb'name="login_csrf" value="([^"]+)"',
                         resp.body).group(1).decode()
        cookie = resp.headers["Set-Cookie"].split(";")[0]
        resp = self.app.handle(self.request(
            "POST", "/login",
            form={"password": password, "login_csrf": csrf},
            headers={"Cookie": cookie}))
        self.assertEqual(resp.status, 302, resp.body[:200])
        session_cookie = resp.headers["Set-Cookie"].split(";")[0]
        resp = self.app.handle(self.request(
            "GET", "/", headers={"Cookie": session_cookie}))
        csrf_token = re.search(
            rb'name="csrf_token" value="([^"]+)"',
            resp.body).group(1).decode()
        return session_cookie, csrf_token

    def get(self, path, query=None):
        return self.app.handle(self.request(
            "GET", path, headers={"Cookie": self.session_cookie},
            query=query or {}))

    def post(self, path, form=None):
        payload = dict(form or {})
        payload["csrf_token"] = self.csrf_token
        return self.app.handle(self.request(
            "POST", path, form=payload,
            headers={"Cookie": self.session_cookie}))

    def post_form_list(self, path, fields):
        """POST urlencoded voi field lap lai (vd assigned_to x2):
        fields la list tuple (key, value)."""
        pairs = list(fields) + [("csrf_token", self.csrf_token)]
        body = urlencode(pairs).encode("utf-8")
        req = ui_app_module.Request(
            method="POST", path=path, query={},
            headers={"Cookie": self.session_cookie,
                     "Content-Type":
                     "application/x-www-form-urlencoded"},
            body=body, client_ip="127.0.0.1")
        return self.app.handle(req)

    def post_multipart(self, path, fields, files=None):
        all_fields = dict(fields or {})
        all_fields["csrf_token"] = self.csrf_token
        body, headers = _build_multipart(all_fields, files)
        headers["Cookie"] = self.session_cookie
        req = ui_app_module.Request(
            method="POST", path=path, query={}, headers=headers,
            body=body, client_ip="127.0.0.1")
        return self.app.handle(req)

    def create_task(self, assigned_to="muse-phu", due_hours=24,
                    title="Việc thử"):
        """Tao 1 viec qua store (caller owner), tra task_id."""
        self._ck = getattr(self, "_ck", 0) + 1
        record, _ = self.center.create_task(
            "muse-chinh", title, "Nội dung việc thử.", "khac",
            assigned_to, "2026-11-25T10:00:00+07:00",
            "ck-ui-%d" % self._ck)
        return record["task_id"]

    def _event_count(self, kind):
        return _event_count(self.center, kind)


def _build_brief_root():
    """Tam thu muc root cho center_worker_brief: 2 tai khoan,
    1 viec chung tao tu tag + tin tag (co viec / khong viec)."""
    root = Path(tempfile.mkdtemp())
    (root / "center-config").mkdir()
    (root / "center-config" / "accounts.json").write_text(
        json.dumps({
            "version": 1,
            "accounts": {
                "muse-chinh": {
                    "label": "muse-chinh",
                    "center_participating": True,
                },
                "muse-phu": {
                    "label": "muse-phu",
                    "center_participating": True,
                },
            },
        }),
        encoding="utf-8")
    (root / "center_tasks").mkdir()
    task_record = {
        "task_id": "t-abcdef000001", "title": "Lam bao cao",
        "body": "@muse-phu @muse-chinh\nLam bao cao tuan nay.",
        "kind": "khac", "status": "moi",
        "assigned_from": "muse-chinh",
        "assigned_to": ["muse-phu", "muse-chinh"],
        "due_at": None, "created_at": "2026-11-20T00:00:00+07:00",
        "seen_at": None, "history": [], "reported_by": [],
        "refused_by": [], "source_msg_id": "m-000000000001",
        "schema_version": 2,
    }
    (root / "center_tasks" / "t-abcdef000001.json").write_text(
        json.dumps({"seq": 2, "at": "2026-11-20T00:00:00+07:00",
                    "kind": "task_created", "task_id": "t-abcdef000001",
                    "record": task_record}),
        encoding="utf-8")
    rows = [
        {"seq": 1, "at": "2026-11-20T00:00:00+07:00",
         "kind": "message", "msg_id": "m-000000000001",
         "record": {"kind": "hoi", "from_account": "muse-chinh",
                    "body": "@muse-phu @muse-chinh\nLam bao cao tuan nay.",
                    "room": "chung", "task_id": None,
                    "mentions": ["muse-phu", "muse-chinh"]}},
        {"seq": 2, "at": "2026-11-20T00:01:00+07:00",
         "kind": "message", "msg_id": "m-000000000002",
         "record": {"kind": "hoi", "from_account": "muse-chinh",
                    "body": "@muse-phu xem giup phong rieng.",
                    "room": "dm:muse-phu", "task_id": None,
                    "mentions": ["muse-phu"]}},
        {"seq": 3, "at": "2026-11-20T00:02:00+07:00",
         "kind": "message", "msg_id": "m-000000000003",
         "record": {"kind": "bao-cao", "from_account": "muse-phu",
                    "body": "@muse-chinh bao cao viec t-abcdef000001 xong.",
                    "room": "chung", "task_id": "t-abcdef000001",
                    "mentions": ["muse-chinh"]}},
    ]
    (root / "center_messages.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False)
                  for row in rows) + "\n",
        encoding="utf-8")
    return root


class TestParseMentions(unittest.TestCase):
    """parse_mentions: khong doi o T3e (giu tu T3d)."""

    KNOWN = {"muse-phu", "muse-ba", "muse-chinh", "owner"}

    def test_parse_plain_accounts(self):
        self.assertEqual(
            parse_mentions("@muse-phu xem giúp.", self.KNOWN),
            ["muse-phu"])

    def test_parse_owner(self):
        self.assertEqual(parse_mentions("@owner nghe giúp.", self.KNOWN),
                         ["owner"])

    def test_parse_multiple(self):
        self.assertEqual(
            parse_mentions("@muse-phu @muse-ba họp.", self.KNOWN),
            ["muse-phu", "muse-ba"])

    def test_parse_dedupes(self):
        self.assertEqual(
            parse_mentions("@muse-phu @muse-phu.", self.KNOWN),
            ["muse-phu"])

    def test_parse_ignores_non_mention(self):
        self.assertEqual(parse_mentions("email@muse-phu.com",
                                        self.KNOWN),
                         None)

    def test_parse_ignores_email_like(self):
        self.assertEqual(
            parse_mentions("gửi mail cho muse-phu@vd.com", self.KNOWN),
            None)


class TestStoreTagCreatesTask(T3eStoreFixture):
    """Tieu chi 1+3: tag khung chung sinh viec (store, cung 1 lan
    giu khoa)."""

    def test_tag_chung_creates_task_and_giao_viec(self):
        before = self.store.read_events()
        seq_msg = before[-1]["seq"] + 1 if before else 1
        msg, dup = self.store.append_message(
            "muse-chinh", "hoi", None,
            None, "@muse-phu xem giúp việc này.",
            msg_id="m-000000000004", client_key="ck-a")
        self.assertFalse(dup)
        self.assertEqual(msg["seq"], seq_msg)
        task = self.store.get_task_by_source("m-000000000004")
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "@muse-phu xem giúp việc này.")
        self.assertEqual(task["body"],
                         "@muse-phu xem giúp việc này.")
        self.assertEqual(task["kind"], "khac")
        self.assertIsNone(task["due_at"])
        self.assertEqual(task["assigned_to"], ["muse-phu"])
        self.assertEqual(task["assigned_from"], "muse-chinh")
        self.assertEqual(task["status"], "moi")
        self.assertEqual(task["source_msg_id"], "m-000000000004")
        self.assertEqual(task["reported_by"], [])
        self.assertEqual(task["refused_by"], [])
        rows = self.store.read_events()
        task_row = [row for row in rows
                    if row["record"].get("task_id") == task["task_id"]
                    and row["kind"] == "task_created"][0]
        self.assertEqual(task_row["seq"], seq_msg + 1)
        gv_rows = [row for row in rows
                   if row["record"].get("kind") == "giao-viec"
                   and row["record"].get("task_id") == task["task_id"]]
        self.assertEqual(len(gv_rows), 1)
        self.assertEqual(gv_rows[0]["seq"], seq_msg + 2)
        self.assertIsNone(gv_rows[0]["record"].get("to_account"))

    def test_tag_multiple_creates_one_joint_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None,
            None, "@muse-phu @muse-ba cùng làm.",
            msg_id="m-000000000005", client_key="ck-b")
        task = self.store.get_task_by_source("m-000000000005")
        self.assertEqual(task["assigned_to"], ["muse-phu", "muse-ba"])
        rows = self.store.read_events()
        gv_rows = [row for row in rows
                   if row["record"].get("kind") == "giao-viec"
                   and row["record"].get("task_id") == task["task_id"]]
        self.assertEqual(len(gv_rows), 1)
        self.assertIsNone(gv_rows[0]["record"].get("to_account"))

    def test_tag_title_first_nonempty_line_and_truncated(self):
        long_first = "X" * 250
        self.store.append_message(
            "muse-chinh", "hoi", None,
            None, "@muse-phu " + long_first + "\ndòng hai.",
            msg_id="m-000000000006", client_key="ck-c")
        task = self.store.get_task_by_source("m-000000000006")
        self.assertEqual(len(task["title"]), 200)
        self.assertTrue(task["title"].endswith("…"))
        self.assertTrue(task["title"].startswith("@muse-phu "))
        self.assertIn("dòng hai.", task["body"])

    def test_tag_owner_only_creates_no_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@owner nghe giúp.",
            msg_id="m-000000000007", client_key="ck-d")
        self.assertIsNone(self.store.get_task_by_source("m-000000000007"))

    def test_tag_in_dm_creates_no_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu xem giúp.",
            msg_id="m-000000000008", room="dm:muse-phu", client_key="ck-e")
        self.assertIsNone(self.store.get_task_by_source("m-000000000008"))

    def test_tag_suspended_account_creates_no_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-ngung xem giúp.",
            msg_id="m-000000000009", client_key="ck-f")
        self.assertIsNone(self.store.get_task_by_source("m-000000000009"))
        # Tin van ghi, mention van trong ban ghi (chi nhac ten).
        rows = self.store.read_events()
        msg_row = [row for row in rows
                   if row.get("msg_id") == "m-000000000009"][0]
        self.assertEqual(msg_row["record"]["mentions"], ["muse-ngung"])

    def test_tag_self_creates_no_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-chinh tự nhắc.",
            msg_id="m-00000000000a", client_key="ck-g")
        self.assertIsNone(self.store.get_task_by_source("m-00000000000a"))

    def test_tag_unknown_mention_dropped_keeps_message(self):
        self.store.append_message(
            "muse-chinh", "hoi", None,
            None, "@muse-khong-co xem giúp @muse-phu.",
            msg_id="m-00000000000b", client_key="ck-h")
        task = self.store.get_task_by_source("m-00000000000b")
        self.assertEqual(task["assigned_to"], ["muse-phu"])
        rows = self.store.read_events()
        msg_row = [row for row in rows
                   if row.get("msg_id") == "m-00000000000b"][0]
        self.assertEqual(msg_row["record"]["mentions"], ["muse-phu"])
        self.assertIn("@muse-khong-co",
                      msg_row["record"]["body"])

    def test_tag_bao_cao_creates_no_task(self):
        self.store.append_message(
            "muse-phu", "bao-cao", None, None, "@muse-chinh báo cáo.",
            msg_id="m-00000000000c", client_key="ck-i")
        self.assertIsNone(self.store.get_task_by_source("m-00000000000c"))

    def test_tag_file_only_creates_no_task(self):
        file_id = self.store.save_file(
            "muse-chinh", "a.txt", b"noi dung file")["file_id"]
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "",
            msg_id="m-00000000000d", file_id=file_id, client_key="ck-j")
        # Chi co tag, khong co chu that -> khong tao viec.
        self.assertIsNone(self.store.get_task_by_source("m-00000000000d"))

    def test_resend_same_msg_id_returns_duplicate_no_second_task(self):
        msg1, _ = self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu làm giúp.",
            msg_id="m-00000000000e", client_key="ck-k")
        msg2, dup = self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu làm giúp.",
            msg_id="m-00000000000e", client_key="ck-k2")
        self.assertTrue(dup)
        self.assertEqual(msg2["msg_id"], msg1["msg_id"])
        tasks = [row for row in self.store.read_events()
                 if row["kind"] == "task_created"
                 and row["record"].get("source_msg_id") == "m-00000000000e"]
        self.assertEqual(len(tasks), 1)

    def test_create_task_from_tag_dedupe(self):
        task1, dup1 = self.store.create_task_from_tag(
            "muse-chinh", "@muse-phu làm giúp.", "m-00000000000f",
            ["muse-phu"])
        self.assertFalse(dup1)
        task2, dup2 = self.store.create_task_from_tag(
            "muse-chinh", "@muse-phu làm giúp.", "m-00000000000f",
            ["muse-phu"])
        self.assertTrue(dup2)
        self.assertEqual(task1["task_id"], task2["task_id"])

    def test_create_task_list_and_too_many(self):
        record, _ = self.store.create_task(
            "muse-chinh", "Việc chung", "b.", "khac",
            ["muse-phu", "muse-ba"], "2026-11-25T10:00:00+07:00",
            "ck-list-1")
        self.assertEqual(record["assigned_to"], ["muse-phu", "muse-ba"])
        with self.assertRaises(store_module.CenterError) as ctx:
            self.store.create_task(
                "muse-chinh", "Quá nhiều", "b.", "khac",
                ["muse-phu", "muse-ba", "muse-bon", "muse-nam",
                 "muse-sau", "muse-chinh"],
                "2026-11-25T10:00:00+07:00", "ck-list-2")
        self.assertEqual(ctx.exception.error, "too_many_assignees")

    def test_create_task_manual_requires_due(self):
        with self.assertRaises(store_module.CenterError) as ctx:
            self.store.create_task(
                "muse-chinh", "Tay", "b.", "khac", "muse-phu", None,
                "ck-nodue")
        self.assertEqual(ctx.exception.error, "missing_due_at")

    def test_form_client_key_dedupe_and_conflict(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu lần một.",
            msg_id="m-000000000010", client_key="ck-form-1")
        msg2, dup = self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu lần một.",
            msg_id="m-000000000011", client_key="ck-form-1")
        self.assertTrue(dup)
        self.assertEqual(msg2["msg_id"], "m-000000000010")
        with self.assertRaises(store_module.CenterError) as ctx:
            self.store.append_message(
                "muse-chinh", "hoi", None, None, "@muse-phu nội dung khác.",
                msg_id="m-000000000012", client_key="ck-form-1")
        self.assertEqual(ctx.exception.error, "client_key_conflict")
        self.assertEqual(ctx.exception.http_status, 409)


class TestStoreTagRepair(T3eStoreFixture):
    """Tieu chi 3, duong bu CHANG-1: chet giua chung khi tao viec."""

    def _simulate_death_between_message_and_task(self, msg_id):
        """Gia lap chet giua (1) tin da ghi va (2) viec chua tao:
        xoa task_created + giao-viec cua tin khoi log, xoa viec
        khoi snapshot."""
        events_path = self.root / "data" / "center" / "events.jsonl"
        rows = [json.loads(line) for line in
                events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        kept = []
        task_id = None
        for row in rows:
            record = row.get("record") or {}
            if row.get("kind") == "task_created" and \
                    record.get("source_msg_id") == msg_id:
                task_id = record.get("task_id")
                continue
            if row.get("kind") == "message" and \
                    record.get("kind") == "giao-viec" and \
                    record.get("task_id") == task_id:
                continue
            kept.append(row)
        events_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in kept),
            encoding="utf-8")
        state_path = self.root / "data" / "center" / "tasks_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["tasks"] = {tid: rec for tid, rec in
                          state["tasks"].items() if tid != task_id}
        state_path.write_text(json.dumps(state, ensure_ascii=False),
                              encoding="utf-8")
        return task_id

    def test_retry_repairs_missing_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu làm giúp.",
            msg_id="m-000000000013", client_key="ck-rep-1")
        old_task_id = self._simulate_death_between_message_and_task(
            "m-000000000013")
        self.assertIsNone(self.store.get_task_by_source("m-000000000013"))
        msg2, dup = self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu làm giúp.",
            msg_id="m-000000000013", client_key="ck-rep-2")
        self.assertTrue(dup)
        task = self.store.get_task_by_source("m-000000000013")
        self.assertIsNotNone(task)
        self.assertNotEqual(task["task_id"], old_task_id)
        self.assertEqual(task["assigned_to"], ["muse-phu"])
        rows = self.store.read_events()
        gv_rows = [row for row in rows
                   if row["record"].get("kind") == "giao-viec"
                   and row["record"].get("task_id") == task["task_id"]]
        self.assertEqual(len(gv_rows), 1)
        self.assertTrue(gv_rows[0].get("orphan_repair"))

    def test_rebuild_repairs_tag_message_without_task(self):
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu @muse-ba làm.",
            msg_id="m-000000000014", client_key="ck-rep-2")
        self._simulate_death_between_message_and_task("m-000000000014")
        stats = self.store.rebuild_tasks_state()
        self.assertEqual(stats.get("tag_repaired"), 1)
        task = self.store.get_task_by_source("m-000000000014")
        self.assertIsNotNone(task)
        self.assertEqual(task["assigned_to"], ["muse-phu", "muse-ba"])


class TestStoreJointLifecycle(T3eStoreFixture):
    """Tieu chi 2+4+5: viec chung, bao xong theo tung nguoi
    (CHANG-2), tu choi / ngung / dat han / tra lai (Q-V040-1..4).

    Luu y may trang thai CHUNG chi co 1: nguoi dau tien dua viec
    moi -> da-nhan -> dang-lam; nguoi con lai bao xong thang tu
    dang-lam (khong co duong dang-lam -> da-nhan)."""

    def _make_joint_task(self, assignees=("muse-phu", "muse-ba")):
        task, _ = self.store.create_task(
            "muse-chinh", "Việc chung", "b.", "khac",
            list(assignees), "2026-11-25T10:00:00+07:00",
            "ck-joint-%s" % "-".join(assignees))
        return task

    def _move(self, task_id, to_status, caller, role="assigned_to",
              note=None, key=None):
        key = key or "ck-%s-%s" % (to_status, caller)
        return self.store.change_status(task_id, to_status, caller,
                                        role, note, key)

    def _start_work(self, task_id, first="muse-phu"):
        """Nguoi dau tien dua viec vao dang-lam."""
        self._move(task_id, "da-nhan", first)
        self._move(task_id, "dang-lam", first)

    def _task_events(self, task_id):
        return [row for row in self.store.read_events()
                if row["kind"] == "task_status"
                and row["record"].get("task_id") == task_id]

    def test_joint_report_needs_all(self):
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        record = self.store.get_task(task_id)
        self.assertEqual(record["status"], "dang-lam")
        self.assertEqual(record["reported_by"], ["muse-phu"])
        self._move(task_id, "da-bao-xong", "muse-ba", key="ck-r2")
        record = self.store.get_task(task_id)
        self.assertEqual(record["status"], "da-bao-xong")
        self.assertEqual(record["reported_by"],
                         ["muse-phu", "muse-ba"])

    def test_last_reporter_appends_exactly_one_event(self):
        """NS-L2-03: nguoi bao cuoi cung chi append 1 su kien."""
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        before = len(self._task_events(task_id))
        self._move(task_id, "da-bao-xong", "muse-ba", key="ck-r2")
        after = len(self._task_events(task_id))
        self.assertEqual(after - before, 1)
        last = self._task_events(task_id)[-1]
        self.assertEqual(last.get("report_by"), "muse-ba")

    def test_single_person_report_is_single_event(self):
        task = self._make_joint_task(assignees=("muse-phu",))
        task_id = task["task_id"]
        self._start_work(task_id)
        before = len(self._task_events(task_id))
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        self.assertEqual(len(self._task_events(task_id)) - before, 1)
        record = self.store.get_task(task_id)
        self.assertEqual(record["status"], "da-bao-xong")
        self.assertEqual(record["reported_by"], ["muse-phu"])

    def test_report_twice_with_new_key_is_duplicate(self):
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        before = len(self._task_events(task_id))
        record, dup = self._move(task_id, "da-bao-xong", "muse-phu",
                                 key="ck-r9")
        self.assertTrue(dup)
        self.assertEqual(len(self._task_events(task_id)), before)

    def test_bao_cao_match_per_person(self):
        """2B-NS-03: tin bao-cao cua A chi ghep voi su kien bao cao
        cua chinh A."""
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        # A chua bao xong: bao-cao cua A chi la tien do.
        msg_a1, _ = self.store.append_message(
            "muse-phu", "bao-cao", None, task_id, "Báo cáo phần A.",
            msg_id="m-000000000015")
        self.assertEqual(self.store.classify_bao_cao(
            task_id, msg_a1["seq"], "muse-phu"), "progress")
        # A bao xong -> bao-cao moi cua A duoc tinh.
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        msg_a2, _ = self.store.append_message(
            "muse-phu", "bao-cao", None, task_id, "Báo cáo phần A lần 2.",
            msg_id="m-000000000016")
        self.assertEqual(self.store.classify_bao_cao(
            task_id, msg_a2["seq"], "muse-phu"), "completion")
        # B chua bao: bao-cao cua B van chi la tien do (du A da
        # bao xong).
        msg_b1, _ = self.store.append_message(
            "muse-ba", "bao-cao", None, task_id, "Báo cáo phần B.",
            msg_id="m-000000000017")
        self.assertEqual(self.store.classify_bao_cao(
            task_id, msg_b1["seq"], "muse-ba"), "progress")
        # Sau khi B bao xong, bao-cao moi cua B duoc tinh.
        self._move(task_id, "da-bao-xong", "muse-ba", key="ck-r2")
        msg_b2, _ = self.store.append_message(
            "muse-ba", "bao-cao", None, task_id, "Báo cáo phần B lần 2.",
            msg_id="m-000000000018")
        self.assertEqual(self.store.classify_bao_cao(
            task_id, msg_b2["seq"], "muse-ba"), "completion")
        # Bao-cao cu cua A (truoc su kien bao cao) khong duoc tinh
        # lai.
        self.assertEqual(self.store.classify_bao_cao(
            task_id, msg_a1["seq"], "muse-phu"), "progress")

    def test_task_report_match_flags(self):
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        self.store.append_message(
            "muse-phu", "bao-cao", None, task_id, "Báo cáo phần A.",
            msg_id="m-000000000019")
        match = self.store.task_report_match(task_id)
        self.assertTrue(match["muse-phu"]["reported"])
        self.assertTrue(match["muse-phu"]["matched"])
        self.assertFalse(match["muse-ba"]["reported"])
        self.assertFalse(match["muse-ba"]["matched"])

    def test_return_resets_reported_by(self):
        """Q-V040-4: tra lai -> reported_by rong, tat ca bao lai;
        su kien cu khong duoc dung."""
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        self._move(task_id, "da-bao-xong", "muse-ba", key="ck-r2")
        self.assertEqual(self.store.get_task(task_id)["status"],
                         "da-bao-xong")
        self._move(task_id, "dang-lam", "muse-chinh",
                   role="assigned_from",
                   note="Trả cho muse-phu: phần A chưa đạt.",
                   key="ck-rt")
        record = self.store.get_task(task_id)
        self.assertEqual(record["status"], "dang-lam")
        self.assertEqual(record["reported_by"], [])
        # Bao-cao sau luc tra lai nhung truoc khi bao lai thi chua
        # duoc tinh.
        msg_a, _ = self.store.append_message(
            "muse-phu", "bao-cao", None, task_id, "Báo cáo lại phần A.",
            msg_id="m-00000000001a")
        self.assertEqual(self.store.classify_bao_cao(
            task_id, msg_a["seq"], "muse-phu"), "progress")

    def test_refuse_one_keeps_task_for_rest(self):
        """Q-V040-1: 1 nguoi tu choi -> go khoi danh sach, viec tiep
        tuc; tin hoi thong bao khong ghi noi dung note."""
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._move(task_id, "huy", "muse-phu",
                   note="Bận việc khác.", key="ck-x1")
        record = self.store.get_task(task_id)
        self.assertEqual(record["assigned_to"], ["muse-ba"])
        self.assertEqual(record["refused_by"], ["muse-phu"])
        self.assertEqual(record["status"], "moi")
        self.assertIn("muse-phu", record["history"][0]["note"])
        rows = self.store.read_events()
        hoi_rows = [row for row in rows
                    if row["record"].get("kind") == "hoi"
                    and row["record"].get("task_id") == task_id
                    and "đã từ chối việc" in
                    (row["record"].get("body") or "")]
        self.assertEqual(len(hoi_rows), 1)
        self.assertNotIn("Bận việc khác", hoi_rows[0]["record"]["body"])
        self.assertEqual(hoi_rows[0]["record"].get("room"), "chung")

    def test_refuse_last_cancels_task(self):
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._move(task_id, "huy", "muse-phu",
                   note="Bận.", key="ck-x1")
        self._move(task_id, "huy", "muse-ba",
                   note="Cũng bận.", key="ck-x2")
        record = self.store.get_task(task_id)
        self.assertEqual(record["status"], "huy")
        self.assertEqual(record["assigned_to"], [])
        self.assertEqual(record["refused_by"],
                         ["muse-phu", "muse-ba"])
        # Nguoi da tu choi khong quay lai: huy -> da-nhan khong co
        # duong chuyen.
        with self.assertRaises(store_module.CenterError) as ctx:
            self._move(task_id, "da-nhan", "muse-phu", key="ck-x3")
        self.assertEqual(ctx.exception.error, "bad_transition")

    def test_refuse_requires_note_and_moi_only(self):
        task = self._make_joint_task()
        task_id = task["task_id"]
        with self.assertRaises(store_module.CenterError) as ctx:
            self._move(task_id, "huy", "muse-phu",
                       note="   ", key="ck-x1")
        self.assertEqual(ctx.exception.error, "note_required")
        self._move(task_id, "da-nhan", "muse-phu")
        # Tu choi chi tu moi; tu da-nhan thi nguoi lam viec khong co
        # quyen huy (chi nguoi giao/chu nhan huy duoc).
        with self.assertRaises(store_module.CenterError) as ctx:
            self._move(task_id, "huy", "muse-phu",
                       note="Muộn rồi.", key="ck-x2")
        self.assertEqual(ctx.exception.error, "forbidden")
        # (FIXL C3) Nhanh change_status sai vai tro gan ma rieng.
        self.assertEqual(ctx.exception.code, "transition_forbidden")

    def test_remove_assignee_keeps_history_and_reports(self):
        """Q-V040-2: go khoi danh sach, giu history + bao cao da
        lam (trong history); reported_by cua ho go theo."""
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._start_work(task_id)
        self._move(task_id, "da-bao-xong", "muse-phu", key="ck-r1")
        result = self.store.remove_assignee("muse-phu", by="owner")
        self.assertIn(task_id, result["removed_from"])
        record = self.store.get_task(task_id)
        self.assertEqual(record["assigned_to"], ["muse-ba"])
        self.assertEqual(record["reported_by"], [])
        self.assertEqual(record["status"], "dang-lam")
        self.assertTrue(any("Q-V040-2" in (entry.get("note") or "")
                            for entry in record["history"]))

    def test_remove_last_assignee_orphans_task(self):
        task = self._make_joint_task(assignees=("muse-phu",))
        task_id = task["task_id"]
        result = self.store.remove_assignee("muse-phu", by="owner")
        self.assertIn(task_id, result["orphan"])
        record = self.store.get_task(task_id)
        self.assertEqual(record["assigned_to"], [])
        self.assertEqual(record["status"], "moi")

    def test_set_task_due(self):
        """Q-V040-3: dat han cho viec chua co han; da co han thi
        tu choi."""
        self.store.append_message(
            "muse-chinh", "hoi", None, None, "@muse-phu làm giúp.",
            msg_id="m-00000000001b", client_key="ck-due-1")
        task = self.store.get_task_by_source("m-00000000001b")
        task_id = task["task_id"]
        self.assertIsNone(task["due_at"])
        due = "2026-11-27T10:00:00+07:00"
        record = self.store.set_task_due(task_id, due, by="muse-chinh")
        self.assertEqual(record["due_at"], due)
        self.assertTrue(any("Đặt hạn" in (entry.get("note") or "")
                            for entry in record["history"]))
        with self.assertRaises(store_module.CenterError) as ctx:
            self.store.set_task_due(task_id,
                                    "2026-11-28T10:00:00+07:00",
                                    by="muse-chinh")
        self.assertEqual(ctx.exception.error, "due_already_set")
        # Dinh dang han sai -> bad_due_at (kiem tra dinh dang
        # truoc khi kiem tra da co han).
        with self.assertRaises(store_module.CenterError) as ctx:
            self.store.set_task_due(task_id, "khong-phai-ngay",
                                    by="muse-chinh")
        self.assertEqual(ctx.exception.error, "bad_due_at")

    def test_set_task_due_bad_format(self):
        task = self._make_joint_task()
        with self.assertRaises(store_module.CenterError) as ctx:
            self.store.set_task_due(task["task_id"], "2026/11/20",
                                    by="muse-chinh")
        self.assertEqual(ctx.exception.error, "bad_due_at")

    def test_reassign_replaces_whole_list(self):
        task = self._make_joint_task()
        task_id = task["task_id"]
        self._move(task_id, "da-nhan", "muse-phu")
        record = self.store.reassign_task(
            task_id, ["muse-bon", "muse-nam"], by="muse-chinh")
        self.assertEqual(record["assigned_to"],
                         ["muse-bon", "muse-nam"])
        self.assertEqual(record["status"], "moi")
        self.assertEqual(record["reported_by"], [])
        self.assertEqual(record["refused_by"], [])
        self.assertIsNone(record["seen_at"])

    def test_due_null_never_overdue(self):
        """Tieu chi 5: viec due_at null khong bao gio qua han (quet
        center_overdue.scan tren ban sao local)."""
        root = Path(tempfile.mkdtemp())
        try:
            (root / "center_tasks").mkdir()
            null_task = {
                "task_id": "t-null", "title": "Việc không hạn",
                "body": "b.", "kind": "khac", "status": "dang-lam",
                "assigned_from": "muse-chinh",
                "assigned_to": ["muse-phu"], "due_at": None,
                "created_at": "2026-11-20T00:00:00+07:00",
                "seen_at": None, "history": [], "reported_by": [],
                "refused_by": [], "source_msg_id": "m-00000000001c",
                "schema_version": 2,
            }
            past_task = dict(null_task)
            past_task.update({
                "task_id": "t-past", "title": "Việc đã quá hạn",
                "due_at": "2026-11-18T00:00:00+07:00",
                "source_msg_id": None})
            for name, record in (("t-null", null_task),
                                 ("t-past", past_task)):
                (root / "center_tasks" / ("%s.json" % name)).write_text(
                    json.dumps({"seq": 1,
                                "at": "2026-11-20T00:00:00+07:00",
                                "kind": "task_created",
                                "task_id": name, "record": record}),
                    encoding="utf-8")
            summary = center_overdue.scan(
                str(root), account_id="muse-phu",
                now=1795132800)  # 2026-11-20 00:00 UTC
            # Viec due null bi bo qua truoc khi dem "checked"
            # (khong bao gio qua han); chi viec co han moi dem.
            self.assertEqual(summary["checked"], 1)
            self.assertEqual(summary["overdue"], 1)
            self.assertEqual(summary["notified"], 1)
            alerts = (root / "center" / "overdue_alerts.jsonl"
                      ).read_text(encoding="utf-8")
            self.assertIn("t-past", alerts)
            self.assertNotIn("t-null", alerts)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_overdue_alert_carries_reported_by_for_joint_task(self):
        """NS-E01: muc hang cho K4 mang reported_by de tang gui K4
        soan duoc "x/y da bao xong" (doc 10 §2.5). Viec chung 2
        nguoi, 1 nguoi da bao xong -> 1/2; viec 1 nguoi chua ai
        bao xong -> 0/1."""
        root = Path(tempfile.mkdtemp())
        try:
            (root / "center_tasks").mkdir()
            joint = {
                "task_id": "t-joint", "title": "Việc chung quá hạn",
                "body": "b.", "kind": "khac", "status": "dang-lam",
                "assigned_from": "muse-chinh",
                "assigned_to": ["muse-phu", "muse-chinh"],
                "reported_by": ["muse-phu"],
                "due_at": "2026-11-18T00:00:00+07:00",
                "created_at": "2026-11-20T00:00:00+07:00",
                "seen_at": None, "history": [],
                "refused_by": [], "source_msg_id": "m-00000000001d",
                "schema_version": 2,
            }
            single = dict(joint)
            single.update({
                "task_id": "t-single", "title": "Việc một người",
                "assigned_to": ["muse-phu"], "reported_by": [],
                "source_msg_id": None})
            for name, record in (("t-joint", joint),
                                 ("t-single", single)):
                (root / "center_tasks" / ("%s.json" % name)).write_text(
                    json.dumps({"seq": 1,
                                "at": "2026-11-20T00:00:00+07:00",
                                "kind": "task_created",
                                "task_id": name, "record": record}),
                    encoding="utf-8")
            summary = center_overdue.scan(
                str(root), account_id="muse-phu",
                now=1795132800)  # 2026-11-20 00:00 UTC
            self.assertEqual(summary["notified"], 2)
            entries = [
                json.loads(line) for line in
                (root / "center" / "overdue_alerts.jsonl"
                 ).read_text(encoding="utf-8").splitlines()
                if line.strip()]
            by_id = {e["task_id"]: e for e in entries}
            # Viec chung: 1/2 da bao xong.
            self.assertEqual(by_id["t-joint"]["reported_by"],
                             ["muse-phu"])
            self.assertEqual(len(by_id["t-joint"]["assigned_to"]), 2)
            # Viec 1 nguoi: 0/1 da bao xong.
            self.assertEqual(by_id["t-single"]["reported_by"], [])
            self.assertEqual(by_id["t-single"]["assigned_to"],
                             ["muse-phu"])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Receiver: tao viec chung + doi trang thai theo ∈ + tag sinh viec
# ---------------------------------------------------------------------------

class TestReceiverJoint(T3eHttpFixture):
    """Tieu chi 1/6/7 o tang receiver (server that)."""

    def _create_joint(self):
        status, body, _ = self.rc_create(
            "muse-chinh",
            {"title": "Việc chung", "body": "b.", "kind": "khac",
             "assigned_to": ["muse-phu", "muse-ba"],
             "due_at": "2026-11-25T10:00:00+07:00",
             "client_key": "rc-joint-1"},
            PUSH_CHINH)
        self.assertEqual(status, 200, body)
        return body["task_id"]

    def test_create_task_list_via_receiver(self):
        task_id = self._create_joint()
        task = self.store.get_task(task_id)
        self.assertEqual(task["assigned_to"], ["muse-phu", "muse-ba"])
        self.assertEqual(task["status"], "moi")

    def test_create_task_too_many_assignees(self):
        status, body, _ = self.rc_create(
            "muse-chinh",
            {"title": "Quá nhiều", "body": "b.", "kind": "khac",
             "assigned_to": ["muse-phu", "muse-ba", "muse-bon",
                             "muse-nam", "muse-sau", "muse-chinh"],
             "due_at": "2026-11-25T10:00:00+07:00",
             "client_key": "rc-many"},
            PUSH_CHINH)
        self.assertEqual(status, 400, body)
        self.assertEqual(body.get("error"), "bad_assigned_to")

    def test_create_task_unknown_assignee(self):
        status, body, _ = self.rc_create(
            "muse-chinh",
            {"title": "Lạ", "body": "b.", "kind": "khac",
             "assigned_to": ["muse-khong-co"],
             "due_at": "2026-11-25T10:00:00+07:00",
             "client_key": "rc-unk"},
            PUSH_CHINH)
        self.assertEqual(status, 400, body)
        self.assertEqual(body.get("error"), "bad_assigned_to")

    def test_create_task_suspended_assignee(self):
        status, body, _ = self.rc_create(
            "muse-chinh",
            {"title": "Ngưng", "body": "b.", "kind": "khac",
             "assigned_to": ["muse-ngung"],
             "due_at": "2026-11-25T10:00:00+07:00",
             "client_key": "rc-sus"},
            PUSH_CHINH)
        self.assertEqual(status, 400, body)
        self.assertEqual(body.get("error"), "not_participating")

    def test_status_by_non_assignee_forbidden(self):
        task_id = self._create_joint()
        status, body, _ = self.rc_status(
            "muse-bon", task_id,
            {"to_status": "da-nhan", "client_key": "rc-st-1"},
            "TEST-FAKE-CENTER-PUSH-BON")
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("error"), "forbidden")

    def test_status_transition_forbidden_code_via_receiver(self):
        # (FIXL C3) Assignee tu day huy tu da-nhan: qua duoc muc
        # receiver (co vai tro) roi moi toi 403 o change_status —
        # body giu error "forbidden" + them code rieng.
        task_id = self._create_joint()
        status, body, _ = self.rc_status(
            "muse-phu", task_id,
            {"to_status": "da-nhan", "client_key": "rc-tf-1"},
            PUSH_PHU)
        self.assertEqual(status, 200, body)
        status, body, _ = self.rc_status(
            "muse-phu", task_id,
            {"to_status": "huy", "note": "Muộn rồi.",
             "client_key": "rc-tf-2"},
            PUSH_PHU)
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("error"), "forbidden")
        self.assertEqual(body.get("code"), "transition_forbidden")

    def test_status_by_second_assignee_ok(self):
        task_id = self._create_joint()
        status, body, _ = self.rc_status(
            "muse-ba", task_id,
            {"to_status": "da-nhan", "client_key": "rc-st-2"},
            PUSH_BA)
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("status"), "da-nhan")

    def test_tag_message_creates_task_via_receiver(self):
        status, body, _ = self.rc_msg(
            "muse-chinh",
            {"msg_id": "m-000000000020", "kind": "hoi",
             "to_account": None, "task_id": None,
             "body": "@muse-phu làm giúp việc này."},
            PUSH_CHINH)
        self.assertEqual(status, 200, body)
        task = self.store.get_task_by_source("m-000000000020")
        self.assertIsNotNone(task)
        self.assertEqual(task["assigned_to"], ["muse-phu"])
        self.assertIsNone(task["due_at"])
        _status, feed, _ = self.gw_feed("muse-phu", FETCH_PHU,
                                        "?since=0")
        kinds = [e.get("kind") for e in feed["events"]]
        self.assertIn("task", kinds)

    def test_bao_cao_by_non_assignee_forbidden(self):
        task_id = self._create_joint()
        status, body, _ = self.rc_msg(
            "muse-bon",
            {"msg_id": "m-000000000021", "kind": "bao-cao",
             "to_account": None, "task_id": task_id,
             "body": "Báo cáo ké."},
            "TEST-FAKE-CENTER-PUSH-BON")
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("error"), "forbidden")

    def test_too_many_mentions_400_writes_nothing(self):
        before = _event_count(self.store)
        status, body, _ = self.rc_msg(
            "muse-phu",
            {"msg_id": "m-000000000022", "kind": "hoi",
             "to_account": None, "task_id": None,
             "body": "@muse-chinh @muse-phu @muse-ba @muse-bon "
                     "@muse-nam @muse-sau cùng xem"},
            PUSH_PHU)
        self.assertEqual(status, 400, body)
        self.assertEqual(body.get("error"), "too_many_mentions")
        self.assertEqual(_event_count(self.store), before)


# ---------------------------------------------------------------------------
# UI: viec chung + dat han + dedupe form + bong bong tag
# ---------------------------------------------------------------------------

@unittest.skipIf(not UI_AVAILABLE,
                  "pc.ui khong co trong cay ban giao (chi co tren kho nguon)")
class TestUiJoint(T3eUiFixture):
    """Tieu chi 5/9 o tang UI."""

    def _make_tag_task(self):
        self.center.append_message(
            "muse-chinh", "hoi", None, None,
            "@muse-phu @muse-ba làm giúp.",
            msg_id="m-000000000023", client_key="ck-ui-tag")
        return self.center.get_task_by_source("m-000000000023")

    def test_create_form_has_assignee_checkboxes(self):
        # (Gop UI checkbox, 22/11) Form tao viec dung checkbox thay
        # cho <select multiple> (bat giu Ctrl/Cmd).
        resp = self.get("/", query={"tab": ["center"]})
        text = resp.body.decode("utf-8")
        self.assertIn('name="assigned_to"', text)
        self.assertIn('type="checkbox"', text)
        self.assertNotIn("multiple", text)

    def test_chat_form_has_client_key(self):
        resp = self.get("/", query={"tab": ["chat"]})
        text = resp.body.decode("utf-8")
        self.assertIn('name="client_key"', text)

    def test_create_joint_task_via_ui(self):
        resp = self.post_form_list("/center/tasks", [
            ("title", "Việc chung"), ("body", "b."),
            ("kind", "khac"), ("due_at", "2026-11-25T10:00"),
            ("assigned_to", "muse-phu"), ("assigned_to", "muse-ba")])
        self.assertEqual(resp.status, 302, resp.body[:300])
        task_id = resp.headers["Location"].rsplit("/", 1)[-1]
        task = self.center.get_task(task_id)
        self.assertEqual(task["assigned_to"], ["muse-phu", "muse-ba"])

    def test_create_task_too_many_via_ui(self):
        resp = self.post_form_list("/center/tasks", [
            ("title", "Quá nhiều"), ("body", "b."),
            ("kind", "khac"), ("due_at", "2026-11-25T10:00"),
            ("assigned_to", "muse-phu"), ("assigned_to", "muse-ba"),
            ("assigned_to", "muse-bon"), ("assigned_to", "muse-nam"),
            ("assigned_to", "muse-sau"),
            ("assigned_to", "muse-chinh")])
        self.assertEqual(resp.status, 200, resp.body[:300])
        self.assertIn("1–5", resp.body.decode("utf-8"))

    def test_create_task_no_assignee_means_all_via_ui(self):
        # Spec #227 (sua lech goi gop 22/11): khong tick ai = giao
        # cho TAT CA tai khoan duoc giao (khong gioi han 5).
        resp = self.post_form_list("/center/tasks", [
            ("title", "Việc cho tất cả"), ("body", "b."),
            ("kind", "khac"), ("due_at", "2026-11-25T10:00")])
        self.assertEqual(resp.status, 302, resp.body[:300])
        task_id = resp.headers["Location"].rsplit("/", 1)[-1]
        task = self.center.get_task(task_id)
        self.assertEqual(task["assigned_to"], [
            "muse-ba", "muse-bon", "muse-chinh",
            "muse-nam", "muse-phu", "muse-sau"])

    def test_reassign_no_assignee_means_all_via_ui(self):
        # Spec #227 (sua lech goi gop 22/11): khong tick ai = giao
        # cho TAT CA tai khoan duoc giao (khong gioi han 5).
        task = self._make_tag_task()
        task_id = task["task_id"]
        resp = self.post_form_list(
            "/center/tasks/%s/reassign" % task_id, [])
        self.assertEqual(resp.status, 302, resp.body[:300])
        self.assertEqual(
            self.center.get_task(task_id)["assigned_to"], [
                "muse-ba", "muse-bon", "muse-chinh",
                "muse-nam", "muse-phu", "muse-sau"])

    def test_task_row_shows_report_chip_and_no_due(self):
        task = self._make_tag_task()
        task_id = task["task_id"]
        self.center.change_status(task_id, "da-nhan", "muse-phu",
                                  "assigned_to", None, "ck-ui-r1")
        self.center.change_status(task_id, "dang-lam", "muse-phu",
                                  "assigned_to", None, "ck-ui-r2")
        self.center.change_status(task_id, "da-bao-xong", "muse-phu",
                                  "assigned_to", None, "ck-ui-r3")
        names = {"muse-phu": "Muse phu", "muse-ba": "Muse ba",
                 "muse-chinh": "Muse chinh"}
        html = ui_pages_module.center_task_row_html(
            self.center.get_task(task_id), names, "2026-11-20")
        self.assertIn("1/2 đã báo xong", html)
        self.assertIn("chưa có hạn", html)

    def test_task_row_shows_kind_label_not_code(self):
        # NS-01: loai viec hien nhan tieng Viet, khong hien ma tho.
        task = self._make_tag_task()
        names = {"muse-phu": "Muse phu", "muse-ba": "Muse ba",
                 "muse-chinh": "Muse chinh"}
        html = ui_pages_module.center_task_row_html(
            self.center.get_task(task["task_id"]), names, "2026-11-20")
        self.assertIn(">Khác</span>", html)
        self.assertNotIn(">khac</span>", html)
        row = dict(self.center.get_task(task["task_id"]))
        row["kind"] = "nghien-cuu"
        html2 = ui_pages_module.center_task_row_html(
            row, names, "2026-11-20")
        self.assertIn(">Nghiên cứu</span>", html2)
        self.assertNotIn(">nghien-cuu</span>", html2)
        # Loai la (khong co trong bang nhan) van hien ma tho.
        row["kind"] = "la"
        html3 = ui_pages_module.center_task_row_html(
            row, names, "2026-11-20")
        self.assertIn(">la</span>", html3)

    def test_detail_shows_kind_label_not_code(self):
        # NS-01: trang chi tiet viec cung hien nhan tieng Viet.
        task = self._make_tag_task()
        names = {"muse-phu": "Muse phu", "muse-ba": "Muse ba",
                 "muse-chinh": "Muse chinh"}
        html = ui_pages_module.center_task_detail_html(
            self.center.get_task(task["task_id"]), [], names,
            "csrf-test",
            [("muse-phu", "Muse phu"), ("muse-ba", "Muse ba")],
            "2026-11-20")
        self.assertIn("Loại: Khác", html)
        self.assertNotIn("Loại: khac", html)

    def test_detail_shows_per_person_set_due_and_reassign(self):
        task = self._make_tag_task()
        task_id = task["task_id"]
        resp = self.get("/center/tasks/%s" % task_id)
        self.assertEqual(resp.status, 200, resp.body[:200])
        text = resp.body.decode("utf-8")
        self.assertIn("Muse phu", text)
        self.assertIn("Muse ba", text)
        self.assertIn("chưa có hạn", text)
        self.assertIn("/center/tasks/%s/due" % task_id, text)
        self.assertIn("Đặt hạn", text)
        # (Gop UI checkbox, 22/11) Form giao lai dung checkbox thay
        # cho <select multiple>.
        self.assertIn('type="checkbox"', text)
        self.assertIn('name="assigned_to"', text)
        self.assertNotIn("multiple", text)
        # Dat han qua POST.
        resp = self.post("/center/tasks/%s/due" % task_id,
                         {"due_at": "2026-11-25T10:00"})
        self.assertEqual(resp.status, 302, resp.body[:300])
        self.assertEqual(
            self.center.get_task(task_id)["due_at"],
            "2026-11-25T10:00:00+07:00")

    def test_pause_removes_assignee(self):
        task = self._make_tag_task()
        task_id = task["task_id"]
        resp = self.post("/accounts/muse-phu/center/pause", {})
        self.assertEqual(resp.status, 302, resp.body[:300])
        record = self.center.get_task(task_id)
        self.assertEqual(record["assigned_to"], ["muse-ba"])

    def test_chat_form_resubmit_same_client_key(self):
        resp = self.get("/", query={"tab": ["chat"]})
        text = resp.body.decode("utf-8")
        key = re.search(
            r'name="client_key" value="([^"]+)"', text).group(1)
        fields = {"body": "@muse-phu làm giúp.", "room": "chung",
                  "client_key": key}
        resp1 = self.post_multipart("/center/messages", fields)
        self.assertEqual(resp1.status, 302, resp1.body[:200])
        before = _event_count(self.center, kind="task_created")
        resp2 = self.post_multipart("/center/messages", fields)
        self.assertEqual(resp2.status, 302, resp2.body[:200])
        self.assertEqual(_event_count(self.center, kind="task_created"),
                         before)
        # Cung client_key nhung noi dung khac -> 400.
        fields2 = {"body": "@muse-phu nội dung khác.", "room": "chung",
                   "client_key": key}
        resp3 = self.post_multipart("/center/messages", fields2)
        self.assertEqual(resp3.status, 400, resp3.body[:200])

    def test_tag_message_via_ui_creates_task_and_bubble(self):
        resp = self.post_multipart(
            "/center/messages",
            {"body": "@muse-phu làm giúp.", "room": "chung",
             "client_key": "ck-ui-bubble-1"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        feed, _, _ = self.center.get_feed(0, room="chung")
        # msg_id do store sinh (m- + 12 hex) nen tim theo body +
        # kind "hoi" (tin giao-viec tu sinh co cung body = tieu de).
        msg = [e for e in feed
               if (e.get("record") or {}).get("body")
               == "@muse-phu làm giúp."
               and (e.get("record") or {}).get("kind") == "hoi"]
        self.assertEqual(len(msg), 1)
        record = msg[0]["record"]
        task = self.center.get_task_by_source(record["msg_id"])
        self.assertIsNotNone(task)
        names = {"muse-phu": "Muse phu", "muse-chinh": "Muse chinh"}
        html = ui_pages_module.center_message_bubble_html(
            record, names,
            task_titles={record["msg_id"]: task["title"]})
        self.assertIn(task["title"], html)
        self.assertIn('<span class="msg-mention">@muse-phu</span>',
                      html)

    def test_ui_send_too_many_mentions_friendly_error(self):
        before = _event_count(self.center)
        resp = self.post_multipart(
            "/center/messages",
            {"body": "@muse-chinh @muse-phu @muse-ba @muse-bon "
                     "@muse-nam @muse-sau cùng xem",
             "room": "chung"})
        self.assertEqual(resp.status, 400, resp.body[:200])
        text = resp.body.decode("utf-8")
        self.assertIn("tách thành nhiều tin", text)
        self.assertIn("tối đa 5 tag", text)
        self.assertEqual(_event_count(self.center), before)

    def test_css_mention_weight_500(self):
        # GC-D03: .msg-mention weight 500 theo thiet ke DESIGN §8.3
        # (T3d dung 700).
        block = re.search(r"\.msg-mention \{[^}]*\}", APP_CSS)
        self.assertIsNotNone(block)
        self.assertIn("font-weight: 500", block.group(0))
        self.assertIn("#714cb6", APP_CSS)


# ---------------------------------------------------------------------------
# Brief worker: cau ranh gioi moi + VIEC MOI TU TAG
# ---------------------------------------------------------------------------

NEW_BOUNDARY_SENTENCE = (
    "Tin tag bạn CÓ THỂ là việc mới: tag trong khung chung tạo 1 "
    "việc chung cho mọi tài khoản được tag — việc đó nằm trong mục "
    "việc của bạn, báo xong như việc thường; tag trong phòng riêng "
    "chỉ là nhắc tên, không tạo việc.")


class TestBriefTags(unittest.TestCase):
    def _run_brief(self, root, account_id, task_id):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = center_worker_brief.main([
                "--root", str(root), "--account-id", account_id,
                "--center-task-id", task_id])
        return code, buf.getvalue()

    def test_boundary_sentence_verbatim_and_tag_task_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_brief_root()
            code, out = self._run_brief(root, "muse-phu", "t-abcdef000001")
            self.assertEqual(code, 0)
            self.assertIn("=== TIN NHAN TAG BAN ===", out)
            self.assertIn(NEW_BOUNDARY_SENTENCE, out)
            self.assertIn("VIỆC MỚI TỪ TAG", out)
            self.assertIn("t-abcdef000001: Lam bao cao", out)
            self.assertIn("Lam bao cao tuan nay.", out)

    def test_tagged_section_excludes_task_creating_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_brief_root()
            _code, out = self._run_brief(root, "muse-phu", "t-abcdef000001")
            # Tin tag da sinh viec (m-tag-1) KHONG nam o muc tag
            # nhac ten nua; chi con tin phong rieng + tin bao-cao.
            tagged = out.split(
                "-- Tin tag chỉ nhắc tên (không tạo việc) --")[1]
            self.assertNotIn("@muse-phu @muse-chinh\nLam bao cao",
                             tagged)
            self.assertIn("@muse-phu xem giup phong rieng.", tagged)
            self.assertIn("phong=dm:muse-phu", tagged)

    def test_tagged_section_other_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_brief_root()
            code, out = self._run_brief(root, "muse-chinh", "t-abcdef000001")
            self.assertEqual(code, 0)
            self.assertIn("t-abcdef000001: Lam bao cao", out)
            tagged = out.split(
                "-- Tin tag chỉ nhắc tên (không tạo việc) --")[1]
            self.assertIn("@muse-chinh bao cao viec t-abcdef000001 xong.",
                          tagged)

    def test_brief_is_mine_with_list_assignees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_brief_root()
            code, out = self._run_brief(root, "muse-ba", "t-abcdef000001")
            # muse-ba khong nam trong assigned_to -> khong phai viec
            # cua minh, brief dung o do.
            self.assertEqual(code, 0)
            self.assertIn("VIEC CUA MINH: KHONG", out)


# ---------------------------------------------------------------------------
# Dispatcher: claim theo ∈
# ---------------------------------------------------------------------------

class TestDispatcherClaim(unittest.TestCase):
    """Tieu chi 6: dispatcher claim viec khi tai khoan ∈ assigned_to."""

    def _build_root(self):
        root = Path(tempfile.mkdtemp())
        (root / "center_tasks").mkdir()
        record = {
            "task_id": "t-joint-1", "title": "Việc chung",
            "body": "b.", "kind": "khac", "status": "moi",
            "assigned_from": "muse-chinh",
            "assigned_to": ["muse-phu", "muse-ba"],
            "due_at": "2026-11-25T10:00:00+07:00",
            "created_at": "2026-11-20T00:00:00+07:00",
            "seen_at": None, "history": [], "reported_by": [],
            "refused_by": [], "source_msg_id": None,
            "schema_version": 2,
        }
        (root / "center_tasks" / "t-joint-1.json").write_text(
            json.dumps({"seq": 1, "at": "2026-11-20T00:00:00+07:00",
                        "kind": "task_created", "task_id": "t-joint-1",
                        "record": record}),
            encoding="utf-8")
        return root

    def test_claims_second_assignee(self):
        import shutil
        root = self._build_root()
        try:
            woken = []
            summary = center_dispatcher.run_cycle_for_account(
                str(root), "muse-ba",
                deps={"worker_fn": lambda acc, cand, ctx: woken.append(cand)})
            self.assertEqual(summary["woken"], 1)
            self.assertEqual(summary["other_tasks_read_only"], 0)
            self.assertEqual(len(woken), 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_no_claim_for_outsider(self):
        import shutil
        root = self._build_root()
        try:
            woken = []
            summary = center_dispatcher.run_cycle_for_account(
                str(root), "muse-ngoai",
                deps={"worker_fn": lambda acc, cand, ctx: woken.append(cand)})
            self.assertEqual(summary["woken"], 0)
            self.assertEqual(summary["other_tasks_read_only"], 1)
            self.assertEqual(woken, [])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
