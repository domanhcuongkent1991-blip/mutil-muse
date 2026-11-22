#!/usr/bin/env python3
"""Test chặng T3k (v0.6.0): phong cách C — Superhuman mặc định +
công tắc Hades tối.

unittest, stdlib-only, UI qua UiApp trực tiếp. Không secret thật,
không nội dung tin thật.

Ánh xạ tiêu chí PLAN T3k (16 mục) -> test:
- 1 (NT-01, ảnh duyệt): TestMockupGate
- 2 (công tắc theme): TestThemeSwitch
- 3+9 (23 cặp + gộp 1 lần với cặp T3i): TestContrastMerged (ĐÂY là
  lần chạy gộp duy nhất C-14: 23 cặp T3k + 10 cặp §3.3 + cặp mới
  do T3i thêm + 2 cặp sửa sáng D-Q-V060-41 — T3i không có test
  tương phản riêng; 2 cặp mới do T3i thêm là viên "N tin mới"
  và nút "Về cuối", liệt kê rõ trong test)
- 4 (không còn thẻ trắng): TestNoHardcodedColors
- 5 (tab Trung tâm tím + link cyan tối): TestCenterTabPurple
- 6 (nút chính cyan tối): TestPrimaryDark
- 7 (khung/gap/topbar): TestSizes
- 8 (360px bằng test): TestMobile360
- 10 (bong bóng 75%/85%): TestSizes
- 11 (nút chính 11 vị trí): TestPrimaryDark
- 12 (NT-10, không đổi quyết): TestNoDecisionChange
- 13 (suite >= 1541 xanh): lần chạy toàn bộ suite cuối chặng
- 14 (NT-02, đủ 34 token): TestTokens34
- 15 (NT-08, tối phẳng): TestDarkFlat
- 16 (cổng chặn): đủ 1–15 thì xong (bàn giao, không test riêng)
- D-Q-V060-43 (phòng đang mở tối): TestRoomActiveDark
- D-Q-V060-45 (hover nút chính ĐỂ VÒNG SAU): TestNoDecisionChange
"""

import json
import math
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

PASSWORD = "Test-Pass-2026-T3k"


# ---------------------------------------------------------------------------
# Tương phản WCAG (tính lại độc lập, không tin số thiết kế)
# ---------------------------------------------------------------------------

def _lum(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = f(r), f(g), f(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(fg, bg):
    la, lb = _lum(fg), _lum(bg)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _blend(fg, bg, alpha):
    """Màu hiệu dụng của tint rgba(alpha) pha trên nền bg."""
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3))


def _parse_tokens(block):
    """{token: giá trị} từ 1 khối CSS :root / hades (bỏ comment)."""
    out = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block):
        out[name] = value.strip()
    return out


def _css_blocks():
    css = re.sub(r"/\*.*?\*/", "", APP_CSS, flags=re.S)
    root = re.search(r":root \{.*?\n\}", css, re.S).group(0)
    hades = re.search(r'html\[data-theme="hades"\] \{.*?\n\}', css,
                      re.S).group(0)
    rest = css.replace(root, "").replace(hades, "")
    return _parse_tokens(root), _parse_tokens(hades), rest


# Bảng token DESIGN §8.4: (token, sáng, tối) — tối None = giữ nguyên
# cả 2 theme, không ghi lại trong khối hades (frag-04 §1.1).
# --blue sáng #1a56db (không phải #1a73e8 trong bảng gốc): tối màu
# tối thiểu theo D-Q-V060-41 để đạt 4,5:1 trên #d3e3fd (4,76:1).
TOKENS_34 = [
    ("--page", "#f2f0eb", "#04060f"),
    ("--surface", "#ffffff", "#0d1530"),
    ("--surface2", "#faf9f6", "#111a38"),
    ("--border", "#e3e3e2", "#1e2a52"),
    ("--text", "#292827", "#dbe7ff"),
    ("--gray", "#5f6368", "#7f93c4"),
    ("--muted", "var(--gray)", None),
    ("--accent", "#714cb6", "#38bdf8"),
    ("--purple", "#714cb6", None),
    ("--purple-bg", "#ede7f6", "#12204a"),
    ("--purple-bg-strong", "#d6c8ef", None),
    ("--wine", "#421d24", None),
    ("--primary-bg", "#421d24", "#38bdf8"),
    ("--primary-text", "#ffffff", "#0b1220"),
    ("--secondary-bg", "#ede7f6", "#12204a"),
    ("--secondary-text", "#714cb6", "#a5d8ff"),
    ("--msg-owner-bg", "#ede7f6", "#2a2140"),
    ("--msg-account-bg", "#e3e3e2", "#1b2740"),
    ("--invert-bg", "#292827", "#dbe7ff"),
    ("--invert-text", "#ffffff", "#0b1220"),
    ("--green", "#137333", "#34d399"),
    ("--green-bg", "#ceead6", "rgba(52,211,153,.14)"),
    ("--yellow", "#7a4a00", "#f59e0b"),
    ("--yellow-bg", "#feefc3", "rgba(245,158,11,.14)"),
    ("--red", "#a50e0e", "#f87171"),
    ("--red-bg", "#f9dedc", "rgba(248,113,113,.14)"),
    ("--gray-bg", "#f1f3f4", "rgba(127,147,196,.14)"),
    ("--blue", "#1a56db", "#38bdf8"),
    ("--blue-bg", "#d3e3fd", "rgba(56,189,248,.14)"),
    ("--zalo", "#0068ff", None),
    ("--telegram", "#0097a7", None),
    ("--log-bg", "#0d1117", None),
    ("--log-text", "#e6edf3", None),
    ("--danger-text", "#ffffff", "#0b1220"),
]


# ---------------------------------------------------------------------------
# Fixture (theo mẫu T3i — tự chứa, không import fixture T3i để khỏi
# dính code T3l đội 1 đang sửa trong file test đó)
# ---------------------------------------------------------------------------

