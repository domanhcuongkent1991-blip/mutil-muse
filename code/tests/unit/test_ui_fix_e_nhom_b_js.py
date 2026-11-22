#!/usr/bin/env python3
"""Test sua UI nhom B + D5 (21/11/2026) — thanh vien JS.

- NS-01: refreshOne() khoi phuc VI TRI CON TRO (selectionStart/
  selectionEnd + setSelectionRange) + tick checkbox khong co id
  (need_owner, overdue) theo name.
- D5/NS-02: pollTick() phong dang mo ma RONG (newestSeq = null) van
  fetch fragment thuong, chong trung lap theo data-seq, xoa o trong.
- L9: .cardlink co min-height: 44px (dam bao cung, giu padding 7px 0).

unittest, stdlib-only, assert chuoi/CSS/JS + render HTML that (theo
mau tests/unit/test_ui_fix_b_search.py). Khong secret that.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pc.ui import ui_pages  # noqa: E402
from pc.ui.ui_static import APP_CSS, APP_JS  # noqa: E402

UI_PAGES_SRC = (REPO_ROOT / "pc" / "ui" / "ui_pages.py").read_text(
    encoding="utf-8")


def _poll_tick_body():
    """Than ham pollTick() trong APP_JS (tu 'function pollTick()' toi
    het lenh hen lai chu ky)."""
    start = APP_JS.index("function pollTick()")
    end = APP_JS.index("window.setTimeout(pollTick, CHAT_POLL_MS);",
                       start)
    return APP_JS[start:end]


def _refresh_one_body():
    """Than ham refreshOne() trong APP_JS."""
    start = APP_JS.index("function refreshOne(")
    end = APP_JS.index("function schedule(", start)
    return APP_JS[start:end]


class TestNs01CaretSavedAndRestored(unittest.TestCase):
    """(a) NS-01: luu/khoi phuc vi tri con tro trong refreshOne()."""

    def test_caret_saved_before_fetch(self):
        body = _refresh_one_body()
        self.assertIn("savedCarets", body)
        self.assertIn("selectionStart", body)
        self.assertIn("selectionEnd", body)
        self.assertIn("savedCarets[formFields[i].id] = {", body)

    def test_caret_restored_after_replace(self):
        body = _refresh_one_body()
        self.assertIn("setSelectionRange", body)
        # Khoi phuc sau khi gan lai value (gan value dat lai selection
        # ve cuoi) — khoi caret phai o SAU khoi gan value.
        self.assertLess(body.index("savedNode.value = savedValues[savedId];"),
                        body.index("setSelectionRange"))
        # (NS-01 audit) Kep bien theo do dai value moi — neu ai xoa
        # Math.min thi setSelectionRange co the vuot qua cuoi o.
        self.assertIn("Math.min(savedCarets[caretId].start, caretLen)",
                      body)
        self.assertIn("Math.min(savedCarets[caretId].end, caretLen)",
                      body)
        self.assertIn("caretNode.value.length", body)
        # caretLen duoc tinh truoc khi goi setSelectionRange (chuoi
        # "setSelectionRange" con xuat hien som hon o dieu kien guard
        # `if (caretNode && caretNode.setSelectionRange)` nen phai
        # so voi dung loi goi co dau "(").
        self.assertLess(body.index("var caretLen"),
                        body.index("caretNode.setSelectionRange("))

    def test_caret_read_wrapped_in_try_catch(self):
        # Guard (a): doc selectionStart boc try/catch (checkbox /
        # hidden / file nem InvalidStateError) — ca luc luu lan luc
        # khoi phuc.
        body = _refresh_one_body()
        self.assertGreaterEqual(body.count("try {"), 2)
        self.assertIn("catch (e)", body)

    def test_caret_guards_kept(self):
        body = _refresh_one_body()
        # Guard (b): hasFocus sau fetch van con (2 lan trong ham).
        self.assertGreaterEqual(body.count("if (hasFocus(el)) {"), 2)
        # Guard (c): SELECT chi gan khi gia tri con trong options.
        self.assertIn("hasOption", body)
        # Guard (d): tra cuu trong fragment (el.querySelector), khong
        # document.getElementById toan cuc de tim node da luu.
        self.assertIn('el.querySelector("#" + caretId)', body)
        self.assertNotIn('document.getElementById(savedId)', body)
        self.assertNotIn('document.getElementById(caretId)', body)


class TestNs01CheckboxCheckedByName(unittest.TestCase):
    """(b) NS-01: checkbox khong co id duoc luu/khoi phuc checked."""

    def test_checked_saved_by_name_and_value_and_restored(self):
        # (Gop UI checkbox, 22/11) Khoa theo name + value: nhieu
        # checkbox cung name (nhom chon tai khoan) thi moi o co khoa
        # rieng; khoa theo name don se ghi de lan nhau.
        body = _refresh_one_body()
        self.assertIn("savedChecked", body)
        self.assertIn(
            'savedChecked[formFields[i].name + "=" + formFields[i].value]',
            body)
        self.assertIn(
            'el.querySelectorAll(\'input[type="checkbox"]\')', body)
        self.assertIn(
            'var boxKey = savedBoxes[k].name + "=" + savedBoxes[k].value;',
            body)
        self.assertIn(
            "savedBoxes[k].checked = savedChecked[boxKey];",
            body)

    def test_two_checkboxes_have_name_but_no_id(self):
        # need_owner (ui_pages, tab Canh bao) + overdue (tab Trung
        # tam): co name, KHONG co id — ly do phai khoa theo name thay
        # vi them id moi (tranh trung id).
        html = ui_pages.center_filter_bar_html({}, [])
        self.assertIn('name="overdue"', html)
        overdue_tag = re.search(r"<input[^>]*name=\"overdue\"[^>]*>",
                                html)
        self.assertIsNotNone(overdue_tag)
        self.assertNotIn("id=", overdue_tag.group(0))
        need_owner_tag = re.search(
            r"<input[^>]*name=\"need_owner\"[^>]*>", UI_PAGES_SRC)
        self.assertIsNotNone(need_owner_tag)
        self.assertNotIn("id=", need_owner_tag.group(0))


class TestD5EmptyRoomPolls(unittest.TestCase):
    """(c) D5/NS-02: phong rong van poll tin moi trong pollTick()."""

    def test_poll_tick_has_empty_branch_fetching_plain_fragment(self):
        body = _poll_tick_body()
        # Nhanh delta khi newestSeq != null van con.
        self.assertIn("if (newestSeq !== null) {", body)
        self.assertIn("&after_seq=\" + newestSeq", body)
        # Nhanh rong: else + fetch fragment thuong (khong after_seq).
        self.assertIn("} else {", body)
        self.assertIn('"/fragments/center-chat?room="', body)
        # Trong ca ham pollTick chi co dung 1 cho ghep tham so
        # after_seq vao URL (nhanh delta) — nhanh rong khong kem
        # after_seq (comment co the nhac ten tham so).
        self.assertEqual(body.count('"&after_seq="'), 1)

    def test_empty_branch_dedupes_and_clears_notice(self):
        body = _poll_tick_body()
        # Chong trung lap theo data-seq (fragment thuong tra lai ca
        # danh sach): chi chen tin chua co trong hop.
        self.assertIn("seenSeqs", body)
        self.assertIn('.msg[data-seq]', body)
        self.assertIn("parsed.bubbles.filter", body)
        # Co bong bóng moi thi xoa o trong (B1) + cap nhat newestSeq.
        self.assertIn("removeEmptyNotice();", body)
        self.assertIn("newestSeq = nextNewest", body)
        # (NS-01 audit) Nhanh rong cung cap nhat oldestSeq + hasMore
        # tu meta cua fragment — neu thieu, phan trang tin cu / nut
        # "tai them" se dung gia tri cu.
        self.assertIn("oldestSeq = nextOldest", body)
        self.assertIn('hasMore = parsed.meta.getAttribute("data-has-more")',
                      body)
        # (SUA B3, 21/11) Append thang vao hop nhu nhanh delta
        # (khong con wrapper anchor sticky trong hop).
        self.assertIn("box.appendChild(frag);", body)
        self.assertNotIn("box.insertBefore(frag, chatAnchor);", body)

    def test_empty_branch_shared_for_both_room_types(self):
        # Cung 1 duong code cho phong rieng (dm:) + phong chung
        # (chung): nhanh rong dung bien room, khong re nhanh theo
        # loai phong.
        body = _poll_tick_body()
        self.assertIn("encodeURIComponent(room)", body)
        self.assertNotIn('room === "chung"', body)
        self.assertNotIn('room === "dm:', body)
        self.assertNotIn("room.startsWith", body)

    def test_plain_fragment_bubbles_carry_data_seq(self):
        # Dieu kien tien quyet cho chong trung lap: bong bóng trong
        # fragment thuong mang data-seq.
        html = ui_pages.center_chat_fragment_html(
            [{"kind": "hoi", "from_account": "muse-chinh",
              "created_at": "2026-11-21T08:00:00+07:00",
              "body": "xin chào", "seq": 7}],
            {"muse-chinh": "Muse Chinh"}, {}, 50, False,
            room="chung", oldest_seq=7, newest_seq=7)
        self.assertIn('data-seq="7"', html)
        self.assertIn('class="msg ', html)


class TestL9CardlinkMinHeight(unittest.TestCase):
    """(d) L9: .cardlink co min-height: 44px."""

    def test_cardlink_has_min_height_44(self):
        m = re.search(r"\.cardlink\s*\{([^}]*)\}", APP_CSS)
        self.assertIsNotNone(m, "không tìm thấy rule .cardlink")
        rule = m.group(1)
        self.assertIn("min-height: 44px", rule)
        # Padding cu giu nguyen (7px 0) — chi them min-height.
        self.assertIn("padding: 7px 0", rule)


if __name__ == "__main__":
    unittest.main()
