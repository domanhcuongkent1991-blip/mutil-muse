#!/usr/bin/env python3
"""Test khung chat phuong an B2 (21/11/2026) — hop cao calc(100vh - 266px),
khung trang 1600px, giu nguyen form day du.

- B2 (quyet chu nhan 13:51 21/11): hop chat tab Chat cao
  ``calc(100vh - 266px)``, toi thieu 400px; khung trang toi da
  1600px; form day du giu nguyen (h2 "Hoi qua trung tam" NGOAI
  form, khong nut "Tuy chon", khong class compact); h1/h2 giu
  nguyen (KHONG B3); mobile <=720px giu 40vh/320px; vien bottom
  81px + nut bottom 25px giu nguyen; 4 hang JS khong doi.
- Thiet ke: ``THIET-KE-KHUNG-CHAT-B-21-11.md`` v0.5, §3.2/§3.3/§3.7.
- TDD: test khung 1600px + hop calc viet TRUOC code (rot truoc),
  test hoi quy con lai ghim hanh vi hien tai khong doi.

unittest, stdlib-only. Moi gia tri secret trong test deu la mau gia
dang ro rang ("TEST-FAKE-..."), khong secret that, khong noi dung
tin that.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pc.ui.ui_static import APP_CSS  # noqa: E402
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
# B2 — khung trang 1600px (§3.2)
# ---------------------------------------------------------------------------

class TestB2Frame(unittest.TestCase):
    def test_body_max_width_1600(self):
        # Khung trang toi da 1600px tu >=1000px (B2 doi tu 1280px).
        self.assertIn("@media (min-width: 1000px) {\n"
                      "  body { max-width: 1600px; }", APP_CSS)
        block = _media_block(APP_CSS, "@media (min-width: 1000px)")
        self.assertNotIn("1280px", block)
        # Quy tac 1100px o >=700px + padding body KHONG DOI.
        self.assertIn("@media (min-width: 700px) {\n"
                      "  body { max-width: 1100px; margin: 0 auto; }",
                      APP_CSS)
        self.assertIn("body {\n  margin: 0; padding: 0 12px 40px;",
                      APP_CSS)


# ---------------------------------------------------------------------------
# B2 — hop chat calc(100vh - 266px) (§3.3) + hoi quy CSS
# ---------------------------------------------------------------------------

class TestB2ChatBoxHeight(unittest.TestCase):
    def test_chat_box_calc_height(self):
        # Chi desktop (>=721px), chi trong .center-chat-section.
        self.assertIn("@media (min-width: 721px)", APP_CSS)
        block = _media_block(APP_CSS, "@media (min-width: 721px)")
        rule = _css_block(block,
                          ".center-chat-section .center-chat-box")
        self.assertTrue(rule, "thieu quy tac hop chat B2")
        self.assertIn("height: calc(100vh - 266px", rule)
        self.assertIn("min-height: 400px", rule)
        # B2 KHONG dung flex cho muc chat (khong quy tac flex nao
        # tren section, ca trong khoi 721px lan toan bo CSS).
        self.assertNotIn("display: flex", block)
        self.assertIsNone(
            re.search(r"\.center-chat-section\s*\{[^}]*"
                      r"display\s*:\s*flex", APP_CSS))

    def test_chat_box_65vh_only_outside_section(self):
        # 65vh chi con 1 cho: quy tac goc .center-chat-box — nay chi
        # con la hoi quy cho hop TINH trang chi tiet viec (ngoai
        # .center-chat-section); hop tab Chat desktop nay la calc.
        self.assertEqual(APP_CSS.count("height: 65vh"), 1)
        base = _css_block(APP_CSS, ".center-chat-box")
        self.assertIn("height: 65vh", base)
        self.assertIn("min-height: 400px", base)
        block = _media_block(APP_CSS, "@media (min-width: 721px)")
        self.assertNotIn("65vh", block)

    def test_mobile_chat_box_unchanged(self):
        # Mobile <=720px giu nguyen 40vh/320px (ngoai pham vi B).
        mobile = _media_block(APP_CSS, "@media (max-width: 720px)")
        self.assertTrue(mobile, "không tìm thấy khối <=720px")
        box = _css_block(mobile, ".center-chat-box")
        self.assertIn("height: 40vh", box)
        self.assertIn("min-height: 320px", box)

    def test_new_pill_and_scroll_button_offsets_unchanged(self):
        # Vien "N tin moi" + nut "Ve cuoi" neo theo wrap nen khong
        # doi so nao khi hop cao len.
        pill = _css_block(APP_CSS, ".center-new-pill")
        self.assertIn("bottom: 81px", pill)
        button = _css_block(APP_CSS, ".center-scroll-bottom")
        self.assertIn("bottom: 25px", button)

    def test_chat_h2_visible_on_desktop(self):
        # B3 KHONG lam theo B2: h2 "Chat" van hien o desktop — khong
        # co quy tac an truc quan h2#center-chat o >=721px, va khong
        # co quy tac display:none nao cho #center-chat trong CSS.
        self.assertIsNone(
            re.search(r"#center-chat\s*\{[^}]*display\s*:\s*none",
                      APP_CSS))
        self.assertIsNone(
            re.search(r"\.center-chat-section\s+h2\s*\{[^}]*"
                      r"display\s*:\s*none", APP_CSS))
        block = _media_block(APP_CSS, "@media (min-width: 721px)")
        self.assertNotIn("display: none", block)


# ---------------------------------------------------------------------------
# B2 — form day du giu nguyen (§3.4) + h2 ngoai form (D-B-21)
# ---------------------------------------------------------------------------

class TestB2ChatFormFull(UiFixDFixture):
    def _chat_html(self):
        session_cookie, _ = self.login()
        return self.get("/", session_cookie,
                        query={"tab": ["chat"]}).body.decode()

    def test_chat_form_unchanged_full(self):
        # Form day du nhu hien tai: h2 "Hoi qua trung tam" ngoai
        # form, nhom checkbox "Gửi cho:" (thay <select multiple>,
        # gop UI checkbox 22/11), 4 hint; KHONG nut "Tuy chon"
        # (B1), KHONG class .center-chat-form--compact (B1).
        html = self._chat_html()
        self.assertIn("<h2>Hỏi qua trung tâm</h2>", html)
        form_start = html.index('<form class="center-chat-form"')
        form = html[form_start:html.index("</form>", form_start)]
        self.assertIn('class="check-group"', form)
        self.assertIn('type="checkbox" name="to_account"', form)
        self.assertNotIn('id="center-hoi-to"', form)
        self.assertNotIn("multiple", form)
        self.assertEqual(form.count('<div class="hint'), 4)
        self.assertNotIn("center-form-toggle", html)
        self.assertNotIn("center-chat-form--compact", html)

    def test_form_h2_stays_outside_form(self):
        # Hoi quy chong ap dung B1 (v0.4 §3.4, D-B-21): h2 "Hoi qua
        # trung tam" giu nguyen vi tri NGOAI <form
        # class="center-chat-form"> nhu hien tai.
        html = self._chat_html()
        h2_pos = html.index("<h2>Hỏi qua trung tâm</h2>")
        form_pos = html.index('<form class="center-chat-form"')
        self.assertLess(h2_pos, form_pos)
        form = html[form_pos:html.index("</form>", form_pos)]
        self.assertNotIn("Hỏi qua trung tâm", form)
