#!/usr/bin/env python3
"""Test chặng T3 (v0.2.3): tab Trung tâm + Cài đặt giao diện + bật/tắt
node + phong cách Superhuman.

Chạy bằng: python3 -m unittest tests.unit.test_ui_center_t3
Chuẩn: unittest, stdlib-only, không socket, không secret thật.
"""

import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from pc.center import center_store
from pc.ui import ui_app, ui_auth, ui_pages, ui_store
from pc.ui.ui_static import APP_CSS

PASSWORD = "mat-khau-dai-123"


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _queue_row(seq, epoch, chat_id="chat1", text="xin chào",
               event_id=None, title="Nhóm 1"):
    return {
        "seq": seq,
        "received_at": _iso(epoch),
        "event_id": event_id or ("ev%d" % seq),
        "record": {
            "chat_id": chat_id,
            "chat_title": title,
            "sender_id": "s1",
            "sender_name": "Bạn A",
            "text": text,
            "event_id": event_id or ("ev%d" % seq),
            "received_at": _iso(epoch),
        },
    }


def _contrast_ratio(fg_hex, bg_hex):
    """Tỉ lệ tương phản WCAG (fg trên bg)."""

    def _lum(hexs):
        r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (0, 2, 4))

        def _f(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = _f(r), _f(g), _f(b)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    l1, l2 = _lum(fg_hex), _lum(bg_hex)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


class CenterFixture(unittest.TestCase):
    """Fixture UI + 2 tài khoản trung tâm (có đủ secret giả)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "secrets").mkdir()
        (self.root / "data" / "bot-a").mkdir(parents=True)
        (self.root / "data" / "bot-b").mkdir(parents=True)
        (self.root / "ui-config").mkdir()
        self.now = time.time()
        self.write_registry({
            "bot-a": {"bot_id": "bot-a", "platform": "zalo",
                      "display_name": "Bot A",
                      "enabled": True, "paused": False,
                      "awaiting_muse_sync": False,
                      "token_pending_verification": False},
            "bot-b": {"bot_id": "bot-b", "platform": "telegram",
                      "display_name": "Bot B",
                      "enabled": False, "paused": False,
                      "awaiting_muse_sync": True,
                      "token_pending_verification": False},
        })
        self.write_accounts({
            "muse-chinh": {"display_name": "Muse chính",
                           "center_participating": True},
            "muse-phu": {"display_name": "Muse phụ",
                         "center_participating": True},
            "muse-ngoai": {"display_name": "Muse ngoài",
                           "center_participating": False},
        })
        for account_id in ("muse-chinh", "muse-phu"):
            for kind in ("fetch", "push"):
                path = (self.root / "secrets" /
                        ("center-%s.%s_secret" % (account_id, kind)))
                path.write_text("TEST-FAKE-SECRET-%s\n" % kind,
                                encoding="utf-8")
        hashed = ui_auth.hash_password(PASSWORD, n=2 ** 10, r=8, p=1)
        (self.root / "ui-config" / "password.hash").write_text(
            hashed + "\n", encoding="utf-8")
        self.log_calls = []
        self.app = ui_app.UiApp(
            self.root,
            unit_runner=lambda a, b: (0, ""),
            unit_status_fn=lambda bot_id: "active",
            tailscale_fn=lambda: {"up": True, "ip": "100.64.0.5"},
            log_fn=self._counting_log_fn,
            verify_fn=lambda p, u, t: (True, {"id": "1", "name": "Bot"}),
            gateway_status_fn=lambda: "active")
        self.center = center_store.CenterStore(self.root)

    def _counting_log_fn(self, bot_id, limit=50):
        self.log_calls.append(bot_id)
        return ["log ok"]

    def write_registry(self, bots):
        path = self.root / "registry.json"
        path.write_text(json.dumps({"version": 1, "bots": bots}),
                        encoding="utf-8")

    def write_accounts(self, accounts):
        path = self.root / "ui-config" / "accounts.json"
        path.write_text(json.dumps({"version": 1, "accounts": accounts}),
                        encoding="utf-8")

    def write_queue(self, bot_id, rows):
        path = self.root / "data" / bot_id / "queue.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                for row in rows), encoding="utf-8")

    # -- request helpers (theo mẫu test_ui_app_v2) -----------------------
    def request(self, method, path, form=None, headers=None, query=None,
                client_ip="127.0.0.1", raw_body=None, raw_headers=None):
        body = raw_body if raw_body is not None else b""
        hdrs = dict(headers or {})
        if raw_headers:
            hdrs.update(raw_headers)
        if form is not None:
            body = urlencode(form).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        return ui_app.Request(method, path, query=query or {}, headers=hdrs,
                              body=body, client_ip=client_ip)

    def login(self, password=PASSWORD):
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

    def get(self, path, session_cookie, query=None):
        return self.app.handle(self.request(
            "GET", path, headers={"Cookie": session_cookie},
            query=query or {}))

    def post(self, path, session_cookie, csrf_token, form=None):
        payload = dict(form or {})
        payload["csrf_token"] = csrf_token
        return self.app.handle(self.request(
            "POST", path, form=payload,
            headers={"Cookie": session_cookie}))

    def post_no_csrf(self, path, session_cookie, form=None):
        return self.app.handle(self.request(
            "POST", path, form=dict(form or {}),
            headers={"Cookie": session_cookie}))

    def audit_entries(self):
        path = self.root / "ui-config" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def prefs_path(self):
        return self.root / "ui-config" / "ui_prefs.json"

    def set_prefs(self, collapsed):
        self.prefs_path().write_text(
            json.dumps({"version": 1, "collapsed": collapsed}),
            encoding="utf-8")

    def create_task(self, assigned_to="muse-phu", due_hours=24,
                    title="Việc thử"):
        """Tạo 1 việc qua store (caller owner), trả task_id."""
        due = time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                            time.localtime(self.now + due_hours * 3600))
        task, _dup = self.center.create_task(
            "owner", title, "Nội dung việc thử", "nghien-cuu",
            assigned_to, due, "key-%s" % title)
        return task["task_id"]


class TestUiPrefsStore(CenterFixture):
    def test_defaults_7_closed_6_open(self):
        prefs = ui_store.load_ui_prefs(self.root)
        self.assertEqual(len(prefs), 13)
        closed = {k for k, v in prefs.items() if v}
        self.assertEqual(closed, set(ui_store.UI_PREF_DEFAULT_COLLAPSED))
        self.assertEqual(len(closed), 7)
        for key in ("five-stats", "bot-cards", "open-alerts", "health",
                    "controls", "stats-7d"):
            self.assertFalse(prefs[key], key)

    def test_corrupt_file_falls_back_to_defaults_no_crash(self):
        self.prefs_path().write_text("{hong", encoding="utf-8")
        prefs = ui_store.load_ui_prefs(self.root)
        self.assertEqual(len(prefs), 13)
        self.assertTrue(prefs["activity"])
        # Không ghi đè file hỏng.
        self.assertEqual(self.prefs_path().read_text(), "{hong")
        # Trang chính vẫn mở được 200.
        session_cookie, _ = self.login()
        resp = self.get("/", session_cookie)
        self.assertEqual(resp.status, 200)

    def test_unknown_keys_ignored_and_round_trip_600(self):
        self.set_prefs({"activity": False, "la-khoa": True,
                        "ear-log": True})
        prefs = ui_store.load_ui_prefs(self.root)
        self.assertFalse(prefs["activity"])
        self.assertTrue(prefs["ear-log"])
        self.assertNotIn("la-khoa", prefs)
        _result, saved = ui_store.modify_ui_prefs(
            self.root, lambda p: p.__setitem__("activity", True))
        self.assertTrue(saved["activity"])
        self.assertEqual(
            self.prefs_path().stat().st_mode & 0o777, 0o600)

    def test_reset_removes_file_back_to_defaults(self):
        self.set_prefs({"activity": False})
        collapsed = ui_store.reset_ui_prefs(self.root)
        self.assertFalse(self.prefs_path().exists())
        self.assertTrue(collapsed["activity"])


class TestUiPrefsEndpoint(CenterFixture):
    def test_toggle_persists_and_redirects_to_same_tab(self):
        session_cookie, csrf = self.login()
        resp = self.post("/api/ui-prefs", session_cookie, csrf, {
            "node": "activity", "collapsed": "false",
            "next": "/?tab=zalo"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], "/?tab=zalo")
        self.assertFalse(ui_store.load_ui_prefs(self.root)["activity"])

    def test_toggle_survives_polling_fragment(self):
        session_cookie, csrf = self.login()
        self.post("/api/ui-prefs", session_cookie, csrf, {
            "node": "activity", "collapsed": "true", "next": "/"})
        frag = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["all"]}).body.decode()
        self.assertIn("30 dòng gần nhất", frag)
        self.assertNotIn("Hoạt động mới nhất</h2>\n", frag)
        page = self.get("/", session_cookie).body.decode()
        self.assertIn("30 dòng gần nhất", page)

    def test_toggle_survives_logout_and_second_device(self):
        session_cookie, csrf = self.login()
        self.post("/api/ui-prefs", session_cookie, csrf, {
            "node": "bot-cards", "collapsed": "true", "next": "/"})
        # Đăng xuất rồi đăng nhập lại (phiên mới = "thiết bị thứ 2").
        self.app.handle(self.request(
            "POST", "/logout",
            form={"csrf_token": csrf},
            headers={"Cookie": session_cookie}))
        session2, _csrf2 = self.login()
        page = self.get("/", session2).body.decode()
        self.assertIn('data-node="bot-cards"', page)
        self.assertIn("bot", page.lower())
        self.assertFalse(ui_store.load_ui_prefs(self.root)["bot-cards"]
                         is False)
        self.assertTrue(ui_store.load_ui_prefs(self.root)["bot-cards"])

    def test_invalid_key_rejected_whole_operation(self):
        session_cookie, csrf = self.login()
        resp = self.post("/api/ui-prefs", session_cookie, csrf, {
            "node": "khong-co", "collapsed": "true", "next": "/"})
        self.assertEqual(resp.status, 400)
        self.assertFalse(self.prefs_path().exists())

    def test_open_close_all_scoped_to_current_tab(self):
        session_cookie, csrf = self.login()
        resp = self.post("/api/ui-prefs", session_cookie, csrf, {
            "action": "close-all", "tab": "all", "next": "/"})
        self.assertEqual(resp.status, 302)
        prefs = ui_store.load_ui_prefs(self.root)
        for key in ("five-stats", "bot-cards", "activity"):
            self.assertTrue(prefs[key], key)
        # Node trang chi tiết không bị đóng theo.
        self.assertFalse(prefs["health"])
        self.assertFalse(prefs["controls"])
        # Mở lại chỉ trong tab hiện tại.
        self.post("/api/ui-prefs", session_cookie, csrf, {
            "action": "open-all", "tab": "alerts", "next": "/"})
        prefs = ui_store.load_ui_prefs(self.root)
        self.assertFalse(prefs["open-alerts"])
        self.assertTrue(prefs["activity"])

    def test_open_close_all_invalid_tab_rejected(self):
        session_cookie, csrf = self.login()
        resp = self.post("/api/ui-prefs", session_cookie, csrf, {
            "action": "close-all", "tab": "toan-ui", "next": "/"})
        self.assertEqual(resp.status, 400)

    def test_json_variant_returns_full_map(self):
        session_cookie, csrf = self.login()
        resp = self.app.handle(self.request(
            "POST", "/api/ui-prefs",
            raw_body=json.dumps(
                {"node": "activity", "collapsed": False,
                 "csrf_token": csrf}).encode("utf-8"),
            raw_headers={"Content-Type": "application/json"},
            headers={"Cookie": session_cookie}))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode())
        self.assertTrue(data["ok"])
        self.assertFalse(data["collapsed"]["activity"])

    def test_csrf_required(self):
        session_cookie, _ = self.login()
        resp = self.post_no_csrf("/api/ui-prefs", session_cookie, {
            "node": "activity", "collapsed": "true", "next": "/"})
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.prefs_path().exists())

    def test_reset_defaults_one_audit_line_no_toggle_audit(self):
        session_cookie, csrf = self.login()
        # Toggle trước: không ghi audit.
        self.post("/api/ui-prefs", session_cookie, csrf, {
            "node": "activity", "collapsed": "false", "next": "/"})
        self.post("/api/ui-prefs", session_cookie, csrf, {
            "action": "close-all", "tab": "all", "next": "/"})
        self.assertEqual(self.audit_entries(), [])
        # Về mặc định: 1 lần bấm, đúng 1 dòng audit.
        resp = self.post("/api/ui-prefs", session_cookie, csrf, {
            "action": "reset-defaults", "next": "/settings"})
        self.assertEqual(resp.status, 302)
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "ui-prefs-reset")
        self.assertIsNone(entries[0]["bot_id"])
        prefs = ui_store.load_ui_prefs(self.root)
        self.assertTrue(prefs["activity"])
        self.assertFalse(prefs["five-stats"])

    def test_settings_page_lists_13_nodes_and_reset_button(self):
        session_cookie, _ = self.login()
        html = self.get("/settings", session_cookie).body.decode()
        self.assertIn("Cài đặt chung", html)
        self.assertNotIn("Cài đặt giao diện", html)
        self.assertIn("Về mặc định", html)
        for _key, name in ui_pages.UI_NODE_NAMES:
            self.assertIn(name, html)
        self.assertNotIn("chỉ chủ nhân", html)
        # Cài đặt không lên thanh tab.
        self.assertNotIn('data-tab="settings"', html)
        page = self.get("/", session_cookie).body.decode()
        self.assertIn('href="/settings"', page)
        self.assertIn('data-tab="center"', page)


class TestNodeSkipReads(CenterFixture):
    def test_collapsed_activity_skips_activity_feed(self):
        self.write_queue("bot-a", [_queue_row(1, self.now - 60)])
        self.set_prefs({"activity": True})
        session_cookie, _ = self.login()
        with mock.patch.object(
                ui_store, "activity_feed",
                wraps=ui_store.activity_feed) as feed:
            self.get("/", session_cookie)
            feed.assert_not_called()
        self.set_prefs({"activity": False})
        with mock.patch.object(
                ui_store, "activity_feed",
                wraps=ui_store.activity_feed) as feed:
            self.get("/", session_cookie)
            self.assertGreaterEqual(feed.call_count, 1)

    def test_collapsed_detail_nodes_skip_heavy_reads(self):
        self.set_prefs({"message-history": True, "stats-7d": True,
                        "alerts-history-detail": True, "health": True,
                        "ear-log": True})
        session_cookie, _ = self.login()
        self.log_calls.clear()
        with mock.patch.object(ui_store, "merged_history",
                               wraps=ui_store.merged_history) as hist, \
                mock.patch.object(ui_store, "daily_stats",
                                  wraps=ui_store.daily_stats) as daily, \
                mock.patch.object(ui_store, "read_alert_events",
                                  wraps=ui_store.read_alert_events) as ev, \
                mock.patch.object(ui_store, "read_poll_samples",
                                  wraps=ui_store.read_poll_samples) as poll:
            self.get("/bots/bot-a", session_cookie)
            hist.assert_not_called()
            daily.assert_not_called()
            ev.assert_not_called()
            poll.assert_not_called()
            self.assertEqual(self.log_calls, [])
        # Mở lại: các lệnh đọc chạy.
        self.set_prefs({})
        with mock.patch.object(ui_store, "merged_history",
                               wraps=ui_store.merged_history) as hist:
            self.get("/bots/bot-a", session_cookie)
            self.assertGreaterEqual(hist.call_count, 1)

    def test_collapsed_nodes_show_summaries(self):
        session_cookie, _ = self.login()
        page = self.get("/bots/bot-a", session_cookie).body.decode()
        self.assertIn("5 trường ·", page)  # config đóng
        self.assertIn("50 mục/trang", page)  # message-history đóng
        self.assertIn("50 dòng cuối", page)  # ear-log đóng


class TestCenterTab(CenterFixture):
    def test_tab_order_and_six_tabs(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie).body.decode()
        tabs = re.findall(r'data-tab="([^"]+)"', html)
        # (v0.6.0, T3i — DOI Q-T3-05) Thanh tab 5 -> 6 tab: Chat
        # ngay sau Trung tam.
        self.assertEqual(tabs, ["all", "center", "chat", "zalo",
                                "telegram", "alerts"])
        self.assertNotIn('data-tab="settings"', html)

    def test_center_fragment_order_and_chat_outside_fragment(self):
        self.create_task(due_hours=-1, title="Việc quá hạn")
        self.create_task(due_hours=24, title="Việc sau")
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        # (v0.6.0, T3i — DOI Q-T3-05) Tab Trung tam ket thuc o dong
        # dan sang tab Chat (khong con khung chat trong tab nay).
        order = ["Việc đang mở",
                 # Tiêu đề mục Trung tâm nay có huy hiệu số lượng
                 # (đợt mẫu rẻ Hades): 2 việc, 3 tài khoản mẫu.
                 '<h2>Việc<span class="node-badge">2</span></h2>',
                 '<h2>Tài khoản<span class="node-badge">3</span></h2>',
                 "Mở khung chat →", "Tạo việc mới"]
        positions = [html.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Quá hạn", html)
        # Huy hiệu quá hạn trên tab Trung tâm.
        self.assertIn('data-tab="center"', html)
        self.assertRegex(html, r'data-tab="center"[^<]*<span '
                               r'class="badge">1</span>')
        # Khung chat + form tạo việc KHÔNG nằm trong fragment 5 giây.
        frag = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertNotIn("center-chat-box", frag)
        self.assertNotIn('id="center-hoi-body"', frag)
        self.assertNotIn("Tạo việc mới", frag)
        self.assertIn("Việc quá hạn", frag)
        self.assertIn("Muse phụ", frag)
        # Dong dan sang tab Chat co trong fragment Trung tam.
        self.assertIn("Mở khung chat →", frag)

    def test_overdue_tasks_first(self):
        late = self.create_task(due_hours=-2, title="Việc muộn")
        soon = self.create_task(due_hours=5, title="Việc sớm")
        session_cookie, _ = self.login()
        frag = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertLess(frag.index("Việc muộn"), frag.index("Việc sớm"))
        self.assertLess(frag.index(late), frag.index(soon))

    def test_create_task_missing_due_rejected_with_message(self):
        session_cookie, csrf = self.login()
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "Việc không hạn", "body": "nội dung",
            "kind": "app", "assigned_to": "muse-phu", "due_at": ""})
        self.assertEqual(resp.status, 200)
        html = resp.body.decode()
        self.assertIn("Thiếu hạn xong", html)
        self.assertEqual(self.center.list_tasks(), {})

    def test_create_task_ok_creates_task_and_assign_message(self):
        session_cookie, csrf = self.login()
        due = time.strftime("%Y-%m-%dT%H:%M",
                            time.localtime(self.now + 86400))
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "Việc mới", "body": "làm app",
            "kind": "app", "assigned_to": "muse-phu", "due_at": due})
        self.assertEqual(resp.status, 302)
        tasks = self.center.list_tasks()
        self.assertEqual(len(tasks), 1)
        task = next(iter(tasks.values()))
        self.assertEqual(task["status"], "moi")
        self.assertEqual(task["assigned_from"], "owner")
        self.assertTrue(task["due_at"].endswith("+07:00"))
        events = self.center.read_events()
        kinds = [row.get("kind") for row in events]
        self.assertEqual(kinds, ["task_created", "message"])
        seqs = [row.get("seq") for row in events]
        self.assertEqual(seqs, [seqs[0], seqs[0] + 1])
        msg = events[1].get("record") or {}
        self.assertEqual(msg.get("kind"), "giao-viec")
        # Trang chi tiết việc mở được.
        detail = self.get("/center/tasks/%s" % task["task_id"],
                          session_cookie)
        self.assertEqual(detail.status, 200)
        self.assertIn("Việc mới", detail.body.decode())

    def test_create_task_xss_escaped(self):
        session_cookie, csrf = self.login()
        due = time.strftime("%Y-%m-%dT%H:%M",
                            time.localtime(self.now + 86400))
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "<script>alert(1)</script>", "body": "x",
            "kind": "app", "assigned_to": "muse-phu", "due_at": due})
        self.assertEqual(resp.status, 302)
        html = self.get("/", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_create_task_secret_like_rejected(self):
        session_cookie, csrf = self.login()
        due = time.strftime("%Y-%m-%dT%H:%M",
                            time.localtime(self.now + 86400))
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "Việc", "body": "queue_secret: TEST-FAKE-ABC",
            "kind": "app", "assigned_to": "muse-phu", "due_at": due})
        html = resp.body.decode()
        self.assertIn("giống mẫu secret", html)
        self.assertEqual(self.center.list_tasks(), {})

    def test_create_task_unassigned_account_rejected(self):
        session_cookie, csrf = self.login()
        due = time.strftime("%Y-%m-%dT%H:%M",
                            time.localtime(self.now + 86400))
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "Việc", "body": "x",
            "kind": "app", "assigned_to": "muse-ngoai", "due_at": due})
        self.assertIn("chưa tham gia trung tâm",
                      resp.body.decode())
        self.assertEqual(self.center.list_tasks(), {})

    def test_send_hoi_requires_csrf_and_stores_hoi(self):
        session_cookie, csrf = self.login()
        resp = self.post_no_csrf("/center/messages", session_cookie, {
            "body": "Hỏi gì đó"})
        self.assertEqual(resp.status, 403)
        resp = self.post("/center/messages", session_cookie, csrf, {
            "body": "Hỏi gì đó"})
        self.assertEqual(resp.status, 302)
        events = self.center.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["record"]["kind"], "hoi")
        self.assertEqual(events[0]["record"]["from_account"], "owner")
        # (v0.6.0, T3i) Tin hien o tab Chat (khong con o tab Trung tam).
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn("Hỏi gì đó", html)
        self.assertIn('>Gửi</button>', html)
        html = self.get("/", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertNotIn("Hỏi gì đó", html)

    def test_no_hand_send_route_for_giao_viec_bao_cao(self):
        """Form gửi tin không có trường kind; server luôn ép kind=hoi
        kể cả khi attacker gửi kind=giao-viec/bao-cao."""
        session_cookie, csrf = self.login()
        for kind in ("giao-viec", "bao-cao"):
            resp = self.post("/center/messages", session_cookie, csrf, {
                "body": "thử %s" % kind, "kind": kind})
            self.assertEqual(resp.status, 302)
        events = self.center.read_events()
        self.assertTrue(events)
        self.assertTrue(all(
            row["record"]["kind"] == "hoi" for row in events))
        # Grep code: chi 2 cho goi append_message o tang UI, deu
        # kind=hoi — form "Hoi qua trung tam" + tin thong bao link
        # chung (sua Link chung 22/11). Khong route gui
        # giao-viec/bao-cao tay (F18).
        source = (Path(ui_app.__file__).read_text(encoding="utf-8"))
        self.assertEqual(source.count("append_message"), 2)
        self.assertEqual(source.count('"hoi"'), 2)
        # (v0.6.0, T3i — D8) Goi kem to_account (None = Tat ca).
        self.assertIn('"hoi", to_account, None, body,', source)

    def test_task_actions_confirm_return_reject_reassign(self):
        session_cookie, csrf = self.login()
        # Từ chối việc ở trạng thái mới: cần note.
        task_id = self.create_task()
        resp = self.post("/center/tasks/%s/status" % task_id,
                         session_cookie, csrf,
                         {"to_status": "huy", "note": ""})
        self.assertIn("note bắt buộc", resp.body.decode())
        resp = self.post("/center/tasks/%s/status" % task_id,
                         session_cookie, csrf,
                         {"to_status": "huy",
                          "note": "Không làm được việc này"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(
            self.center.get_task(task_id)["status"], "huy")
        # Chuyển tay ở mọi trạng thái chưa xong, reset về mới.
        first_task_id = task_id
        task_id = self.create_task(title="Việc chuyển")
        self.center.change_status(task_id, "da-nhan", caller="muse-phu",
                                  caller_role="assigned_to", note=None,
                                  client_key="k1")
        resp = self.post("/center/tasks/%s/reassign" % task_id,
                         session_cookie, csrf,
                         {"assigned_to": "muse-chinh"})
        self.assertEqual(resp.status, 302)
        task = self.center.get_task(task_id)
        self.assertEqual(task["status"], "moi")
        self.assertIsNone(task["seen_at"])
        self.assertEqual(task["assigned_to"], ["muse-chinh"])
        # Xác nhận xong + trả lại.
        self.center.change_status(task_id, "da-nhan", caller="muse-chinh",
                                  caller_role="assigned_to", note=None,
                                  client_key="k2")
        self.center.change_status(task_id, "dang-lam",
                                  caller="muse-chinh",
                                  caller_role="assigned_to", note=None,
                                  client_key="k3")
        self.center.change_status(task_id, "da-bao-xong",
                                  caller="muse-chinh",
                                  caller_role="assigned_to", note=None,
                                  client_key="k4")
        resp = self.post("/center/tasks/%s/status" % task_id,
                         session_cookie, csrf,
                         {"to_status": "dang-lam", "note": ""})
        self.assertIn("note bắt buộc", resp.body.decode())
        resp = self.post("/center/tasks/%s/status" % task_id,
                         session_cookie, csrf,
                         {"to_status": "dang-lam",
                          "note": "Làm lại phần này"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(
            self.center.get_task(task_id)["status"], "dang-lam")
        self.center.change_status(task_id, "da-bao-xong",
                                  caller="muse-chinh",
                                  caller_role="assigned_to", note=None,
                                  client_key="k5")
        resp = self.post("/center/tasks/%s/status" % task_id,
                         session_cookie, csrf,
                         {"to_status": "da-xac-nhan"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(
            self.center.get_task(task_id)["status"], "da-xac-nhan")
        # Audit trung tâm có task_id.
        entries = [e for e in self.audit_entries()
                   if e["action"].startswith("center_task")]
        self.assertTrue(entries)
        self.assertTrue(all(e["task_id"] in (task_id, first_task_id)
                            for e in entries))
        self.assertTrue(any(e["task_id"] == task_id and
                            e["action"] == "center_task_confirm"
                            for e in entries))
        self.assertTrue(all(e["bot_id"] is None for e in entries))

    def test_account_status_unknown_without_fetch_state_source(self):
        """v0.2.4: trạng thái tài khoản là "chưa thấy kéo" khi chưa có
        nguồn gateway-fetch-state.json mở rộng (mục "center", khóa
        account_id, trường "last_fetch_at") — mục "center" thiếu thì
        đọc về {}; KHÔNG suy từ seen_state.json — kể cả khi tài khoản
        đã kéo feed có việc của mình (record_seen ghi seen_state.json)
        thì trạng thái vẫn "chưa thấy kéo", và không hiện
        "đang hoạt động". (TODO UCT-08 cũ — "chưa rõ" — đã bị thay
        thế bởi hành vi v0.2.4 này.)"""
        task_id = self.create_task()
        session_cookie, _ = self.login()
        frag = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertIn("Chưa thấy kéo", frag)
        self.assertNotIn("đang hoạt động", frag)
        # Tài khoản kéo feed có chứa việc của mình -> vẫn "chưa thấy kéo"
        # (seen_state.json không phải nguồn trạng thái tài khoản).
        self.center.record_seen("muse-phu", [task_id])
        frag = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertIn("Chưa thấy kéo", frag)
        self.assertNotIn("đang hoạt động", frag)
        self.assertNotIn("chưa rõ", frag)


class TestSuperhumanTokens(CenterFixture):
    def test_style_tokens_present(self):
        for token in ("#f2f0eb", "#292827", "#e3e3e2", "#714cb6",
                      "#421d24", "#ede7f6"):
            self.assertIn(token, APP_CSS)
        self.assertIn(".btn.secondary", APP_CSS)
        self.assertIn(".primary-action", APP_CSS)
        self.assertIn("font-weight: 500", APP_CSS)
        self.assertIn("padding: 16px", APP_CSS)
        self.assertIn("padding: 12px 20px", APP_CSS)
        # Chỉ còn bóng thanh tab mobile; thẻ không còn bóng.
        shadows = [line for line in APP_CSS.splitlines()
                   if "box-shadow" in line]
        self.assertEqual(len(shadows), 1)
        self.assertIn("0 4px 16px", shadows[0])
        # Viền 2px trung tính đã về 1px; chỉ còn 2px báo lỗi/cảnh báo.
        self.assertNotIn("2px solid var(--border)", APP_CSS)
        self.assertIn("2px solid var(--red)", APP_CSS)
        self.assertIn("2px solid var(--yellow)", APP_CSS)
        # .danger giữ nguyên #a50e0e.
        self.assertIn("--red: #a50e0e", APP_CSS)

    def test_secondary_contrast_at_16_17px(self):
        ratio = _contrast_ratio("714cb6", "ede7f6")
        self.assertGreaterEqual(ratio, 4.5)
        # Nút đỏ rượu chữ trắng cũng đạt.
        self.assertGreaterEqual(_contrast_ratio("ffffff", "421d24"), 4.5)

    def test_no_inline_handlers_and_no_details_node_toggle(self):
        for path in (ui_pages.__file__, ui_app.__file__):
            source = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("onclick=", source)
        # <details> duy nhất còn lại là "Xem thêm" của thẻ bot (không
        # phải toggle node); các section node không chứa <details>.
        session_cookie, _ = self.login()
        page = self.get("/", session_cookie).body.decode()
        for section in re.findall(
                r'<section class="node"[^>]*>.*?</section>', page,
                flags=re.DOTALL):
            self.assertNotIn("<details", section)
        detail = self.get("/bots/bot-a", session_cookie).body.decode()
        for section in re.findall(
                r'<section class="node"[^>]*>.*?</section>', detail,
                flags=re.DOTALL):
            self.assertNotIn("<details", section)

    def test_360px_tab_bar_fits_one_row(self):
        """6 tab vua 1 hang o man 360px (do bang font Liberation Bold
        12px — tuong duong Arial/Roboto tren dien thoai).
        (v0.6.0, T3i) Them tab Chat: nhan dai ("Trung tam",
        "Telegram") duoc xuong 2 dong trong tab (thanh tab van nam
        gon trong dem chan 130px); nhan ngan vua 1 dong."""
        from PIL import ImageFont
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/liberation/"
            "LiberationSans-Bold.ttf", 12)
        # Thanh tab: 360 - lề cố định 2x10 - đệm 2x4 - khe 5x4 = 312;
        # mỗi tab 52px, trừ đệm tab 2x2 còn 48px cho nhãn/dòng.
        budget = (360 - 20 - 8 - 20) / 6 - 4
        for label in ("Tất cả", "Trung tâm", "Chat", "Zalo", "Telegram",
                      "Cảnh báo"):
            width = font.getlength(label)
            self.assertLessEqual(width, 2 * budget,
                                 "%s rộng %s > 2 dòng %s"
                                 % (label, width, 2 * budget))
        for label in ("Tất cả", "Chat", "Zalo"):
            width = font.getlength(label)
            self.assertLessEqual(width, budget,
                                 "%s rộng %s > %s" % (label, width, budget))
        # Đệm chân trang đủ cho thanh tab kể cả khi 1 nhãn xuống 2 dòng.
        self.assertIn("padding-bottom: 130px", APP_CSS)
        self.assertIn("font-size: 12px", APP_CSS)
        # Trường hợp xấu nhất (nhãn + huy hiệu xuống 2 dòng): chiều cao
        # thanh ~ 2 dòng 12px*1.55 + đệm 16 + đệm khung 12 + đáy 10 < 130.
        worst_bar = 2 * 12 * 1.55 + 16 + 12 + 10
        self.assertLess(worst_bar, 130)

    def test_button_classes_per_spec_table(self):
        session_cookie, _ = self.login()
        login_html = self.app.handle(
            self.request("GET", "/login")).body.decode()
        self.assertIn('class="primary-action"', login_html)
        page = self.get("/", session_cookie).body.decode()
        self.assertIn('<a class="btn" href="/bots/new">', page)
        self.assertNotIn('class="btn primary"', page)
        self.assertNotIn('class="primary"', page)


if __name__ == "__main__":
    unittest.main()
