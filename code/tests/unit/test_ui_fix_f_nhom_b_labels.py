#!/usr/bin/env python3
"""Test vòng sửa UI nhóm B — L5 (nhãn phòng Chat) + L6 (tab sai tên
báo lỗi), ngày 21/11/2026, theo quyết chủ nhân D3.

Chạy bằng: python3 -m unittest tests.unit.test_ui_fix_f_nhom_b_labels -v
Chuẩn: unittest, stdlib-only, không socket, không secret thật.
"""

import re
import unittest

from pc.ui import ui_app
from tests.unit.test_ui_center_t3 import CenterFixture

BANNER = ("Tab không tồn tại — đã chuyển về tab Tất cả.")


def _room_list(html):
    """Trích đúng khối danh sách phòng (không lẫn header phòng)."""
    m = re.search(r'class="center-room-list">(.*?)</div>', html, re.S)
    assert m is not None, "không tìm thấy khối danh sách phòng"
    return m.group(1)


class TestL5RoomLabels(CenterFixture):
    """L5 — nhãn phòng ở tab Chat rõ hơn (quyết chủ nhân 21/11)."""

    def test_danh_sach_phong_khong_con_tien_to_rieng(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie, query={
            "tab": ["chat"], "room": ["dm:muse-chinh"]}).body.decode()
        room_list = _room_list(html)
        self.assertIn("Muse chính", room_list)
        self.assertIn("Khung chung", room_list)
        self.assertNotIn("Riêng:", room_list)

    def test_header_phong_dang_mo_phong_rieng(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie, query={
            "tab": ["chat"], "room": ["dm:muse-chinh"]}).body.decode()
        self.assertIn("Phòng đang mở: Riêng: Muse chính", html)

    def test_header_phong_dang_mo_khung_chung(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertIn("Phòng đang mở: Khung chung", html)

    def test_o_gui_cho_nhan_mac_dinh_moi(self):
        # (Gop UI checkbox, 22/11) O "Gửi cho:" nay la nhom checkbox,
        # khong con <select> va khong con o "Gửi cho tất cả tài khoản"
        # rieng: khong tick o nao = gui cho tat ca. O loc "Lọc theo
        # người gửi" vẫn giữ option "Tất cả" (ngoài L5); nhãn tab
        # "Tất cả" vẫn giữ nguyên.
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertNotIn('id="center-hoi-to"', html)
        self.assertIn('class="check-group"', html)
        self.assertIn('type="checkbox" name="to_account"', html)
        self.assertIn("không chọn gì = gửi cho tất cả", html)
        self.assertNotIn("Gửi cho tất cả tài khoản", html)
        self.assertIn("Tất cả", html)


class TestL6InvalidTab(CenterFixture):
    """L6 — ?tab= sai tên thì báo lỗi, không fallback lặng lẽ."""

    def test_params_ghi_co_tab_invalid(self):
        req = self.request("GET", "/", query={"tab": ["canhbao"]})
        params = ui_app.UiApp._dashboard_params(req)
        self.assertEqual(params["tab"], "all")
        self.assertTrue(params["tab_invalid"])
        req = self.request("GET", "/", query={"tab": ["all"]})
        params = ui_app.UiApp._dashboard_params(req)
        self.assertEqual(params["tab"], "all")
        self.assertFalse(params["tab_invalid"])

    def test_tab_sai_tai_trang_day_du_redirect_302(self):
        session_cookie, _ = self.login()
        resp = self.get("/", session_cookie, query={"tab": ["canhbao"]})
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"],
                         "/?tab=all&notice=tab-khong-hop-le")

    def test_url_dich_hien_banner_tab_khong_ton_tai(self):
        session_cookie, _ = self.login()
        resp = self.get("/", session_cookie, query={
            "tab": ["all"], "notice": ["tab-khong-hop-le"]})
        self.assertEqual(resp.status, 200)
        html = resp.body.decode()
        self.assertIn(
            '<div class="banner">%s</div>' % BANNER, html)

    def test_tab_hop_le_khong_redirect_khong_banner(self):
        session_cookie, _ = self.login()
        for tab in ("all", "center", "chat", "zalo", "telegram",
                    "alerts"):
            with self.subTest(tab=tab):
                resp = self.get("/", session_cookie,
                                query={"tab": [tab]})
                self.assertEqual(resp.status, 200, tab)
                self.assertNotIn(BANNER, resp.body.decode(), tab)

    def test_khong_notice_thi_khong_banner(self):
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["all"]}).body.decode()
        self.assertNotIn(BANNER, html)
        # Notice lạ (không phải giá trị đã biết) cũng không in ra.
        html = self.get("/", session_cookie, query={
            "tab": ["all"],
            "notice": ["<script>alert(1)</script>"]}).body.decode()
        self.assertNotIn(BANNER, html)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_fragment_tab_sai_khong_redirect(self):
        # Fragment polling KHONG được trả 302 (sẽ phá poll): giữ
        # fallback lặng lẽ về tab all, trả 200 như cũ.
        session_cookie, _ = self.login()
        resp = self.get("/fragments/dashboard", session_cookie,
                        query={"tab": ["canhbao"]})
        self.assertEqual(resp.status, 200)
        self.assertNotIn(BANNER, resp.body.decode())


if __name__ == "__main__":
    unittest.main()
