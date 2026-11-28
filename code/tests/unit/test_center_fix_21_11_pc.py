"""Test dot fix gop qua dem 21->22/11/2026 — phia PC (thiet ke
PHUONG-AN-FIX-GOP-21-11.md v0.2.1, §5.1).

UT-06: parse_mentions (8 cau cu + moi: @tat ca, khoang trang,
hoa/thuong, ten la) + mo rong @tat ca + mien tran.
UT-07: create_task_from_tag + Luat 2.
UT-10: S2-e — hien ai chua tra loi + canh bao assignee cham >5 phut
(khong doi luat P2).
Phu: S2-b (unknown_mentions 400 JSON + confirmed=1), A5
(count_stats/activity_feed theo nen tang), A10 (ngay cap tu audit),
A2 (dong giai thich trang 403), A11 (DX-01/04/05/06/07 da co san
tu T3m — kiem chung lai, khong sua code).

(Ban Muse cua dot nay o test_center_fix_21_11.py — file rieng,
khong dung ten.)
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pc.center.center_store import (  # noqa: E402
    MENTION_ALL_MARKER, CenterError, CenterStore,
    find_unknown_mentions, parse_mentions,
)
from pc.center import center_store as store_module  # noqa: E402
from pc.ui import ui_app as ui_app_module  # noqa: E402
from pc.ui import ui_pages as ui_pages_module  # noqa: E402
from pc.ui import ui_store  # noqa: E402

# 7 tai khoan tham gia + 1 da ngung (de tinh @tat ca = 7 nguoi,
# vuot tran 5 — dung luc mien tran co tac dung).
ACCOUNT_NAMES = {
    "muse-chinh": "Muse chinh",
    "muse-phu": "Muse phu",
    "muse-ba": "Muse ba",
    "muse-bon": "Muse bon",
    "muse-nam": "Muse nam",
    "muse-sau": "Muse sau",
    "muse-ngung": "Muse ngung",
    "muse-1992-a": "Muse 1992 A",
}
ACCOUNT_FLAGS = {
    "muse-chinh": True, "muse-phu": True, "muse-ba": True,
    "muse-bon": True, "muse-nam": True, "muse-sau": True,
    "muse-ngung": False, "muse-1992-a": True,
}


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _write_accounts(root, flags=None):
    flags = ACCOUNT_FLAGS if flags is None else flags
    _write_json(root / "ui-config" / "accounts.json", {
        "version": 1, "accounts": {
            account_id: {
                "display_name": ACCOUNT_NAMES[account_id],
                "center_participating": participating}
            for account_id, participating in flags.items()}})


def _iso_now_minus(minutes=0):
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.isoformat()


class FixStoreFixture(unittest.TestCase):
    """Fixture chi-store: CenterStore tren root tam + accounts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "ui-config").mkdir(parents=True)
        (self.root / "data" / "center").mkdir(parents=True)
        _write_accounts(self.root)
        self.store = CenterStore(self.root)
        self.accounts = store_module.read_accounts(self.root)