class T3kUiFixture(unittest.TestCase):
    """Fixture UI + 2 tài khoản trung tâm (có đủ secret giả)."""

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

    def prefs_path(self):
        return self.root / "ui-config" / "ui_prefs.json"

    def audit_entries(self):
        path = self.root / "ui-config" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

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

    def post_json(self, path, session_cookie, csrf_token, payload):
        body = json.dumps(payload).encode("utf-8")
        return self.app.handle(self.request(
            "POST", path, raw_body=body,
            headers={"Cookie": session_cookie,
                     "Content-Type": "application/json",
                     "X-CSRF-Token": csrf_token}))

    def set_theme(self, session_cookie, csrf_token, theme):
        return self.post("/api/ui-prefs", session_cookie, csrf_token,
                         {"action": "set-theme", "theme": theme,
                          "next": "/settings"})


# ---------------------------------------------------------------------------
# Tiêu chí 14 (NT-02): đủ 34 token, đúng mã hex từng token ở cả 2 theme
# ---------------------------------------------------------------------------

class TestTokens34(unittest.TestCase):
    def test_34_tokens_exact_values(self):
        light, dark, _rest = _css_blocks()
        self.assertEqual(len(TOKENS_34), 34)
        for token, light_value, dark_value in TOKENS_34:
            self.assertIn(token, light, token)
            self.assertEqual(light[token], light_value, token)
            if dark_value is None:
                # Giữ nguyên cả 2 theme: KHÔNG ghi lại trong khối tối.
                self.assertNotIn(token, dark, token)
            else:
                self.assertIn(token, dark, token)
                self.assertEqual(dark[token], dark_value, token)

    def test_count_singles_doubles(self):
        # Đếm lại theo PLAN T3k tiêu chí 14: 14 token đơn (đã gồm
        # --muted) + 10 hàng đôi = 34. "Hàng đôi" = 1 hàng bảng có
        # 2 token (vd --primary-bg/--primary-text); token đơn = 14
        # token còn lại (kể cả token đơn có giá trị tối khác sáng).
        pairs = [("--primary-bg", "--primary-text"),
                 ("--secondary-bg", "--secondary-text"),
                 ("--msg-owner-bg", "--msg-account-bg"),
                 ("--invert-bg", "--invert-text"),
                 ("--green", "--green-bg"),
                 ("--yellow", "--yellow-bg"),
                 ("--red", "--red-bg"),
                 ("--blue", "--blue-bg"),
                 ("--zalo", "--telegram"),
                 ("--log-bg", "--log-text")]
        self.assertEqual(len(pairs), 10)
        in_pairs = {t for pair in pairs for t in pair}
        self.assertEqual(len(in_pairs), 20)
        singles = [t for t in TOKENS_34 if t[0] not in in_pairs]
        self.assertEqual(len(singles), 14)
        self.assertIn(("--muted", "var(--gray)", None), singles)

    def test_muted_is_gray_alias_once(self):
        light, dark, _rest = _css_blocks()
        self.assertEqual(light["--muted"], "var(--gray)")
        self.assertNotIn("--muted", dark)
        self.assertEqual(APP_CSS.count("--muted:"), 1)

    def test_dark_tints_decided_overnight(self):
        # D-Q-V060-40: 2 giá trị tint tối ĐÃ CHỐT.
        _light, dark, _rest = _css_blocks()
        self.assertEqual(dark["--gray-bg"], "rgba(127,147,196,.14)")
        self.assertEqual(dark["--blue-bg"], "rgba(56,189,248,.14)")

    def test_extra_tokens_besides_34(self):
        # Token hóa chấm sức khỏe (D-Q-V060-42) + chấm typing T3i:
        # 3 token dot + 1 token typing, cùng giá trị cả 2 theme,
        # KHÔNG tính vào 34 token bảng §8.4 (ghi rõ ở BAN-GIAO-T3k).
        light, dark, _rest = _css_blocks()
        for token, value in (("--dot-ok", "#34a853"),
                             ("--dot-stale", "#fbbc04"),
                             ("--dot-dead", "#ea4335"),
                             ("--typing", "#16a34a")):
            self.assertEqual(light[token], value, token)
            self.assertNotIn(token, dark, token)
        self.assertEqual(len(light), 34 + 4)

    def test_no_grid_or_glow_tokens(self):
        # Q8: lưới/phát sáng BỎ — token --grid/--glow không tồn tại.
        light, dark, _rest = _css_blocks()
        for token in ("--grid", "--glow", "--accent2"):
            self.assertNotIn(token, light)
            self.assertNotIn(token, dark)


# ---------------------------------------------------------------------------
# Tiêu chí 3 + 9 (C-14): GỘP test tương phản 1 LẦN ở cuối — 23 cặp T3k
# + 10 cặp §3.3 + cặp mới do T3i thêm + 2 cặp sửa sáng D-Q-V060-41.
# T3i không có test tương phản riêng (grep test_center_t3i.py = 0);
# 2 cặp mới do T3i thêm là viên "N tin mới" và nút "Về cuối".
# ---------------------------------------------------------------------------

