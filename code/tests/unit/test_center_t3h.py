#!/usr/bin/env python3
"""Test chặng T3h (v0.6.0): nhánh chat + auto-wake W1–W10 + tín hiệu
"đang trả lời" + tin hub tự sinh + chat tắc + câu tạm thời D7.

unittest, stdlib-only, HTTP thật trên localhost với receiver T1 thật
chạy trong tiến trình. Mọi giá trị secret trong test đều là mẫu giả
dạng rõ ràng ("TEST-FAKE-..."), không secret thật, không nội dung
tin thật. Mọi tiêu đề/nội dung việc trong test đều là dữ liệu giả.
"""

import io
import ipaddress
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "muse-side"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT.parent / "multibot-runtime" / "ops"))

import center_chat_brief  # noqa: E402
import center_claims  # noqa: E402
import center_dispatcher  # noqa: E402
import center_outbox  # noqa: E402
import center_plan_cycle  # noqa: E402
from pc.center import center_store  # noqa: E402
from pc.sync_receiver import receiver_main as receiver  # noqa: E402

FETCH_CHINH = "TEST-FAKE-CENTER-FETCH-CHINH"
PUSH_CHINH = "TEST-FAKE-CENTER-PUSH-CHINH"
FETCH_PHU = "TEST-FAKE-CENTER-FETCH-PHU"
PUSH_PHU = "TEST-FAKE-CENTER-PUSH-PHU"
ME = "muse-chinh"
OTHER = "muse-phu"
NOW = time.time()


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _msg_record(kind="hoi", sender="owner", room="chung",
                to_account=None, mentions=None, msg_id="m-000000000001",
                created_at=None, source=None, task_id=None):
    record = {
        "schema_version": 1,
        "msg_id": msg_id,
        "from_account": sender,
        "to_account": to_account,
        "kind": kind,
        "task_id": task_id,
        "body": "Tin thu",
        "created_at": created_at or _iso(NOW),
        "report_role": None,
        "room": room,
        "mentions": mentions,
    }
    if source is not None:
        record["source"] = source
    return record


def _muse_root(tmp, messages=(), tasks=(), chat_state=None):
    """Thư mục gốc Muse tạm: center_messages.jsonl + center_tasks/ +
    center_chat_state/ (tùy chọn)."""
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    for seq, record in messages:
        _append_jsonl(root / "center_messages.jsonl", {
            "seq": seq, "at": record.get("created_at"),
            "msg_id": record.get("msg_id"), "record": record})
    for task_id, record in tasks:
        _write_json(root / "center_tasks" / ("%s.json" % task_id), {
            "task_id": task_id, "seq": 1, "record": record})
    if chat_state is not None:
        _write_json(root / "center_chat_state" / ("%s.json" % ME),
                    chat_state)
    return root


def _base_ctx(marker=0, source_ids=None, rounds=None,
              participating=True, discussion_enabled=True):
    return {
        "replied_up_to_seq": marker,
        "source_msg_ids": source_ids or set(),
        "discussion_rounds": rounds or {},
        "participating": participating,
        "discussion_enabled": discussion_enabled,
    }