class TestParseMentionsUT06(unittest.TestCase):
    """UT-06: 8 cau cu phai khop nhu truoc + cac cau moi."""

    KNOWN = {"muse-phu", "muse-ba", "muse-chinh", "muse-1992-a",
             "owner"}

    # -- 8 cau cu (giu tu T3d/T3e) --
    def test_old_plain(self):
        self.assertEqual(
            parse_mentions("@muse-phu xem giúp.", self.KNOWN),
            ["muse-phu"])

    def test_old_owner(self):
        self.assertEqual(parse_mentions("@owner nghe giúp.", self.KNOWN),
                         ["owner"])

    def test_old_multiple(self):
        self.assertEqual(
            parse_mentions("@muse-phu @muse-ba họp.", self.KNOWN),
            ["muse-phu", "muse-ba"])

    def test_old_dedup(self):
        self.assertEqual(
            parse_mentions("@muse-phu @muse-phu.", self.KNOWN),
            ["muse-phu"])

    def test_old_ignores_email(self):
        self.assertEqual(parse_mentions("email@muse-phu.com",
                                        self.KNOWN),
                         None)

    def test_old_ignores_email_like(self):
        self.assertEqual(
            parse_mentions("gửi mail cho muse-phu@vd.com", self.KNOWN),
            None)

    def test_old_unknown_dropped(self):
        self.assertEqual(
            parse_mentions("@muse-1992 xem giúp.", self.KNOWN), None)

    def test_old_over_cap_400(self):
        known = {"a-%d" % i for i in range(6)}
        with self.assertRaises(CenterError) as ctx:
            parse_mentions("@a-0 @a-1 @a-2 @a-3 @a-4 @a-5",
                           known)
        self.assertEqual(ctx.exception.error, "too_many_mentions")

    # -- cau moi: @tat ca --
    def test_all_vietnamese(self):
        self.assertEqual(parse_mentions("@tất cả xem giúp.", self.KNOWN),
                         [MENTION_ALL_MARKER])

    def test_all_ascii(self):
        self.assertEqual(parse_mentions("@all xem giúp.", self.KNOWN),
                         [MENTION_ALL_MARKER])

    def test_all_hyphen_forms(self):
        self.assertEqual(parse_mentions("@tat-ca họp.", self.KNOWN),
                         [MENTION_ALL_MARKER])
        self.assertEqual(parse_mentions("@tất-cả họp.", self.KNOWN),
                         [MENTION_ALL_MARKER])

    def test_all_space_after_at(self):
        self.assertEqual(parse_mentions("@  tất cả xem.", self.KNOWN),
                         [MENTION_ALL_MARKER])

    def test_all_not_reported_as_unknown_t(self):
        # Thu tu bat buoc (muc 16): "@tất cả" KHONG duoc bao thanh
        # token "t" trong danh sach bi bo.
        self.assertEqual(
            find_unknown_mentions("@tất cả xem giúp.", self.KNOWN), [])
        self.assertEqual(
            parse_mentions("@tất cả xem giúp.", self.KNOWN),
            [MENTION_ALL_MARKER])

    # -- cau moi: chuan hoa khoang trang / hoa-thuong --
    def test_space_after_at(self):
        self.assertEqual(
            parse_mentions("@ muse-1992-a xem giúp.", self.KNOWN),
            ["muse-1992-a"])

    def test_uppercase(self):
        self.assertEqual(
            parse_mentions("@MUSE-CHINH xem giúp.", self.KNOWN),
            ["muse-chinh"])

    def test_uppercase_all(self):
        self.assertEqual(parse_mentions("@ALL xem giúp.", self.KNOWN),
                         [MENTION_ALL_MARKER])

    # -- marker khong tinh vao tran 5 --
    def test_all_marker_not_counted_in_cap(self):
        known = {"a-%d" % i for i in range(5)}
        result = parse_mentions(
            "@tất cả @a-0 @a-1 @a-2 @a-3 @a-4", known)
        self.assertEqual(result, [MENTION_ALL_MARKER, "a-0", "a-1",
                                  "a-2", "a-3", "a-4"])

    def test_six_real_names_still_400(self):
        known = {"a-%d" % i for i in range(6)}
        with self.assertRaises(CenterError) as ctx:
            parse_mentions("@tất cả @a-0 @a-1 @a-2 @a-3 @a-4 @a-5",
                           known)
        self.assertEqual(ctx.exception.error, "too_many_mentions")

    # -- ham phu ten bi bo (S2-b) --
    def test_unknown_mentions_helper(self):
        self.assertEqual(
            find_unknown_mentions("@muse-1992 xem @muse-phu giúp.",
                                  self.KNOWN),
            ["muse-1992"])
        self.assertEqual(
            find_unknown_mentions("@muse-phu xem giúp.", self.KNOWN),
            [])
        self.assertEqual(
            find_unknown_mentions("@MUSE-1992 @muse-1992.", self.KNOWN),
            ["muse-1992"])


