#!/usr/bin/env python3
"""Test mục "Link chung" trong tab Chat (thiết kế v0.2.1, 21/11/2026).

unittest, stdlib-only, UI qua UiApp trực tiếp. Mọi giá trị trong test
đều là mẫu giả, không secret thật, không nội dung tin thật.

Seam theo thiết kế §10: S-01 (store file), S-02 (nhận diện kind),
S-03 (POST add), S-04 (POST delete), S-05 (cờ bool + form->bool),
S-06 (trạng thái thu gọn, test vòng tròn), S-07 (target=_blank +
CSP), S-08 (CSS + label + aria-expanded + 44px), S-09 (audit),
S-11 (chuẩn hóa URL, 8 cặp), S-12 ("Về mặc định" reset khóa trạng
thái), S-13 (nhóm 7 + mục lục /settings). S-10 là việc đóng gói,
không có test code.
"""

import json
import multiprocessing
import os
import re
import shutil
import stat
import subprocess
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


def _add_link_in_subprocess(root_str, url):
    """Worker cho test ghi dong thoi (C-NS-03, S-01): chay trong
    tien trinh con, them 1 link qua dung duong store nhu UI that
    (flock rieng + tmp+rename). Chi dung voi fork (Linux)."""
    ui_store.add_shared_link(Path(root_str), url, "")

PASSWORD = "TEST-FAKE-PASSWORD"


class SharedLinksFixture(unittest.TestCase):
    """Fixture UI tối thiểu + 2 tài khoản trung tâm tham gia."""

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
        (self.root / "ui-config" / "accounts.json").write_text(
            json.dumps({"version": 1, "accounts": {
                "muse-chinh": {"display_name": "Muse chính",
                               "center_participating": True},
                "muse-phu": {"display_name": "Muse phụ",
                             "center_participating": True}}}),
            encoding="utf-8")
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

    # -- request helpers (theo mẫu test_center_t3i.py) -------------------
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

    def chat_tab(self, session_cookie, room="chung"):
        resp = self.get("/", session_cookie,
                        query={"tab": ["chat"], "room": [room]})
        self.assertEqual(resp.status, 200, resp.body[:200])
        return resp.body.decode("utf-8")

    def add_link(self, session_cookie, csrf_token, url, title="",
                 room="chung"):
        return self.post("/api/shared-links/add", session_cookie,
                         csrf_token,
                         {"url": url, "title": title, "room": room})

    def audit_lines(self):
        path = self.root / "ui-config" / "audit.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows


