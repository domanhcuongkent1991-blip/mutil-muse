#!/usr/bin/env python3
"""Test chặng T3i (v0.6.0): tab Chat riêng + thanh cuộn + Enter.

unittest, stdlib-only, UI qua UiApp trực tiếp. Mọi giá trị secret
trong test đều là mẫu giả dạng rõ ràng ("TEST-FAKE-..."), không
secret thật, không nội dung tin thật. File đính kèm trong test chỉ
là bytes giả.
"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pc.center import center_store  # noqa: E402
from pc.ui import ui_app, ui_auth  # noqa: E402
from pc.ui import ui_pages, ui_store  # noqa: E402
from pc.ui.ui_static import APP_CSS, APP_JS  # noqa: E402

DESIGN_ROOT = Path(__file__).resolve().parents[2].parent / "multibot-design"

PASSWORD = "Test-Pass-123"


def _build_multipart(fields, files, boundary="----TESTI"):
    """Dựng body multipart/form-data.

    fields: [(name, value)]; files: [(field, filename, data_bytes)].
    Trả về (body_bytes, content_type)."""
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


class T3iUiFixture(unittest.TestCase):
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
        for account_id in ("muse-chinh", "muse-phu"):
            for kind in ("fetch", "push"):
                path = (self.root / "secrets" /
                        ("center-%s.%s_secret" % (account_id, kind)))
                path.write_text("TEST-FAKE-SECRET-%s\n" % kind,
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

    def _write_accounts(self, flags):
        names = {"muse-chinh": "Muse chính", "muse-phu": "Muse phụ",
                 "muse-ngoai": "Muse ngoài"}
        (self.root / "ui-config" / "accounts.json").write_text(
            json.dumps({"version": 1, "accounts": {
                account_id: {
                    "display_name": names[account_id],
                    "center_participating": participating}
                for account_id, participating in flags.items()}}),
            encoding="utf-8")

    # -- request helpers --------------------------------------------------
    def request(self, method, path, form=None, headers=None, query=None,
                raw_body=None):
        body = raw_body if raw_body is not None else b""
        hdrs = dict(headers or {})
        if form is not None:
            body = urlencode(form).encode("utf-8")
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

    def post_multipart(self, path, session_cookie, csrf_token,
                       fields, files):
        all_fields = list(fields) + [("csrf_token", csrf_token)]
        body, ctype = _build_multipart(all_fields, files)
        return self.app.handle(self.request(
            "POST", path, raw_body=body,
            headers={"Cookie": session_cookie,
                     "Content-Type": ctype}))

    def seed(self, from_account, kind, body, msg_id, room="chung",
             task_id=None):
        msg, _ = self.center.append_message(
            from_account, kind, None, task_id, body, msg_id, room=room)
        return msg

    def backdate_all_messages(self, seconds_ago):
        """Lùi giờ tạo của mọi tin đã ghi (để kiểm ngưỡng chờ).
        Store ghi created_at = now_iso() nên test sửa trực tiếp
        events.jsonl thay vì truyền giờ giả qua append_message."""
        from datetime import datetime, timedelta
        stamp = (datetime.now().astimezone()
                 - timedelta(seconds=seconds_ago)).isoformat()
        rows = []
        for line in self.center.events_path.read_text(
                encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("kind") == "message":
                row["record"]["created_at"] = stamp
            rows.append(row)
        self.center.events_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n"
                    for row in rows), encoding="utf-8")

    def make_phu_archived(self):
        """Phụ tạm ngưng (có tắt) nhưng đã có tin trong phòng riêng
        -> phòng lưu trữ."""
        self.seed("muse-phu", "hoi", "tin trước khi ngưng",
                  "m-cccccccc2021", room="dm:muse-phu")
        self._write_accounts({"muse-chinh": True, "muse-phu": False,
                              "muse-ngoai": False})

    def unread_json(self, session_cookie):
        resp = self.get("/api/center/unread", session_cookie)
        self.assertEqual(resp.status, 200, resp.body[:200])
        return json.loads(resp.body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Tab Chat riêng (mô hình A) — tiêu chí PLAN 1–8
# ---------------------------------------------------------------------------

class TestChatTab(T3iUiFixture):
    def test_six_tabs_in_order(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie).body.decode()
        tabs = re.findall(r'data-tab="([^"]+)"', html)
        self.assertEqual(tabs, ["all", "center", "chat", "zalo",
                                "telegram", "alerts"])

    def test_center_tab_only_lead_line(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        # Tab Trung tâm chỉ còn dòng dẫn sang tab Chat.
        self.assertIn("Mở khung chat →", html)
        self.assertNotIn('id="center-chat-box"', html)
        self.assertNotIn('id="center-hoi-body"', html)
        self.assertNotIn("Khung chung", html)
        # Tab Chat có đủ khung chat + danh sách phòng + ô nhập.
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn('id="center-chat-box"', html)
        self.assertIn('id="center-hoi-body"', html)
        self.assertIn("Khung chung", html)

    def test_center_lead_carries_total_unread(self):
        self.seed("muse-phu", "hoi", "tin phu", "m-cccccccc2101")
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertRegex(html, r'Mở khung chat →.*?<span '
                               r'class="room-badge">1</span>')

    def test_chat_layout_300px_gap_16px_mobile_stack(self):
        # Cột trái danh sách phòng 300px, khoảng cách 2 cột 16px.
        self.assertIn("flex: 0 0 300px", APP_CSS)
        self.assertIn("gap: 16px", APP_CSS)
        # Điện thoại ≤ 720px: xếp dọc, phòng full chiều rộng.
        self.assertIn("@media (max-width: 720px)", APP_CSS)
        mobile = APP_CSS.split("@media (max-width: 720px)", 1)[1]
        self.assertIn("flex-direction: column", mobile)
        self.assertIn("width: 100%", mobile)

    def test_chat_badges_total_and_mention(self):
        self.seed("muse-phu", "hoi", "chào @owner nhé",
                  "m-cccccccc2102")
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn('<span class="badge" data-chat-unread-badge>'
                      '1</span>', html)
        self.assertIn('<span class="badge badge--mention" '
                      'data-chat-mention-badge>@</span>', html)
        data = self.unread_json(session_cookie)
        self.assertEqual(data["total"], 1)
        self.assertTrue(data["has_mention"])
        self.assertEqual(data["mention_rooms"], ["chung"])

    def test_empty_room_shown(self):
        # Phòng riêng chưa có tin vẫn hiện trong danh sách phòng.
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        # (L5, nhóm B 21/11 — quyết chủ nhân) Danh sách phòng không
        # còn tiền tố "Riêng:" — phòng riêng chỉ hiện tên tài khoản.
        self.assertIn("Muse phụ", html)
        self.assertNotIn("Riêng: Muse phụ", html)

    def test_chat_tab_has_no_task_list(self):
        session_cookie, csrf = self.login()
        due = time.strftime("%Y-%m-%dT%H:%M",
                            time.localtime(self.now + 86400))
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "Việc trên tab Trung tâm", "body": "làm app",
            "kind": "app", "assigned_to": "muse-phu", "due_at": due})
        self.assertEqual(resp.status, 302)
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        # Tab Chat chỉ chat: không có danh sách việc / form tạo việc.
        # (Tin giao-viec vẫn hiện trong khung chat như 1 tin nhắn —
        # đó là nội dung chat, không phải danh sách việc.)
        self.assertNotIn("Việc đang mở", html)
        self.assertNotIn("Tạo việc mới", html)
        self.assertNotIn('action="/center/tasks"', html)
        html = self.get("/", session_cookie,
                        query={"tab": ["center"]}).body.decode()
        self.assertIn("Việc trên tab Trung tâm", html)


# ---------------------------------------------------------------------------
# Nhớ phòng đang chat dở — tiêu chí PLAN 5
# ---------------------------------------------------------------------------

class TestLastRoom(T3iUiFixture):
    def test_get_set_last_chat_room(self):
        # Chưa lưu -> "chung"; lưu xong đọc lại đúng; file 600.
        self.assertEqual(center_store.get_last_chat_room(self.root),
                         "chung")
        self.assertTrue(center_store.set_last_chat_room(
            self.root, "dm:muse-phu"))
        self.assertEqual(center_store.get_last_chat_room(self.root),
                         "dm:muse-phu")
        path = (self.root / "data" / "center" / "chat_ui_state.json")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        # Phòng sai mẫu -> không ghi, trả False.
        self.assertFalse(center_store.set_last_chat_room(
            self.root, "phong la"))
        self.assertEqual(center_store.get_last_chat_room(self.root),
                         "dm:muse-phu")

    def test_corrupt_state_falls_back_to_chung(self):
        path = (self.root / "data" / "center" / "chat_ui_state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{hong", encoding="utf-8")
        self.assertEqual(center_store.get_last_chat_room(self.root),
                         "chung")
        # File hỏng KHÔNG bị ghi đè.
        self.assertEqual(path.read_text(encoding="utf-8"), "{hong")

    def test_query_room_wins_and_is_saved(self):
        self.seed("muse-phu", "hoi", "tin rieng",
                  "m-cccccccc2103", room="dm:muse-phu")
        session_cookie, _ = self.login()
        # Mở ?room=dm:muse-phu -> hộp chat theo phòng đó + lưu lại.
        html = self.get("/", session_cookie, query={
            "tab": ["chat"], "room": ["dm:muse-phu"]}).body.decode()
        self.assertIn('data-room="dm:muse-phu"', html)
        self.assertEqual(center_store.get_last_chat_room(self.root),
                         "dm:muse-phu")
        # Mở tab Chat không ?room= -> mở lại phòng đã lưu.
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn('data-room="dm:muse-phu"', html)

    def test_unknown_room_falls_back_silently(self):
        # Q-V060-11: phòng không còn -> rớt lặng lẽ về "chung".
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie, query={
            "tab": ["chat"], "room": ["dm:muse-ngoai"]}).body.decode()
        self.assertIn('data-room="chung"', html)
        self.assertNotIn("không còn tồn tại", html)
        self.assertNotIn("không tồn tại", html)


# ---------------------------------------------------------------------------
# Hộp chat + cuộn — tiêu chí PLAN 9–11, 14, 17–19
# ---------------------------------------------------------------------------

class TestChatScroll(T3iUiFixture):
    def test_static_box_fixed_size_65vh(self):
        # B2 (21/11): hop chat tab Chat desktop nay cao
        # calc(100vh - 266px) — xem THIET-KE-KHUNG-CHAT-B-21-11.md
        # §3.3. Quy tac goc 65vh/400px chi con la hoi quy cho hop
        # TINH trang chi tiet viec (ngoai .center-chat-section);
        # test nay doi ten/doi nghia theo do (G-CNTC).
        self.assertIn("height: 65vh", APP_CSS)
        self.assertIn("min-height: 400px", APP_CSS)
        mobile = APP_CSS.split("@media (max-width: 720px)", 1)[1]
        # (SUA L1, 21/11) Hop mobile 50vh -> 40vh de nut Gui sach
        # thanh tab co dinh o vi tri doc tu nhien.
        self.assertIn("height: 40vh", mobile)
        self.assertIn("min-height: 320px", mobile)

    def test_fragment_meta_before_after(self):
        for i in range(60):
            self.seed("muse-phu", "hoi", "tin %02d" % i,
                      "m-%012d" % i)
        session_cookie, _ = self.login()
        frag = self.get("/fragments/center-chat", session_cookie,
                        query={"chat_limit": ["50"]}).body.decode()
        self.assertIn("tin 59", frag)
        self.assertNotIn("tin 09", frag)
        self.assertNotIn("Xem thêm tin cũ", frag)
        self.assertIn('data-chat-meta', frag)
        self.assertIn('data-has-more="true"', frag)
        self.assertIn('data-oldest-seq="11"', frag)
        self.assertIn('data-newest-seq="60"', frag)
        # before_seq: 50 tin liền trước seq 11.
        frag = self.get("/fragments/center-chat", session_cookie,
                        query={"before_seq": ["11"]}).body.decode()
        self.assertIn("tin 00", frag)
        self.assertIn("tin 09", frag)
        self.assertNotIn("tin 10", frag)
        self.assertIn('data-has-more="false"', frag)
        # after_seq: chỉ tin mới hơn seq 58.
        frag = self.get("/fragments/center-chat", session_cookie,
                        query={"after_seq": ["58"]}).body.decode()
        self.assertIn("tin 58", frag)
        self.assertIn("tin 59", frag)
        self.assertNotIn("tin 57", frag)
        # after_seq vượt tối đa -> rỗng, không lỗi.
        frag = self.get("/fragments/center-chat", session_cookie,
                        query={"after_seq": ["60"]}).body.decode()
        self.assertNotIn('class="msg ', frag)

    def test_no_500_cap_page_back_past_500(self):
        # BỎ trần 500: 600 tin vẫn xem lại tới tin cũ nhất bằng
        # before_seq nhiều lượt.
        for i in range(600):
            self.seed("muse-phu", "hoi", "tin %03d" % i,
                      "m-%012d" % i)
        session_cookie, _ = self.login()
        frag = self.get("/fragments/center-chat", session_cookie,
                        query={"chat_limit": ["50"]}).body.decode()
        oldest = int(re.search(r'data-oldest-seq="(\d+)"',
                               frag).group(1))
        self.assertEqual(oldest, 551)
        seen_oldest = oldest
        while True:
            frag = self.get("/fragments/center-chat", session_cookie,
                            query={"before_seq": [str(seen_oldest)]}
                            ).body.decode()
            self.assertIn('data-chat-meta', frag)
            seen_oldest = int(re.search(r'data-oldest-seq="(\d+)"',
                                        frag).group(1))
            if 'data-has-more="false"' in frag:
                break
        self.assertEqual(seen_oldest, 1)
        self.assertIn("tin 000", frag)
        # Tới tin cũ nhất còn lưu thì hiện dòng đó.
        html = ui_pages.center_chat_tab_html(
            rooms=[], current_room="chung", room_label="Khung chung",
            messages=[], account_names={}, task_titles={},
            chat_limit=50, has_more=False, oldest_seq=1, newest_seq=1,
            unread_counts={}, mention_rooms=set(), typing_rooms={},
            typing_names={}, csrf_token="TEST-FAKE-CSRF", draft="",
            archived=False, client_key="TEST-FAKE-KEY")
        self.assertIn("Đã tới tin cũ nhất còn lưu", html)

    def test_fragment_400s(self):
        session_cookie, _ = self.login()
        # before_seq + after_seq cùng lúc.
        resp = self.get("/fragments/center-chat", session_cookie,
                        query={"before_seq": ["10"],
                               "after_seq": ["5"]})
        self.assertEqual(resp.status, 400)
        # task_id + room rõ ràng cùng lúc.
        resp = self.get("/fragments/center-chat", session_cookie,
                        query={"task_id": ["t-abc"],
                               "room": ["chung"]})
        self.assertEqual(resp.status, 400)
        # seq không phải số.
        resp = self.get("/fragments/center-chat", session_cookie,
                        query={"before_seq": ["abc"]})
        self.assertEqual(resp.status, 400)
        # task_id sai mẫu / không tồn tại.
        resp = self.get("/fragments/center-chat", session_cookie,
                        query={"task_id": ["khong-phai-task"]})
        self.assertEqual(resp.status, 400)
        resp = self.get("/fragments/center-chat", session_cookie,
                        query={"task_id": ["t-000000000000"]})
        self.assertEqual(resp.status, 400)

    def test_scroll_js_constants_and_guards(self):
        # Ngưỡng cuộn + giới hạn vẽ + nhịp polling + neo vị trí.
        self.assertIn("var CHAT_NEAR_BOTTOM_PX = 150;", APP_JS)
        self.assertIn("var CHAT_LOAD_TOP_PX = 24;", APP_JS)
        self.assertIn("var CHAT_MAX_BUBBLES = 300;", APP_JS)
        self.assertIn("var CHAT_POLL_MS = 5000;", APP_JS)
        self.assertIn("loadingOlder", APP_JS)
        self.assertIn("box.scrollTop += box.scrollHeight - prevHeight;",
                      APP_JS)
        # Tin mới chỉ append ở cuối hộp, không ghi đè chỗ đang đọc.
        self.assertIn("box.appendChild(frag);", APP_JS)
        # Không còn tự cuộn theo cửa sổ trình duyệt.
        self.assertNotIn("window.scrollTo", APP_JS)

    def test_chat_constants_unchanged(self):
        # B2 (21/11): D-B-05 — 4 hang JS cuon/polling KHONG DOI
        # (ghi rieng ten test theo THIET-KE-KHUNG-CHAT-B-21-11.md
        # §6.1, canh assert hang so tren).
        self.assertIn("var CHAT_NEAR_BOTTOM_PX = 150;", APP_JS)
        self.assertIn("var CHAT_LOAD_TOP_PX = 24;", APP_JS)
        self.assertIn("var CHAT_MAX_BUBBLES = 300;", APP_JS)
        self.assertIn("var CHAT_POLL_MS = 5000;", APP_JS)


# ---------------------------------------------------------------------------
# Luật đã đọc mới — tiêu chí PLAN 12–13, 25
# ---------------------------------------------------------------------------

class TestReadLaw(T3iUiFixture):
    def test_fragment_polling_does_not_mark_read(self):
        self.seed("muse-phu", "hoi", "tin phu 1", "m-cccccccc2111",
                  room="dm:muse-phu")
        self.seed("muse-phu", "hoi", "tin phu 2", "m-cccccccc2112",
                  room="dm:muse-phu")
        session_cookie, _ = self.login()
        before = (self.root / "data" / "center" /
                  "read_state.json").read_bytes() \
            if (self.root / "data" / "center" /
                "read_state.json").exists() else None
        # Mở fragment 3 chu kỳ polling -> mốc đã đọc không đổi.
        for _ in range(3):
            resp = self.get("/fragments/center-chat", session_cookie,
                            query={"room": ["dm:muse-phu"],
                                   "chat_limit": ["50"]})
            self.assertEqual(resp.status, 200)
        after = (self.root / "data" / "center" /
                 "read_state.json").read_bytes() \
            if (self.root / "data" / "center" /
                "read_state.json").exists() else None
        self.assertEqual(before, after)
        self.assertEqual(
            self.center.get_read_watermark("dm:muse-phu"), 0)
        # Huy hiệu vẫn 2.
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn('<span class="badge" data-chat-unread-badge>'
                      '2</span>', html)

    def test_mark_read_endpoint(self):
        self.seed("muse-phu", "hoi", "tin phu", "m-cccccccc2113",
                  room="dm:muse-phu")
        session_cookie, csrf = self.login()
        # Không CSRF -> 403.
        resp = self.post_no_csrf("/center/mark-read", session_cookie,
                                 {"room": "dm:muse-phu", "seq": "1"})
        self.assertEqual(resp.status, 403)
        # Phòng sai mẫu / seq sai -> 400.
        resp = self.post("/center/mark-read", session_cookie, csrf,
                         {"room": "phong la", "seq": "1"})
        self.assertEqual(resp.status, 400)
        resp = self.post("/center/mark-read", session_cookie, csrf,
                         {"room": "dm:muse-phu", "seq": "abc"})
        self.assertEqual(resp.status, 400)
        resp = self.post("/center/mark-read", session_cookie, csrf,
                         {"room": "dm:muse-phu", "seq": "-1"})
        self.assertEqual(resp.status, 400)
        # Đánh dấu đúng -> mốc lên 1, huy hiệu về 0.
        resp = self.post("/center/mark-read", session_cookie, csrf,
                         {"room": "dm:muse-phu", "seq": "1"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(data, {"ok": True, "room": "dm:muse-phu",
                                "read_seq": 1})
        self.assertEqual(
            self.center.get_read_watermark("dm:muse-phu"), 1)
        # seq nhỏ hơn mốc hiện tại -> mốc giữ nguyên, vẫn OK.
        resp = self.post("/center/mark-read", session_cookie, csrf,
                         {"room": "dm:muse-phu", "seq": "0"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            self.center.get_read_watermark("dm:muse-phu"), 1)

    def test_mark_read_archived_room_400(self):
        self.make_phu_archived()
        session_cookie, csrf = self.login()
        resp = self.post("/center/mark-read", session_cookie, csrf,
                         {"room": "dm:muse-phu", "seq": "1"})
        self.assertEqual(resp.status, 400)
        self.assertIn(b"room_archived", resp.body)

    def test_unread_endpoint_shape(self):
        self.seed("muse-phu", "hoi", "tin phu", "m-cccccccc2114",
                  room="dm:muse-phu")
        session_cookie, _ = self.login()
        data = self.unread_json(session_cookie)
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["total"], 1)
        self.assertFalse(data["has_mention"])
        self.assertEqual(data["rooms"], {"dm:muse-phu": 1})
        self.assertEqual(data["mention_rooms"], [])
        self.assertIn("typing", data)
        self.assertIn("typing_names", data)

    def test_pill_style_and_no_audio(self):
        # Viên "N tin mới": nền #421d24, chữ trắng, bo 16px,
        # đệm 8px 16px, chữ 14px, weight 500.
        self.assertIn("#421d24", APP_CSS)
        pill = APP_CSS.split(".center-new-pill", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 16px", pill)
        self.assertIn("padding: 8px 16px", pill)
        self.assertIn("font-size: 14px", pill)
        self.assertIn("font-weight: 500", pill)
        self.assertIn("color: #fff", pill)
        # Chỉ viên đếm lặng lẽ, không âm thanh.
        self.assertNotIn("new Audio", APP_JS)
        self.assertNotIn(".play()", APP_JS)

    def test_scroll_bottom_button_only_scrolls(self):
        # Nút "Về cuối" 44px × 44px; handler chỉ cuộn, không
        # đánh dấu đã đọc (Q-V060-08).
        btn = APP_CSS.split(".center-scroll-bottom", 1)[1].split("}", 1)[0]
        self.assertIn("width: 44px", btn)
        self.assertIn("height: 44px", btn)
        handler = APP_JS.split(
            'toBottomBtn.addEventListener("click"', 1)[1]
        handler = handler.split("});", 1)[0]
        self.assertIn("box.scrollTop = box.scrollHeight;", handler)
        self.assertNotIn("markRead", handler)


# ---------------------------------------------------------------------------
# Enter gửi tin + ô "Gửi cho:" — tiêu chí PLAN 15, 21–22 + D8
# ---------------------------------------------------------------------------

class TestEnterAndSendTo(T3iUiFixture):
    def test_enter_js(self):
        # Enter gửi, Shift+Enter xuống dòng, bỏ qua khi isComposing;
        # ô trống + không file thì không gửi; chỉ có file thì gửi.
        self.assertIn('chatBody.addEventListener("keydown"', APP_JS)
        self.assertIn("if (event.isComposing) { return; }", APP_JS)
        self.assertIn('if (event.key !== "Enter" || event.shiftKey)'
                      ' { return; }', APP_JS)
        self.assertIn("if (!chatBody.value.trim() && !hasFile)"
                      " { return; }", APP_JS)
        self.assertIn("form.requestSubmit();", APP_JS)
        # Không có nút "Xuống dòng" trên điện thoại (Q-V060-13).
        self.assertNotIn("Xuống dòng", APP_JS)
        self.assertNotIn("Xuống dòng", APP_CSS)

    def test_send_to_selector(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        # T3l (schema v4): ô "Gửi cho:" cho chọn NHIỀU tài khoản.
        # (Gop UI checkbox, 22/11) Nhom checkbox thay <select multiple>.
        self.assertIn('type="checkbox" name="to_account"', html)
        self.assertIn('value="muse-phu"', html)
        self.assertIn("Muse phụ", html)
        self.assertNotIn('id="center-hoi-to"', html)

    def test_send_to_one_account(self):
        session_cookie, csrf = self.login()
        resp = self.post("/center/messages", session_cookie, csrf, {
            "body": "hỏi riêng phụ", "room": "chung",
            "to_account": "muse-phu"})
        self.assertEqual(resp.status, 302)
        events = self.center.read_events()
        self.assertEqual(len(events), 1)
        # T3l (schema v4): to_account LUÔN lưu dạng danh sách, kể cả 1 người.
        self.assertEqual(events[0]["record"]["to_account"], ["muse-phu"])
        # Tất cả (để trống) -> to_account None.
        resp = self.post("/center/messages", session_cookie, csrf, {
            "body": "hỏi tất cả", "room": "chung", "to_account": ""})
        self.assertEqual(resp.status, 302)
        events = self.center.read_events()
        self.assertIsNone(events[1]["record"]["to_account"])
        # Tài khoản không tồn tại / "owner" -> lỗi 400, không ghi tin.
        resp = self.post("/center/messages", session_cookie, csrf,
                         {"body": "thử", "room": "chung",
                          "to_account": "muse-khong-co"})
        self.assertEqual(resp.status, 400)
        self.assertIn("Tài khoản nhận không tồn tại",
                      resp.body.decode())
        resp = self.post("/center/messages", session_cookie, csrf,
                         {"body": "thử", "room": "chung",
                          "to_account": "owner"})
        self.assertEqual(resp.status, 400)
        self.assertIn("Không gửi cho chính chủ nhân được",
                      resp.body.decode())
        self.assertEqual(len(self.center.read_events()), 2)

    def test_empty_body_no_file_rejected(self):
        # Enter khi ô nhập trống (không file) -> không gửi gì.
        session_cookie, csrf = self.login()
        resp = self.post("/center/messages", session_cookie, csrf,
                         {"body": "   ", "room": "chung"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.center.read_events(), [])

    def test_file_only_sends(self):
        # Chỉ có file đính kèm (ô nhập trống) -> gửi được.
        session_cookie, csrf = self.login()
        resp = self.post_multipart(
            "/center/messages", session_cookie, csrf,
            [("body", ""), ("room", "chung")],
            [("file", "ket-qua.pdf", b"noi dung gia")])
        self.assertEqual(resp.status, 302, resp.body[:200])
        events = self.center.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["record"]["kind"], "hoi")
        file_meta = events[0]["record"]["file"]
        self.assertIsInstance(file_meta, dict)
        self.assertTrue(file_meta["file_id"].startswith("f-"))
        self.assertEqual(file_meta["file_name"], "ket-qua.pdf")


# ---------------------------------------------------------------------------
# Typing — tiêu chí PLAN 24
# ---------------------------------------------------------------------------

class TestTyping(T3iUiFixture):
    def _write_typing(self, account_id, room, started_at):
        path = self.root / "data" / "center" / "typing.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({account_id: {room: {
            "started_at": started_at}}}), encoding="utf-8")

    def test_typing_shown_and_expires_90s(self):
        # TTL 90 giây: còn hạn -> hiện; quá hạn -> biến mất.
        self.assertTrue(center_store.typing_entry_is_live(
            {"started_at": self.now - 89}, self.now))
        self.assertFalse(center_store.typing_entry_is_live(
            {"started_at": self.now - 91}, self.now))
        self._write_typing("muse-phu", "chung", self.now - 10)
        session_cookie, _ = self.login()
        data = self.unread_json(session_cookie)
        self.assertEqual(data["typing"], {"chung": ["muse-phu"]})
        self.assertEqual(data["typing_names"],
                         {"chung": ["Muse phụ"]})
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn("đang trả lời", html)
        self.assertIn('data-room-typing="chung"', html)
        # Quá 90 giây -> không còn.
        self._write_typing("muse-phu", "chung", self.now - 91)
        data = self.unread_json(session_cookie)
        self.assertEqual(data["typing"], {})
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertNotIn("đang trả lời", html)


# ---------------------------------------------------------------------------
# Trang chi tiết việc tĩnh — tiêu chí PLAN 8, 20 + Q-V060-09
# ---------------------------------------------------------------------------

class TestTaskDetailStatic(T3iUiFixture):
    def _create_task(self, session_cookie, csrf):
        due = time.strftime("%Y-%m-%dT%H:%M",
                            time.localtime(self.now + 86400))
        resp = self.post("/center/tasks", session_cookie, csrf, {
            "title": "Việc kiểm tra", "body": "làm app",
            "kind": "app", "assigned_to": "muse-phu", "due_at": due})
        self.assertEqual(resp.status, 302)
        tasks = self.center.list_tasks()
        self.assertEqual(len(tasks), 1)
        return next(iter(tasks.values()))["task_id"]

    def test_task_detail_keeps_chat_static(self):
        session_cookie, csrf = self.login()
        task_id = self._create_task(session_cookie, csrf)
        self.seed("muse-phu", "bao-cao", "đã xong phần 1",
                  "m-cccccccc2121", task_id=task_id)
        html = self.get("/center/tasks/%s" % task_id,
                        session_cookie).body.decode()
        self.assertIn("đã xong phần 1", html)
        # Cùng cơ chế cuộn nhưng tĩnh: không fragment, không viên,
        # không nút, không ô gửi.
        self.assertIn("center-chat-box--static", html)
        self.assertNotIn("data-center-chat-box", html)
        self.assertNotIn('id="center-new-pill"', html)
        self.assertNotIn('id="center-scroll-bottom"', html)
        self.assertNotIn('id="center-hoi-body"', html)
        self.assertNotIn("data-fragment-url=\"/fragments/center-chat",
                         html)

    def test_detail_page_static_box_unchanged(self):
        # B2 (21/11): hop tinh trang chi tiet viec KHONG DOI theo B2
        # — van .center-chat-box--static, khong vien/nut/data
        # (quy tac calc chi nam trong .center-chat-section, xem
        # THIET-KE-KHUNG-CHAT-B-21-11.md §3.3/§3.7).
        session_cookie, csrf = self.login()
        task_id = self._create_task(session_cookie, csrf)
        html = self.get("/center/tasks/%s" % task_id,
                        session_cookie).body.decode()
        self.assertIn("center-chat-box--static", html)
        self.assertNotIn('id="center-new-pill"', html)
        self.assertNotIn('id="center-scroll-bottom"', html)
        self.assertNotIn("data-center-chat-box", html)

    def test_task_detail_view_does_not_change_watermark(self):
        # Xem trang chi tiết việc -> mốc đã đọc mọi phòng không đổi.
        self.seed("muse-phu", "hoi", "tin phu", "m-cccccccc2122",
                  room="dm:muse-phu")
        session_cookie, csrf = self.login()
        task_id = self._create_task(session_cookie, csrf)
        before = self.center.get_read_watermark("dm:muse-phu")
        resp = self.get("/center/tasks/%s" % task_id, session_cookie)
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            self.center.get_read_watermark("dm:muse-phu"), before)
        self.assertEqual(before, 0)


# ---------------------------------------------------------------------------
# Cảnh báo chat tắc — tiêu chí PLAN 8 (D4)
# ---------------------------------------------------------------------------

class TestStuckAlerts(T3iUiFixture):
    def test_center_chat_stuck_alerts_helper(self):
        # 2 tin chờ > 3 phút -> 1 mục; chủ nhân trả lời -> hết.
        self.center.append_message(
            "muse-phu", "hoi", None, None, "cho tra loi 1",
            "m-cccccccc2131", room="chung")
        self.center.append_message(
            "muse-phu", "hoi", None, None, "cho tra loi 2",
            "m-cccccccc2132", room="chung")
        self.backdate_all_messages(200)
        items = ui_store.center_chat_stuck_alerts(self.root,
                                                  now=self.now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["room"], "chung")
        self.assertEqual(items[0]["waiting_count"], 2)
        self.assertEqual(items[0]["level"], "cần xem")
        self.assertGreaterEqual(items[0]["oldest_wait_seconds"], 180)
        # Chủ nhân trả lời -> không còn tắc.
        self.center.append_message(
            "owner", "hoi", None, None, "đã trả lời",
            "m-cccccccc2133", room="chung")
        self.assertEqual(ui_store.center_chat_stuck_alerts(
            self.root, now=self.now), [])

    def test_stuck_alerts_ignore_hub_and_fresh(self):
        # Tin hub (source="hub") không tính; tin mới < 3 phút và
        # ít hơn 3 tin cũng không tính.
        self.center.append_message(
            "muse-phu", "hoi", None, None, "tin hub",
            "m-cccccccc2134", room="chung", source="hub")
        self.backdate_all_messages(500)
        self.center.append_message(
            "muse-phu", "hoi", None, None, "tin moi",
            "m-cccccccc2135", room="chung")
        self.assertEqual(ui_store.center_chat_stuck_alerts(
            self.root, now=self.now), [])

    def test_alerts_tab_shows_stuck_line(self):
        self.center.append_message(
            "muse-phu", "hoi", None, None, "cho tra loi",
            "m-cccccccc2136", room="chung")
        self.center.append_message(
            "muse-phu", "hoi", None, None, "cho tra loi 2",
            "m-cccccccc2137", room="chung")
        self.center.append_message(
            "muse-phu", "hoi", None, None, "cho tra loi 3",
            "m-cccccccc2138", room="chung")
        self.backdate_all_messages(400)
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["alerts"]}).body.decode()
        self.assertIn("Trung tâm — chat tắc", html)
        self.assertIn("Chat tắc: phòng", html)
        self.assertIn("3 tin chờ trả lời", html)
        self.assertIn("mức cần xem", html)
        self.assertIn("/?tab=chat&amp;room=chung", html)


# ---------------------------------------------------------------------------
# Q-T3-05 cũ đã thay hết ở 3 file — tiêu chí PLAN 23
# ---------------------------------------------------------------------------

class TestQOldReplaced(unittest.TestCase):
    OLD = "khung chat đặt dưới danh sách việc trong tab Trung tâm"

    def test_q_t3_05_replaced_in_three_docs(self):
        docs = {
            "DESIGN §8.2": DESIGN_ROOT / "DESIGN-TRUNG-TAM.md",
            "doc 10 §7.4": (DESIGN_ROOT / "design-docs" /
                            "10-trung-tam-kiem-soat-giao-viec.md"),
            "SPEC TT11": DESIGN_ROOT / "SPEC-TRUNG-TAM.md",
        }
        for label, path in docs.items():
            text = path.read_text(encoding="utf-8")
            # Câu quyết cũ chỉ còn trong câu ghi "quyết cũ ... bị
            # thay" — không còn là chỉ dẫn hiện hành.
            for line in text.splitlines():
                if self.OLD in line:
                    self.assertIn("quyết cũ", line,
                                  "%s: câu cũ còn sót ngoài câu "
                                  "'quyết cũ ... bị thay': %s"
                                  % (label, line[:120]))
            # Câu quyết mới phải có mặt (tab Chat riêng + ĐỔI).
            self.assertTrue("tab Chat riêng" in text
                            or 'tab "Chat" riêng' in text, label)
            self.assertTrue("ĐỔI Q-T3-05" in text
                            or "Q-T3-05 ĐÃ ĐỔI" in text, label)


if __name__ == "__main__":
    unittest.main()