class TestAllExpansionUT06(FixStoreFixture):
    """UT-06 (tiep): mo rong @tat ca o _filter_tag_assignees."""

    def test_expand_all_from_owner(self):
        out = self.store._filter_tag_assignees(
            [MENTION_ALL_MARKER], "owner", self.accounts)
        self.assertEqual(out, ["muse-chinh", "muse-phu", "muse-ba",
                               "muse-bon", "muse-nam", "muse-sau",
                               "muse-1992-a"])

    def test_expand_all_excludes_sender_and_suspended(self):
        out = self.store._filter_tag_assignees(
            [MENTION_ALL_MARKER], "muse-chinh", self.accounts)
        self.assertNotIn("muse-chinh", out)
        self.assertNotIn("muse-ngung", out)
        self.assertEqual(len(out), 6)

    def test_expand_all_sixth_account_no_cut_no_400(self):
        # 7 tai khoan tham gia (vuot tran 5): mo rong du, khong cat.
        msg, dup = self.store.append_message(
            "owner", "hoi", None, None, "@tất cả xem giúp việc này.",
            msg_id="m-000000000001", client_key="ck-all-1")
        self.assertFalse(dup)
        task = self.store.get_task_by_source("m-000000000001")
        self.assertIsNotNone(task)
        self.assertEqual(len(task["assigned_to"]), 7)
        self.assertEqual(task["assigned_to"], [
            "muse-chinh", "muse-phu", "muse-ba", "muse-bon",
            "muse-nam", "muse-sau", "muse-1992-a"])

    def test_append_all_plus_space_and_case(self):
        msg, _dup = self.store.append_message(
            "owner", "hoi", None, None,
            "@  tất cả + @MUSE-PHU xem giúp.",
            msg_id="m-000000000002", client_key="ck-all-2")
        self.assertIn(MENTION_ALL_MARKER, msg["mentions"])
        self.assertIn("muse-phu", msg["mentions"])
        task = self.store.get_task_by_source("m-000000000002")
        self.assertIsNotNone(task)
        # muse-phu chi giao 1 lan (dedup giua marker + mention rieng).
        self.assertEqual(task["assigned_to"].count("muse-phu"), 1)

    def test_create_task_from_tag_over_cap_flag(self):
        assignees = ["muse-chinh", "muse-phu", "muse-ba", "muse-bon",
                     "muse-nam", "muse-sau"]
        with self.assertRaises(CenterError) as ctx:
            self.store.create_task_from_tag(
                "owner", "Viec 6 nguoi", "m-000000000003", assignees)
        self.assertEqual(ctx.exception.error, "too_many_assignees")
        task, dup = self.store.create_task_from_tag(
            "owner", "Viec 6 nguoi", "m-000000000003", assignees,
            allow_over_cap=True)
        self.assertFalse(dup)
        self.assertEqual(len(task["assigned_to"]), 6)


class TestCreateTaskFromTagLaw2UT07(FixStoreFixture):
    """UT-07: create_task_from_tag + Luat 2."""

    def test_owner_no_to_account_creates_task(self):
        task, dup = self.store.create_task_from_tag(
            "owner", "Xem giúp việc này.", "m-000000000010",
            ["muse-phu"])
        self.assertFalse(dup)
        self.assertIsNotNone(task)

    def test_muse_no_to_account_creates_no_task(self):
        task, dup = self.store.create_task_from_tag(
            "muse-chinh", "Xem giúp việc này.", "m-000000000015",
            ["muse-phu"])
        self.assertIsNone(task)
        self.assertFalse(dup)
        self.assertIsNone(
            self.store.get_task_by_source("m-000000000015"))

    def test_to_account_string_suppresses(self):
        task, dup = self.store.create_task_from_tag(
            "muse-chinh", "Xem giúp việc này.", "m-000000000011",
            ["muse-phu"], to_account="muse-phu")
        self.assertIsNone(task)
        self.assertFalse(dup)
        self.assertIsNone(
            self.store.get_task_by_source("m-000000000011"))

    def test_to_account_list_suppresses(self):
        task, dup = self.store.create_task_from_tag(
            "muse-chinh", "Xem giúp việc này.", "m-000000000012",
            ["muse-phu"], to_account=["muse-phu", "muse-ba"])
        self.assertIsNone(task)
        self.assertIsNone(
            self.store.get_task_by_source("m-000000000012"))

    def test_owner_message_suppressed_too(self):
        # Luat 2 ap CA tin chu nhan (D14).
        task, _dup = self.store.create_task_from_tag(
            "owner", "Xem giúp việc này.", "m-000000000013",
            ["muse-phu"], to_account=["muse-phu"])
        self.assertIsNone(task)
        self.assertIsNone(
            self.store.get_task_by_source("m-000000000013"))

    def test_empty_to_account_does_not_suppress_owner(self):
        task, dup = self.store.create_task_from_tag(
            "owner", "Xem giúp việc này.", "m-000000000014",
            ["muse-phu"], to_account=[])
        self.assertFalse(dup)
        self.assertIsNotNone(task)