class ChatWakeConditionsTest(unittest.TestCase):
    """W1–W10 từng điều kiện (check_chat_wake)."""

    def test_w1_kind_must_be_hoi(self):
        record = _msg_record(kind="bao-cao", mentions=[ME])
        self.assertIn("W1", center_dispatcher.check_chat_wake(
            record, 5, ME, _base_ctx()))
        record = _msg_record(kind="hoi", mentions=[ME])
        self.assertNotIn("W1", center_dispatcher.check_chat_wake(
            record, 5, ME, _base_ctx()))

    def test_w2_seq_must_be_above_marker(self):
        record = _msg_record(mentions=[ME])
        self.assertIn("W2", center_dispatcher.check_chat_wake(
            record, 10, ME, _base_ctx(marker=10)))
        self.assertNotIn("W2", center_dispatcher.check_chat_wake(
            record, 11, ME, _base_ctx(marker=10)))

    def test_w3_not_from_self(self):
        record = _msg_record(sender=ME, mentions=[OTHER])
        self.assertIn("W3", center_dispatcher.check_chat_wake(
            record, 5, ME, _base_ctx()))

    def test_w4_chung_needs_tag(self):
        untagged = _msg_record(room="chung")
        self.assertIn("W4", center_dispatcher.check_chat_wake(
            untagged, 5, ME, _base_ctx()))
        by_to = _msg_record(room="chung", to_account=ME)
        self.assertNotIn("W4", center_dispatcher.check_chat_wake(
            by_to, 5, ME, _base_ctx()))
        by_mention = _msg_record(room="chung", mentions=[ME])
        self.assertNotIn("W4", center_dispatcher.check_chat_wake(
            by_mention, 5, ME, _base_ctx()))

    def test_w4_dm_room(self):
        mine = _msg_record(room="dm:%s" % ME, sender="owner")
        self.assertNotIn("W4", center_dispatcher.check_chat_wake(
            mine, 5, ME, _base_ctx()))
        others = _msg_record(room="dm:%s" % OTHER, sender="owner")
        self.assertIn("W4", center_dispatcher.check_chat_wake(
            others, 5, ME, _base_ctx()))

    def test_w5_hub_source(self):
        hub = _msg_record(mentions=[ME], source="hub")
        self.assertIn("W5", center_dispatcher.check_chat_wake(
            hub, 5, ME, _base_ctx()))
        missing = _msg_record(mentions=[ME])
        self.assertNotIn("source", missing)
        self.assertNotIn("W5", center_dispatcher.check_chat_wake(
            missing, 5, ME, _base_ctx()))

    def test_w6_already_created_task(self):
        record = _msg_record(mentions=[ME], msg_id="m-000000000099")
        ctx = _base_ctx(source_ids={"m-000000000099"})
        self.assertIn("W6", center_dispatcher.check_chat_wake(
            record, 5, ME, ctx))

    def test_w7_discussion_round_closed(self):
        record = _msg_record(sender=OTHER, to_account=ME)
        closed = {"chung|%s|%s" % (OTHER, ME): {
            "count": 5, "state": "dong", "opened_seq": 1, "last_seq": 9}}
        ctx = _base_ctx(rounds=closed)
        self.assertIn("W7", center_dispatcher.check_chat_wake(
            record, 5, ME, ctx))
        open_round = {"chung|%s|%s" % (OTHER, ME): {
            "count": 4, "state": "mo", "opened_seq": 1, "last_seq": 9}}
        ctx = _base_ctx(rounds=open_round)
        self.assertNotIn("W7", center_dispatcher.check_chat_wake(
            record, 5, ME, ctx))
        # Tin thuong (khong phai thao luan) khong bi W7 chan.
        plain = _msg_record(mentions=[ME])
        ctx = _base_ctx(rounds=closed)
        self.assertNotIn("W7", center_dispatcher.check_chat_wake(
            plain, 5, ME, ctx))

    def test_w10_participating_and_discussion_flag(self):
        plain = _msg_record(mentions=[ME])
        ctx = _base_ctx(participating=False)
        self.assertIn("W10", center_dispatcher.check_chat_wake(
            plain, 5, ME, ctx))
        discussion = _msg_record(sender=OTHER, to_account=ME)
        ctx = _base_ctx(discussion_enabled=False)
        self.assertIn("W10", center_dispatcher.check_chat_wake(
            discussion, 5, ME, ctx))
        ctx = _base_ctx(discussion_enabled=False)
        self.assertNotIn("W10", center_dispatcher.check_chat_wake(
            plain, 5, ME, ctx))