# 23 cặp frag-04 §6.3: (chữ, nền, tỉ lệ thiết kế, ghi chú).
# Cặp 19–23 nền là màu hiệu dụng sau khi pha tint 14% trên #0d1530.
PAIRS_23 = [
    ("#38bdf8", "#04060f", 9.44, "link cyan/nền trang"),
    ("#0b1220", "#38bdf8", 8.74, "chữ nút chính/nút cyan"),
    ("#0b1220", "#dbe7ff", 15.06, "tab Tất cả active"),
    ("#0b1220", "#f87171", 6.77, "chữ huy hiệu/nền đỏ"),
    ("#0b1220", "#f87171", 6.77, "chữ nút nguy hiểm/nền đỏ"),
    ("#0b1220", "#0097a7", 5.34, "tab Telegram/huy hiệu Telegram"),
    ("#0b1220", "#7f93c4", 6.13, "huy hiệu xám"),
    ("#f87171", "#0b1220", 6.77, "chữ đỏ/huy hiệu tab Cảnh báo active"),
    ("#7f93c4", "#2a2140", 4.94, "chữ phụ/bong bóng chủ nhân (sát)"),
    ("#7f93c4", "#1b2740", 4.87, "chữ phụ/bong bóng tài khoản (sát)"),
    ("#38bdf8", "#2a2140", 7.05, "tên người gửi/bong bóng chủ nhân"),
    ("#dbe7ff", "#2a2140", 12.15, "chữ/bong bóng chủ nhân"),
    ("#dbe7ff", "#1b2740", 11.97, "chữ/bong bóng tài khoản"),
    ("#34d399", "#0d1530", 9.36, "chip xanh lá/thẻ"),
    ("#f59e0b", "#0d1530", 8.38, "chip vàng/thẻ"),
    ("#f87171", "#0d1530", 6.51, "chip đỏ/thẻ"),
    ("#7f93c4", "#0d1530", 5.89, "chip xám/thẻ"),
    ("#38bdf8", "#0d1530", 8.40, "chip xanh dương/thẻ"),
    ("#7f93c4", _blend("#7f93c4", "#0d1530", .14), 4.81,
     "chữ phụ/tint xám hiệu dụng (sát)"),
    ("#34d399", _blend("#34d399", "#0d1530", .14), 7.19,
     "chip xanh lá/tint xanh lá hiệu dụng"),
    ("#f59e0b", _blend("#f59e0b", "#0d1530", .14), 6.74,
     "chip vàng/tint vàng hiệu dụng"),
    ("#f87171", _blend("#f87171", "#0d1530", .14), 5.41,
     "chip đỏ/tint đỏ hiệu dụng"),
    ("#38bdf8", _blend("#38bdf8", "#0d1530", .14), 6.50,
     "chip xanh dương/tint xanh hiệu dụng"),
]

PAIRS_OLD_33 = [
    ("#dbe7ff", "#04060f", 16.27),
    ("#7f93c4", "#04060f", 6.62),
    ("#dbe7ff", "#0d1530", 14.47),
    ("#7f93c4", "#0d1530", 5.89),
    ("#a5d8ff", "#12204a", 10.42),
    ("#e6edf3", "#2a2140", 12.79),
    ("#e6edf3", "#1b2740", 12.60),
    ("#ffffff", "#421d24", 14.66),
    ("#ffffff", "#714cb6", 6.16),
    ("#714cb6", "#ede7f6", 5.09),
]


class TestContrastMerged(unittest.TestCase):
    """Bộ test tương phản gộp 1 lần (C-14 — T3k sở hữu)."""

    def test_23_pairs_dark_all_pass(self):
        self.assertEqual(len(PAIRS_23), 23)
        for fg, bg, claimed, note in PAIRS_23:
            got = _ratio(fg, bg)
            self.assertAlmostEqual(got, claimed, delta=0.15,
                                   msg="%s: %s/%s" % (note, fg, bg))
            self.assertGreaterEqual(got, 4.5, "%s: %s/%s" % (note, fg, bg))

    def test_three_near_standard_pairs_flagged(self):
        # 3 cặp sát chuẩn 4,81–4,94:1 (DESIGN §8.4) — đạt nhưng sát,
        # test phải đo lại mỗi lần sửa token (không nới ngưỡng).
        near = [PAIRS_23[i] for i in (8, 9, 18)]
        for fg, bg, claimed, note in near:
            got = _ratio(fg, bg)
            self.assertGreaterEqual(got, 4.5, note)
            self.assertLess(got, 5.0, note + " (phải còn sát chuẩn)")
            self.assertAlmostEqual(got, claimed, delta=0.15, msg=note)

    def test_old_section_33_pairs_still_pass(self):
        for fg, bg, claimed in PAIRS_OLD_33:
            got = _ratio(fg, bg)
            self.assertAlmostEqual(got, claimed, delta=0.15,
                                   msg="%s/%s" % (fg, bg))
            self.assertGreaterEqual(got, 4.5, "%s/%s" % (fg, bg))

    def test_t3i_new_pairs_merged(self):
        # Cặp mới do T3i thêm (không có trong bảng 23 của T3k):
        # viên "N tin mới" (trắng trên đỏ rượu) + nút "Về cuối"
        # (tím trên trắng). Chấm typing + viền phòng đang mở là
        # yếu tố không phải chữ (chuẩn 3:1).
        self.assertAlmostEqual(_ratio("#ffffff", "#421d24"), 14.66,
                               delta=0.15)
        self.assertAlmostEqual(_ratio("#714cb6", "#ffffff"), 6.16,
                               delta=0.15)
        self.assertGreaterEqual(_ratio("#16a34a", "#0d1530"), 3.0)
        self.assertGreaterEqual(_ratio("#d6c8ef", "#0d1530"), 3.0)
        self.assertGreaterEqual(_ratio("#d6c8ef", "#111a38"), 3.0)

    def test_light_pairs_fixed_by_overnight_decision(self):
        # D-Q-V060-41: 2 cặp dưới chuẩn ở theme sáng — sửa luôn.
        self.assertAlmostEqual(_ratio("#0b1220", "#0097a7"), 5.34,
                               delta=0.15)
        self.assertGreaterEqual(_ratio("#0b1220", "#0097a7"), 4.5)
        self.assertAlmostEqual(_ratio("#1a56db", "#d3e3fd"), 4.76,
                               delta=0.15)
        self.assertGreaterEqual(_ratio("#1a56db", "#d3e3fd"), 4.5)
        # Giá trị cũ PHẢI dưới chuẩn (chứng minh test bắt được lỗi).
        self.assertLess(_ratio("#ffffff", "#0097a7"), 4.5)
        self.assertLess(_ratio("#1a73e8", "#d3e3fd"), 4.5)
        # NS-K1: ghim đúng quy tắc CSS (không chỉ tỉ lệ số học) —
        # nếu code bị trả về chữ trắng thì test này đỏ.
        self.assertIn(
            '.tab.active[data-tab="telegram"] { background: '
            'var(--telegram); color: #0b1220; }', APP_CSS)
        self.assertIn(
            '.plat.telegram { background: var(--telegram); '
            'color: #0b1220; }', APP_CSS)

    def test_alerts_tab_dark_text_fixed(self):
        # §2.7 bỏ sót quy tắc tab Cảnh báo active ở theme tối:
        # chữ trắng trên --red tối chỉ 2,77:1 -> chữ tối #0b1220.
        self.assertLess(_ratio("#ffffff", "#f87171"), 4.5)
        self.assertAlmostEqual(_ratio("#0b1220", "#f87171"), 6.77,
                               delta=0.15)
        # NS-K1: ghim đúng quy tắc CSS (không chỉ tỉ lệ số học).
        self.assertIn(
            'html[data-theme="hades"] .tab.active[data-tab="alerts"] {\n'
            '  color: #0b1220;\n}', APP_CSS)

    def test_documented_accepted_low_pairs(self):
        # GC-18: huy hiệu đỏ rượu trên nền thẻ tối chỉ 1,23:1 —
        # CHẤP NHẬN vì đọc bằng chữ trắng 14,66:1, không bằng hình.
        # Nền phòng đang mở trên nền thẻ tối chỉ 1,14:1 (D-Q-V060-43)
        # — nhận biết bằng viền #d6c8ef (11,45:1), không bằng nền.
        badge_bg = _ratio("#421d24", "#0d1530")
        self.assertGreater(badge_bg, 1.0)
        self.assertLess(badge_bg, 1.5)
        self.assertGreaterEqual(_ratio("#ffffff", "#421d24"), 4.5)
        room_bg = _ratio("#12204a", "#0d1530")
        self.assertGreater(room_bg, 1.0)
        self.assertLess(room_bg, 1.5)
        self.assertGreaterEqual(_ratio("#d6c8ef", "#0d1530"), 3.0)

    def test_purple_link_would_fail_dark_without_split(self):
        # Lý do tách --purple làm hai: tím trên nền tối chỉ 3,29:1.
        self.assertLess(_ratio("#714cb6", "#04060f"), 4.5)
        self.assertGreaterEqual(_ratio("#38bdf8", "#04060f"), 4.5)