class TestSlowAssigneeUT10(FixStoreFixture):
    """UT-10: hien ai chua tra loi + canh bao assignee cham."""

    def _write_tasks(self, tasks):
        _write_json(self.root / "data" / "center" / "tasks_state.json",
                    {"version": 1, "tasks": tasks})

    def _task(self, task_id, assigned, reported, minutes, status="moi",
              refused=None, history=None):
        return {
            "task_id": task_id, "title": "Viec %s" % task_id,
            "assigned_to": assigned, "reported_by": reported,
            "refused_by": refused or [], "status": status,
            "created_at": _iso_now_minus(minutes),
            "updated_at": _iso_now_minus(minutes),
            "history": history or [],
        }

    def test_row_shows_unreported_names(self):
        task = self._task("t-aaaaaaaaaaaa",
                          ["muse-chinh", "muse-phu", "muse-ba"],
                          ["muse-phu"], minutes=10)
        html = ui_pages_module.center_task_row_html(
            task, ACCOUNT_NAMES, time.time())
        self.assertIn("1/3 đã báo xong", html)
        self.assertIn("Chưa báo:", html)
        self.assertIn("Muse chinh", html)
        self.assertIn("Muse ba", html)
        self.assertNotIn("Muse phu", html.split("Chưa báo:")[1])

    def test_row_no_unreported_note_when_all_reported(self):
        task = self._task("t-aaaaaaaaaaaa",
                          ["muse-chinh", "muse-phu"],
                          ["muse-chinh", "muse-phu"], minutes=10)
        html = ui_pages_module.center_task_row_html(
            task, ACCOUNT_NAMES, time.time())
        self.assertIn("2/2 đã báo xong", html)
        self.assertNotIn("Chưa báo:", html)

    def test_slow_assignee_over_5_min(self):
        self._write_tasks({"t-aaaaaaaaaaaa": self._task(
            "t-aaaaaaaaaaaa",
            ["muse-chinh", "muse-phu", "muse-ba"],
            ["muse-phu"], minutes=10)})
        items = ui_store.center_slow_assignees(self.root, time.time())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["task_id"], "t-aaaaaaaaaaaa")
        self.assertEqual(items[0]["slow"], ["muse-chinh", "muse-ba"])
        self.assertGreaterEqual(items[0]["wait_seconds"], 600)

    def test_slow_assignee_under_5_min_none(self):
        self._write_tasks({"t-aaaaaaaaaaaa": self._task(
            "t-aaaaaaaaaaaa", ["muse-chinh"], [], minutes=2)})
        self.assertEqual(
            ui_store.center_slow_assignees(self.root, time.time()), [])

    def test_slow_excludes_reported_refused_suspended_done(self):
        self._write_tasks({
            "t-aaaaaaaaaaaa": self._task(
                "t-aaaaaaaaaaaa",
                ["muse-chinh", "muse-phu", "muse-ngung"],
                ["muse-phu"], minutes=10, refused=["muse-ngung"]),
            "t-bbbbbbbbbbbb": self._task(
                "t-bbbbbbbbbbbb", ["muse-chinh"], ["muse-chinh"],
                minutes=10, status="da-bao-xong"),
        })
        items = ui_store.center_slow_assignees(self.root, time.time())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["slow"], ["muse-chinh"])

    def test_slow_returned_task_clock_restarts(self):
        returned_at = _iso_now_minus(2)
        history = [{"from_status": "da-bao-xong",
                    "to_status": "dang-lam", "at": returned_at,
                    "by": "owner"}]
        self._write_tasks({"t-aaaaaaaaaaaa": self._task(
            "t-aaaaaaaaaaaa", ["muse-chinh"], [], minutes=60,
            status="dang-lam", history=history)})
        self.assertEqual(
            ui_store.center_slow_assignees(self.root, time.time()), [])

    def test_slow_does_not_complete_task(self):
        # Chi hien thi: tinh xong, trang thai viec van "moi".
        self._write_tasks({"t-aaaaaaaaaaaa": self._task(
            "t-aaaaaaaaaaaa", ["muse-chinh", "muse-phu"], ["muse-phu"],
            minutes=10)})
        ui_store.center_slow_assignees(self.root, time.time())
        data = json.loads((self.root / "data" / "center"
                           / "tasks_state.json").read_text(
                               encoding="utf-8"))
        self.assertEqual(
            data["tasks"]["t-aaaaaaaaaaaa"]["status"], "moi")


