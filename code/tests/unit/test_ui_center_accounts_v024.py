#!/usr/bin/env python3
"""Test đợt code bổ sung nhỏ v0.2.4 (đội B): quản lý tài khoản trung tâm
trên tab Trung tâm + khu vực Tài khoản.

- Tab Trung tâm: mỗi tài khoản đủ 4 mục (tên+mã, tham gia, lần kéo cuối,
  đang làm/quá hạn) + mã bí mật chỉ "đã có"/"chưa có" + ngày cấp + nút
  "Cấp lại mã mới"; việc mồ côi hiện nhắc "cần chuyển tay".
- Khu vực Tài khoản: tạm ngưng (chỉ tắt cờ, giữ mã) / gỡ hẳn (tắt cờ +
  xoay mã) / tham gia lại (cấp mã mới, làm lại như tham gia mới).
- Cấp/xoay secret: 1 dòng audit có account_id + thời điểm, KHÔNG giá
  trị secret; CSRF mọi POST thay đổi; UI không hiện secret.

Chạy bằng: python3 -m unittest tests.unit.test_ui_center_accounts_v024
Chuẩn: unittest, stdlib-only, không socket, không secret thật.
"""

import json
import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from pc.center import center_store
from pc.ui import ui_app, ui_auth, ui_pages, ui_store

PASSWORD = "mat-khau-dai-123"
FAKE_FETCH = "TEST-FAKE-SECRET-fetch"
FAKE_PUSH = "TEST-FAKE-SECRET-push"


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