# ---------------------------------------------------------------------------
# Tiêu chí 4 (NT-03): không còn thẻ trắng / màu ghi cứng ở theme tối
# ---------------------------------------------------------------------------

class TestNoHardcodedColors(unittest.TestCase):
    def test_no_hardcoded_background_or_border(self):
        # Sau A6 + §2.6.1: ngoài 2 khối token, mã hex chỉ còn là CHỮ
        # trên nền màu (#fff / #0b1220 — ngoại lệ §2.6.1 ghi rõ) và
        # đúng 1 nền huy hiệu tab Cảnh báo active theo §2.7 hàng 2
        # (#fff sáng / #0b1220 tối). Mọi nền/border khác = 0 hex.
        _light, _dark, rest = _css_blocks()
        bg_hex = re.findall(r"background[^:;]*:\s*(#[0-9a-fA-F]+)", rest)
        self.assertEqual(sorted(set(bg_hex)), ["#0b1220", "#fff"])
        self.assertEqual(
            re.findall(r"border[^:;]*:\s*[^;]*(#[0-9a-fA-F]+)", rest), [])
        colors = set(re.findall(
            r"(?<!background-)color:\s*(#[0-9a-fA-F]+)", rest))
        self.assertEqual(colors, {"#fff", "#0b1220"})

    def test_six_hardcoded_spots_now_tokens(self):
        # DESIGN §8.3: 6 chỗ ghi cứng -> biến token (đúng từng rule).
        self.assertRegex(
            APP_CSS,
            r"\.card \{\n  background: var\(--surface\);")
        self.assertRegex(
            APP_CSS,
            r"\.stat \{\n  background: var\(--surface2\);")
        self.assertRegex(
            APP_CSS,
            r"\.msg--account \{\n  margin-right: auto;"
            r" background: var\(--msg-account-bg\);")
        self.assertRegex(
            APP_CSS,
            r"\.center-file-chip \{\n  background: var\(--surface\);")
        self.assertIn(".center-chat-form {\n"
                      "  position: sticky; bottom: 0;"
                      " background: var(--page);", APP_CSS)
        self.assertIn("background: var(--surface);", APP_CSS)

    def test_ink_and_muted_fixed(self):
        # §2.6.2: 2 biến dùng mà chưa định nghĩa.
        self.assertNotIn("var(--ink)", APP_CSS)
        self.assertIn("color: var(--text);", APP_CSS)
        self.assertIn("--muted: var(--gray);", APP_CSS)

    def test_primary_blue_class_removed(self):
        # CH-04: class .primary xanh cũ đã xóa (chỉ còn .primary-action
        # + token --primary-bg/--primary-text).
        _light, _dark, rest = _css_blocks()
        self.assertEqual(re.findall(r"\.primary(?![-a-z])", rest), [])
        self.assertNotIn("button.primary,", APP_CSS)
        self.assertIn("button.primary-action,", APP_CSS)

    def test_dots_tokenized(self):
        # D-Q-V060-42: chấm sức khỏe token hóa.
        self.assertIn(".dot.ok { background: var(--dot-ok); }", APP_CSS)
        self.assertIn(".dot.stale { background: var(--dot-stale); }",
                      APP_CSS)
        self.assertIn(".dot.dead { background: var(--dot-dead); }",
                      APP_CSS)

    def test_no_hex_in_css_comments(self):
        # Comment không được chứa mã màu dạng khai báo CSS (vd
        # "background: #fff") — test grep ngây thơ sẽ đỏ oan. Mã màu
        # nhắc trong câu văn (vd màu chat T3l) thì được, vì test
        # NT-03 đã bỏ comment trước khi grep.
        comments = re.findall(r"/\*.*?\*/", APP_CSS, flags=re.S)
        self.assertTrue(comments)
        for comment in comments:
            self.assertEqual(
                re.findall(r":\s*#[0-9a-fA-F]{3,8}\b", comment), [],
                comment[:100])