class TestCapAuthAlertsTolerance(unittest.TestCase):
    """Muc 20: tab Canh bao doc them 2 file alert — chiu duoc file
    thieu/rong/hong, khong crash."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "data" / "center").mkdir(parents=True)

    def test_missing_files_empty(self):
        self.assertEqual(ui_store.center_cap_auth_alerts(self.root), [])

    def test_empty_files_empty(self):
        (self.root / "data" / "center" / "cap_alerts.jsonl").write_text(
            "", encoding="utf-8")
        (self.root / "data" / "center" / "auth_alerts.jsonl").write_text(
            "\n", encoding="utf-8")
        self.assertEqual(ui_store.center_cap_auth_alerts(self.root), [])

    def test_corrupt_lines_skipped(self):
        (self.root / "data" / "center" / "cap_alerts.jsonl").write_text(
            "khong phai json\n"
            + json.dumps({"kind": "cap", "account_id": "muse-chinh",
                          "at": _iso_now_minus(1),
                          "text": "Cap kẹt: muse-chinh"},
                         ensure_ascii=False) + "\n",
            encoding="utf-8")
        items = ui_store.center_cap_auth_alerts(self.root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "cap")
        self.assertEqual(items[0]["account_id"], "muse-chinh")

    def test_alerts_fragment_renders_sections(self):
        html = ui_pages_module.alerts_fragment_html(
            [], [], {}, {}, "csrf", {"view": "open"},
            center_cap_auth=[{
                "kind": "cap", "account_id": "muse-chinh",
                "at": int(time.time()), "level": "cần xem",
                "text": "Cap kẹt: muse-chinh"}],
            center_slow=[{
                "task_id": "t-aaaaaaaaaaaa", "title": "Viec test",
                "names": "Muse chinh", "wait_seconds": 360,
                "level": "cần xem"}])
        self.assertIn("Trung tâm — cảnh báo cap/lỗi mã", html)
        self.assertIn("Cap kẹt: muse-chinh", html)
        self.assertIn("Trung tâm — tài khoản chậm báo", html)
        self.assertIn("Muse chinh", html)


class UiHandlerFixture(unittest.TestCase):
    """Fixture goi truc tiep _handle_center_message (khong server)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "ui-config").mkdir(parents=True)
        (self.root / "data" / "center").mkdir(parents=True)
        _write_accounts(self.root)
        self.app = ui_app_module.UiApp(self.root)
        self.session = {"csrf_token": "TEST-CSRF"}
        self.store = CenterStore(self.root)

    def _post_message(self, body, extra=None):
        fields = {"csrf_token": "TEST-CSRF", "room": "chung",
                  "body": body,
                  "client_key": "ck-%d" % abs(hash(body))}
        if extra:
            fields.update(extra)
        request = ui_app_module.Request(
            "POST", "/center/messages", headers={},
            body=urlencode(fields).encode("utf-8"))
        request.headers["content-type"] = (
            "application/x-www-form-urlencoded")
        return self.app._handle_center_message(request, self.session)


class TestUnknownMentionsFlow(UiHandlerFixture):
    """S2-b: server 400 unknown_mentions -> UI confirm -> gui lai
    kem confirmed=1."""

    def test_unknown_returns_400_json_no_write(self):
        resp = self._post_message("@muse-1992 xem giúp việc này.")
        self.assertEqual(resp.status, 400)
        self.assertIn("application/json",
                      resp.headers.get("Content-Type", ""))
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(payload["error"], "unknown_mentions")
        self.assertEqual(payload["unknown"], ["muse-1992"])
        self.assertTrue(payload["confirm_required"])
        self.assertEqual(self.store.read_events(), [])

    def test_confirmed_resend_writes(self):
        resp = self._post_message("@muse-1992 xem giúp việc này.",
                                  {"confirmed": "1"})
        self.assertEqual(resp.status, 302)
        events = self.store.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["record"]["mentions"], None)

    def test_known_mention_no_confirm_needed(self):
        resp = self._post_message("@muse-phu xem giúp việc này.")
        self.assertEqual(resp.status, 302)
        task = self.store.get_task_by_source(
            self.store.read_events()[0]["record"]["msg_id"])
        self.assertIsNotNone(task)

    def test_all_mention_not_unknown(self):
        resp = self._post_message("@tất cả xem giúp việc này.")
        self.assertEqual(resp.status, 302)
        events = self.store.read_events()
        task = self.store.get_task_by_source(
            events[0]["record"]["msg_id"])
        self.assertIsNotNone(task)
        self.assertEqual(len(task["assigned_to"]), 7)