# ---------------------------------------------------------------------------
# S-01: store file ui-config/shared_links.json
# ---------------------------------------------------------------------------
class TestSharedLinksStore(SharedLinksFixture):
    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(ui_store.load_shared_links(self.root), [])

    def test_load_corrupt_file_returns_empty_list(self):
        (self.root / "ui-config" / "shared_links.json").write_text(
            "{hong", encoding="utf-8")
        self.assertEqual(ui_store.load_shared_links(self.root), [])

    def test_load_drops_hand_written_javascript_url(self):
        # (SUA THEO AUDIT B-N01, 21/11) File ghi tay co the chua
        # scheme la; doc lai phai bo muc do, chi giu http(s).
        (self.root / "ui-config" / "shared_links.json").write_text(
            json.dumps({"version": 1, "links": [
                {"id": "l-000000000001", "url": "javascript:alert(9)",
                 "title": "X", "kind": "other", "added_by": "owner",
                 "added_at": 1},
                {"id": "l-000000000002",
                 "url": "https://example.com/a", "title": "A",
                 "kind": "other", "added_by": "owner", "added_at": 1},
            ]}), encoding="utf-8")
        links = ui_store.load_shared_links(self.root)
        self.assertEqual([item["url"] for item in links],
                         ["https://example.com/a"])

    def test_add_roundtrip_newest_first(self):
        link1, err1 = ui_store.add_shared_link(
            self.root, "https://example.com/a", "A")
        link2, err2 = ui_store.add_shared_link(
            self.root, "https://example.com/b", "B")
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        links = ui_store.load_shared_links(self.root)
        self.assertEqual([item["id"] for item in links],
                         [link2["id"], link1["id"]])

    def test_add_writes_file_mode_600(self):
        ui_store.add_shared_link(self.root, "https://example.com/a", "A")
        mode = stat.S_IMODE(os.stat(
            self.root / "ui-config" / "shared_links.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_add_id_is_l_plus_12_hex(self):
        link, _err = ui_store.add_shared_link(
            self.root, "https://example.com/a", "A")
        self.assertRegex(link["id"], r"^l-[0-9a-f]{12}$")

    def test_add_empty_title_defaults_to_kind_name(self):
        link, _err = ui_store.add_shared_link(
            self.root,
            "https://docs.google.com/document/d/ABCdef123/edit", "")
        self.assertEqual(link["title"], "Google Docs")
        self.assertEqual(link["kind"], "docs")

    def test_add_empty_title_unknown_domain_uses_domain(self):
        link, _err = ui_store.add_shared_link(
            self.root, "https://example.com/x", "")
        self.assertEqual(link["title"], "example.com")
        self.assertEqual(link["kind"], "other")

    def test_add_title_longer_than_200_is_truncated_not_rejected(self):
        link, err = ui_store.add_shared_link(
            self.root, "https://example.com/x", "T" * 201)
        self.assertIsNone(err)
        self.assertEqual(len(link["title"]), 200)

    def test_add_rejects_url_without_http_scheme(self):
        _link, err = ui_store.add_shared_link(
            self.root, "docs.google.com/document/d/ABC/edit", "")
        self.assertEqual(
            err, "Dán link bắt đầu bằng http:// hoặc https://")

    def test_add_rejects_url_with_inner_whitespace(self):
        _link, err = ui_store.add_shared_link(
            self.root, "https://example.com/a b", "")
        self.assertIsNotNone(err)
        self.assertEqual(ui_store.load_shared_links(self.root), [])

    def test_add_rejects_url_over_2000_chars(self):
        _link, err = ui_store.add_shared_link(
            self.root, "https://example.com/" + "x" * 2000, "")
        self.assertIsNotNone(err)
        self.assertEqual(ui_store.load_shared_links(self.root), [])

    def test_add_limit_50(self):
        for index in range(50):
            _link, err = ui_store.add_shared_link(
                self.root, "https://example.com/%d" % index, "")
            self.assertIsNone(err)
        _link, err = ui_store.add_shared_link(
            self.root, "https://example.com/51", "")
        self.assertEqual(
            err, "Đã đủ 50 link — xóa bớt 1 link rồi thêm lại.")
        self.assertEqual(len(ui_store.load_shared_links(self.root)), 50)

    def test_delete_removes_link(self):
        link, _err = ui_store.add_shared_link(
            self.root, "https://example.com/a", "A")
        deleted, err = ui_store.delete_shared_link(self.root, link["id"])
        self.assertIsNone(err)
        self.assertEqual(deleted["id"], link["id"])
        self.assertEqual(ui_store.load_shared_links(self.root), [])

    def test_delete_unknown_id_returns_error(self):
        _deleted, err = ui_store.delete_shared_link(
            self.root, "l-000000000000")
        self.assertEqual(err, "Link không còn trong danh sách.")

    def test_delete_bad_id_format_returns_error(self):
        _deleted, err = ui_store.delete_shared_link(self.root, "xxx")
        self.assertEqual(err, "Link không còn trong danh sách.")

    def test_two_processes_adding_at_once_keep_both_links(self):
        # (SUA THEO AUDIT C-NS-03, 21/11 — S-01: "2 tien trinh
        # ghi cung luc khong mat link", nho flock rieng +
        # tmp+rename trong add_shared_link.)
        ctx = multiprocessing.get_context("fork")
        first = ctx.Process(
            target=_add_link_in_subprocess,
            args=(str(self.root), "https://example.com/a"))
        second = ctx.Process(
            target=_add_link_in_subprocess,
            args=(str(self.root), "https://example.com/b"))
        first.start()
        second.start()
        first.join(30)
        second.join(30)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        links = ui_store.load_shared_links(self.root)
        self.assertEqual({item["url"] for item in links},
                         {"https://example.com/a",
                          "https://example.com/b"})
        # File van doc duoc (JSON hop le, khong rac do ghi de nhau).
        raw = json.loads((self.root / "ui-config"
                          / "shared_links.json").read_text(
                              encoding="utf-8"))
        self.assertEqual(len(raw["links"]), 2)


# ---------------------------------------------------------------------------
# S-02: nhận diện kind theo bảng §4.1
# ---------------------------------------------------------------------------
class TestDetectSharedLinkKind(SharedLinksFixture):
    def test_drive_file(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/file/d/1AbCdef/view?usp=sharing"),
            "drive")

    def test_drive_file_u_account(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/file/u/2/d/1AbCdef/view"), "drive")

    def test_drive_folder(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/drive/folders/1AbCdef"), "drive")

    def test_drive_folder_u_account(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/drive/u/0/folders/1AbCdef"),
            "drive")

    def test_drive_open(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/open?id=1AbCdef"), "drive")

    def test_drive_open_u_account(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/u/3/open?id=1AbCdef"), "drive")

    def test_drive_embeddedfolderview(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/embeddedfolderview?id=1AbCdef"),
            "drive")

    def test_drive_my_drive(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/drive/u/0/my-drive"), "drive")

    def test_docs(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://docs.google.com/document/d/1AbCdef/edit"), "docs")

    def test_docs_u_account(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://docs.google.com/document/u/5/d/1AbCdef/edit"),
            "docs")

    def test_sheets_with_gid_fragment(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://docs.google.com/spreadsheets/d/1AbCdef/edit#gid=0"),
            "sheets")

    def test_sheets_u_account(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://docs.google.com/spreadsheets/u/2/d/1AbCdef/edit"),
            "sheets")

    def test_slides(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://docs.google.com/presentation/d/1AbCdef/edit"),
            "slides")

    def test_slides_u_account(self):
        # (SUA THEO AUDIT C-NS-02, 21/11 — N-02: /u/<N>/ o ca 4 ho)
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://docs.google.com/presentation/u/2/d/1AbCdef/edit"),
            "slides")

    def test_uppercase_scheme_and_host_still_detected(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "HTTPS://DRIVE.GOOGLE.COM/file/d/1AbCdef/view"), "drive")

    def test_shortened_link_is_other(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://bit.ly/abc123"), "other")

    def test_resourcekey_still_detected(self):
        self.assertEqual(ui_store.detect_shared_link_kind(
            "https://drive.google.com/file/d/1AbCdef/view"
            "?usp=sharing&resourcekey=0-abc"), "drive")


# ---------------------------------------------------------------------------
# S-11: chuẩn hóa URL chống trùng — 8 cặp bảng §3.3
# ---------------------------------------------------------------------------
class TestNormalizeSharedLinkUrl(SharedLinksFixture):
    BASE_DOC = "https://docs.google.com/document/d/IDa/edit"
    BASE_FILE = "https://drive.google.com/file/d/ID/view"

    def test_trailing_slash_is_duplicate(self):
        self.assertEqual(
            ui_store.normalize_shared_link_url(self.BASE_DOC + "/"),
            ui_store.normalize_shared_link_url(self.BASE_DOC))
        # (SUA THEO AUDIT C-NS-01, 21/11) Ky vong literal theo
        # bang 8 cap §3.3 — khong chi so f(x) == f(y).
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "https://docs.google.com/document/d/IDa/edit/"),
            "https://docs.google.com/document/d/IDa/edit")

    def test_http_vs_https_is_duplicate(self):
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                self.BASE_DOC.replace("https://", "http://")),
            ui_store.normalize_shared_link_url(self.BASE_DOC))
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "http://docs.google.com/document/d/IDa/edit"),
            "https://docs.google.com/document/d/IDa/edit")

    def test_slash_before_query_is_duplicate(self):
        first = self.BASE_FILE + "?usp=sharing"
        second = self.BASE_FILE + "/?usp=sharing"
        self.assertEqual(ui_store.normalize_shared_link_url(first),
                         ui_store.normalize_shared_link_url(second))
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "https://drive.google.com/file/d/ID/view/?usp=sharing"),
            "https://drive.google.com/file/d/ID/view?usp=sharing")

    def test_uppercase_scheme_host_is_duplicate(self):
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "HTTPS://DRIVE.GOOGLE.COM/file/d/ID/view"),
            ui_store.normalize_shared_link_url(
                "https://drive.google.com/file/d/ID/view"))
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "HTTPS://DRIVE.GOOGLE.COM/file/d/ID/view"),
            "https://drive.google.com/file/d/ID/view")

    def test_www_prefix_is_duplicate(self):
        self.assertEqual(
            ui_store.normalize_shared_link_url("https://www.example.com/x"),
            ui_store.normalize_shared_link_url("https://example.com/x"))
        self.assertEqual(
            ui_store.normalize_shared_link_url("https://www.example.com/x"),
            "https://example.com/x")

    def test_default_port_is_duplicate(self):
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "https://example.com:443/x"),
            ui_store.normalize_shared_link_url("https://example.com/x"))
        self.assertEqual(
            ui_store.normalize_shared_link_url(
                "https://example.com:443/x"),
            "https://example.com/x")
        # (GHI CHU C-GC-06, lam kem NS-01, 21/11 — buoc 4: bo
        # :80 voi http nhu :443 voi https.)
        self.assertEqual(
            ui_store.normalize_shared_link_url("http://example.com:80/x"),
            "https://example.com/x")

    def test_different_fragment_is_not_duplicate(self):
        first = ("https://docs.google.com/spreadsheets/d/ID/edit#gid=0")
        second = ("https://docs.google.com/spreadsheets/d/ID/edit#gid=5")
        self.assertNotEqual(ui_store.normalize_shared_link_url(first),
                            ui_store.normalize_shared_link_url(second))

    def test_different_file_id_is_not_duplicate(self):
        first = "https://docs.google.com/document/d/IDa/edit"
        second = "https://docs.google.com/document/d/IDb/edit"
        self.assertNotEqual(ui_store.normalize_shared_link_url(first),
                            ui_store.normalize_shared_link_url(second))

    def test_stored_url_keeps_original_not_normalized(self):
        link, _err = ui_store.add_shared_link(
            self.root, "http://example.com/x/", "")
        self.assertEqual(link["url"], "http://example.com/x/")


