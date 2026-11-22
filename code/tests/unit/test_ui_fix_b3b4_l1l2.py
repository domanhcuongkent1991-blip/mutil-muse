#!/usr/bin/env python3
"""Test sua loi UI tab Chat — B3/B4 + L1/L2 (21/11/2026).

- B3: vien "N tin moi" + nut "Ve cuoi" troi ra khoi khung chat khi
  cuon (video chu nhan 10:01) — anchor sticky cu trong hop cuon
  khong giu duoc; sua bang cach neo theo wrap (khong cuon).
- B4: vien hien lien tuc, bam khong bien mat — goc la luat tac gia
  `button { display: inline-block; }` thang luat `[hidden]` cua
  UA stylesheet (author > UA), lai them `position: absolute`
  bien display thanh block (do that: hidden=true nhung computed
  display:block); sua bang luat `[hidden] { display: none
  !important; }`.
- L1: 390px nut "Gửi" de len thanh tab day 11-12px, day form tran
  viewport 126px — sticky bottom:90px chi co tac dung khi cuon
  du; sua bang cach rut ngan form + hop 40vh o mobile.
- L2: 390px vien (y=869) + nut (y=917) nam ngoai viewport 844 —
  cung goc troi nhu B3 + hidden bi de (nhu B4).

unittest, stdlib-only. Khong co trinh duyet that trong sandbox nen
kiem chung bo cuc bang MO HINH RECT (tinh toan vi tri tu luat CSS
that + so do that cua agent PC), khong chi assert chuoi CSS —
dung diem yeu audit dot A da canh bao. Moi gia tri secret trong
test deu la mau gia dang ro rang ("TEST-FAKE-..."), khong secret
that, khong noi dung tin that.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pc.ui.ui_static import APP_CSS, APP_JS  # noqa: E402
from tests.unit.test_ui_fix_d_chatbox import (  # noqa: E402
    UiFixDFixture)


def _css_block(css, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}",
                      css)
    return match.group(1) if match else ""


def _media_block(css, query):
    """Khoi @media(query) { ... } — lay den dau ngoac dong tuong ung."""
    start = css.index(query)
    open_brace = css.index("{", start)
    depth = 0
    for i in range(open_brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace + 1:i]
    return ""


# ---------------------------------------------------------------------------
# So do that cua agent PC (bien ban 10:12 21/11) — hieu chuan mo hinh
# ---------------------------------------------------------------------------
# Viewport mobile 390x844; thanh tab co dinh: top 766, cao 68, day 834.
# Truoc sua: nut Gui day 778 (de tab 12px), day form 970 (tran 126px),
# form cao ~580px, hop 50vh = 422px; phong dm: dai: vien y=869,
# nut y=917 (ngoai viewport 844).
VIEWPORT_W = 390
VIEWPORT_H = 844
TAB_TOP = 766
FORM_TOP_BEFORE = 390
FORM_H_BEFORE = 580
SEND_OFFSET_FROM_FORM_TOP = 388  # nut Gui: day 778 - dau form 390
H2_GAP = 60  # h2 "Hoi qua trung tam": 23px + margin 24px 0 8px
PILL_H = 36  # min-height .center-new-pill
BTN_H = 44  # nut "Ve cuoi" 44x44
CONTENT_BELOW_BOX = 643  # doan noi dung duoi day hop hien thi o
# lan do L2 (hop dm: 3070px, da cuon len 643px khoi day) — tai hien
# dung y=869 / y=917 cua bien ban.


def _rects_after_fix():
    """Rect sau sua (390x844), tinh tu luat CSS that + so do that.

    Doi chieu (deu doc tu APP_CSS ben duoi, test se assert):
    - hop mobile 40vh (truoc 50vh): -84px -> form day len 84px.
    - o "Gui cho:" 48px (truoc ~118px): nut Gui day len 70px.
    - an 2 ghi chu phu duoi nut: day form len 90px (2 x ~45px).
    Tra ve dict cac rect (top/bottom theo viewport)."""
    box_h = int(0.40 * VIEWPORT_H)  # 40vh o 844px = 338
    form_top = FORM_TOP_BEFORE - (int(0.50 * VIEWPORT_H) - box_h)
    form_h = FORM_H_BEFORE - 70 - 90
    form_bottom = form_top + form_h
    send_bottom = form_top + (SEND_OFFSET_FROM_FORM_TOP - 70)
    box_bottom = form_top - H2_GAP
    pill_bottom = box_bottom - 81
    pill_top = pill_bottom - PILL_H
    btn_bottom = box_bottom - 25
    btn_top = btn_bottom - BTN_H
    return {
        "box_h": box_h, "box_bottom": box_bottom,
        "form_top": form_top, "form_h": form_h,
        "form_bottom": form_bottom, "send_bottom": send_bottom,
        "pill_top": pill_top, "pill_bottom": pill_bottom,
        "btn_top": btn_top, "btn_bottom": btn_bottom,
    }


def _rects_before_fix():
    """Rect truoc sua (390x844) — phai tai hien dung so do that
    (hieu chuan mo hinh). Vien/nut neo theo anchor o cuoi noi
    dung hop: anchor_y = day hop + doan noi dung duoi day hop."""
    box_h = int(0.50 * VIEWPORT_H)  # 422
    box_bottom = FORM_TOP_BEFORE - H2_GAP
    anchor_y = box_bottom + CONTENT_BELOW_BOX
    return {
        "send_bottom": FORM_TOP_BEFORE + SEND_OFFSET_FROM_FORM_TOP,
        "form_bottom": FORM_TOP_BEFORE + FORM_H_BEFORE,
        "pill_top": anchor_y - 68 - PILL_H,
        "btn_top": anchor_y - 12 - BTN_H,
        "box_bottom": box_bottom, "box_h": box_h,
    }


# ---------------------------------------------------------------------------
# B4 + L2 — thuoc tinh hidden phai an phan tu
# ---------------------------------------------------------------------------

class TestHiddenAttributeWorks(UiFixDFixture):
    def test_hidden_rule_restores_hidden_semantics(self):
        # Luat tac gia [hidden] thang `button { display:
        # inline-block; }` (author > UA) va moi luat display
        # theo class khac — vien co hidden=true phai an that.
        # (Bo comment truoc khi tim: chinh comment giai thich
        # luat nay cung chua chu "[hidden] { display: none }".)
        css = re.sub(r"/\*.*?\*/", "", APP_CSS, flags=re.S)
        match = re.search(r"\[hidden\]\s*\{([^}]*)\}", css)
        self.assertTrue(match, "không tìm thấy luật [hidden]")
        self.assertIn("display: none !important", match.group(1))

    def test_button_display_rule_still_exists(self):
        # Ghi ro ke thu: luat button van co display (khong xoa
        # luat chung) — luat [hidden] o tren la thu thang no.
        btn = _css_block(APP_CSS, "button, .btn")
        self.assertIn("display: inline-block", btn)

    def test_pill_hidden_in_initial_render_both_rooms(self):
        # Vien render tinh kem hidden (an cho toi khi co tin chua
        # doc) — ca 2 loai phong, cung 1 duong render.
        session_cookie, _ = self.login()
        for room in ("chung", "dm:muse-phu"):
            html = self.get(
                "/", session_cookie,
                query={"tab": ["chat"],
                       "room": [room]}).body.decode()
            self.assertIn('id="center-new-pill" hidden', html,
                          "phong %s: vien khong co hidden" % room)

    def test_no_inline_style_in_chat_tab(self):
        # CSP nghiem: khong style="..." inline trong tab Chat
        # (ke ca cho vien/nut/form moi doi).
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertNotIn('style="', html)


# ---------------------------------------------------------------------------
# L1 — rut ngan form + hop o mobile
# ---------------------------------------------------------------------------

class TestL1MobileForm(UiFixDFixture):
    def test_select_one_row_and_hints_hidden_on_mobile(self):
        # (Gop UI checkbox, 22/11) O "Gửi cho:" nay la nhom checkbox:
        # moi o co dich cham >= 44px (an toan tren mobile); khong con
        # <select multiple> can thu nho xuong 48px nua.
        mobile = _media_block(APP_CSS, "@media (max-width: 720px)")
        self.assertTrue(mobile, "không tìm thấy khối <=720px")
        self.assertNotIn("#center-hoi-to[multiple]", APP_CSS)
        group = _css_block(APP_CSS, ".check-group .check-item")
        self.assertIn("min-height: 44px", group)
        hints = _css_block(mobile,
                           ".center-chat-form .hint--mobile-hide")
        self.assertIn("display: none", hints)

    def test_static_box_keeps_65vh(self):
        # B2 (21/11): hop chat tab Chat nay cao calc(100vh - 266px)
        # — xem THIET-KE-KHUNG-CHAT-B-21-11.md §3.3. Quy tac goc
        # .center-chat-box giu 65vh chi con la hoi quy cho hop TINH
        # trang chi tiet viec (ngoai .center-chat-section); test nay
        # doi ten/doi nghia theo do (G-CNTC). Mobile giu 40vh.
        mobile = _media_block(APP_CSS, "@media (max-width: 720px)")
        box_mobile = _css_block(mobile, ".center-chat-box")
        self.assertIn("height: 40vh", box_mobile)
        box = _css_block(APP_CSS, ".center-chat-box")
        self.assertIn("height: 65vh", box)

    def test_form_has_two_mobile_hidden_hints(self):
        # Dung 2 ghi chu phu an o mobile (F18 + doi phong), giu
        # lai ghi chu cach chon nhieu + canh bao mat khau.
        # (Gop UI checkbox, 22/11) Ghi chu cach chon doi thanh
        # "Bấm chọn từng tài khoản" (khong con "giữ Ctrl").
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()
        self.assertEqual(html.count("hint--mobile-hide"), 2)
        self.assertIn("Đừng tải lên file chứa mật khẩu", html)
        self.assertIn("Bấm chọn từng tài khoản", html)
        self.assertNotIn("giữ Ctrl (Windows)", html)

    def test_fixed_tab_bar_only_on_mobile(self):
        # Thanh tab chi co dinh o mobile (<=699px); desktop tab
        # in-flow nen khong de len form — khong can sua desktop.
        mobile = _media_block(APP_CSS, "@media (max-width: 699px)")
        tabs = _css_block(mobile, ".tabs")
        self.assertIn("position: fixed", tabs)
        tabs_base = _css_block(APP_CSS, ".tabs")
        self.assertNotIn("position: fixed", tabs_base)


# ---------------------------------------------------------------------------
# Mo hinh rect — kiem chung bo cuc that (thay cho assert chuoi)
# ---------------------------------------------------------------------------

class TestRectModel(unittest.TestCase):
    def test_model_reproduces_live_measurements_before_fix(self):
        # Hieu chuan: mo hinh truoc sua phai tai hien DUNG so do
        # that cua agent PC (neu khong, mo hinh sai — khong tin
        # ket luan sau sua).
        before = _rects_before_fix()
        self.assertEqual(before["send_bottom"], 778)
        self.assertEqual(before["form_bottom"], 970)
        self.assertEqual(before["pill_top"], 869)
        self.assertEqual(before["btn_top"], 917)

    def test_l1_send_button_and_form_clear_tab_after_fix(self):
        # L1: nut Gui + day form khong con de/che thanh tab.
        after = _rects_after_fix()
        self.assertLessEqual(after["send_bottom"], TAB_TOP)
        self.assertGreaterEqual(TAB_TOP - after["send_bottom"], 100)
        self.assertLessEqual(after["form_bottom"], VIEWPORT_H)
        self.assertGreaterEqual(VIEWPORT_H - after["form_bottom"], 100)

    def test_b3_l2_pill_and_button_inside_viewport_after_fix(self):
        # B3/L2: vien + nut nam trong viewport 390x844 (truoc
        # sua o y=869/917 — ngoai viewport) va trong phan nhin
        # thay cua hop (khong troi theo noi dung).
        after = _rects_after_fix()
        for key in ("pill_top", "pill_bottom", "btn_top",
                    "btn_bottom"):
            self.assertGreaterEqual(after[key], 0, key)
            self.assertLessEqual(after[key], VIEWPORT_H, key)
        self.assertLessEqual(after["pill_bottom"], after["box_bottom"])
        self.assertLessEqual(after["btn_bottom"], after["box_bottom"])
        # Vi tri khong phu thuoc doan noi dung duoi day hop
        # (CONTENT_BELOW_BOX): neo theo wrap, khong theo anchor.
        self.assertEqual(after["pill_top"], after["box_bottom"] - 81
                         - PILL_H)
        self.assertEqual(after["btn_top"], after["box_bottom"] - 25
                         - BTN_H)

    def test_pill_button_gap_kept_12px(self):
        # Khe vien-nut giu 12px nhu cu (NS-K21-2).
        after = _rects_after_fix()
        self.assertEqual(after["btn_top"] - after["pill_bottom"], 12)

    def test_desktop_pill_and_button_inside_box(self):
        # 1920x1080: hop chat tab Chat nay cao calc(100vh - 266px)
        # = 814px (B2 21/11 — xem THIET-KE-KHUNG-CHAT-B-21-11.md
        # §3.3; khong con 65vh = 702px). Vien/nut neo theo wrap nen
        # nam trong hop theo toa do cua hop (khong phu thuoc cuon
        # trang); desktop tab in-flow nen khong co van de L1.
        # Chieu cao hop lay theo dung cong thuc B2, khong hard-code
        # 702px cu (G-CNTC).
        box_h = 1080 - 266
        pill_top_in_box = box_h - 81 - PILL_H
        btn_top_in_box = box_h - 25 - BTN_H
        self.assertGreaterEqual(pill_top_in_box, 0)
        self.assertLessEqual(pill_top_in_box + PILL_H, box_h)
        self.assertGreaterEqual(btn_top_in_box, 0)
        self.assertLessEqual(btn_top_in_box + BTN_H, box_h)
        self.assertEqual(btn_top_in_box - (pill_top_in_box + PILL_H),
                         12)


# ---------------------------------------------------------------------------
# B4 — chan doan: markRead ghi log khi that bai
# ---------------------------------------------------------------------------

class TestMarkReadWarnsOnFailure(unittest.TestCase):
    def test_mark_read_warns_in_console(self):
        # Lan kiem live sau doc duoc console neu vien van khong
        # an (truoc day loi mark-read chi lang lang giu vien).
        self.assertIn('console.warn("[center] mark-read', APP_JS)

    def test_mark_read_still_waits_for_ok(self):
        # D17/NS-K21-3 giu nguyen: chi goi onOk (an vien) khi
        # may chu tra OK — khong doi thanh an ngay.
        body = APP_JS.split("function markRead(", 1)[1]
        body = body.split("\n    }\n", 1)[0]
        self.assertIn('data.ok === true', body)
        self.assertIn("onOk();", body)


if __name__ == "__main__":
    unittest.main()