class CenterAccountsFixture(unittest.TestCase):
    """Fixture UI + 3 tài khoản (2 tham gia có đủ secret giả, 1 ngoài
    chưa tham gia/chưa có secret)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "secrets").mkdir()
        (self.root / "data" / "bot-a").mkdir(parents=True)
        (self.root / "ui-config").mkdir()
        self.now = time.time()
        self.write_registry({
            "bot-a": {"bot_id": "bot-a", "platform": "zalo",
                      "display_name": "Bot A",
                      "enabled": True, "paused": False,
                      "awaiting_muse_sync": False,
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
            (self.root / "secrets" /
             ("center-%s.fetch_secret" % account_id)).write_text(
                FAKE_FETCH + "\n", encoding="utf-8")
            (self.root / "secrets" /
             ("center-%s.push_secret" % account_id)).write_text(
                FAKE_PUSH + "\n", encoding="utf-8")
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

    def write_registry(self, bots):
        (self.root / "registry.json").write_text(
            json.dumps({"version": 1, "bots": bots}), encoding="utf-8")

    def write_accounts(self, accounts):
        (self.root / "ui-config" / "accounts.json").write_text(
            json.dumps({"version": 1, "accounts": accounts}),
            encoding="utf-8")

    def write_fetch_state(self, center):
        (self.root / "gateway-fetch-state.json").write_text(
            json.dumps({"version": 1, "bots": {}, "center": center}),
            encoding="utf-8")

    def request(self, method, path, form=None, headers=None, query=None,
                client_ip="127.0.0.1"):
        body = b""
        hdrs = dict(headers or {})
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

    def audit_raw(self):
        path = self.root / "ui-config" / "audit.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_accounts(self):
        return ui_store.read_accounts(self.root)["accounts"]

    def read_secret(self, account_id, kind):
        path = self.root / "secrets" / (
            "center-%s.%s_secret" % (account_id, kind))
        return path.read_text(encoding="utf-8").strip() \
            if path.exists() else None

    def center_fragment(self, session_cookie):
        return self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["center"]}).body.decode()

    def create_task(self, assigned_to="muse-phu", due_hours=24,
                    title="Việc thử"):
        """Tạo 1 việc qua store (caller owner), trả task_id."""
        due = time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                            time.localtime(self.now + due_hours * 3600))
        task, _dup = self.center.create_task(
            "owner", title, "Nội dung việc thử", "nghien-cuu",
            assigned_to, due, "key-%s" % title)
        return task["task_id"]

    def confirm_token(self, html, action_path):
        """Trích confirm_token từ trang xác nhận 2 bước."""
        match = re.search(
            rb'name="confirm_token" value="([^"]+)"', html.encode()
            if isinstance(html, str) else html)
        self.assertIsNotNone(match, "không thấy confirm_token ở %s"
                             % action_path)
        return match.group(1).decode()

    def _seed_view_password(self, password="mat-khau-xem-ma-test"):
        """Đặt mật khẩu xem mã (hash scrypt rẻ cho test) để đi route
        xem mã bằng mật khẩu trong các test HM (21/11)."""
        path = self.root / "ui-config" / "view_secret.hash"
        path.write_text(
            ui_auth.hash_password(password, n=2 ** 10, r=8, p=1) + "\n",
            encoding="utf-8")
        os.chmod(path, 0o600)

    def _view_secret(self, cookie, csrf, account_id,
                     password="mat-khau-xem-ma-test"):
        """Đi route xem mã bằng mật khẩu (GET trang nhập -> POST
        execute), trả response POST execute."""
        resp = self.get("/center/accounts/%s/view-secret" % account_id,
                        cookie)
        self.assertEqual(resp.status, 200, resp.body[:300])
        token = re.search(
            rb'name="confirm_token" value="([^"]+)"',
            resp.body).group(1).decode()
        return self.post(
            "/center/accounts/%s/view-secret/execute" % account_id,
            cookie, csrf,
            form={"confirm_token": token, "view_password": password})


class TestCenterStoreHelpers(CenterAccountsFixture):
    def test_read_center_fetch_state_missing_section_is_empty(self):
        self.assertEqual(ui_store.read_center_fetch_state(self.root), {})

    def test_read_center_fetch_state_parses_center_only(self):
        self.write_fetch_state({
            "muse-phu": {"last_fetch_at": _iso(self.now - 60),
                         "fetch_count": 3},
            "muse-chinh": {"last_fetch_at": "không-phải-iso"},
            "muse-ngoai": {"fetch_count": 1},
        })
        state = ui_store.read_center_fetch_state(self.root)
        self.assertEqual(list(state), ["muse-phu"])
        self.assertAlmostEqual(state["muse-phu"], self.now - 60, delta=1)

    def test_center_fetch_status_active_within_15_minutes(self):
        active, label = ui_store.center_fetch_status(self.now - 60,
                                                     now=self.now)
        self.assertTrue(active)
        self.assertEqual(label, "Đang hoạt động")
        active, label = ui_store.center_fetch_status(self.now - 901,
                                                     now=self.now)
        self.assertFalse(active)
        self.assertEqual(label, "Chưa thấy kéo")
        _active, label = ui_store.center_fetch_status(None, now=self.now)
        self.assertEqual(label, "Chưa thấy kéo")

    def test_count_tasks_open_overdue_terminal(self):
        tasks = [
            {"assigned_to": "muse-phu", "status": "moi",
             "due_at": _iso(self.now + 3600)},
            {"assigned_to": "muse-phu", "status": "dang-lam",
             "due_at": _iso(self.now - 60)},
            {"assigned_to": "muse-phu", "status": "da-xac-nhan",
             "due_at": _iso(self.now - 60)},
            {"assigned_to": "muse-chinh", "status": "huy",
             "due_at": _iso(self.now - 60)},
        ]
        counts = ui_store.count_center_tasks_by_account(tasks, now=self.now)
        self.assertEqual(counts["muse-phu"], {"open": 2, "overdue": 1})
        # Tài khoản chỉ có việc đã xong/hủy vẫn có mục đếm 0.
        self.assertEqual(counts["muse-chinh"], {"open": 0, "overdue": 0})

    def test_is_center_orphan(self):
        accounts = {"accounts": self.read_accounts()}
        self.assertFalse(ui_store.is_center_orphan(
            {"assigned_to": "muse-phu"}, accounts))
        self.assertTrue(ui_store.is_center_orphan(
            {"assigned_to": "muse-ngoai"}, accounts))
        # Tài khoản đã gỡ khỏi danh sách cũng là mồ côi.
        self.assertTrue(ui_store.is_center_orphan(
            {"assigned_to": "muse-da-xoa"}, accounts))

    def test_center_secret_status_and_saigon_date(self):
        status = ui_store.center_secret_status(self.root, "muse-phu")
        self.assertTrue(status["has"])
        self.assertIsNotNone(status["issued_at"])
        status = ui_store.center_secret_status(self.root, "muse-ngoai")
        self.assertFalse(status["has"])
        self.assertIsNone(status["issued_at"])
        # Giờ Asia/Saigon: epoch cố định -> ngày/giờ +07:00.
        epoch = 1763400000
        saigon = timezone(timedelta(hours=7))
        self.assertEqual(
            ui_store.format_day_time_saigon(epoch),
            datetime.fromtimestamp(epoch, saigon).strftime("%d/%m %H:%M"))
        self.assertEqual(
            ui_store.format_date_saigon(epoch),
            datetime.fromtimestamp(epoch, saigon).strftime("%d/%m/%Y"))


class TestCenterTabAccounts(CenterAccountsFixture):
    def test_four_items_per_account(self):
        self.create_task(assigned_to="muse-phu", due_hours=24,
                         title="Việc phụ")
        self.create_task(assigned_to="muse-chinh", due_hours=-1,
                         title="Việc chính muộn")
        self.write_fetch_state({
            "muse-phu": {"last_fetch_at": _iso(self.now - 60),
                         "fetch_count": 3},
            "muse-chinh": {"last_fetch_at": _iso(self.now - 3600),
                           "fetch_count": 5},
        })
        # Ngày cấp mã: đặt mtime file fetch_secret về mốc cố định.
        issued_epoch = 1763400000
        fetch_path = self.root / "secrets" / "center-muse-phu.fetch_secret"
        os.utime(fetch_path, (issued_epoch, issued_epoch))
        issued_date = datetime.fromtimestamp(
            issued_epoch, timezone(timedelta(hours=7))).strftime("%d/%m/%Y")
        session_cookie, _ = self.login()
        frag = self.center_fragment(session_cookie)
        # (1) tên + mã.
        self.assertIn("Muse phụ", frag)
        self.assertIn("muse-phu", frag)
        # (2) đang tham gia hay không.
        self.assertIn("Tham gia", frag)
        self.assertIn("Chưa tham gia", frag)  # muse-ngoai
        # (3) lần cuối kéo tin (giờ + ngày Asia/Saigon) + trạng thái.
        self.assertIn("Lần kéo cuối:", frag)
        self.assertIn("Đang hoạt động", frag)  # muse-phu kéo 1 phút trước
        self.assertIn("Chưa thấy kéo", frag)  # muse-chinh quá 15 phút
        # (4) đang làm / quá hạn mấy việc (đếm trên hub).
        self.assertIn("Đang làm: 1 việc · Quá hạn: 0 việc", frag)
        self.assertIn("Đang làm: 1 việc · Quá hạn: 1 việc", frag)
        # Mã bí mật: chỉ "đã có"/"chưa có" + ngày cấp + nút cấp lại.
        self.assertIn("Mã bí mật: đã có · Cấp ngày %s" % issued_date, frag)
        self.assertIn("Mã bí mật: chưa có", frag)
        self.assertIn("Cấp lại mã mới", frag)
        # Chú thích mtime chỉ là ước lượng (DESIGN §9).
        self.assertIn("ước lượng theo file trên máy", frag)
        self.assertIn("tin nhật ký audit", frag)
        # KHÔNG cảnh báo 7 ngày im lặng (Q-AM-08).
        self.assertNotIn("7 ngày", frag)
        # KHÔNG lộ giá trị secret dưới mọi hình thức.
        self.assertNotIn(FAKE_FETCH, frag)
        self.assertNotIn(FAKE_PUSH, frag)

    def test_missing_center_section_means_never_fetched(self):
        """File chưa có mục center -> đọc về {} -> 'chưa thấy kéo'
        (không suy từ seen_state.json)."""
        task_id = self.create_task()
        session_cookie, _ = self.login()
        frag = self.center_fragment(session_cookie)
        self.assertIn("Chưa thấy kéo", frag)
        self.assertNotIn("Đang hoạt động", frag)
        self.center.record_seen("muse-phu", [task_id])
        frag = self.center_fragment(session_cookie)
        self.assertIn("Chưa thấy kéo", frag)
        self.assertNotIn("Đang hoạt động", frag)

    def test_orphan_task_shows_can_chuyen_tay(self):
        self.create_task(assigned_to="muse-ngoai", title="Việc mồ côi")
        self.create_task(assigned_to="muse-phu", title="Việc bình thường")
        session_cookie, _ = self.login()
        frag = self.center_fragment(session_cookie)
        self.assertEqual(frag.count("Cần chuyển tay"), 1)
        # Chip "Cần chuyển tay" đứng trước tiêu đề trong cùng 1 dòng việc
        # (chip trạng thái render trước link tiêu đề), và dòng việc mồ
        # côi xếp sau dòng việc bình thường (tạo sau lên trước).
        self.assertLess(frag.index("Việc bình thường"),
                        frag.index("Cần chuyển tay"))
        self.assertLess(frag.index("Cần chuyển tay"),
                        frag.index("Việc mồ côi"))

    def test_task_counts_use_all_tasks_not_filtered_list(self):
        self.create_task(assigned_to="muse-phu", due_hours=24,
                         title="Việc phụ 1")
        self.create_task(assigned_to="muse-phu", due_hours=-1,
                         title="Việc phụ muộn")
        session_cookie, _ = self.login()
        # Lọc chỉ còn việc quá hạn nhưng số đếm tài khoản vẫn đủ 2 việc.
        frag = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["center"],
                               "overdue": ["1"]}).body.decode()
        self.assertIn("Việc phụ muộn", frag)
        self.assertNotIn("Việc phụ 1", frag)
        self.assertIn("Đang làm: 2 việc · Quá hạn: 1 việc", frag)


class TestCenterRotateSecret(CenterAccountsFixture):
    def test_rotate_flow_rotates_files_and_audits_no_secret(self):
        old_fetch = self.read_secret("muse-phu", "fetch")
        old_push = self.read_secret("muse-phu", "push")
        session_cookie, csrf = self.login()
        confirm = self.get(
            "/center/accounts/muse-phu/rotate-secret", session_cookie)
        self.assertEqual(confirm.status, 200)
        html = confirm.body.decode()
        self.assertIn("Cấp lại mã mới", html)
        # HM (21/11): hien ma khi cap — trang xac nhan dung cau moi,
        # xem THIET-KE-HIEN-MA-KHI-CAP-21-11.md §3.7(a).
        self.assertIn("hiện ngay trên trang tiếp theo", html)
        self.assertNotIn("không hiện trên giao diện", html)
        self.assertNotIn(FAKE_FETCH, html)
        token = self.confirm_token(
            html, "/center/accounts/muse-phu/rotate-secret")
        resp = self.post(
            "/center/accounts/muse-phu/rotate-secret/execute",
            session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        body = resp.body.decode()
        # HM (21/11): hien ma khi cap — trang ket qua tai dung
        # view_secret_page_html (issue_show=True) nen h1 la
        # "Mã bí mật mới", khong con h1 cu "Đã cấp mã mới".
        self.assertIn("Mã bí mật mới", body)
        new_fetch = self.read_secret("muse-phu", "fetch")
        new_push = self.read_secret("muse-phu", "push")
        self.assertNotEqual(new_fetch, old_fetch)
        self.assertNotEqual(new_push, old_push)
        self.assertEqual(
            (self.root / "secrets" /
             "center-muse-phu.fetch_secret").stat().st_mode & 0o777, 0o600)
        # HM (21/11): hien ma khi cap — body ket qua nay CHUA ma moi;
        # xem THIET-KE-HIEN-MA-KHI-CAP-21-11.md §4.2.
        self.assertIn(new_fetch, body)
        self.assertIn(new_push, body)
        # Audit: 1 dòng, có account_id + thời điểm, không giá trị secret.
        entries = [e for e in self.audit_entries()
                   if e["action"] == "center_secret_rotate"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["account_id"], "muse-phu")
        self.assertIsInstance(entry["ts"], (int, float))
        # HM (21/11): hien ma khi cap — audit ghi shown_on_issue=true.
        self.assertTrue(entry["after"].get("shown_on_issue"))
        # HM (21/11): hien ma khi cap — co shown_at_issue da dat.
        state, _status = ui_store.read_center_view_state(self.root)
        self.assertIsNotNone(
            state["accounts"]["muse-phu"]["shown_at_issue"])
        raw = self.audit_raw()
        self.assertNotIn(old_fetch, raw)
        self.assertNotIn(new_fetch, raw)
        self.assertNotIn(new_push, raw)

    def test_rotate_execute_shows_new_secrets(self):
        # HM (21/11): seam S1 — POST rotate execute hien ca 2 ma moi
        # + no-store + nut "Hien lai ma" (hidden) + KHONG link xoay.
        session_cookie, csrf = self.login()
        confirm = self.get(
            "/center/accounts/muse-phu/rotate-secret", session_cookie)
        token = self.confirm_token(
            confirm.body.decode(),
            "/center/accounts/muse-phu/rotate-secret")
        resp = self.post(
            "/center/accounts/muse-phu/rotate-secret/execute",
            session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
        body = resp.body.decode()
        new_fetch = self.read_secret("muse-phu", "fetch")
        new_push = self.read_secret("muse-phu", "push")
        self.assertIn(new_fetch, body)
        self.assertIn(new_push, body)
        self.assertIn('id="view-secret-show-again" hidden', body)
        self.assertIn("Hiện lại mã", body)
        self.assertNotIn("Xoay mã mới ngay", body)
        self.assertIn("Mã bí mật mới", body)
        entries = [e for e in self.audit_entries()
                   if e["action"] == "center_secret_rotate"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["after"].get("shown_on_issue"))
        raw = self.audit_raw()
        self.assertNotIn(new_fetch, raw)
        self.assertNotIn(new_push, raw)

    def test_rotate_execute_read_failure_is_fail_closed(self):
        # HM (21/11): seam S1/S5 — doc 1 ma that bai sau khi xoay
        # (gia lap file push thieu) -> trang loi, khong hien nua cap,
        # khong dat co, audit 1 dong KHONG co shown_on_issue.
        session_cookie, csrf = self.login()
        confirm = self.get(
            "/center/accounts/muse-phu/rotate-secret", session_cookie)
        token = self.confirm_token(
            confirm.body.decode(),
            "/center/accounts/muse-phu/rotate-secret")
        orig_read = center_store.read_center_secret

        def _read_missing_push(root, account_id, kind):
            if kind == "push":
                return None
            return orig_read(root, account_id, kind)

        with mock.patch.object(
                center_store, "read_center_secret",
                side_effect=_read_missing_push):
            resp = self.post(
                "/center/accounts/muse-phu/rotate-secret/execute",
                session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        body = resp.body.decode()
        self.assertIn("Không đọc được mã mới", body)
        new_fetch = self.read_secret("muse-phu", "fetch")
        self.assertNotIn(new_fetch, body)
        state, _status = ui_store.read_center_view_state(self.root)
        entry = (state.get("accounts") or {}).get("muse-phu") \
            if state else None
        self.assertTrue(entry is None
                        or entry.get("shown_at_issue") is None)
        entries = [e for e in self.audit_entries()
                   if e["action"] == "center_secret_rotate"]
        self.assertEqual(len(entries), 1)
        self.assertNotIn("shown_on_issue", entries[0]["after"])

    def test_view_after_rotate_again_allowed(self):
        # HM (21/11): seam S3/S4 — hien luc cap -> xem lai bi chan;
        # xoay thang bang ham (khong hien ma) thi co cu sach -> xem
        # duoc ma moi. Luu y: POST rotate execute lan 2 CUNG hien ma
        # moi + dat lai co nen khong dung de mo khoa xem — thiet ke
        # §7.3 ghi "POST rotate execute lan 2" nhung ky vong 200 sau
        # do mau thuan D-HM-19; test nay kiem dung y dinh "(co cu da
        # sach)" bang duong xoay khong hien.
        self._seed_view_password()
        session_cookie, csrf = self.login()
        confirm = self.get(
            "/center/accounts/muse-phu/rotate-secret", session_cookie)
        token = self.confirm_token(
            confirm.body.decode(),
            "/center/accounts/muse-phu/rotate-secret")
        resp = self.post(
            "/center/accounts/muse-phu/rotate-secret/execute",
            session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        resp = self._view_secret(session_cookie, csrf, "muse-phu")
        self.assertEqual(resp.status, 403)
        self.assertIn("Mã này đã hiện lúc cấp",
                      resp.body.decode())
        self.app._rotate_center_secrets("muse-phu")
        new_fetch = self.read_secret("muse-phu", "fetch")
        new_push = self.read_secret("muse-phu", "push")
        resp = self._view_secret(session_cookie, csrf, "muse-phu")
        self.assertEqual(resp.status, 200, resp.body[:300])
        body = resp.body.decode()
        self.assertIn(new_fetch, body)
        self.assertIn(new_push, body)

    def test_rotate_requires_csrf_and_confirm_token(self):
        session_cookie, csrf = self.login()
        confirm = self.get(
            "/center/accounts/muse-phu/rotate-secret", session_cookie)
        token = self.confirm_token(
            confirm.body.decode(),
            "/center/accounts/muse-phu/rotate-secret")
        # Không CSRF -> 403, không xoay gì.
        resp = self.post_no_csrf(
            "/center/accounts/muse-phu/rotate-secret/execute",
            session_cookie, {"confirm_token": token})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.read_secret("muse-phu", "fetch"), FAKE_FETCH)
        # Sai confirm_token -> 403, không xoay gì.
        resp = self.post(
            "/center/accounts/muse-phu/rotate-secret/execute",
            session_cookie, csrf, {"confirm_token": "sai"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.read_secret("muse-phu", "fetch"), FAKE_FETCH)
        self.assertEqual(
            [e for e in self.audit_entries()
             if e["action"] == "center_secret_rotate"], [])

    def test_rotate_unknown_account_404(self):
        session_cookie, _ = self.login()
        resp = self.get("/center/accounts/muse-khong-co/rotate-secret",
                        session_cookie)
        self.assertEqual(resp.status, 404)


class TestCenterMembership(CenterAccountsFixture):
    def test_accounts_page_shows_center_actions(self):
        session_cookie, _ = self.login()
        html = self.get("/accounts", session_cookie).body.decode()
        self.assertIn("Trung tâm: Đang tham gia", html)
        self.assertIn("Trung tâm: Chưa tham gia", html)
        self.assertIn("Tạm ngưng tham gia", html)
        self.assertIn("Gỡ hẳn khỏi trung tâm", html)
        self.assertIn("Tham gia trung tâm", html)  # muse-ngoai chưa có mã
        self.assertIn("Mã bí mật: đã có", html)
        self.assertIn("Mã bí mật: chưa có", html)
        self.assertNotIn(FAKE_FETCH, html)
        self.assertNotIn(FAKE_PUSH, html)

    def test_pause_keeps_secret_and_resume_restores(self):
        session_cookie, csrf = self.login()
        resp = self.post("/accounts/muse-phu/center/pause",
                         session_cookie, csrf)
        self.assertEqual(resp.status, 302)
        self.assertFalse(
            self.read_accounts()["muse-phu"]["center_participating"])
        # Tạm ngưng giữ nguyên mã.
        self.assertEqual(self.read_secret("muse-phu", "fetch"), FAKE_FETCH)
        self.assertEqual(self.read_secret("muse-phu", "push"), FAKE_PUSH)
        frag = self.center_fragment(session_cookie)
        self.assertIn("Chưa tham gia", frag)
        # Bật lại: chỉ bật cờ, giữ mã cũ — chạy ngay.
        resp = self.post("/accounts/muse-phu/center/resume",
                         session_cookie, csrf)
        self.assertEqual(resp.status, 302)
        self.assertTrue(
            self.read_accounts()["muse-phu"]["center_participating"])
        self.assertEqual(self.read_secret("muse-phu", "fetch"), FAKE_FETCH)
        actions = [e["action"] for e in self.audit_entries()]
        self.assertIn("center_pause", actions)
        self.assertIn("center_resume", actions)
        entries = [e for e in self.audit_entries()
                   if e["action"] in ("center_pause", "center_resume")]
        self.assertTrue(all(e["account_id"] == "muse-phu"
                            for e in entries))

    def test_pause_and_resume_require_csrf(self):
        session_cookie, _ = self.login()
        resp = self.post_no_csrf("/accounts/muse-phu/center/pause",
                                 session_cookie)
        self.assertEqual(resp.status, 403)
        self.assertTrue(
            self.read_accounts()["muse-phu"]["center_participating"])

    def test_resume_without_secret_rejected(self):
        session_cookie, csrf = self.login()
        resp = self.post("/accounts/muse-ngoai/center/resume",
                         session_cookie, csrf)
        self.assertEqual(resp.status, 200)
        self.assertIn("chưa có mã bí mật", resp.body.decode())
        self.assertFalse(
            self.read_accounts()["muse-ngoai"]["center_participating"])

    def test_remove_rotates_secret(self):
        old_fetch = self.read_secret("muse-phu", "fetch")
        session_cookie, csrf = self.login()
        confirm = self.get("/accounts/muse-phu/center/remove",
                           session_cookie)
        self.assertEqual(confirm.status, 200)
        self.assertIn("Gỡ hẳn", confirm.body.decode())
        token = self.confirm_token(confirm.body.decode(),
                                   "/accounts/muse-phu/center/remove")
        resp = self.post("/accounts/muse-phu/center/remove/execute",
                         session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        self.assertIn("Đã gỡ hẳn", resp.body.decode())
        self.assertFalse(
            self.read_accounts()["muse-phu"]["center_participating"])
        # Gỡ hẳn xoay mã: mã cũ chết.
        self.assertNotEqual(self.read_secret("muse-phu", "fetch"), old_fetch)
        actions = [e["action"] for e in self.audit_entries()]
        self.assertIn("center_remove", actions)
        self.assertIn("center_secret_rotate", actions)

    def test_join_issues_new_secret_and_flag(self):
        session_cookie, csrf = self.login()
        confirm = self.get("/accounts/muse-ngoai/center/join",
                           session_cookie)
        self.assertEqual(confirm.status, 200)
        self.assertIn("Tham gia trung tâm", confirm.body.decode())
        token = self.confirm_token(confirm.body.decode(),
                                   "/accounts/muse-ngoai/center/join")
        resp = self.post("/accounts/muse-ngoai/center/join/execute",
                         session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        body = resp.body.decode()
        # HM (21/11): hien ma khi cap — trang ket qua tai dung
        # view_secret_page_html (issue_show=True) nen h1 la
        # "Mã bí mật mới", khong con h1 cu "Đã cấp mã mới".
        self.assertIn("Mã bí mật mới", body)
        self.assertTrue(
            self.read_accounts()["muse-ngoai"]["center_participating"])
        new_fetch = self.read_secret("muse-ngoai", "fetch")
        new_push = self.read_secret("muse-ngoai", "push")
        self.assertTrue(new_fetch)
        self.assertTrue(new_push)
        # HM (21/11): hien ma khi cap — body ket qua nay CHUA ma moi;
        # xem THIET-KE-HIEN-MA-KHI-CAP-21-11.md §4.3.
        self.assertIn(new_fetch, body)
        self.assertIn(new_push, body)
        actions = [e["action"] for e in self.audit_entries()]
        self.assertIn("center_join", actions)
        self.assertIn("center_secret_rotate", actions)
        # HM (21/11): hien ma khi cap — co shown_at_issue da dat +
        # audit ghi shown_on_issue=true.
        state, _status = ui_store.read_center_view_state(self.root)
        self.assertIsNotNone(
            state["accounts"]["muse-ngoai"]["shown_at_issue"])
        rotate_entries = [e for e in self.audit_entries()
                          if e["action"] == "center_secret_rotate"]
        self.assertEqual(len(rotate_entries), 1)
        self.assertTrue(rotate_entries[0]["after"].get("shown_on_issue"))
        self.assertNotIn(new_fetch, self.audit_raw())
        self.assertNotIn(new_push, self.audit_raw())

    def test_join_execute_shows_new_secrets(self):
        # HM (21/11): seam S2 — POST join execute hien ca 2 ma moi +
        # no-store + nut "Hien lai ma" + KHONG link xoay; nhac
        # "Da xem — chua xoay" KHONG hien sau khi cap.
        session_cookie, csrf = self.login()
        confirm = self.get("/accounts/muse-ngoai/center/join",
                           session_cookie)
        token = self.confirm_token(confirm.body.decode(),
                                   "/accounts/muse-ngoai/center/join")
        resp = self.post("/accounts/muse-ngoai/center/join/execute",
                         session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
        body = resp.body.decode()
        new_fetch = self.read_secret("muse-ngoai", "fetch")
        new_push = self.read_secret("muse-ngoai", "push")
        self.assertIn(new_fetch, body)
        self.assertIn(new_push, body)
        self.assertIn('id="view-secret-show-again" hidden', body)
        self.assertNotIn("Xoay mã mới ngay", body)
        self.assertTrue(
            self.read_accounts()["muse-ngoai"]["center_participating"])
        center_html = self.get("/", session_cookie,
                              query={"tab": ["center"]}).body.decode()
        self.assertNotIn("chưa xoay mã mới", center_html)
        actions = [e["action"] for e in self.audit_entries()]
        self.assertIn("center_join", actions)
        rotate_entries = [e for e in self.audit_entries()
                          if e["action"] == "center_secret_rotate"]
        self.assertEqual(len(rotate_entries), 1)
        self.assertTrue(rotate_entries[0]["after"].get("shown_on_issue"))

    def test_join_execute_read_failure_keeps_participating_and_recoverable_via_view(self):
        # HM (21/11): seam N-11 — join doc ma loi: giu
        # participating=True (khong rollback), co chua dat, audit
        # center_join + 1 dong rotate KHONG co shown_on_issue; phuc
        # hoi qua route xem ma bang mat khau -> 200, hien ma moi.
        self._seed_view_password()
        session_cookie, csrf = self.login()
        confirm = self.get("/accounts/muse-ngoai/center/join",
                           session_cookie)
        token = self.confirm_token(confirm.body.decode(),
                                   "/accounts/muse-ngoai/center/join")
        orig_read = center_store.read_center_secret

        def _read_missing_push(root, account_id, kind):
            if kind == "push":
                return None
            return orig_read(root, account_id, kind)

        with mock.patch.object(
                center_store, "read_center_secret",
                side_effect=_read_missing_push):
            resp = self.post(
                "/accounts/muse-ngoai/center/join/execute",
                session_cookie, csrf, {"confirm_token": token})
        self.assertEqual(resp.status, 200)
        body = resp.body.decode()
        self.assertIn("Không đọc được mã mới", body)
        new_fetch = self.read_secret("muse-ngoai", "fetch")
        self.assertNotIn(new_fetch, body)
        self.assertTrue(
            self.read_accounts()["muse-ngoai"]["center_participating"])
        state, _status = ui_store.read_center_view_state(self.root)
        entry = (state.get("accounts") or {}).get("muse-ngoai") \
            if state else None
        self.assertTrue(entry is None
                        or entry.get("shown_at_issue") is None)
        actions = [e["action"] for e in self.audit_entries()]
        self.assertIn("center_join", actions)
        rotate_entries = [e for e in self.audit_entries()
                          if e["action"] == "center_secret_rotate"]
        self.assertEqual(len(rotate_entries), 1)
        self.assertNotIn("shown_on_issue", rotate_entries[0]["after"])
        resp = self._view_secret(session_cookie, csrf, "muse-ngoai")
        self.assertEqual(resp.status, 200, resp.body[:300])
        view_body = resp.body.decode()
        self.assertIn(new_fetch, view_body)
        self.assertIn(self.read_secret("muse-ngoai", "push"), view_body)

    def test_join_confirm_shows_new_wording(self):
        # HM (21/11): seam S8 — trang xac nhan tham gia dung cau moi.
        session_cookie, _ = self.login()
        html = self.get("/accounts/muse-ngoai/center/join",
                        session_cookie).body.decode()
        self.assertIn("hiện ngay trên trang tiếp theo", html)
        self.assertNotIn("không hiện trên giao diện", html)

    def test_join_when_already_participating_rejected(self):
        session_cookie, _ = self.login()
        resp = self.get("/accounts/muse-phu/center/join", session_cookie)
        self.assertEqual(resp.status, 200)
        self.assertIn("đã tham gia trung tâm", resp.body.decode())


class TestNoHandSendRoutes(CenterAccountsFixture):
    def test_no_hand_send_route_for_giao_viec_bao_cao(self):
        """Chi 2 cho goi append_message o tang UI, deu kind=hoi —
        khong route gui giao-viec/bao-cao tay (F18). Cho thu 2 la tin
        thong bao link chung (sua Link chung 22/11)."""
        source = Path(ui_app.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("append_message"), 2)
        self.assertEqual(source.count('"hoi"'), 2)
        # (v0.6.0, T3i — D8) Goi kem to_account (None = Tat ca).
        self.assertIn('"hoi", to_account, None, body,', source)
        self.assertNotIn("giao-viec\",", source)
        self.assertNotIn("bao-cao\",", source)


if __name__ == "__main__":
    unittest.main()