class ChatPlanTest(unittest.TestCase):
    """Lap ke hoach chat: phong cu nhat len dau, toi da 1/chu ky,
    chong wake trung 3 lop, dispatcher khong tu ghi moc."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _plan(self, root):
        return center_dispatcher.plan_chat_for_account(
            root, ME, deps={"log_fn": lambda msg: None}, now=self.now)

    def test_dm_message_planned(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        plan = self._plan(root)
        self.assertEqual(plan, {"account_id": ME, "room": "dm:%s" % ME,
                                "seqs": [3], "kind": "center_chat",
                                "holder": "center_dispatcher:%d"
                                % int(self.now)})

    def test_oldest_room_first(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (5, _msg_record(room="chung", mentions=[ME])),
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        plan = self._plan(root)
        self.assertEqual(plan["room"], "dm:%s" % ME)
        self.assertEqual(plan["seqs"], [3])

    def test_untagged_chung_not_planned(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="chung")),
        ])
        self.assertIsNone(self._plan(root))

    def test_marker_blocks_replied(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (10, _msg_record(room="dm:%s" % ME, sender="owner")),
            (11, _msg_record(room="dm:%s" % ME, sender="owner")),
        ], chat_state={"account_id": ME, "rooms": {
            "dm:%s" % ME: {"replied_up_to_seq": 10,
                           "updated_at": _iso(self.now)}},
            "discussion_rounds": {}})
        plan = self._plan(root)
        self.assertEqual(plan["seqs"], [11])

    def test_second_plan_blocked_by_room_claim(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        self.assertIsNotNone(self._plan(root))
        self.assertIsNone(self._plan(root))

    def test_expired_room_claim_can_be_taken_over(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        room = "dm:%s" % ME
        self.assertTrue(center_claims.claim_room(
            root, ME, room, "holder-cu", now=self.now))
        claim_path = center_claims.claim_path(
            root, ME, center_claims.chat_room_key(room))
        old = self.now - 2000
        os.utime(claim_path, (old, old))
        plan = self._plan(root)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["seqs"], [3])

    def test_cap_two_task_workers_do_not_block_chat(self):
        # (S2, 22/11) Lan viec va lan chat tach cap rieng: 2 worker
        # VIEC dang chay KHONG chan worker CHAT (luat cu truoc S2 la
        # chan — test nay thay the test_cap_two_workers_blocks_chat).
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        center_claims.claim(root, ME, "t-aaaaaaaaaaaa", "w1",
                            now=self.now)
        center_claims.claim(root, ME, "t-bbbbbbbbbbbb", "w2",
                            now=self.now)
        plan = self._plan(root)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["seqs"], [3])

    def test_cap_chat_worker_blocks_second_chat(self):
        # (S2, 22/11) Cap lan chat rieng = 1: da co 1 worker chat
        # dang chay thi khong mo them worker chat thu 2.
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        center_claims.claim(
            root, ME, center_claims.chat_room_key("dm:%s" % ME),
            "w-chat", now=self.now)
        self.assertIsNone(self._plan(root))

    def test_cap_total_three_blocks_chat(self):
        # (S2, 22/11) Tran tong = cap viec (2) + cap chat (1) = 3:
        # 2 viec + 1 chat dang chay thi chat cho (du tran tong).
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        center_claims.claim(root, ME, "t-aaaaaaaaaaaa", "w1",
                            now=self.now)
        center_claims.claim(root, ME, "t-bbbbbbbbbbbb", "w2",
                            now=self.now)
        center_claims.claim(
            root, ME, center_claims.chat_room_key("chung"),
            "w-chat", now=self.now)
        self.assertIsNone(self._plan(root))

    def test_dispatcher_never_writes_chat_state(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        self._plan(root)
        self.assertFalse(
            (root / "center_chat_state").exists())

    def test_room_claim_key_format(self):
        self.assertEqual(center_claims.chat_room_key("chung"),
                         "chat__chung")
        self.assertEqual(center_claims.chat_room_key("dm:muse-chinh"),
                         "chat__dm-muse-chinh")
        with self.assertRaises(ValueError):
            center_claims.chat_room_key("dm:muse phu")


class ChatStuckTest(unittest.TestCase):
    """Chat tac (D4): >3 phut hoac >=3 tin/phong, muc "can xem"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def _root(self, messages):
        return _muse_root(Path(self.tmp.name) / "muse",
                          messages=messages)

    def test_old_message_over_three_minutes(self):
        root = self._root([(3, _msg_record(
            room="dm:%s" % ME, sender="owner",
            created_at=_iso(self.now - 200)))])
        stuck = center_dispatcher.detect_stuck_chats(root, ME, now=self.now)
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["level"], "cần xem")
        self.assertEqual(stuck[0]["waiting_count"], 1)
        self.assertEqual(stuck[0]["oldest_seq"], 3)
        self.assertGreaterEqual(stuck[0]["oldest_wait_seconds"], 180)

    def test_three_fresh_messages_by_count(self):
        root = self._root([
            (3, _msg_record(room="dm:%s" % ME, sender="owner",
                            msg_id="m-000000000001")),
            (4, _msg_record(room="dm:%s" % ME, sender="owner",
                            msg_id="m-000000000002")),
            (5, _msg_record(room="dm:%s" % ME, sender="owner",
                            msg_id="m-000000000003")),
        ])
        stuck = center_dispatcher.detect_stuck_chats(root, ME, now=self.now)
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["waiting_count"], 3)

    def test_two_fresh_messages_not_stuck(self):
        root = self._root([
            (3, _msg_record(room="dm:%s" % ME, sender="owner",
                            msg_id="m-000000000001")),
            (4, _msg_record(room="dm:%s" % ME, sender="owner",
                            msg_id="m-000000000002")),
        ])
        self.assertEqual(
            center_dispatcher.detect_stuck_chats(root, ME, now=self.now),
            [])

    def test_untagged_chung_not_stuck(self):
        root = self._root([(3, _msg_record(
            room="chung", created_at=_iso(self.now - 600)))])
        self.assertEqual(
            center_dispatcher.detect_stuck_chats(root, ME, now=self.now),
            [])

    def test_record_dedupes_same_episode(self):
        root = self._root([(3, _msg_record(
            room="dm:%s" % ME, sender="owner",
            created_at=_iso(self.now - 200)))])
        stuck = center_dispatcher.detect_stuck_chats(root, ME, now=self.now)
        self.assertEqual(
            center_dispatcher.record_chat_stuck_alerts(
                root, stuck, now=self.now), 1)
        self.assertEqual(
            center_dispatcher.record_chat_stuck_alerts(
                root, stuck, now=self.now), 0)
        lines = (root / "center" / "chat_stuck_alerts.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        alert = json.loads(lines[0])
        self.assertEqual(alert["level"], "cần xem")
        self.assertNotIn("Tin thu", alert["text"])


class ChatBriefTest(unittest.TestCase):
    """Helper center_chat_brief: 20 tin, tin gan viec, cau tam D7,
    tach tin >5.000 ky tu, moc chi ghi sau outbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        self.root = Path(self.tmp.name) / "muse"

    def _root_with_dm(self, created_at):
        messages = [(seq, _msg_record(
            room="dm:%s" % ME, sender="owner",
            msg_id="m-%012d" % seq, created_at=created_at))
            for seq in (1, 2, 3)]
        tasks = [("t-aaaaaaaaaaaa", {
            "task_id": "t-aaaaaaaaaaaa", "title": "Viec thu dai hon "
            "tam muoi ky tu de kiem tra cat tieu de",
            "status": "dang-lam", "assigned_to": [ME],
            "source_msg_id": None})]
        return _muse_root(self.root, messages=messages, tasks=tasks)

    def test_brief_contains_recent_and_task_line(self):
        root = self._root_with_dm(_iso(self.now - 10))
        brief = center_chat_brief.build_brief(
            root, ME, "dm:%s" % ME, [3], now=self.now)
        self.assertIn("seq 3", brief)
        self.assertIn("20 TIN GẦN NHẤT", brief)
        self.assertNotIn("CÂU THỪA NHẬN CHẬM — TẠM THỜI", brief)

    def test_brief_stale_message_has_temp_ack_with_minutes(self):
        root = self._root_with_dm(_iso(self.now - 610))
        brief = center_chat_brief.build_brief(
            root, ME, "dm:%s" % ME, [1, 2, 3], now=self.now)
        self.assertIn("TẠM THỜI", brief)
        self.assertIn("Xin lỗi vì trả lời chậm, tin này đã chờ 10 phút.",
                      brief)

    def test_split_grouped_reply(self):
        text = "x" * 12000
        parts = center_chat_brief.split_grouped_reply(text)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(part) <= 5000 for part in parts))
        self.assertEqual("".join(parts), text)
        self.assertEqual(
            center_chat_brief.split_grouped_reply("ngan"), ["ngan"])
        multi = "\n\n".join("dong %d" % i for i in range(2000))
        parts = center_chat_brief.split_grouped_reply(multi)
        self.assertTrue(all(len(part) <= 5000 for part in parts))
        self.assertEqual("".join(parts), multi)

    def test_mark_replied_requires_outbox_reply(self):
        root = self._root_with_dm(_iso(self.now - 10))
        with self.assertRaises(ValueError):
            center_chat_brief.mark_chat_replied(
                root, ME, "dm:%s" % ME, [3])
        self.assertFalse((root / "center_chat_state").exists())

    def test_mark_replied_writes_marker_after_outbox(self):
        root = self._root_with_dm(_iso(self.now - 10))
        center_outbox.enqueue_message(
            root, ME, "hoi", "Tra loi thu", task_id=None,
            room="dm:%s" % ME)
        marker = center_chat_brief.mark_chat_replied(
            root, ME, "dm:%s" % ME, [1, 3])
        self.assertEqual(marker, 3)
        state = center_dispatcher.read_chat_state(root, ME)
        self.assertEqual(
            state["rooms"]["dm:%s" % ME]["replied_up_to_seq"], 3)
        # Moc khong bao gio lui.
        center_outbox.enqueue_message(
            root, ME, "hoi", "Tra loi thu 2", task_id=None,
            room="dm:%s" % ME)
        marker = center_chat_brief.mark_chat_replied(
            root, ME, "dm:%s" % ME, [2])
        self.assertEqual(marker, 3)


class OutboxCliTest(unittest.TestCase):
    """CLI outbox: --task-id bo trong cho hoi (NS-32 (a)); --typing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "muse"
        self.root.mkdir()
        self.body = self.root / "body.txt"
        self.body.write_text("Noi dung tra loi thu", encoding="utf-8")

    def test_message_hoi_without_task_id(self):
        code = center_outbox.main([
            "--root", str(self.root), "--account-id", ME,
            "--message-kind", "hoi", "--body-file", str(self.body),
            "--room", "chung"])
        self.assertEqual(code, 0)
        rows, _total, _skipped = center_outbox.read_outbox(self.root)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][1]["task_id"])
        self.assertEqual(rows[0][1]["kind"], "hoi")

    def test_message_bao_cao_without_task_id_rejected(self):
        code = center_outbox.main([
            "--root", str(self.root), "--account-id", ME,
            "--message-kind", "bao-cao", "--body-file", str(self.body),
            "--room", "chung"])
        self.assertEqual(code, 2)

    def test_status_without_task_id_rejected(self):
        code = center_outbox.main([
            "--root", str(self.root), "--account-id", ME,
            "--status", "da-nhan"])
        self.assertEqual(code, 2)

    def test_typing_cli(self):
        code = center_outbox.main([
            "--root", str(self.root), "--account-id", ME,
            "--typing", "--room", "chung"])
        self.assertEqual(code, 0)
        rows, _total, _skipped = center_outbox.read_outbox(self.root)
        self.assertEqual(rows[0][1]["kind"], "typing")
        self.assertEqual(rows[0][1]["room"], "chung")
        self.assertGreater(rows[0][1]["started_at"], 0)
        code = center_outbox.main([
            "--root", str(self.root), "--account-id", ME, "--typing"])
        self.assertEqual(code, 2)