# ---------------------------------------------------------------------------
# S-03/S-04: route thêm/xóa
# ---------------------------------------------------------------------------
class TestSharedLinksRoutes(SharedLinksFixture):
    def test_add_success_redirects_keeping_room(self):
        session_cookie, csrf_token = self.login()
        resp = self.add_link(session_cookie, csrf_token,
                             "https://example.com/a", "Link A",
                             room="dm:muse-phu")
        self.assertEqual(resp.status, 302, resp.body[:200])
        self.assertEqual(resp.headers["Location"],
                         "/?tab=chat&room=dm%3Amuse-phu#link-chung")

    def test_add_success_link_shown_first_in_chat_tab(self):
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://example.com/a", "Link A")
        self.add_link(session_cookie, csrf_token,
                      "https://example.com/b", "Link B")
        html = self.chat_tab(session_cookie)
        self.assertIn("Link B", html)
        self.assertLess(html.index("Link B"), html.index("Link A"))

    def test_add_bad_scheme_returns_400_and_keeps_values(self):
        session_cookie, csrf_token = self.login()
        resp = self.add_link(session_cookie, csrf_token,
                             "example.com/a", "Tiêu đề giữ lại")
        self.assertEqual(resp.status, 400, resp.body[:200])
        html = resp.body.decode("utf-8")
        self.assertIn("Dán link bắt đầu bằng http:// hoặc https://",
                      html)
        self.assertIn("Tiêu đề giữ lại", html)
        self.assertIn("example.com/a", html)
        self.assertIn('aria-invalid="true"', html)

    def test_add_duplicate_returns_400(self):
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://example.com/a", "A")
        resp = self.add_link(session_cookie, csrf_token,
                             "https://example.com/a/", "A nữa")
        self.assertEqual(resp.status, 400, resp.body[:200])
        self.assertIn("Link này đã có trong danh sách rồi.",
                      resp.body.decode("utf-8"))

    def test_add_error_renders_room_from_form_not_last_saved_room(
            self):
        # (SUA THEO AUDIT A-NS-02, 21/11 — D-LC-06/N-10) Mo
        # phong 2 tab: tab mo sau cung dang o phong "chung"
        # (phong da luu); tab kia (phong dm:muse-phu) gui them
        # loi -> render lai phai o dung phong dm:muse-phu.
        session_cookie, csrf_token = self.login()
        self.chat_tab(session_cookie, room="chung")
        resp = self.add_link(session_cookie, csrf_token,
                             "example.com/a", "T", room="dm:muse-phu")
        self.assertEqual(resp.status, 400, resp.body[:200])
        html = resp.body.decode("utf-8")
        self.assertIn("Dán link bắt đầu bằng http:// hoặc https://",
                      html)
        self.assertIn("Phòng đang mở: Riêng: Muse phụ", html)
        self.assertIn(
            '<input type="hidden" name="room" value="dm:muse-phu">',
            html)

    def test_add_over_50_returns_400(self):
        session_cookie, csrf_token = self.login()
        for index in range(50):
            self.add_link(session_cookie, csrf_token,
                          "https://example.com/%d" % index, "")
        resp = self.add_link(session_cookie, csrf_token,
                             "https://example.com/51", "")
        self.assertEqual(resp.status, 400, resp.body[:200])
        self.assertIn("Đã đủ 50 link",
                      resp.body.decode("utf-8"))

    def test_add_without_csrf_is_rejected(self):
        session_cookie, _csrf = self.login()
        resp = self.post_no_csrf(
            "/api/shared-links/add", session_cookie,
            {"url": "https://example.com/a", "title": "",
             "room": "chung"})
        self.assertEqual(resp.status, 403, resp.body[:200])

    def test_add_get_is_not_allowed(self):
        session_cookie, _csrf = self.login()
        resp = self.get("/api/shared-links/add", session_cookie)
        self.assertEqual(resp.status, 405, resp.body[:200])

    def test_delete_success_redirects_and_notice_shown_once(self):
        session_cookie, csrf_token = self.login()
        link, _err = ui_store.add_shared_link(
            self.root, "https://example.com/a", "Link A")
        resp = self.post("/api/shared-links/delete", session_cookie,
                         csrf_token,
                         {"link_id": link["id"], "room": "dm:muse-phu"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        self.assertEqual(resp.headers["Location"],
                         "/?tab=chat&room=dm%3Amuse-phu#link-chung")
        first = self.chat_tab(session_cookie, room="dm:muse-phu")
        self.assertIn("Đã xóa link", first)
        self.assertIn("Link A", first)
        second = self.chat_tab(session_cookie, room="dm:muse-phu")
        self.assertNotIn("Đã xóa link", second)

    def test_delete_unknown_id_returns_400(self):
        session_cookie, csrf_token = self.login()
        resp = self.post("/api/shared-links/delete", session_cookie,
                         csrf_token,
                         {"link_id": "l-000000000000", "room": "chung"})
        self.assertEqual(resp.status, 400, resp.body[:200])
        self.assertIn("Link không còn trong danh sách.",
                      resp.body.decode("utf-8"))

    def test_delete_without_csrf_is_rejected(self):
        session_cookie, _csrf = self.login()
        resp = self.post_no_csrf(
            "/api/shared-links/delete", session_cookie,
            {"link_id": "l-000000000000", "room": "chung"})
        self.assertEqual(resp.status, 403, resp.body[:200])


# ---------------------------------------------------------------------------
# S-05: cờ shared_links_open_to_all (bool + form "1"/"0")
# ---------------------------------------------------------------------------
class TestSharedLinksFlag(SharedLinksFixture):
    def test_validate_accepts_real_bools(self):
        clean, err = center_store.validate_center_config_updates(
            {"shared_links_open_to_all": True})
        self.assertIsNone(err)
        self.assertEqual(clean, {"shared_links_open_to_all": True})

    def test_validate_rejects_string_one(self):
        _clean, err = center_store.validate_center_config_updates(
            {"shared_links_open_to_all": "1"})
        self.assertEqual(err, "giá trị không hợp lệ")

    def test_validate_rejects_int_one(self):
        _clean, err = center_store.validate_center_config_updates(
            {"shared_links_open_to_all": 1})
        self.assertEqual(err, "giá trị không hợp lệ")

    def test_load_center_config_defaults_flag_false(self):
        config = center_store.load_center_config(self.root)
        self.assertIs(config["shared_links_open_to_all"], False)

    def test_settings_form_1_saves_true(self):
        session_cookie, csrf_token = self.login()
        resp = self.post("/api/settings/center", session_cookie,
                         csrf_token, {"shared_links_open_to_all": "1"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        self.assertEqual(resp.headers["Location"],
                         "/settings#nhom-link-chung")
        config = center_store.load_center_config(self.root)
        self.assertIs(config["shared_links_open_to_all"], True)

    def test_settings_form_0_saves_false(self):
        session_cookie, csrf_token = self.login()
        center_store.save_center_config(
            self.root, {"shared_links_open_to_all": True})
        resp = self.post("/api/settings/center", session_cookie,
                         csrf_token, {"shared_links_open_to_all": "0"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        config = center_store.load_center_config(self.root)
        self.assertIs(config["shared_links_open_to_all"], False)

    def test_settings_form_on_is_rejected_without_writing(self):
        session_cookie, csrf_token = self.login()
        resp = self.post("/api/settings/center", session_cookie,
                         csrf_token, {"shared_links_open_to_all": "on"})
        self.assertEqual(resp.status, 400, resp.body[:200])
        config = center_store.load_center_config(self.root)
        self.assertIs(config["shared_links_open_to_all"], False)

    def test_settings_form_yes_is_rejected_without_writing(self):
        # (SUA THEO AUDIT C-NS-06, 21/11 — S-05: "on"/"yes"/
        # chuoi la -> 400, khong ghi gi.)
        session_cookie, csrf_token = self.login()
        resp = self.post("/api/settings/center", session_cookie,
                         csrf_token, {"shared_links_open_to_all": "yes"})
        self.assertEqual(resp.status, 400, resp.body[:200])
        config = center_store.load_center_config(self.root)
        self.assertIs(config["shared_links_open_to_all"], False)

    def test_settings_change_writes_one_audit_line(self):
        session_cookie, csrf_token = self.login()
        self.post("/api/settings/center", session_cookie,
                  csrf_token, {"shared_links_open_to_all": "1"})
        actions = [row["action"] for row in self.audit_lines()]
        self.assertIn("center_settings_shared_links_open_to_all",
                      actions)


# ---------------------------------------------------------------------------
# S-06: trạng thái thu gọn — test vòng tròn
# ---------------------------------------------------------------------------
class TestSharedLinksState(SharedLinksFixture):
    def test_state_defaults_to_unset(self):
        self.assertEqual(
            ui_store.load_shared_links_state(self.root), "unset")

    def test_set_open_persists_and_renders(self):
        session_cookie, csrf_token = self.login()
        resp = self.post("/api/ui-prefs", session_cookie, csrf_token,
                         {"action": "set-shared-links-state",
                          "state": "open",
                          "next": "/?tab=chat&room=chung#link-chung"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        self.assertEqual(
            ui_store.load_shared_links_state(self.root), "open")
        html = self.chat_tab(session_cookie)
        self.assertIn('data-state="open"', html)

    def test_set_closed_roundtrip_survives_reload(self):
        session_cookie, csrf_token = self.login()
        self.post("/api/ui-prefs", session_cookie, csrf_token,
                  {"action": "set-shared-links-state", "state": "open",
                   "next": "/?tab=chat&room=chung#link-chung"})
        self.post("/api/ui-prefs", session_cookie, csrf_token,
                  {"action": "set-shared-links-state", "state": "closed",
                   "next": "/?tab=chat&room=chung#link-chung"})
        self.assertEqual(
            ui_store.load_shared_links_state(self.root), "closed")
        html = self.chat_tab(session_cookie)
        self.assertIn('data-state="closed"', html)

    def test_unset_renders_data_state_unset(self):
        session_cookie, _csrf = self.login()
        html = self.chat_tab(session_cookie)
        self.assertIn('data-state="unset"', html)

    def test_bad_state_returns_400_without_writing(self):
        session_cookie, csrf_token = self.login()
        resp = self.post("/api/ui-prefs", session_cookie, csrf_token,
                         {"action": "set-shared-links-state",
                          "state": "mo", "next": "/"})
        self.assertEqual(resp.status, 400, resp.body[:200])
        self.assertEqual(
            ui_store.load_shared_links_state(self.root), "unset")

    def test_toggle_writes_no_audit(self):
        session_cookie, csrf_token = self.login()
        before = len(self.audit_lines())
        self.post("/api/ui-prefs", session_cookie, csrf_token,
                  {"action": "set-shared-links-state", "state": "open",
                   "next": "/"})
        self.assertEqual(len(self.audit_lines()), before)


# ---------------------------------------------------------------------------
# S-07/S-08/S-09/S-12/S-13
# ---------------------------------------------------------------------------
class TestSharedLinksRenderAndAudit(SharedLinksFixture):
    def test_link_opens_in_new_tab_with_noopener(self):
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://docs.google.com/document/d/ABC/edit",
                      "Tài liệu")
        html = self.chat_tab(session_cookie)
        self.assertIn(
            '<a href="https://docs.google.com/document/d/ABC/edit" '
            'target="_blank" rel="noopener">Tài liệu</a>', html)

    def test_csp_has_no_frame_src(self):
        self.assertNotIn("frame-src", ui_app.CSP)

    def test_xss_title_and_url_are_escaped_in_chat_tab(self):
        # (SUA THEO AUDIT B-N02, 21/11) Luoi bat hoi quy XSS:
        # tieu de chua <script> + URL chua "/& phai duoc escape
        # khi render (probe P1 cua audit B da chung minh code
        # hien dung; test nay giu hanh vi do).
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      'https://example.com/a?x=1&y="z"',
                      "<script>alert(1)</script>")
        html = self.chat_tab(session_cookie)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&quot;", html)
        self.assertIn("&amp;", html)

    def test_add_form_has_visible_labels(self):
        session_cookie, _csrf = self.login()
        html = self.chat_tab(session_cookie)
        self.assertIn('<label for="shared-link-url">Link</label>', html)
        self.assertIn(
            '<label for="shared-link-title">Tiêu đề (không bắt buộc)'
            "</label>", html)

    def test_delete_button_has_aria_label(self):
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://example.com/a", "Link A")
        html = self.chat_tab(session_cookie)
        self.assertIn('aria-label="Xóa link Link A"', html)

    def test_toggle_button_aria_expanded_matches_state(self):
        session_cookie, csrf_token = self.login()
        self.post("/api/ui-prefs", session_cookie, csrf_token,
                  {"action": "set-shared-links-state", "state": "open",
                   "next": "/"})
        html = self.chat_tab(session_cookie)
        self.assertIn('aria-expanded="true"', html)
        self.post("/api/ui-prefs", session_cookie, csrf_token,
                  {"action": "set-shared-links-state", "state": "closed",
                   "next": "/"})
        html = self.chat_tab(session_cookie)
        self.assertIn('aria-expanded="false"', html)

    def test_unset_aria_expanded_fixed_by_js_on_desktop(self):
        # (SUA THEO AUDIT A-NS-01, 21/11 — cach (b)) May chu chi
        # render aria-expanded="false" khi unset (khong biet co
        # man); APP_JS phai co khoi matchMedia sua thanh "true"
        # khi desktop (>= 721px, nguoc media query mobile 720px).
        # (SUA THEO GIA LAP LC-C01, 21/11 — G-CNTC) Selector JS
        # viet bang NHAY DON ('...') chu khong dung \" trong chuoi
        # Python """: \" bi Python rut thanh " nen JS phuc vu ra
        # trinh duyet loi cu phap, toan bo app.js chet (xem test
        # node --check ben duoi). Hanh vi khong doi, chi doi cach
        # viet chuoi.
        self.assertIn('matchMedia("(min-width: 721px)")', APP_JS)
        self.assertIn(
            "'.shared-links[data-state=\"unset\"] .shared-links-toggle'",
            APP_JS)
        self.assertIn('setAttribute("aria-expanded", "true")', APP_JS)

    @unittest.skipIf(shutil.which("node") is None,
                     "node khong co san — bo qua cong node --check")
    def test_app_js_parses_with_node_check(self):
        # (SUA THEO GIA LAP LC-C01, 21/11 — DX-LC-01) Cong bao ve
        # toan bo APP_JS: node --check tren GIA TRI APP_JS DA DANH
        # GIA (bien import — dung JS trinh duyet nhan duoc). BAT
        # BUOC khong kiem chuoi nguon tho trong ui_static.py:
        # chuoi tho van chua \" nen node --check tren no bao DAT
        # gia, trong khi gia tri da danh gia moi lo loi cu phap
        # that (LC-C01: \" bi Python rut thanh ", JS thanh
        # ".shared-links[data-state="unset"] ..." — SyntaxError,
        # toan bo app.js chet: polling 5 giay + vien "N tin moi"
        # + tai them tin cu + khoi sua aria-expanded deu chet
        # theo). Neu may khong co node thi test skip, khong fail.
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                        delete=False) as tmp:
            tmp.write(APP_JS)
            path = tmp.name
        try:
            proc = subprocess.run(
                ["node", "--check", path],
                capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_css_uses_only_existing_tokens(self):
        self.assertIn(".shared-links {", APP_CSS)
        self.assertIn("background: var(--surface)", APP_CSS)
        # (SUA THEO GIA LAP LC-NS-01, 21/11 — G-CNTC) 160px la
        # SUY LUAN D-LC-19; do that UC-L-10 thay hang link cao
        # 61px nen 160px chi lo ~2,6 hang — nang len 200px de lo
        # ~3,3 hang, gan dung y do ~3,5 hang cua thiet ke.
        self.assertIn("max-height: 200px", APP_CSS)
        self.assertIn('data-state="unset"', APP_CSS)
        self.assertIn("min-height: 44px", APP_CSS)
        # (SUA THEO AUDIT C-NS-05, 21/11) Truoc day lat block toi
        # het file — section CSS them sau nay co mau hex thi test
        # rot oan; nay cat toi comment section tiep theo.
        start = APP_CSS.index("/* --- Link chung")
        end = APP_CSS.find("/* ---", start + 1)
        block = APP_CSS[start:end] if end != -1 else APP_CSS[start:]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,6}\b")

    def test_audit_add_and_delete_one_line_each_and_no_full_url(self):
        # (SUA THEO AUDIT C-NS-04, 21/11) Truoc day 2 dong dau goi
        # truc tiep add/delete o tang store (khong qua route nen
        # khong sinh audit) — ket qua bo di; nay chi them/xoa qua
        # route. Test giu 1 test vi ca 2 khang dinh deu doc cung
        # 2 dong audit do (noi dung + khong lo full URL).
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://docs.google.com/document/d/XYZ/edit",
                      "Kế hoạch Q4")
        link2 = ui_store.load_shared_links(self.root)[0]
        self.post("/api/shared-links/delete", session_cookie,
                  csrf_token, {"link_id": link2["id"], "room": "chung"})
        rows = [row for row in self.audit_lines()
                if row["action"] in ("shared_link_add",
                                     "shared_link_delete")]
        self.assertEqual(
            [row["action"] for row in rows],
            ["shared_link_add", "shared_link_delete"])
        self.assertEqual(rows[0]["bot_id"], "center")
        self.assertEqual(rows[0]["before"], "")
        self.assertEqual(rows[0]["after"],
                         "Kế hoạch Q4 [docs] docs.google.com")
        self.assertEqual(rows[1]["after"], "")
        for row in rows:
            self.assertNotIn("docs.google.com/document/d/XYZ",
                             json.dumps(row, ensure_ascii=False))

    def test_reset_ui_prefs_removes_state_key(self):
        ui_store.set_shared_links_state(self.root, "open")
        ui_store.reset_ui_prefs(self.root)
        self.assertEqual(
            ui_store.load_shared_links_state(self.root), "unset")

    def test_reset_ui_prefs_keeps_theme(self):
        ui_store.set_ui_theme(self.root, "dark")
        ui_store.set_shared_links_state(self.root, "open")
        ui_store.reset_ui_prefs(self.root)
        self.assertEqual(ui_store.load_ui_theme(self.root), "dark")
        self.assertEqual(
            ui_store.load_shared_links_state(self.root), "unset")

    def test_settings_page_has_group7_and_seven_toc_links(self):
        session_cookie, _csrf = self.login()
        resp = self.get("/settings", session_cookie)
        self.assertEqual(resp.status, 200, resp.body[:200])
        html = resp.body.decode("utf-8")
        self.assertIn('<h2 id="nhom-link-chung">7. Link chung</h2>',
                      html)
        self.assertIn('<a href="#nhom-link-chung">7. Link chung</a>',
                      html)
        toc = html[html.index('<p class="toc">'):
                   html.index("</p>", html.index('<p class="toc">'))]
        self.assertEqual(toc.count("<a href="), 7)
        self.assertIn("Mọi người vào được giao diện này đều thấy nút "
                      "thêm/xóa.", html)

    def test_closed_state_hides_list_and_form(self):
        # (Sua Link chung 22/11) Them link thi gui them 1 tin vao phong
        # chung nen "Link A" van hien trong KHUNG CHAT (la tin nhan
        # thuong). Trang thai "closed" chi an DANH SACH link + FORM
        # them o cot trai — kiem tra dung 2 cho do.
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://example.com/a", "Link A")
        self.post("/api/ui-prefs", session_cookie, csrf_token,
                  {"action": "set-shared-links-state", "state": "closed",
                   "next": "/"})
        html = self.chat_tab(session_cookie)
        self.assertIn('data-state="closed"', html)
        self.assertNotIn('id="shared-link-url"', html)
        # Danh sach link khong render (khong co hang link nao).
        self.assertNotIn("shared-link-row", html)


# ---------------------------------------------------------------------------
# S-06: them link thi gui tin vao phong chung (sua Link chung 22/11)
# ---------------------------------------------------------------------------
class TestSharedLinkNotifyCenter(SharedLinksFixture):
    """(Sua Link chung 22/11 — Q-LC-FIX-01…04) Link chi luu trong
    ui-config/shared_links.json thi tai khoan Muse khong bao gio doc
    duoc (chung khong dang nhap UI, khong co duong dong bo file nay
    sang phia Muse). Them link phai gui them 1 tin vao phong chung
    de link nam trong center_messages.jsonl."""

    def center_messages(self):
        # Tin ghi qua center_store.append_message nam trong
        # data/center/events.jsonl (hub); center_messages.jsonl chi
        # la ban sao phia Muse do fetcher dong bo (fetcher khong chay
        # trong test nay) nen doc truc tiep events o day.
        path = self.root / "data" / "center" / "events.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    def test_add_posts_link_message_to_chung_room(self):
        session_cookie, csrf_token = self.login()
        resp = self.add_link(session_cookie, csrf_token,
                             "https://example.com/bao-cao-q4",
                             "Báo cáo Q4")
        self.assertEqual(resp.status, 302, resp.body[:200])
        msgs = self.center_messages()
        self.assertEqual(len(msgs), 1, msgs)
        record = msgs[0]["record"]
        self.assertEqual(record["room"], "chung")
        self.assertEqual(record["kind"], "hoi")
        self.assertEqual(record["from_account"], "owner")
        self.assertIsNone(record.get("to_account"))
        self.assertIn("https://example.com/bao-cao-q4", record["body"])
        self.assertIn("Báo cáo Q4", record["body"])

    def test_add_from_dm_room_still_posts_to_chung(self):
        # Chu nhan co the dang mo phong rieng khi them link; tin link
        # van phai vao phong chung de moi tai khoan deu doc duoc.
        session_cookie, csrf_token = self.login()
        resp = self.add_link(session_cookie, csrf_token,
                             "https://example.com/a", "Link A",
                             room="dm:muse-phu")
        self.assertEqual(resp.status, 302, resp.body[:200])
        msgs = self.center_messages()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertEqual(msgs[0]["record"]["room"], "chung")

    def test_delete_does_not_post_message(self):
        session_cookie, csrf_token = self.login()
        link, _err = ui_store.add_shared_link(
            self.root, "https://example.com/a", "Link A")
        before = len(self.center_messages())
        resp = self.post("/api/shared-links/delete", session_cookie,
                         csrf_token,
                         {"link_id": link["id"], "room": "chung"})
        self.assertEqual(resp.status, 302, resp.body[:200])
        self.assertEqual(len(self.center_messages()), before)

    def test_add_duplicate_posts_no_message(self):
        session_cookie, csrf_token = self.login()
        self.add_link(session_cookie, csrf_token,
                      "https://example.com/a", "A")
        before = len(self.center_messages())
        resp = self.add_link(session_cookie, csrf_token,
                             "https://example.com/a/", "A nữa")
        self.assertEqual(resp.status, 400, resp.body[:200])
        self.assertEqual(len(self.center_messages()), before)


if __name__ == "__main__":
    unittest.main()