# ---------------------------------------------------------------------------
# Tiêu chí 15 (NT-08): theme tối phẳng
# ---------------------------------------------------------------------------

class TestDarkFlat(unittest.TestCase):
    def test_box_shadow_only_mobile_tab_bar(self):
        # Chỉ còn đúng 1 chỗ: bóng thanh tab mobile.
        self.assertEqual(APP_CSS.count("box-shadow"), 1)
        self.assertIn("0 4px 16px rgba(0, 0, 0, 0.25)", APP_CSS)

    def test_no_gradient_or_background_image(self):
        self.assertEqual(APP_CSS.count("linear-gradient"), 0)
        self.assertEqual(APP_CSS.count("background-image"), 0)

    def test_no_blur_on_topbar(self):
        self.assertNotIn("backdrop-filter", APP_CSS)
        self.assertNotIn("blur(", APP_CSS)

    def test_no_hover_on_primary_action(self):
        # D-Q-V060-45: hover nút chính ĐỂ VÒNG SAU — không làm ở T3k.
        self.assertNotIn(".primary-action:hover", APP_CSS)
        self.assertNotIn(":hover", APP_CSS)


# ---------------------------------------------------------------------------
# Tiêu chí 5 + 6 + 11: tím nhận diện giữ nguyên, link/nút chính tối cyan
# ---------------------------------------------------------------------------

class TestCenterTabPurple(unittest.TestCase):
    def test_center_tab_keeps_purple_both_themes(self):
        light, dark, _rest = _css_blocks()
        self.assertEqual(light["--purple"], "#714cb6")
        self.assertNotIn("--purple", dark)
        self.assertIn(
            '.tab.active[data-tab="center"] { background: var(--purple);'
            " color: #fff; }",
            APP_CSS)

    def test_links_use_accent_cyan_dark(self):
        light, dark, _rest = _css_blocks()
        self.assertEqual(light["--accent"], "#714cb6")
        self.assertEqual(dark["--accent"], "#38bdf8")
        self.assertIn("a { color: var(--accent); }", APP_CSS)

    def test_mention_badge_keeps_purple(self):
        self.assertIn(
            ".room-badge--mention { margin-left: 4px;"
            " background: var(--purple); }",
            APP_CSS)


class TestPrimaryDark(unittest.TestCase):
    def test_primary_rule_uses_tokens(self):
        self.assertIn(
            "background: var(--primary-bg);"
            " border-color: var(--primary-bg);",
            APP_CSS)
        self.assertIn("color: var(--primary-text);", APP_CSS)

    def test_primary_dark_values(self):
        light, dark, _rest = _css_blocks()
        self.assertEqual((light["--primary-bg"],
                          light["--primary-text"]),
                         ("#421d24", "#ffffff"))
        self.assertEqual((dark["--primary-bg"], dark["--primary-text"]),
                         ("#38bdf8", "#0b1220"))

    def test_primary_action_present_on_key_pages(self):
        # 11 vị trí .primary-action (SPEC TT11): nút chính dùng 1
        # token nên đổi 1 chỗ là đổi cả 11 — kiểm có mặt ở các
        # trang chính (đăng nhập, gửi hoi, lưu cài đặt).
        self.assertIn('class="primary-action"', ui_pages.login_page_html())
        html = ui_pages.ui_settings_page_html({}, "TEST-CSRF")
        self.assertIn('class="primary-action"', html)


# ---------------------------------------------------------------------------
# Tiêu chí 2 (NT-05): công tắc theme ở tab Cài đặt, nhóm "Giao diện"
# ---------------------------------------------------------------------------