def _build_pc_root(tmp):
    root = Path(tmp)
    (root / "secrets").mkdir(parents=True)
    (root / "ui-config").mkdir(parents=True)
    _write_json(root / "ui-config" / "accounts.json", {
        "accounts": {
            ME: {"display_name": "Muse chinh",
                 "center_participating": True},
            OTHER: {"display_name": "Muse phu",
                    "center_participating": True},
        }})
    for name, value in (
            ("center-%s.fetch_secret" % ME, FETCH_CHINH),
            ("center-%s.push_secret" % ME, PUSH_CHINH),
            ("center-%s.fetch_secret" % OTHER, FETCH_PHU),
            ("center-%s.push_secret" % OTHER, PUSH_PHU)):
        (root / "secrets" / name).write_text(value + "\n")
    return root


def _serve(make_server_fn):
    server = make_server_fn()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return server, port, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _http(port, method, path, payload=None, secret=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["Authorization"] = "Bearer %s" % secret
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        data = exc.read().decode("utf-8")
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = {"raw": data}
        return exc.code, parsed


class TypingEndpointTest(unittest.TestCase):
    """Endpoint typing thu 6 + TTL 90 giay + xoa khi tin hoi that vao."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.pc_root = _build_pc_root(Path(self.tmp.name) / "pc")
        self.muse_root = Path(self.tmp.name) / "muse"
        self.muse_root.mkdir()
        self.networks = [ipaddress.ip_network("127.0.0.0/8"),
                         ipaddress.ip_network("::1/128")]
        self.rc_ctx = receiver.ReceiverContext(
            self.pc_root, self.networks, log_fn=lambda msg: None, min_gap=0)
        self.rc_server, self.rc_port, self.rc_thread = _serve(
            lambda: receiver.make_server(self.rc_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.rc_server, self.rc_thread)

    def _post(self, path, payload, secret=None):
        return _http(self.rc_port, "POST", path, payload=payload,
                     secret=secret)

    def _typing_path(self):
        return self.pc_root / "data" / "center" / "typing.json"

    def test_typing_ok_and_stored(self):
        started = time.time()
        status, body = self._post("/v1/center/%s/typing" % ME,
                                  {"room": "chung", "started_at": started},
                                  PUSH_CHINH)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        data = json.loads(self._typing_path().read_text(encoding="utf-8"))
        self.assertEqual(data[ME]["chung"]["started_at"], started)

    def test_typing_bad_body_writes_nothing(self):
        status, _body = self._post("/v1/center/%s/typing" % ME,
                                   {"room": "chung"}, PUSH_CHINH)
        self.assertEqual(status, 400)
        self.assertFalse(self._typing_path().exists())
        status, _body = self._post("/v1/center/%s/typing" % ME,
                                   {"room": "phong-la",
                                    "started_at": time.time()}, PUSH_CHINH)
        self.assertEqual(status, 400)
        self.assertFalse(self._typing_path().exists())

    def test_typing_wrong_secret_and_unknown_account(self):
        status, _body = self._post(
            "/v1/center/%s/typing" % ME,
            {"room": "chung", "started_at": time.time()},
            "TEST-FAKE-WRONG")
        self.assertEqual(status, 401)
        status, _body = self._post(
            "/v1/center/muse-khong-co/typing",
            {"room": "chung", "started_at": time.time()}, PUSH_CHINH)
        self.assertEqual(status, 404)

    def test_typing_ttl_90_seconds(self):
        self.assertEqual(center_store.TYPING_TTL_SECONDS, 90)
        started = time.time()
        self.assertTrue(center_store.typing_entry_is_live(
            {"started_at": started}, now=started + 89))
        self.assertFalse(center_store.typing_entry_is_live(
            {"started_at": started}, now=started + 91))

    def test_real_hoi_clears_typing_same_room_only(self):
        started = time.time()
        self._post("/v1/center/%s/typing" % ME,
                   {"room": "chung", "started_at": started}, PUSH_CHINH)
        self._post("/v1/center/%s/typing" % ME,
                   {"room": "dm:%s" % OTHER, "started_at": started},
                   PUSH_CHINH)
        status, _body = self._post("/v1/center/%s/messages" % ME, {
            "msg_id": "m-000000000001", "kind": "hoi",
            "to_account": None, "task_id": None,
            "body": "Tra loi that", "room": "chung"}, PUSH_CHINH)
        self.assertEqual(status, 200)
        data = json.loads(self._typing_path().read_text(encoding="utf-8"))
        self.assertNotIn("chung", data.get(ME) or {})
        self.assertIn("dm:%s" % OTHER, data.get(ME) or {})

    def test_other_account_hoi_does_not_clear(self):
        started = time.time()
        self._post("/v1/center/%s/typing" % ME,
                   {"room": "chung", "started_at": started}, PUSH_CHINH)
        status, _body = self._post("/v1/center/%s/messages" % OTHER, {
            "msg_id": "m-000000000002", "kind": "hoi",
            "to_account": None, "task_id": None,
            "body": "Tin cua tai khoan khac", "room": "chung"}, PUSH_PHU)
        self.assertEqual(status, 200)
        data = json.loads(self._typing_path().read_text(encoding="utf-8"))
        self.assertIn("chung", data.get(ME) or {})

    def test_outbox_flush_sends_typing_row(self):
        _write_json(self.muse_root / "center-config"
                    / ("%s.json" % ME), {
                        "endpoint": "http://127.0.0.1:1",
                        "receiver_endpoint":
                            "http://127.0.0.1:%d" % self.rc_port,
                        "fetch_secret": FETCH_CHINH,
                        "push_secret": PUSH_CHINH,
                        "via_proxy": False,
                    })
        center_outbox.enqueue_typing(self.muse_root, ME, "chung",
                                     started_at=time.time())
        result = center_outbox.flush(
            self.muse_root, account_id=ME, log_fn=lambda msg: None)
        self.assertEqual(result["sent"], 1)
        self.assertIsNone(result["error"])
        data = json.loads(self._typing_path().read_text(encoding="utf-8"))
        self.assertIn("chung", data.get(ME) or {})


class HubSourceTest(unittest.TestCase):
    """Truong source: "hub" (D3) — tin hub tu sinh co, tin thuong
    thieu truong (= khong phai tin hub)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.pc_root = _build_pc_root(Path(self.tmp.name) / "pc")

    def test_append_message_source_param(self):
        store = center_store.CenterStore(self.pc_root)
        msg, dup = store.append_message(
            ME, "hoi", None, None, "Tin thuong", "m-000000000001",
            room="chung")
        self.assertFalse(dup)
        self.assertNotIn("source", msg)
        msg2, dup2 = store.append_message(
            ME, "hoi", None, None, "Tin hub", "m-000000000002",
            room="chung", source="hub")
        self.assertFalse(dup2)
        self.assertEqual(msg2.get("source"), "hub")

    def test_hub_generated_messages_carry_source(self):
        store = center_store.CenterStore(self.pc_root)
        task, _dup = store.create_task(
            ME, "Viec thu", "Noi dung viec", "nghien-cuu", [OTHER],
            "2026-11-25T10:00:00+07:00", "ck-t3h-1")
        events = store.read_events()
        giao = [row for row in events
                if (row.get("record") or {}).get("kind") == "giao-viec"]
        self.assertEqual(len(giao), 1)
        self.assertEqual(giao[0]["record"].get("source"), "hub")
        task2, _dup2 = store.change_status(
            task["task_id"], "huy", OTHER, "assigned_to",
            "Khong lam duoc", "ck-t3h-2")
        self.assertEqual(task2["status"], "huy")
        events = store.read_events()
        hoi = [row for row in events
               if (row.get("record") or {}).get("kind") == "hoi"
               and (row.get("record") or {}).get("source") == "hub"]
        self.assertEqual(len(hoi), 1)


class PlannerJsonTest(unittest.TestCase):
    """Planner in ra center_chats + chat_stuck, dung 1 dong JSON."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t3h-")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def test_run_planning_emits_center_chats(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        plan, chats, stuck, _summary = center_plan_cycle.run_planning(
            root, account_id=ME)
        self.assertEqual(plan, [])
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["room"], "dm:%s" % ME)
        self.assertEqual(chats[0]["seqs"], [3])
        self.assertEqual(chats[0]["kind"], "center_chat")
        self.assertEqual(stuck, [])

    def test_run_planning_emits_chat_stuck(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner",
                            created_at=_iso(self.now - 600))),
        ])
        _plan, chats, stuck, _summary = center_plan_cycle.run_planning(
            root, account_id=ME)
        self.assertEqual(len(chats), 1)
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["level"], "cần xem")

    def test_main_dry_run_one_line_json(self):
        root = _muse_root(Path(self.tmp.name) / "muse", messages=[
            (3, _msg_record(room="dm:%s" % ME, sender="owner")),
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = center_plan_cycle.main([
                "--root", str(root), "--account-id", ME, "--dry-run"])
        self.assertEqual(code, 0)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertIn("center_tasks", data)
        self.assertIn("center_chats", data)
        self.assertIn("chat_stuck", data)
        self.assertEqual(len(data["center_chats"]), 1)


if __name__ == "__main__":
    unittest.main()