class TestCountStatsPlatform(unittest.TestCase):
    """A5: feed tab nen tang chi dem tin cua nen tang do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "data" / "center").mkdir(parents=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [{
            "seq": 1, "at": now_iso, "kind": "message",
            "msg_id": "m-000000000001",
            "record": {"msg_id": "m-000000000001",
                       "from_account": "muse-phu", "kind": "hoi",
                       "body": "chao", "created_at": now_iso}}]
        (self.root / "data" / "center" / "events.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False)
                      for row in rows) + "\n", encoding="utf-8")

    def test_count_stats_platform_excludes_center(self):
        all_stats = ui_store.count_stats(self.root, [], now=time.time())
        self.assertEqual(all_stats["today_in"], 1)
        zalo_stats = ui_store.count_stats(
            self.root, [], now=time.time(), platforms={"zalo"})
        self.assertEqual(zalo_stats["today_in"], 0)

    def test_activity_feed_platform_excludes_center(self):
        feed_all = ui_store.activity_feed(self.root, [], now=time.time())
        self.assertEqual(len(feed_all), 1)
        feed_zalo = ui_store.activity_feed(
            self.root, [], now=time.time(), platforms={"zalo"})
        self.assertEqual(feed_zalo, [])


class TestSecretIssuedAtAudit(unittest.TestCase):
    """A10 NS-J1: ngay cap tu audit center_secret_rotate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "ui-config").mkdir(parents=True)
        for kind in ("fetch", "push"):
            path = (self.root / "secrets"
                    / ("center-muse-chinh.%s_secret" % kind))
            path.write_text("TEST-FAKE-SECRET\n", encoding="utf-8")
            old = 1577836800  # 2020-01-01 — mtime cu co tinh.
            os.utime(path, (old, old))

    def test_issued_at_from_audit(self):
        (self.root / "ui-config" / "audit.jsonl").write_text(
            json.dumps({"ts": 1700000000, "action": "center_secret_rotate",
                        "account_id": "muse-chinh", "result": "ok"},
                       ensure_ascii=False) + "\n"
            + json.dumps({"ts": 1700001000, "action": "center_secret_rotate",
                          "account_id": "muse-phu", "result": "ok"},
                         ensure_ascii=False) + "\n",
            encoding="utf-8")
        status = ui_store.center_secret_status(self.root, "muse-chinh")
        self.assertTrue(status["has"])
        self.assertEqual(status["issued_at"], 1700000000)

    def test_issued_at_falls_back_to_mtime(self):
        status = ui_store.center_secret_status(self.root, "muse-chinh")
        self.assertTrue(status["has"])
        self.assertEqual(status["issued_at"], 1577836800)


class TestViewSecret403Text(UiHandlerFixture):
    """A2 ĐX-HM-01: trang 403 tai lai trang ket qua cap ma co dong
    giai thich ma di dau."""

    def test_expired_confirm_page_explains_code(self):
        request = ui_app_module.Request(
            "POST",
            "/center/accounts/muse-chinh/view-secret/execute",
            headers={},
            body=urlencode({"csrf_token": "TEST-CSRF",
                            "confirm_token": "bogus"}).encode("utf-8"))
        request.headers["content-type"] = (
            "application/x-www-form-urlencoded")
        resp = self.app._handle_center_view_secret(
            request, self.session, "session-token", "muse-chinh",
            "/execute")
        self.assertEqual(resp.status, 403)
        body = resp.body.decode("utf-8")
        self.assertIn("Mã bí mật mới vừa cấp vẫn còn hiệu lực", body)
        self.assertIn("chỉ hiện mã một lần", body)


class TestSettingsDxPresent(unittest.TestCase):
    """A11 ĐX-01/04/05/06/07: da co san tu T3m — kiem chung lai
    (khong sua code o muc nay)."""

    def test_dx06_single_error_sentence(self):
        with self.assertRaises(ValueError) as ctx:
            ui_app_module._parse_int_pct("80.5", "quota_warn_pct")
        self.assertEqual(str(ctx.exception),
                         "Chỉ nhập số nguyên, ví dụ 80")

    def test_dx04_dx05_settings_anchors(self):
        html = ui_pages_module.ui_settings_page_html(
            {}, "csrf",
            center_config={"prune_day": "sun",
                           "prune_retention_days": 30,
                           "prune_near_full_fraction": 0.8,
                           "overdue_severe_hours": 24},
            prune_state={}, file_used_bytes=0,
            alert_settings=0.8)
        self.assertIn('id="ket-qua-don"', html)
        self.assertIn('href="#nhom-don-dep"', html)
        self.assertIn('href="#nhom-canh-bao"', html)


if __name__ == "__main__":
    unittest.main()