class TestThemeSwitch(T3kUiFixture):
    def test_settings_has_theme_switch(self):
        session_cookie, csrf = self.login()
        html = self.get("/settings", session_cookie).body.decode()
        self.assertIn('id="nhom-giao-dien"', html)
        self.assertIn('action="/api/ui-prefs"', html)
        self.assertIn('name="action" value="set-theme"', html)
        self.assertIn('name="theme" value="light"', html)
        self.assertIn('name="theme" value="dark"', html)
        self.assertIn("Sáng Superhuman", html)
        self.assertIn("Tối Hades", html)
        self.assertIn("Lưu", html)
        # Công tắc nằm TRƯỚC bảng 13 node trong nhóm Giao diện.
        self.assertLess(html.index("Tối Hades"),
                        html.index("<table>"))

    def test_switch_dark_takes_effect_and_persists(self):
        session_cookie, csrf = self.login()
        resp = self.set_theme(session_cookie, csrf, "dark")
        self.assertEqual(resp.status, 302, resp.body[:200])
        self.assertEqual(resp.headers["Location"], "/settings")
        self.assertEqual(
            json.loads(self.prefs_path().read_text())["theme"], "dark")
        page = self.get("/", session_cookie).body.decode()
        self.assertIn('<html lang="vi" data-theme="hades">', page)
        settings = self.get("/settings", session_cookie).body.decode()
        self.assertIn('value="dark" checked', settings)
        # Đổi ở 1 "thiết bị" thì thiết bị khác thấy cùng theme sau
        # khi tải lại (cùng file phía máy chủ).
        session2, _csrf2 = self.login()
        page2 = self.get("/", session2).body.decode()
        self.assertIn('<html lang="vi" data-theme="hades">', page2)

    def test_switch_back_to_light(self):
        session_cookie, csrf = self.login()
        self.set_theme(session_cookie, csrf, "dark")
        resp = self.set_theme(session_cookie, csrf, "light")
        self.assertEqual(resp.status, 302)
        page = self.get("/", session_cookie).body.decode()
        self.assertIn('<html lang="vi" data-theme="light">', page)

    def test_theme_survives_logout_and_polling(self):
        session_cookie, csrf = self.login()
        self.set_theme(session_cookie, csrf, "dark")
        self.app.handle(self.request(
            "POST", "/logout", form={"csrf_token": csrf},
            headers={"Cookie": session_cookie}))
        session2, _csrf2 = self.login()
        page = self.get("/", session2).body.decode()
        self.assertIn('<html lang="vi" data-theme="hades">', page)
        # Fragment polling 5 giây không reset theme.
        self.get("/fragments/dashboard", session2, query={"tab": ["all"]})
        page = self.get("/", session2).body.decode()
        self.assertIn('<html lang="vi" data-theme="hades">', page)

    def test_invalid_theme_rejected_no_write(self):
        session_cookie, csrf = self.login()
        for bad in ("hades", "neon", "", "DARK", "dark "):
            resp = self.set_theme(session_cookie, csrf, bad)
            self.assertEqual(resp.status, 400, bad)
        self.assertFalse(self.prefs_path().exists())
        self.assertEqual(self.audit_entries(), [])

    def test_set_theme_requires_csrf(self):
        session_cookie, csrf = self.login()
        resp = self.post_no_csrf(
            "/api/ui-prefs", session_cookie,
            {"action": "set-theme", "theme": "dark", "next": "/"})
        self.assertIn(resp.status, (400, 403))
        self.assertFalse(self.prefs_path().exists())

    def test_set_theme_json(self):
        session_cookie, csrf = self.login()
        resp = self.post_json(
            "/api/ui-prefs", session_cookie, csrf,
            {"action": "set-theme", "theme": "dark", "next": "/"})
        self.assertEqual(resp.status, 200, resp.body[:200])
        self.assertEqual(ui_store.load_ui_theme(self.root), "dark")

    def test_theme_change_writes_one_audit_line(self):
        # D-Q-V060-44: đổi theme ghi 1 dòng audit.
        session_cookie, csrf = self.login()
        self.set_theme(session_cookie, csrf, "dark")
        entries = self.audit_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["action"], "ui-prefs-theme")
        self.assertEqual(entry["bot_id"], "center")
        self.assertEqual(entry["before"], "light")
        self.assertEqual(entry["after"], "dark")
        # Lưu lại cùng giá trị thì KHÔNG phải "đổi" — không ghi thêm.
        self.set_theme(session_cookie, csrf, "dark")
        self.assertEqual(len(self.audit_entries()), 1)
        # Toggle node thì không ghi audit (quy tắc T3 cũ).
        self.post("/api/ui-prefs", session_cookie, csrf,
                  {"node": "activity", "collapsed": "true", "next": "/"})
        self.assertEqual(len(self.audit_entries()), 1)

    def test_theme_preserves_collapsed_and_unknown_keys(self):
        session_cookie, csrf = self.login()
        self.post("/api/ui-prefs", session_cookie, csrf,
                  {"node": "activity", "collapsed": "false", "next": "/"})
        self.prefs_path().write_text(json.dumps({
            "version": 1,
            "collapsed": {"activity": True},
            "theme": "light", "ghi-chu-rieng": "giu-nguyen"}),
            encoding="utf-8")
        self.set_theme(session_cookie, csrf, "dark")
        data = json.loads(self.prefs_path().read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["collapsed"], {"activity": True})
        self.assertEqual(data.get("ghi-chu-rieng"), "giu-nguyen")
        # Toggle node sau đó cũng không làm mất theme/khóa lạ.
        self.post("/api/ui-prefs", session_cookie, csrf,
                  {"node": "bot-cards", "collapsed": "true", "next": "/"})
        data = json.loads(self.prefs_path().read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data.get("ghi-chu-rieng"), "giu-nguyen")

    def test_reset_defaults_keeps_theme(self):
        # frag-04 §3.2: "Về mặc định" chỉ reset 13 node, KHÔNG reset
        # theme (muốn về sáng thì chọn Sáng + bấm Lưu).
        session_cookie, csrf = self.login()
        self.set_theme(session_cookie, csrf, "dark")
        self.post("/api/ui-prefs", session_cookie, csrf,
                  {"node": "activity", "collapsed": "false", "next": "/"})
        resp = self.post("/api/ui-prefs", session_cookie, csrf,
                         {"action": "reset-defaults", "next": "/settings"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(ui_store.load_ui_theme(self.root), "dark")
        prefs = ui_store.load_ui_prefs(self.root)
        self.assertTrue(prefs["activity"])
        page = self.get("/", session_cookie).body.decode()
        self.assertIn('<html lang="vi" data-theme="hades">', page)

    def test_missing_or_bad_theme_falls_back_to_light(self):
        session_cookie, csrf = self.login()
        self.assertEqual(ui_store.load_ui_theme(self.root), "light")
        self.prefs_path().write_text(json.dumps({
            "version": 1, "collapsed": {}, "theme": "neon"}),
            encoding="utf-8")
        self.assertEqual(ui_store.load_ui_theme(self.root), "light")
        page = self.get("/", session_cookie).body.decode()
        self.assertIn('<html lang="vi" data-theme="light">', page)
        # File JSON hỏng hẳn -> về light + mặc định 13 node, trang 200.
        self.prefs_path().write_text("{hong", encoding="utf-8")
        self.assertEqual(ui_store.load_ui_theme(self.root), "light")
        page = self.get("/", session_cookie).body.decode()
        self.assertEqual(self.get("/", session_cookie).status, 200)

    def test_login_page_follows_stored_theme(self):
        # Trang đăng nhập cũng theo theme đã chọn (không theo phiên).
        session_cookie, csrf = self.login()
        self.set_theme(session_cookie, csrf, "dark")
        resp = self.app.handle(self.request("GET", "/login"))
        self.assertIn(b'data-theme="hades"', resp.body)
        self.set_theme(session_cookie, csrf, "light")
        resp = self.app.handle(self.request("GET", "/login"))
        self.assertIn(b'data-theme="light"', resp.body)

    def test_render_both_themes_200(self):
        # Test render 2 theme (frag-04 §6.4.1): trang chính, chi tiết
        # bot, tab Chat, /settings, đăng nhập — cả 2 theme đều 200.
        session_cookie, csrf = self.login()
        for theme in ("light", "dark"):
            self.set_theme(session_cookie, csrf, theme)
            for path, query in (("/", None), ("/settings", None),
                                ("/", {"tab": ["chat"]}),
                                ("/bots/bot-a", None)):
                resp = self.get(path, session_cookie, query=query)
                self.assertEqual(resp.status, 200, (theme, path, query))
                self.assertIn(
                    ('data-theme="%s"' % ui_pages.ui_data_theme(theme)
                     ).encode(), resp.body)
            login = self.app.handle(self.request("GET", "/login"))
            self.assertEqual(login.status, 200)

    def test_data_theme_mapping(self):
        self.assertEqual(ui_pages.ui_data_theme("light"), "light")
        self.assertEqual(ui_pages.ui_data_theme("dark"), "hades")
        self.assertEqual(ui_pages.ui_data_theme("hades"), "light")
        self.assertEqual(ui_pages.ui_data_theme(""), "light")
        self.assertEqual(ui_pages.ui_data_theme(None), "light")


# ---------------------------------------------------------------------------
# Tiêu chí 7 + 10: khung 1600px (B2 21/11 doi tu 1280px), gap 16px desktop,
# topbar dính desktop, bong bóng 75%/85%
# ---------------------------------------------------------------------------

class TestSizes(unittest.TestCase):
    def test_frame_1600_from_1000px(self):
        # B2 (21/11): khung trang 1280px -> 1600px — xem
        # THIET-KE-KHUNG-CHAT-B-21-11.md §3.2. Quy tac 1100px o
        # >=700px KHONG DOI (G-CNTC: sua test ghim gia tri cu,
        # khong go lang le).
        self.assertIn("@media (min-width: 1000px) {\n"
                      "  body { max-width: 1600px; }", APP_CSS)
        self.assertIn("@media (min-width: 700px) {\n"
                      "  body { max-width: 1100px; margin: 0 auto; }",
                      APP_CSS)

    def test_section_gap_16_only_desktop(self):
        self.assertIn("@media (min-width: 1000px) {\n"
                      "  .node, .stats { margin: 16px 0; }\n"
                      "  .cards { gap: 16px; }", APP_CSS)
        self.assertIn(".node { margin: 12px 0; }", APP_CSS)

    def test_topbar_sticky_only_desktop_no_blur(self):
        sticky = APP_CSS.count("position: sticky")
        # SUA B3 21/11: 2 = topbar + ô nhập chat (anchor sticky
        # .center-chat-anchor da bo — vien + nut neo theo wrap
        # bang absolute, khong dung sticky nua).
        self.assertEqual(sticky, 2)
        self.assertIn("@media (min-width: 700px) {\n"
                      "  .topbar {\n"
                      "    position: sticky; top: 0;"
                      " background: var(--page); z-index: 5;",
                      APP_CSS)

    def test_bubbles_75_desktop_85_below(self):
        self.assertIn(".msg--owner, .msg--account { max-width: 85%;",
                      APP_CSS)
        self.assertIn("@media (min-width: 1000px) {\n"
                      "  .msg--owner, .msg--account { max-width: 75%; }",
                      APP_CSS)

    def test_chat_room_column_300px(self):
        # NT-04 (T3i đã làm, T3k giữ nguyên): cột phòng 300px.
        self.assertIn(".center-chat-rooms { flex: 0 0 300px;", APP_CSS)


# ---------------------------------------------------------------------------
# Tiêu chí 8: kiểm 360px bằng test (công tắc theme + 6 tab không tràn)
# ---------------------------------------------------------------------------

class TestMobile360(T3kUiFixture):
    def test_theme_switch_usable_at_360(self):
        # Form công tắc là radio + nút Lưu thường (không min-width
        # cố định, không fixed-width) nên vừa màn 360px; trang
        # /settings render đủ 2 lựa chọn.
        session_cookie, _ = self.login()
        html = self.get("/settings", session_cookie).body.decode()
        self.assertIn('type="radio" name="theme" value="light"', html)
        self.assertIn('type="radio" name="theme" value="dark"', html)
        self.assertIn('class="primary-action"', html)

    def test_six_tabs_fit_one_row_at_360(self):
        # Thanh tab mobile (T3i): chữ 12px, đệm hẹp, 6 tab 1 hàng.
        self.assertIn("@media (max-width: 699px) {", APP_CSS)
        self.assertIn("font-size: 12px; font-weight: 700;", APP_CSS)
        session_cookie, _ = self.login()
        html = self.get("/", session_cookie).body.decode()
        tabs = re.findall(r'data-tab="([^"]+)"', html)
        self.assertEqual(len(tabs), 6)

    def test_topbar_not_sticky_below_700(self):
        # Topbar dính CHỈ trong @media (min-width: 700px); dưới
        # 700px topbar trôi như cũ (không có quy tắc sticky nào
        # ngoài khối đó cho .topbar).
        blocks = re.findall(
            r"@media \(min-width: 700px\) \{(.*?)\n\}", APP_CSS, re.S)
        self.assertTrue(any(".topbar" in b and "sticky" in b
                            for b in blocks))
        outside = re.sub(r"@media \(min-width: 700px\) \{.*?\n\}",
                         "", APP_CSS, flags=re.S)
        self.assertNotIn(".topbar {\n    position: sticky", outside)

    def test_chat_stacks_vertically_below_720(self):
        self.assertIn("@media (max-width: 720px) {\n"
                      "  .center-chat-layout { flex-direction: column; }",
                      APP_CSS)
        # (SUA L1, 21/11) Hop mobile 50vh -> 40vh de nut Gui sach
        # thanh tab co dinh o vi tri doc tu nhien.
        self.assertIn("  .center-chat-box { height: 40vh;"
                      " min-height: 320px; }", APP_CSS)


# ---------------------------------------------------------------------------
# Tiêu chí 12 (NT-10) + D-Q-V060-45: không đổi quyết đã chốt
# ---------------------------------------------------------------------------

class TestNoDecisionChange(unittest.TestCase):
    def test_default_theme_still_light(self):
        light, _dark, _rest = _css_blocks()
        self.assertEqual(light["--page"], "#f2f0eb")
        self.assertEqual(ui_store.UI_THEME_DEFAULT, "light")

    def test_no_high_contrast_variant(self):
        self.assertNotIn("high-contrast", APP_CSS)
        self.assertNotIn("data-theme=\"high", APP_CSS)
        self.assertNotIn("prefers-contrast", APP_CSS)

    def test_platform_colors_unchanged(self):
        light, dark, _rest = _css_blocks()
        self.assertEqual(light["--zalo"], "#0068ff")
        self.assertEqual(light["--telegram"], "#0097a7")
        self.assertNotIn("--zalo", dark)
        self.assertNotIn("--telegram", dark)

    def test_font_and_radius_unchanged(self):
        self.assertIn("font-size: 18px; line-height: 1.55;", APP_CSS)
        self.assertIn("border-radius: 16px;", APP_CSS)
        self.assertIn("h1 { font-size: 28px;", APP_CSS)

    def test_no_auto_theme_by_os_or_time(self):
        self.assertNotIn("prefers-color-scheme", APP_CSS)
        self.assertNotIn("prefers-color-scheme", APP_JS)
        self.assertNotIn("localStorage", APP_JS)
        # (Link chung 21/11 — A-NS-01: dong assertNotIn
        # "matchMedia" cu bo vi APP_JS nay dung matchMedia
        # "(min-width: 721px)" CHI de doc CO MAN (sua
        # aria-expanded khi unset), khong doc theme; tu dong
        # doi theme theo OS/time van bi 2 assert
        # "prefers-color-scheme" o tren chan.)
        self.assertNotIn('matchMedia("(prefers-color-scheme', APP_JS)


# ---------------------------------------------------------------------------
# D-Q-V060-43 + tiêu chí 1 (NT-01): phòng đang mở ở theme tối giữ như
# thiết kế (nhận biết bằng viền) — kiểm lại trên ảnh minh họa tối.
# ---------------------------------------------------------------------------

class TestRoomActiveDark(unittest.TestCase):
    def test_active_room_uses_border_tokens(self):
        self.assertIn(
            ".room-row--active { background: var(--purple-bg);"
            " border-color: var(--purple-bg-strong); }",
            APP_CSS)
        light, dark, _rest = _css_blocks()
        self.assertEqual(dark["--purple-bg"], "#12204a")
        self.assertEqual(light["--purple-bg-strong"], "#d6c8ef")
        self.assertNotIn("--purple-bg-strong", dark)

    def test_active_room_border_contrast_is_the_cue(self):
        # Nền chỉ 1,14:1 nên viền #d6c8ef (11,45:1 so với thẻ tối)
        # là dấu hiệu nhận biết chính — đúng thiết kế đã duyệt.
        self.assertAlmostEqual(_ratio("#12204a", "#0d1530"), 1.14,
                               delta=0.05)
        self.assertAlmostEqual(_ratio("#d6c8ef", "#0d1530"), 11.45,
                               delta=0.15)


class TestMockupGate(unittest.TestCase):
    def test_mockups_approved_before_code(self):
        # NT-01: ảnh minh họa sáng + tối đã vẽ + duyệt (ủy quyền qua
        # đêm 20/11, QUYET-DINH-QUA-DEM-20-11.md) trước khi code.
        light = DESIGN_ROOT / "mockups-v060" / (
            "media-generation-mockup-tab-chat-sang-0-bcd35e3f-"
            "fd80-470d-83bb-206a7907f6ac.webp")
        dark = DESIGN_ROOT / "mockups-v060" / (
            "media-generation-mockup-tab-chat-toi-0-651f78a9-"
            "0549-489e-bf29-a1b2247823dc.webp")
        for path in (light, dark):
            self.assertTrue(path.exists(), path)
            self.assertGreater(path.stat().st_size, 10000, path)
        decisions = (DESIGN_ROOT / "QUYET-DINH-QUA-DEM-20-11.md"
                     ).read_text(encoding="utf-8")
        self.assertIn("media-generation-mockup-tab-chat-toi-0-"
                      "651f78a9-0549-489e-bf29-a1b2247823dc.webp",
                      decisions)
        self.assertIn("Duyệt làm mẫu theme tối", decisions)


# ---------------------------------------------------------------------------
# T3i đã gộp xong (tiền đề tiêu chí 9): tab Chat riêng có trong cây
# ---------------------------------------------------------------------------

class TestT3iMerged(T3kUiFixture):
    def test_chat_tab_exists_in_tree(self):
        session_cookie, _ = self.login()
        resp = self.get("/", session_cookie, query={"tab": ["chat"]})
        self.assertEqual(resp.status, 200)
        html = resp.body.decode()
        self.assertIn("center-chat-layout", html)
        self.assertIn("center-chat-box", html)
        # (Gop UI checkbox, 22/11) O "Gửi cho:" la nhom checkbox.
        self.assertIn('class="check-group"', html)
        self.assertIn('name="to_account"', html)
