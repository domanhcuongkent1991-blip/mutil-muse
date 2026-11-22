#!/usr/bin/env python3
"""Test sua nho D17 (v0.6.0) — thanh vien B: 4 [NEN SUA] nho cua gia
lap lop 1 (BAO-CAO-GIA-LAP-V060.md §3, QUYET-DINH-QUA-DEM-20-11.md
D17).

- NS-K21-1: bong bóng chat mang ``data-seq`` — ``_center_chat_messages``
  gắn seq vào bản sao record; ``trimBubbles()`` đọc được seq từng
  bong bóng nên không còn là no-op.
- NS-K21-2: viên "N tin mới" cách đáy hộp 68px theo frag-02 §2.4
  (không chạm nút "Về cuối" 12px + cao 44px).
- NS-K21-3: ``markRead()`` chỉ ẩn viên khi máy chủ trả OK — bấm viên
  mà gọi thất bại thì viên ở lại để bấm lại.
- NS-R1: bấm "Gửi lại"/"Gửi lại tất cả" xong trang bot hiện câu xác
  nhận kèm đúng số N (flash 1 lần trong session; redirect giữ nguyên
  "/bots/<id>").

unittest, stdlib-only. Mọi giá trị secret trong test đều là mẫu giả
dạng rõ ràng ("TEST-FAKE-..."), không secret thật, không nội dung
tin thật. Chỉ test mới — không sửa test file của người khác.
"""

import json
import re
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pc.ui.ui_static import APP_CSS, APP_JS  # noqa: E402
from tests.unit.test_center_t3i import T3iUiFixture  # noqa: E402
from tests.unit.test_center_t3m import (  # noqa: E402
    TestNs02DeadLettersUi,
)


def _css_block(css, selector):
    """Khối luật CSS của 1 selector (đã bỏ comment), hoặc ""."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}",
                      css, re.S)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# NS-K21-1 — bong bóng chat mang data-seq
# ---------------------------------------------------------------------------

class TestNsK211DataSeq(T3iUiFixture):
    def test_fragment_50_bubbles_carry_data_seq(self):
        # Đúng như K21 đã đếm: 50 bong bóng thì data-seq phải = 50
        # (trước sửa = 0 vì _center_chat_messages làm rơi seq).
        for i in range(50):
            self.seed("muse-phu", "hoi", "tin %02d" % i,
                      "m-%012d" % i)
        session_cookie, _ = self.login()
        frag = self.get("/fragments/center-chat", session_cookie,
                        query={"chat_limit": ["50"]}).body.decode()
        self.assertEqual(frag.count('data-seq="'), 50)
        for seq in range(1, 51):
            self.assertIn('data-seq="%d"' % seq, frag)

    def test_center_chat_messages_attaches_seq_without_touching_store(self):
        for i in range(3):
            self.seed("muse-phu", "hoi", "tin %d" % i,
                      "m-%012d" % i)
        messages, _titles, _more, max_seq, min_seq = \
            self.app._center_chat_messages(50, "chung")
        self.assertEqual([m["seq"] for m in messages], [1, 2, 3])
        self.assertEqual((min_seq, max_seq), (1, 3))
        # Dict gốc trong kho không bị thêm khóa "seq" (chỉ bản sao
        # trả về mới có).
        rows = [json.loads(line) for line in
                self.center.events_path.read_text(
                    encoding="utf-8").splitlines()]
        records = [r["record"] for r in rows
                   if r.get("kind") == "message"]
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertNotIn("seq", record)

    def test_trim_bubbles_reads_data_seq(self):
        # trimBubbles() cập nhật oldestSeq từ data-seq của bong bóng
        # đầu tiên còn lại — không có data-seq thì thành no-op lặng lẽ.
        match = re.search(
            r"function trimBubbles\(\) \{(.*?)\n    \}", APP_JS, re.S)
        self.assertIsNotNone(match, "không tìm thấy trimBubbles()")
        body = match.group(1)
        self.assertIn('first.getAttribute("data-seq")', body)
        self.assertIn("oldestSeq = seq;", body)

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

    def test_task_detail_bubbles_carry_data_seq(self):
        # Cùng luật frag-02 §2.2 cho trang chi tiết việc (cùng
        # renderer center_message_bubble_html).
        session_cookie, csrf = self.login()
        task_id = self._create_task(session_cookie, csrf)
        self.seed("muse-phu", "bao-cao", "đã xong phần 1",
                  "m-cccccccc2121", task_id=task_id)
        html = self.get("/center/tasks/%s" % task_id,
                        session_cookie).body.decode()
        self.assertIn("đã xong phần 1", html)
        # Tạo việc tự ghi 1 tin của chủ nhân (seq 2) + tin báo cáo
        # đã seed (seq 3): cả 2 bong bóng đều mang data-seq.
        self.assertEqual(html.count('data-seq="'), 2)
        self.assertIn('data-seq="2"', html)
        self.assertIn('data-seq="3"', html)


# ---------------------------------------------------------------------------
# NS-K21-2 — viên "N tin mới" cách đáy hộp 81px (SUA B3, 21/11:
# doi moc tu anchor trong hop sang wrap ngoai hop: 81 = 68 cu +
# 13 padding-bottom + border cua hop; nut 25 = 12 cu + 13)
# ---------------------------------------------------------------------------

class TestNsK212NewPillOffset(unittest.TestCase):
    def test_pill_bottom_is_81px(self):
        block = _css_block(APP_CSS, ".center-new-pill")
        self.assertTrue(block, "không tìm thấy .center-new-pill")
        self.assertIn("bottom: 81px", block)
        self.assertNotIn("bottom: 68px", block)

    def test_scroll_bottom_button_unchanged_and_no_overlap(self):
        # Nút "Về cuối" 25px + cao 44px; viên ở 81px nên
        # mép dưới viên (81) nằm trên mép trên nút (25 + 44 = 69) —
        # khe 12px nhu cu, viên dài trên hộp hẹp cũng không chạm nút.
        button = _css_block(APP_CSS, ".center-scroll-bottom")
        self.assertTrue(button, "không tìm thấy .center-scroll-bottom")
        self.assertIn("bottom: 25px", button)
        pill_bottom = int(re.search(r"bottom:\s*(\d+)px",
                                    _css_block(APP_CSS,
                                               ".center-new-pill")
                                    ).group(1))
        button_top = 25 + 44
        self.assertGreater(pill_bottom, button_top)
        self.assertEqual(pill_bottom - button_top, 12)


# ---------------------------------------------------------------------------
# NS-K21-3 — markRead() chỉ ẩn viên khi máy chủ trả OK
# ---------------------------------------------------------------------------

class TestNsK213MarkReadWaitsForOk(unittest.TestCase):
    def _mark_read_body(self):
        match = re.search(
            r"function markRead\(seq, onOk\) \{(.*?)\n    \}",
            APP_JS, re.S)
        self.assertIsNotNone(match, "không tìm thấy markRead(seq, onOk)")
        return match.group(1)

    def test_mark_read_calls_on_ok_only_when_server_ok(self):
        body = self._mark_read_body()
        # Chỉ khi HTTP OK + JSON {"ok": true} mới gọi onOk (ẩn viên).
        self.assertIn('if (!resp.ok) { throw new Error("HTTP "',
                      body)
        self.assertIn("data.ok === true", body)
        self.assertIn("onOk();", body)
        # Bản thân markRead không được tự ẩn viên (không còn
        # fire-and-forget nuốt lỗi rồi ẩn).
        self.assertNotIn("pill.hidden", body)

    def test_pill_click_hides_pill_only_via_mark_read_callback(self):
        # Bấm viên -> gọi markRead kèm callback ẩn viên; không còn
        # dòng "pill.hidden = true;" đồng bộ ngay sau khi cuộn.
        self.assertIn(
            "markRead(newestSeq, function () { pill.hidden = true; })",
            APP_JS)
        self.assertNotIn(
            "box.scrollTop = box.scrollHeight;\n"
            "        pill.hidden = true;",
            APP_JS)

    def test_pill_click_without_server_call_still_hides_at_once(self):
        # Phòng lưu trữ / chưa có tin mới nhất: không gọi máy chủ nên
        # ẩn viên ngay (không chờ gì cả).
        match = re.search(
            r"pill\.addEventListener\(\"click\", function \(\) \{(.*?)\n"
            r"    \}\);",
            APP_JS, re.S)
        self.assertIsNotNone(match, "không tìm thấy handler bấm viên")
        handler = match.group(1)
        self.assertIn("if (archived || newestSeq === null) {", handler)
        self.assertIn("pill.hidden = true;", handler)


# ---------------------------------------------------------------------------
# Q-UR-FIX — kéo xuống sát đáy thì đánh dấu đã đọc (sửa badge 22/11)
# ---------------------------------------------------------------------------

class TestScrollToBottomMarksRead(unittest.TestCase):
    """(Sua badge chua doc 22/11 — Q-UR-FIX-01…03) Luat cu Q4/D17 chi
    cho danh dau khi bam vien "N tin moi" (hoac tin moi ve trong luc
    o sat day) — keo doc het ma badge khong tat thi sai thoi quen app
    nhan tin. Luat moi: keo xuong sat day (<=150px) cung danh dau."""

    def _scroll_handler(self):
        match = re.search(
            r'box\.addEventListener\("scroll", function \(\) \{(.*?)\n'
            r'    \}\);',
            APP_JS, re.S)
        self.assertIsNotNone(match, "không tìm thấy handler scroll")
        return match.group(1)

    def test_scroll_near_bottom_calls_mark_read(self):
        handler = self._scroll_handler()
        self.assertIn("nearBottom()", handler)
        self.assertIn("markRead(newestSeq", handler)

    def test_scroll_mark_read_guarded_by_last_marked_seq(self):
        # Chi goi 1 lan moi seq moi, khong spam request tren moi su
        # kien scroll.
        self.assertIn("var lastMarkedSeq = null;", APP_JS)
        handler = self._scroll_handler()
        self.assertIn("newestSeq !== lastMarkedSeq", handler)
        self.assertIn("lastMarkedSeq = newestSeq;", handler)

    def test_scroll_mark_read_hides_pill_on_ok(self):
        handler = self._scroll_handler()
        self.assertIn(
            "markRead(newestSeq, function () {", handler)
        self.assertIn("pill.hidden = true;", handler)

    def test_scroll_mark_read_skips_archived_room(self):
        handler = self._scroll_handler()
        self.assertIn("!archived", handler)


# ---------------------------------------------------------------------------
# NS-R1 — câu xác nhận sau "Gửi lại"/"Gửi lại tất cả"
# ---------------------------------------------------------------------------

class TestNsR1RetryConfirmation(TestNs02DeadLettersUi):
    def _detail_html_same_session(self, session_cookie):
        return self.get("/bots/bot-a", session_cookie).body.decode()

    def test_retry_one_shows_confirmation_with_count_once(self):
        self._seed_dead([self._dead_row("k1", "Tin chết một"),
                         self._dead_row("k2", "Tin chết hai")])
        session_cookie, csrf = self.login()
        resp = self.post("/api/bots/bot-a/retry-dead", session_cookie,
                         csrf, {"reply_key": "k1"})
        self.assertEqual(resp.status, 302)
        # Redirect giữ nguyên "/bots/bot-a" (test t3m bắt đúng đuôi
        # này — không thêm ?retry=N).
        self.assertTrue(resp.headers["Location"].endswith("/bots/bot-a"))
        self.assertEqual(len(self._requests()), 1)
        html = self._detail_html_same_session(session_cookie)
        self.assertIn(
            "Đã nhận yêu cầu gửi lại 1 thư — sẽ gửi khi phía Muse "
            "kéo yêu cầu (chu kỳ sau).",
            html)
        # Flash 1 lần: tải lại trang thì câu xác nhận biến mất.
        html_again = self._detail_html_same_session(session_cookie)
        self.assertNotIn("Đã nhận yêu cầu gửi lại", html_again)

    def test_retry_all_shows_confirmation_with_total_count(self):
        self._seed_dead([self._dead_row("k1", "Tin một"),
                         self._dead_row("k2", "Tin hai")])
        session_cookie, csrf = self.login()
        resp = self.post("/api/bots/bot-a/retry-dead", session_cookie,
                         csrf, {"retry_all": "1"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(len(self._requests()), 2)
        html = self._detail_html_same_session(session_cookie)
        self.assertIn(
            "Đã nhận yêu cầu gửi lại 2 thư — sẽ gửi khi phía Muse "
            "kéo yêu cầu (chu kỳ sau).",
            html)

    def test_retry_when_nothing_live_shows_zero_notice(self):
        # Không còn thư chết nào: vẫn có câu xác nhận (0 thư cần gửi
        # lại) thay vì redirect lặng lẽ.
        session_cookie, csrf = self.login()
        resp = self.post("/api/bots/bot-a/retry-dead", session_cookie,
                         csrf, {"reply_key": "k1"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(len(self._requests()), 0)
        html = self._detail_html_same_session(session_cookie)
        self.assertIn("Không có thư chết nào cần gửi lại.", html)


if __name__ == "__main__":
    unittest.main()
