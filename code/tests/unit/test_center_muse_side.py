#!/usr/bin/env python3
"""Test phía Muse chặng T2 (module trung tâm v0.2.2): center fetcher +
dispatcher + outbox + ledger + quét quá hạn.

unittest, stdlib-only, HTTP thật trên localhost với hub T1 thật chạy
trong tiến trình (gateway + receiver). Mọi giá trị secret trong test
đều là mẫu giả dạng rõ ràng ("TEST-FAKE-..."), không secret thật,
không nội dung tin thật. Mọi tiêu đề/nội dung việc trong test đều là
dữ liệu giả ("Viec thu", "Noi dung viec").
"""

import ipaddress
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "muse-side"))
sys.path.insert(0, str(REPO_ROOT))

import center_claims  # noqa: E402
import center_dispatcher  # noqa: E402
import center_fetcher  # noqa: E402
import center_ledger  # noqa: E402
import center_outbox  # noqa: E402
import center_overdue  # noqa: E402
import fetcher  # noqa: E402
from pc.gateway import gateway_main as gateway  # noqa: E402
from pc.sync_receiver import receiver_main as receiver  # noqa: E402

FETCH_CHINH = "TEST-FAKE-CENTER-FETCH-CHINH"
PUSH_CHINH = "TEST-FAKE-CENTER-PUSH-CHINH"
FETCH_PHU = "TEST-FAKE-CENTER-FETCH-PHU"
PUSH_PHU = "TEST-FAKE-CENTER-PUSH-PHU"
FETCH_WRONG = "TEST-FAKE-CENTER-FETCH-WRONG"
QUEUE_BOT_A = "TEST-FAKE-QUEUE-BOT-A"
DUE_FUTURE = "2026-11-25T10:00:00+07:00"
DUE_PAST = "2026-11-01T10:00:00+07:00"


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _build_pc_root(tmp):
    root = Path(tmp)
    (root / "secrets").mkdir(parents=True)
    (root / "ui-config").mkdir(parents=True)
    (root / "data" / "bot-a").mkdir(parents=True)
    _write_json(root / "registry.json", {
        "version": 1,
        "bots": {"bot-a": {"bot_id": "bot-a", "platform": "zalo"}}})
    (root / "secrets" / "bot-a.queue_secret").write_text(
        QUEUE_BOT_A + "\n")
    _write_json(root / "data" / "bot-a" / "state.json", {
        "bot_id": "bot-a", "queue_latest_seq": 0, "queue_floor_seq": 0,
    })
    (root / "data" / "bot-a" / "queue.jsonl").write_text("", encoding="utf-8")
    _write_json(root / "ui-config" / "accounts.json", {
        "accounts": {
            "muse-chinh": {"display_name": "Muse chinh",
                           "center_participating": True},
            "muse-phu": {"display_name": "Muse phu",
                         "center_participating": True},
            "muse-ngoai": {"display_name": "Ngoai",
                           "center_participating": False},
        }})
    for name, value in (
            ("center-muse-chinh.fetch_secret", FETCH_CHINH),
            ("center-muse-chinh.push_secret", PUSH_CHINH),
            ("center-muse-phu.fetch_secret", FETCH_PHU),
            ("center-muse-phu.push_secret", PUSH_PHU)):
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


class CenterMuseFixture(unittest.TestCase):
    """Hub T1 thật trong tiến trình + thư mục gốc Muse tạm."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="center-t2-")
        self.addCleanup(self.tmp.cleanup)
        self.pc_root = _build_pc_root(Path(self.tmp.name) / "pc")
        self.muse_root = Path(self.tmp.name) / "muse"
        self.muse_root.mkdir()
        self.logs = []
        self.alerts = []
        self.networks = [ipaddress.ip_network("127.0.0.0/8"),
                         ipaddress.ip_network("::1/128")]
        self.gw_ctx = gateway.GatewayContext(
            self.pc_root, self.networks, log_fn=lambda msg: None)
        self.gw_server, self.gw_port, self.gw_thread = _serve(
            lambda: gateway.make_server(self.gw_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.gw_server, self.gw_thread)
        self.rc_ctx = receiver.ReceiverContext(
            self.pc_root, self.networks, log_fn=lambda msg: None, min_gap=0)
        self.rc_server, self.rc_port, self.rc_thread = _serve(
            lambda: receiver.make_server(self.rc_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.rc_server, self.rc_thread)
        self.gw_base = "http://127.0.0.1:%d" % self.gw_port
        self.rc_base = "http://127.0.0.1:%d" % self.rc_port
        self._write_center_config("muse-phu", FETCH_PHU, PUSH_PHU)

    def _write_center_config(self, account_id, fetch_secret, push_secret):
        _write_json(self.muse_root / "center-config"
                    / ("%s.json" % account_id), {
                        "endpoint": self.gw_base,
                        "receiver_endpoint": self.rc_base,
                        "fetch_secret": fetch_secret,
                        "push_secret": push_secret,
                        "via_proxy": False,
                    })

    def _alert(self, kind, account_id, detail):
        self.alerts.append((kind, account_id, detail))

    def _run_fetch(self):
        return center_fetcher.run_cycle(
            self.muse_root, log_fn=self.logs.append, alert_fn=self._alert)

    def gw_get(self, path, secret=FETCH_PHU):
        return _http(self.gw_port, "GET", path, secret=secret)

    def rc_post(self, path, payload=None, secret=PUSH_CHINH):
        return _http(self.rc_port, "POST", path, payload=payload,
                     secret=secret)

    def create_task(self, client_key="ck-1", assigned_to="muse-phu",
                    secret=PUSH_CHINH, caller="muse-chinh", **overrides):
        payload = {"client_key": client_key, "title": "Viec thu",
                   "body": "Noi dung viec", "kind": "nghien-cuu",
                   "assigned_to": assigned_to, "due_at": DUE_FUTURE}
        payload.update(overrides)
        return self.rc_post("/v1/center/%s/tasks" % caller, payload,
                            secret=secret)

    def _write_local_task(self, task_id, assigned_to, status="moi",
                          created_at="2026-11-19T10:00:00+07:00",
                          due_at=DUE_FUTURE, title="Viec thu"):
        record = {
            "schema_version": 1, "task_id": task_id, "title": title,
            "body": "Noi dung viec", "kind": "nghien-cuu",
            "created_by": "owner", "assigned_from": "owner",
            "assigned_to": assigned_to, "status": status,
            "due_at": due_at, "created_at": created_at,
            "updated_at": created_at, "seen_at": None, "history": [],
        }
        _write_json(self.muse_root / "center_tasks" / ("%s.json" % task_id),
                    {"seq": 1, "task_id": task_id, "record": record})
        return record


class TestCenterFetcher(CenterMuseFixture):
    """Tiêu chí T2 (1): kéo feed thật từ hub giả lập local."""

    def test_fetch_real_feed_cursor_advances_and_local_matches_hub(self):
        status, body = self.create_task()
        self.assertEqual(status, 200)
        task_id = body["task_id"]
        result = self._run_fetch()
        self.assertFalse(result["skipped"])
        stats = result["accounts"]["muse-phu"]
        self.assertEqual(stats["events"], 2)
        self.assertEqual(stats["cursor_before"], 0)
        self.assertEqual(stats["cursor_after"], 2)
        # File local khớp hub: việc + tin giao-viec tự sinh.
        task_file = json.loads(
            (self.muse_root / "center_tasks" / ("%s.json" % task_id))
            .read_text(encoding="utf-8"))
        self.assertEqual(task_file["record"]["title"], "Viec thu")
        self.assertEqual(task_file["record"]["assigned_to"], ["muse-phu"])
        messages, _skipped = center_fetcher.read_jsonl(
            self.muse_root / "center_messages.jsonl")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["record"]["kind"], "giao-viec")
        self.assertEqual(messages[0]["record"]["task_id"], task_id)
        # Chu kỳ 2: không còn sự kiện mới, cursor giữ nguyên, tin
        # không bị ghi trùng.
        result2 = self._run_fetch()
        self.assertEqual(result2["accounts"]["muse-phu"]["events"], 0)
        self.assertEqual(result2["accounts"]["muse-phu"]["cursor_after"], 2)
        messages2, _ = center_fetcher.read_jsonl(
            self.muse_root / "center_messages.jsonl")
        self.assertEqual(len(messages2), 1)

    def test_fetch_stale_cursor_does_not_advance(self):
        _write_json(self.muse_root / "center_cursors" / "muse-phu.json",
                    {"seq": 999})
        result = self._run_fetch()
        stats = result["accounts"]["muse-phu"]
        self.assertEqual(stats["error"], "stale_center_cursor")
        self.assertEqual(stats["cursor_after"], 999)


class TestCenterDispatcherOtherAccount(CenterMuseFixture):
    """Tiêu chí T2 (2): việc giao cho tài khoản khác thì chỉ đọc."""

    def test_task_for_other_account_via_hub_is_read_only(self):
        # Việc giao cho muse-chinh (người giao là muse-phu — hub cấm
        # tự giao nên không tạo việc này với danh nghĩa muse-chinh);
        # muse-phu kéo về vẫn thấy (C5) nhưng dispatcher của muse-phu
        # chỉ đọc, không claim.
        status, body = self.create_task(client_key="ck-other",
                                        assigned_to="muse-chinh",
                                        secret=PUSH_PHU, caller="muse-phu")
        self.assertEqual(status, 200)
        task_id = body["task_id"]
        self._run_fetch()
        woken = []

        def _worker(account_id, task, ctx):
            woken.append(task["task_id"])

        summary = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-phu",
            deps={"worker_fn": _worker,
                  "log_fn": self.logs.append, "alert_fn": self._alert})
        self.assertEqual(woken, [])
        self.assertEqual(summary["woken"], 0)
        self.assertEqual(summary["other_tasks_read_only"], 1)
        self.assertEqual(summary["own_tasks"], 0)
        # Không có claim nào cho việc đó; không có dòng ledger nào.
        self.assertEqual(
            center_claims.list_claims(self.muse_root, "muse-phu"), [])
        self.assertEqual(
            center_ledger.entries_for(self.muse_root, "muse-phu", task_id),
            [])
        # Ngược lại: dispatcher của muse-chinh (cần cấu hình) thì
        # claim + đánh thức đúng việc đó.
        self._write_center_config("muse-chinh", FETCH_CHINH, PUSH_CHINH)
        summary2 = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-chinh",
            deps={"worker_fn": _worker,
                  "log_fn": self.logs.append, "alert_fn": self._alert})
        self.assertEqual(summary2["woken"], 1)
        self.assertEqual(woken, [task_id])

    def test_task_for_other_account_via_local_fake_feed(self):
        # Feed giả lập local (không tạo việc thật trên hub): 1 việc
        # giao cho tài khoản khác đang tham gia trung tâm.
        self._write_local_task("t-abcdef000001", "muse-chinh")
        woken = []

        def _worker(account_id, task, ctx):
            woken.append(task["task_id"])

        summary = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-phu",
            deps={"worker_fn": _worker,
                  "log_fn": self.logs.append, "alert_fn": self._alert})
        self.assertEqual(woken, [])
        self.assertEqual(summary["woken"], 0)
        self.assertEqual(
            center_claims.list_claims(self.muse_root, "muse-phu"), [])
        self.assertEqual(
            center_ledger.read_ledger(self.muse_root, "muse-phu"), [])

    def test_hub_rejects_task_for_non_participating_account(self):
        # Hub từ chối tạo việc cho tài khoản chưa tham gia (400),
        # không ghi gì (feed vẫn rỗng).
        status, body = self.create_task(client_key="ck-ngoai",
                                        assigned_to="muse-ngoai")
        self.assertEqual(status, 400)
        status, body = self.gw_get("/v1/center/muse-phu/feed?since=0")
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])
        self.assertEqual(body["latest_seq"], 0)


class TestCenterDispatcherOwnTasks(CenterMuseFixture):
    def test_one_wake_per_cycle_and_cap_two(self):
        self._write_local_task("t-abcdef000001", "muse-phu",
                               created_at="2026-11-19T10:00:00+07:00")
        self._write_local_task("t-abcdef000002", "muse-phu",
                               created_at="2026-11-19T10:01:00+07:00")
        self._write_local_task("t-abcdef000003", "muse-phu",
                               created_at="2026-11-19T10:02:00+07:00")
        woken = []

        def _worker(account_id, task, ctx):
            # Worker ctx chỉ mang việc + tin gắn task_id — KHÔNG nạp
            # prompt bot, KHÔNG trí nhớ bot.
            self.assertEqual(set(ctx.keys()),
                             {"root", "account_id", "task", "messages",
                              "now"})
            self.assertEqual(ctx["task"]["task_id"], task["task_id"])
            woken.append(task["task_id"])

        summary = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-phu",
            deps={"worker_fn": _worker,
                  "log_fn": self.logs.append, "alert_fn": self._alert})
        # Tối đa 1 việc/chu kỳ, việc cũ nhất trước.
        self.assertEqual(summary["woken"], 1)
        self.assertEqual(woken, ["t-abcdef000001"])
        # Giả lập 2 việc đang chạy (cap 2/tài khoản): dispatcher
        # không đánh thức thêm.
        now = center_dispatcher.now_epoch()
        self.assertTrue(center_claims.claim(
            self.muse_root, "muse-phu", "t-abcdef000002", "test", now=now))
        summary2 = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-phu",
            deps={"worker_fn": _worker,
                  "log_fn": self.logs.append, "alert_fn": self._alert},
            now=now)
        self.assertEqual(summary2["woken"], 0)
        self.assertTrue(summary2["cap_blocked"])
        self.assertEqual(woken, ["t-abcdef000001"])

    def test_terminal_state_from_feed_syncs_ledger(self):
        self._write_local_task("t-abcdef000001", "muse-phu",
                               status="da-xac-nhan")
        summary = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-phu",
            deps={"worker_fn": lambda a, t, c: None,
                  "log_fn": self.logs.append, "alert_fn": self._alert})
        self.assertEqual(summary["terminal_synced"], 1)
        self.assertEqual(
            center_ledger.latest_stage(
                self.muse_root, "muse-phu", "t-abcdef000001"),
            "confirmed")

    def test_boot_scan_marks_stale_claim_unknown(self):
        self._write_local_task("t-abcdef000001", "muse-phu")
        # Ghi sẵn marker boot cũ (trực tiếp, không qua dispatcher để
        # tránh dispatcher claim việc trước khi boot-scan chạy).
        _write_json(
            self.muse_root / "center_dispatcher_state" / "boot_marker.json",
            {"boot_id": "boot-cu", "at": "2026-11-19T09:00:00+07:00"})
        now = 1795057200.0
        self.assertTrue(center_claims.claim(
            self.muse_root, "muse-phu", "t-abcdef000001", "test", now=now))
        # Đặt mtime claim + heartbeat về quá 15 phút trước.
        for suffix in (".json", ".heartbeat"):
            path = (self.muse_root / "center_claims"
                    / ("muse-phu__t-abcdef000001%s" % suffix))
            if suffix == ".heartbeat":
                path.write_text("{}", encoding="utf-8")
            os.utime(path, (now - 3600, now - 3600))
        summary = center_dispatcher.run_cycle_for_account(
            self.muse_root, "muse-phu",
            deps={"worker_fn": lambda a, t, c: None,
                  "boot_id_fn": lambda: "boot-moi",
                  "log_fn": self.logs.append, "alert_fn": self._alert},
            now=now)
        self.assertEqual(summary["boot_unknown"], 1)
        self.assertEqual(
            center_ledger.latest_stage(
                self.muse_root, "muse-phu", "t-abcdef000001"),
            "unknown")
        self.assertFalse(center_claims.is_claimed(
            self.muse_root, "muse-phu", "t-abcdef000001", now=now))


class TestCenterOutbox(CenterMuseFixture):
    """Tiêu chí T2 (3): POST đổi trạng thái + gửi tin qua outbox."""

    def test_status_and_message_flush_advances_cursor_and_dedupes(self):
        status, body = self.create_task()
        task_id = body["task_id"]
        self._run_fetch()
        row = center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "da-nhan",
            client_key="ck-status-1")
        result = center_outbox.flush(
            self.muse_root, "muse-phu",
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["cursor_after"], 1)
        # Hub đã ghi trạng thái mới.
        _s, task_body = self.gw_get(
            "/v1/center/muse-phu/tasks/%s" % task_id)
        self.assertEqual(task_body["task"]["status"], "da-nhan")
        history_len = len(task_body["task"]["history"])
        # Gửi tin hoi qua outbox.
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "Hoi ve viec nay",
            task_id=task_id, msg_id="m-abcdef000001")
        result = center_outbox.flush(
            self.muse_root, "muse-phu",
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["cursor_after"], 2)
        _s, feed = self.gw_get("/v1/center/muse-phu/feed?since=0")
        self.assertEqual(feed["latest_seq"], 4)
        # Gửi lại cùng client_key đổi trạng thái -> duplicate, hub
        # không ghi thêm (history giữ nguyên).
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "da-nhan",
            client_key="ck-status-1")
        result = center_outbox.flush(
            self.muse_root, "muse-phu",
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["duplicates"], 1)
        _s, task_body = self.gw_get(
            "/v1/center/muse-phu/tasks/%s" % task_id)
        self.assertEqual(len(task_body["task"]["history"]), history_len)
        # Gửi lại cùng msg_id -> duplicate, feed không thêm seq.
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "Hoi ve viec nay",
            task_id=task_id, msg_id="m-abcdef000001")
        result = center_outbox.flush(
            self.muse_root, "muse-phu",
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(result["duplicates"], 1)
        _s, feed = self.gw_get("/v1/center/muse-phu/feed?since=0")
        self.assertEqual(feed["latest_seq"], 4)

    def test_report_done_enqueues_status_then_message_adjacent(self):
        status, body = self.create_task()
        task_id = body["task_id"]
        s_row, m_row = center_outbox.report_done(
            self.muse_root, "muse-phu", task_id, "Da lam xong, ket qua o day")
        self.assertEqual(s_row["to_status"], "da-bao-xong")
        self.assertEqual(m_row["kind"], "bao-cao")
        self.assertEqual(m_row["task_id"], task_id)
        rows, total, _ = center_outbox.read_outbox(self.muse_root)
        self.assertEqual(total, 2)
        # Trạng thái trước, tin bao-cao ngay sau — cùng lượt outbox.
        self.assertEqual(rows[0][1]["kind"], "status")
        self.assertEqual(rows[1][1]["kind"], "bao-cao")
        # Flush cả 2 (mỗi lần 1 dòng): hub ghi da-bao-xong trước,
        # tin bao-cao sau (seq tin > seq trạng thái).
        center_outbox.flush(self.muse_root, "muse-phu",
                            log_fn=self.logs.append, alert_fn=self._alert)
        center_outbox.flush(self.muse_root, "muse-phu",
                            log_fn=self.logs.append, alert_fn=self._alert)
        _s, feed = self.gw_get("/v1/center/muse-phu/feed?since=0")
        seqs = [(e["kind"], e["seq"]) for e in feed["events"]]
        status_seq = [s for k, s in seqs if k == "task"][-1]
        msg_seq = [s for k, s in seqs if k == "message"][-1]
        self.assertGreater(msg_seq, status_seq)

    def test_giao_viec_message_refused_client_side(self):
        with self.assertRaises(ValueError):
            center_outbox.enqueue_message(
                self.muse_root, "muse-phu", "giao-viec", "Tu sinh",
                task_id="t-abcdef000001")

    # --- FIXL giai doan 1 (22/11): A2 + bo dem + dead-letter + hint ---
    # Mock http_post_fn cua flush (khong dung test fetcher):
    # (url, secret, payload, timeout, account_id)
    #   -> (status, body_dict_or_None, error_text).

    def _mock_post(self, replies):
        calls = []

        def _fake(url, secret, payload, timeout, account_id):
            calls.append((url, payload))
            if replies:
                return replies.pop(0)
            return (200, {}, None)

        return _fake, calls

    def _flush_mock(self, http_post_fn):
        return center_outbox.flush(
            self.muse_root, "muse-phu",
            log_fn=self.logs.append, alert_fn=self._alert,
            http_post_fn=http_post_fn)

    def _failure_state(self):
        return center_outbox._load_failure_state(
            self.muse_root, "muse-phu")

    def _dead_lines(self):
        path = self.muse_root / "center_outbox_dead.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_flush_drops_transition_forbidden_403_advances_cursor(self):
        task_id = "t-abcdef000001"
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "dang-lam",
            client_key="ck-fixl-a2-1")
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "da-bao-xong",
            client_key="ck-fixl-a2-2")
        fake, calls = self._mock_post([
            (403, {"error": "forbidden",
                   "code": "transition_forbidden"}, None),
            (200, {}, None),
        ])
        result = self._flush_mock(fake)
        # Dong lui bi bo trong cung chu ky, con tro tien.
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(result["error"], "transition_forbidden")
        # Bo dem loi ma KHONG tang, khong bao dong loi ma.
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(self.alerts, [])
        # Co ghi dead-letter.
        self.assertEqual(len(self._dead_lines()), 1)
        # Dong sau xa duoc ngay chu ky sau.
        result = self._flush_mock(fake)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["cursor_after"], 2)
        self.assertEqual(len(calls), 2)

    def test_flush_keeps_401_and_not_participating_403(self):
        task_id = "t-abcdef000001"
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "dang-lam",
            client_key="ck-fixl-np-1")
        # 403 not_participating: khong bo dong, khong dem, giu dong
        # thu lai chu ky sau, moi lan gap ghi log kem hint.
        fake, _calls = self._mock_post([
            (403, {"error": "not_participating"}, None),
            (403, {"error": "not_participating"}, None),
        ])
        for _ in range(2):
            result = self._flush_mock(fake)
            self.assertEqual(result["error"], "auth_403")
            self.assertEqual(result["cursor_after"], 0)
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(self.alerts, [])
        self.assertTrue(any("kiểm tra cờ tham gia" in entry
                            for entry in self.logs))
        self.assertEqual(len(self._dead_lines()), 0)
        # 401: giu nguyen hanh vi cu (dem/bao nhu truoc).
        fake401, _c = self._mock_post([
            (401, {"error": "unauthorized"}, None),
        ] * 3)
        for _ in range(3):
            result = self._flush_mock(fake401)
            self.assertEqual(result["error"], "auth_401")
            self.assertEqual(result["cursor_after"], 0)
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 3)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0],
                         "center_outbox_auth_failure")
        self.assertEqual(self.alerts[0][2]["hint"],
                         "kiểm tra mã bí mật")
        self.assertTrue(any("kiểm tra mã bí mật" in entry
                            for entry in self.logs))
        self.assertFalse(any("push secret lech" in entry
                             for entry in self.logs))

    def test_flush_keeps_403_without_code_fail_open(self):
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", "t-abcdef000001", "dang-lam",
            client_key="ck-fixl-fo-1")
        fake, _calls = self._mock_post([
            (403, None, None),
            (403, {"error": "forbidden"}, None),
        ])
        for _ in range(2):
            result = self._flush_mock(fake)
            self.assertEqual(result["error"], "auth_403")
            self.assertEqual(result["cursor_after"], 0)
        # Fail-open: van dem loi xac thuc nhu cu, khong bo dong.
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 2)
        self.assertEqual(len(self._dead_lines()), 0)
        self.assertTrue(any(
            "kiểm tra mã bí mật hoặc phân quyền tài khoản" in entry
            for entry in self.logs))

    # --- C-7 (22/11): bo dong tin nhan 403 vinh vien + status 403
    # not_assignee; giu nguyen 401, not_participating, 403 khong ma.
    def test_flush_drops_message_403_room_forbidden(self):
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "tin hoi",
            room="dm:muse-chinh")
        fake, _calls = self._mock_post([
            (403, {"error": "room_forbidden"}, None),
        ])
        result = self._flush_mock(fake)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(result["error"], "room_forbidden")
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(self.alerts, [])
        self.assertEqual(len(self._dead_lines()), 1)
        self.assertEqual(self._dead_lines()[0]["reason"],
                         "room_forbidden")

    def test_flush_drops_message_403_discussion_disabled(self):
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "tin hoi")
        fake, _calls = self._mock_post([
            (403, {"error": "discussion_disabled"}, None),
        ])
        result = self._flush_mock(fake)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(result["error"], "discussion_disabled")
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(len(self._dead_lines()), 1)
        self.assertEqual(self._dead_lines()[0]["reason"],
                         "discussion_disabled")

    def test_flush_drops_message_403_to_account_not_participating(self):
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "tin hoi",
            to_account=["muse-ngung"])
        fake, _calls = self._mock_post([
            (403, {"error": "to_account_not_participating"}, None),
        ])
        result = self._flush_mock(fake)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(result["error"],
                         "to_account_not_participating")
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(len(self._dead_lines()), 1)
        self.assertEqual(self._dead_lines()[0]["reason"],
                         "to_account_not_participating")

    def test_flush_drops_status_403_not_assignee(self):
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", "t-abcdef000001", "dang-lam",
            client_key="ck-c7-na-1")
        fake, _calls = self._mock_post([
            (403, {"error": "forbidden", "code": "not_assignee"},
             None),
        ])
        result = self._flush_mock(fake)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["cursor_after"], 1)
        self.assertEqual(result["error"], "not_assignee")
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(self.alerts, [])
        self.assertEqual(len(self._dead_lines()), 1)
        self.assertEqual(self._dead_lines()[0]["reason"],
                         "not_assignee")

    def test_flush_keeps_message_403_not_participating(self):
        # FIXL §6.1: 403 not_participating giu dong (thu lai), ca
        # voi dong tin nhan — chi 3 ma C-7 moi bo dong.
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "tin hoi")
        fake, _calls = self._mock_post([
            (403, {"error": "not_participating"}, None),
        ])
        result = self._flush_mock(fake)
        self.assertEqual(result["error"], "auth_403")
        self.assertEqual(result["cursor_after"], 0)
        self.assertEqual(
            self._failure_state()["consec_auth_failures"], 0)
        self.assertEqual(len(self._dead_lines()), 0)

    def test_flush_keeps_message_401(self):
        # 401 khong bao gio bo dong, ca voi dong tin nhan.
        center_outbox.enqueue_message(
            self.muse_root, "muse-phu", "hoi", "tin hoi")
        fake, _calls = self._mock_post([
            (401, {"error": "unauthorized"}, None),
        ])
        result = self._flush_mock(fake)
        self.assertEqual(result["error"], "auth_401")
        self.assertEqual(result["cursor_after"], 0)
        self.assertEqual(len(self._dead_lines()), 0)

    def test_dropped_status_row_written_to_dead_letter(self):
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", "t-abcdef000001", "dang-lam",
            client_key="ck-fixl-dl-1")
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", "t-abcdef000002", "da-nhan",
            client_key="ck-fixl-dl-2")
        # Dong alert qua TTL 30 phut (khong goi HTTP).
        old_iso = center_outbox.epoch_to_iso(
            center_outbox.now_epoch() - 2000)
        center_outbox.append_jsonl(
            center_outbox.outbox_path(self.muse_root), {
                "kind": "hoi", "msg_id": "m-abcdef000099",
                "body": "Canh bao", "task_id": None,
                "alert": "cap_stuck", "created_at": old_iso})
        fake, _calls = self._mock_post([
            (403, {"error": "forbidden",
                   "code": "transition_forbidden"}, None),
            (400, {"error": "bad_transition"}, None),
        ])
        for _ in range(3):
            self._flush_mock(fake)
        lines = self._dead_lines()
        self.assertEqual(len(lines), 3)
        required = {"line_no", "task_id", "to_status", "kind",
                    "http_status", "reason", "hub_error", "hub_body",
                    "dropped_at", "account_id"}
        for entry in lines:
            self.assertTrue(required <= set(entry.keys()), entry)
            self.assertEqual(entry["account_id"], "muse-phu")
        self.assertEqual(lines[0]["line_no"], 1)
        self.assertEqual(lines[0]["task_id"], "t-abcdef000001")
        self.assertEqual(lines[0]["to_status"], "dang-lam")
        self.assertEqual(lines[0]["http_status"], 403)
        self.assertEqual(lines[0]["reason"], "transition_forbidden")
        self.assertEqual(lines[0]["hub_error"], "forbidden")
        self.assertEqual(lines[0]["hub_body"], {
            "error": "forbidden", "code": "transition_forbidden"})
        self.assertEqual(lines[1]["line_no"], 2)
        self.assertEqual(lines[1]["http_status"], 400)
        self.assertEqual(lines[1]["hub_error"], "bad_transition")
        self.assertEqual(lines[2]["line_no"], 3)
        self.assertEqual(lines[2]["kind"], "hoi")
        self.assertEqual(lines[2]["reason"], "alert_ttl_expired")
        self.assertIsNone(lines[2]["http_status"])

    def test_transition_drop_counter_alerts_once_per_three_and_rearms(self):
        task_id = "t-abcdef000001"
        fake, _calls = self._mock_post([])
        fake_403 = lambda *a: (  # noqa: E731 - mock ngan gon
            403, {"error": "forbidden",
                  "code": "transition_forbidden"}, None)
        for index in range(3):
            center_outbox.enqueue_status(
                self.muse_root, "muse-phu", task_id, "dang-lam",
                client_key="ck-fixl-ct-%d" % index)
            result = self._flush_mock(fake_403)
            self.assertEqual(result["dropped"], 1)
        # Du 3 moc cung viec trong 60 phut -> bao 1 lan.
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0],
                         "center_outbox_transition_dropped")
        self.assertEqual(self.alerts[0][2]["task_id"], task_id)
        self.assertEqual(self.alerts[0][2]["to_status"], "dang-lam")
        self.assertEqual(self.alerts[0][2]["dropped"], 3)
        self.assertIn("kiểm tra dòng trạng thái lùi",
                      self.alerts[0][2]["hint"])
        # Re-arm: moc cua viec do da xoa, dem lai tu dau.
        self.assertNotIn(task_id,
                         self._failure_state().get("dropped_by_task")
                         or {})
        # Lan bo thu 4: chua du 3 moc moi -> khong bao lai.
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "dang-lam",
            client_key="ck-fixl-ct-3")
        self._flush_mock(fake_403)
        self.assertEqual(len(self.alerts), 1)
        # 2 lan bo nua -> du 3 moc moi -> bao lai 1 lan.
        for index in (4, 5):
            center_outbox.enqueue_status(
                self.muse_root, "muse-phu", task_id, "dang-lam",
                client_key="ck-fixl-ct-%d" % index)
            self._flush_mock(fake_403)
        self.assertEqual(len(self.alerts), 2)
        self.assertEqual(self.alerts[1][0],
                         "center_outbox_transition_dropped")

    def test_transition_drop_counter_missing_task_id_alerts_after_three(self):
        # Audit FIXL §4 #1: dong status thieu task_id (None) — key
        # None ghi JSON thanh "null", lan load sau khong tim thay
        # nen moc dem lai tu dau moi chu ky, khong bao gio du N=3.
        # Sau chuan hoa key ("unknown"), moc phai ton tai qua
        # ghi/load JSON va bao du 1 lan sau 3 lan bo.
        for index in range(3):
            center_outbox._record_transition_drop(
                self.muse_root, "muse-phu", None, "dang-lam",
                self._alert)
            if index < 2:
                # Moc phai ton tai sau khi ghi ra dia + load lai
                # (bat dung loi key "null" cua JSON).
                marks = (self._failure_state().get("dropped_by_task")
                         or {}).get("unknown")
                self.assertEqual(len(marks), index + 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0],
                         "center_outbox_transition_dropped")
        self.assertEqual(self.alerts[0][2]["task_id"], "unknown")
        self.assertEqual(self.alerts[0][2]["to_status"], "dang-lam")
        self.assertEqual(self.alerts[0][2]["dropped"], 3)
        self.assertIn("kiểm tra dòng trạng thái lùi",
                      self.alerts[0][2]["hint"])
        # Re-arm: moc cua "unknown" da xoa sau khi bao.
        self.assertNotIn("unknown",
                         self._failure_state().get("dropped_by_task")
                         or {})

    def test_transition_drop_counter_prunes_marks_older_than_60_minutes(self):
        task_id = "t-abcdef000001"
        now = center_outbox.now_epoch()
        center_outbox._save_failure_state(
            self.muse_root, "muse-phu", {
                "consec_auth_failures": 0, "alerted": False,
                "dropped_by_task": {task_id: [now - 4000, now - 3900]}})
        center_outbox.enqueue_status(
            self.muse_root, "muse-phu", task_id, "dang-lam",
            client_key="ck-fixl-ct-old")
        fake_403 = lambda *a: (  # noqa: E731 - mock ngan gon
            403, {"error": "forbidden",
                  "code": "transition_forbidden"}, None)
        result = self._flush_mock(fake_403)
        self.assertEqual(result["dropped"], 1)
        # Moc cu hon 60 phut bi cat khi ghi moc moi -> chi con 1.
        marks = self._failure_state()["dropped_by_task"][task_id]
        self.assertEqual(len(marks), 1)
        self.assertGreater(marks[0], now - 60)
        self.assertEqual(self.alerts, [])

    def test_dead_letter_concurrent_appends_no_loss(self):
        # Skill python-race-discipline: test xen ke 2 luong cho
        # duong ghi chung moi (dead-letter).
        errors = []

        def _append(index):
            try:
                for seq in range(25):
                    center_outbox.write_dead_letter(
                        self.muse_root, "muse-phu", index * 100 + seq,
                        {"kind": "status",
                         "task_id": "t-abcdef000001",
                         "to_status": "dang-lam"},
                        403, "transition_forbidden",
                        hub_error="forbidden",
                        hub_body={"error": "forbidden",
                                  "code": "transition_forbidden"})
            except Exception as exc:  # noqa: BLE001 - ghi loi luong
                errors.append(exc)

        threads = [threading.Thread(target=_append, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        lines = self._dead_lines()
        self.assertEqual(len(lines), 50)
        self.assertEqual(len({entry["line_no"] for entry in lines}), 50)

    def test_dead_letter_concurrent_appends_trims_to_limit(self):
        # Audit FIXL §4 #2: test 7 chi append 50 dong nen
        # _trim_dead_letter_locked chua bao gio chay — duong dua
        # append <-> cat (G4) can test ep trim that su: ghi truoc
        # >1000 dong (gom dong cu >30 ngay), roi cho 2 luong
        # append dong thoi; assert khong mat dong moi va tong
        # so dong cuoi <=1000, dong cu da cat.
        now = center_outbox.now_epoch()
        fresh_iso = center_outbox.epoch_to_iso(now)
        old_iso = center_outbox.epoch_to_iso(now - 31 * 24 * 3600)
        path = self.muse_root / "center_outbox_dead.jsonl"
        entries = []
        for seq in range(1000):
            entries.append({
                "line_no": seq, "task_id": "t-old",
                "to_status": "dang-lam", "kind": "status",
                "http_status": 403,
                "reason": "transition_forbidden",
                "hub_error": "forbidden", "hub_body": None,
                "dropped_at": fresh_iso, "account_id": "muse-phu"})
        for seq in range(5):
            entries.append({
                "line_no": 1000 + seq, "task_id": "t-old",
                "to_status": "dang-lam", "kind": "status",
                "http_status": 403,
                "reason": "transition_forbidden",
                "hub_error": "forbidden", "hub_body": None,
                "dropped_at": old_iso, "account_id": "muse-phu"})
        path.write_text("".join(
            json.dumps(entry, ensure_ascii=False) + "\n"
            for entry in entries), encoding="utf-8")
        self.assertEqual(len(self._dead_lines()), 1005)
        errors = []

        def _append(index):
            try:
                for seq in range(25):
                    center_outbox.write_dead_letter(
                        self.muse_root, "muse-phu", 2000 + index * 100 + seq,
                        {"kind": "status",
                         "task_id": "t-abcdef000001",
                         "to_status": "dang-lam"},
                        403, "transition_forbidden",
                        hub_error="forbidden",
                        hub_body={"error": "forbidden",
                                  "code": "transition_forbidden"})
            except Exception as exc:  # noqa: BLE001 - ghi loi luong
                errors.append(exc)

        threads = [threading.Thread(target=_append, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        lines = self._dead_lines()
        # Cat: khong vuot 1000 dong; dong cu >30 ngay da cat.
        self.assertEqual(len(lines), 1000)
        self.assertFalse(any(
            entry["dropped_at"] == old_iso for entry in lines))
        # Khong mat dong append moi nao trong luc cat.
        new_line_nos = {2000 + index * 100 + seq
                        for index in range(2) for seq in range(25)}
        self.assertTrue(new_line_nos <=
                        {entry["line_no"] for entry in lines})


class TestCenterAuthIsolation(CenterMuseFixture):
    """Tiêu chí T2 (4): 401 secret trung tâm chỉ dừng kéo trung tâm."""

    def test_center_401_stops_only_center_bot_fetcher_still_runs(self):
        # Bot fetcher: bot-a có queue hợp lệ trên cùng gateway.
        _write_json(self.muse_root / "fetcher-config" / "bot-a.json", {
            "endpoint": self.gw_base, "queue_secret": QUEUE_BOT_A,
            "via_proxy": False,
        })
        # Center fetcher dùng secret sai cho muse-phu.
        self._write_center_config("muse-phu", FETCH_WRONG, PUSH_PHU)
        result = self._run_fetch()
        stats = result["accounts"]["muse-phu"]
        self.assertEqual(stats["error"], "auth_401")
        self.assertEqual(stats["cursor_after"], 0)
        self.assertFalse(
            (self.muse_root / "center_cursors" / "muse-phu.json").exists())
        # Fetcher bot vẫn chạy bình thường trên cùng gateway.
        bot_result = fetcher.run_cycle(
            self.muse_root, log_fn=self.logs.append, alert_fn=self._alert)
        self.assertFalse(bot_result["skipped"])
        self.assertIn("bot-a", bot_result["bots"])
        self.assertIsNone(bot_result["bots"]["bot-a"]["error"])
        self.assertEqual(bot_result["bots"]["bot-a"]["events"], 0)
        # Đổi lại secret đúng: kéo trung tâm chạy lại ngay.
        self._write_center_config("muse-phu", FETCH_PHU, PUSH_PHU)
        self.create_task()
        result = self._run_fetch()
        self.assertEqual(result["accounts"]["muse-phu"]["events"], 2)
        self.assertEqual(result["accounts"]["muse-phu"]["cursor_after"], 2)


class TestCenterNotParticipating(CenterMuseFixture):
    """SPEC TT9 CHẶN-1 (đợt code bổ sung nhỏ v0.2.4, đội C):
    403 not_participating chỉ dừng kéo, im lặng.

    Mock ``fetcher._gateway_get`` vì kiểm tra cờ thuộc gateway phía
    PC (đội khác) — ở đây chỉ kiểm chứng hành vi fetcher phía Muse
    khi nhận đúng status + body đó.
    """

    def _mock_gateway_get(self, status, body):
        return mock.patch.object(
            fetcher, "_gateway_get", return_value=(status, body, None))

    def _auth_state(self):
        return center_fetcher._load_auth_state(self.muse_root, "muse-phu")

    def test_403_not_participating_stops_quietly_no_counter(self):
        with self._mock_gateway_get(
                403, b'{"error": "not_participating"}'):
            for _ in range(3):
                result = self._run_fetch()
                stats = result["accounts"]["muse-phu"]
                self.assertEqual(stats["error"], "not_participating")
                self.assertEqual(stats["cursor_after"], 0)
        # Không báo động nào sau 3 chu kỳ, bộ đếm 401 không tăng.
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._auth_state()["consec_auth_failures"], 0)
        self.assertFalse(self._auth_state()["alerted"])
        # Không tiến cursor.
        self.assertFalse(
            (self.muse_root / "center_cursors" / "muse-phu.json").exists())

    def test_401_three_cycles_still_alerts_once(self):
        with self._mock_gateway_get(401, b'{"error": "unauthorized"}'):
            for _ in range(3):
                result = self._run_fetch()
                self.assertEqual(
                    result["accounts"]["muse-phu"]["error"], "auth_401")
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0],
                         "center_fetcher_auth_failure")
        self.assertEqual(self._auth_state()["consec_auth_failures"], 3)

    def test_403_other_still_counts_like_before(self):
        with self._mock_gateway_get(403, b'{"error": "forbidden"}'):
            for _ in range(3):
                result = self._run_fetch()
                self.assertEqual(
                    result["accounts"]["muse-phu"]["error"], "auth_403")
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0],
                         "center_fetcher_auth_failure")
        self.assertEqual(self._auth_state()["consec_auth_failures"], 3)

    def test_403_without_json_body_still_counts(self):
        # 403 không có body JSON (không đọc được error) -> giữ hành
        # vi cũ, không được im lặng nhầm.
        with self._mock_gateway_get(403, b""):
            result = self._run_fetch()
            self.assertEqual(
                result["accounts"]["muse-phu"]["error"], "auth_403")
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._auth_state()["consec_auth_failures"], 1)


class TestCenterOverdue(CenterMuseFixture):
    def test_overdue_once_per_task_per_day(self):
        self._write_local_task("t-abcdef000001", "muse-phu",
                               due_at=DUE_PAST)
        self._write_local_task("t-abcdef000002", "muse-phu",
                               due_at=DUE_FUTURE)
        self._write_local_task("t-abcdef000003", "muse-phu",
                               due_at=DUE_PAST, status="da-xac-nhan")
        notified = []
        now = 1795057200.0  # 2026-11-19 (UTC) — ngày Saigon 2026-11-19
        summary = center_overdue.scan(
            self.muse_root, account_id="muse-phu", now=now,
            notify_fn=notified.append,
            log_fn=self.logs.append, alert_fn=self._alert)
        # Chỉ việc quá hạn + chưa xong được nhắc (1 việc).
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["overdue"], 1)
        self.assertEqual(summary["notified"], 1)
        self.assertEqual(notified[0]["task_id"], "t-abcdef000001")
        # Quét lại cùng ngày: không nhắc lặp.
        summary2 = center_overdue.scan(
            self.muse_root, account_id="muse-phu", now=now + 3600,
            notify_fn=notified.append,
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(summary2["notified"], 0)
        self.assertEqual(summary2["skipped_repeat"], 1)
        # Sang ngày sau (Asia/Saigon): nhắc lại 1 lần.
        summary3 = center_overdue.scan(
            self.muse_root, account_id="muse-phu", now=now + 86400,
            notify_fn=notified.append,
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(summary3["notified"], 1)
        self.assertEqual(len(notified), 2)

    def test_overdue_tolerance_15_minutes(self):
        # Quá hạn 10 phút (trong dung sai): chưa nhắc.
        due = "2026-11-19T09:50:00+07:00"
        self._write_local_task("t-abcdef000001", "muse-phu", due_at=due)
        notified = []
        # now = 2026-11-19 10:00 +07 = due + 10 phút.
        now = 1795057200.0
        summary = center_overdue.scan(
            self.muse_root, account_id="muse-phu", now=now,
            notify_fn=notified.append,
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(summary["overdue"], 0)
        self.assertEqual(summary["notified"], 0)


class TestCenterLedgerReconcile(CenterMuseFixture):
    def test_reconcile_matches_then_mismatch_alerts_no_autofix(self):
        self._write_local_task("t-abcdef000001", "muse-phu", status="moi")
        result = center_ledger.reconcile(
            self.muse_root, "muse-phu",
            {"t-abcdef000001": "moi"}, now=1795057200.0,
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["missing_local"], [])
        self.assertEqual(result["status_mismatch"], [])
        # Gọi lại trong giờ: bỏ qua (throttle 1 giờ).
        result2 = center_ledger.reconcile(
            self.muse_root, "muse-phu",
            {"t-abcdef000001": "moi"}, now=1795057200.0 + 600,
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertTrue(result2["skipped"])
        # Hub có việc local thiếu + trạng thái lệch: báo, không sửa.
        result3 = center_ledger.reconcile(
            self.muse_root, "muse-phu",
            {"t-abcdef000001": "dang-lam", "t-abcdef000002": "moi"},
            now=1795057200.0 + 3700, force=True,
            log_fn=self.logs.append, alert_fn=self._alert)
        self.assertEqual(result3["missing_local"], ["t-abcdef000002"])
        self.assertEqual(result3["status_mismatch"], ["t-abcdef000001"])
        kinds = [a[0] for a in self.alerts]
        self.assertIn("center_reconcile_mismatch", kinds)
        # Không tự sửa: bản local giữ nguyên.
        local = json.loads(
            (self.muse_root / "center_tasks" / "t-abcdef000001.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(local["record"]["status"], "moi")


class TestCenterGuards(unittest.TestCase):
    """Rào chắn: không ghi mốc đã-thấy phía Muse, không nạp prompt bot,
    không secret/nội dung trong log."""

    def test_no_seen_marker_writes_in_new_center_code(self):
        muse_side = Path(__file__).resolve().parents[2] / "muse-side"
        # Chuỗi "seen_at" không xuất hiện trong code mới phía Muse —
        # mốc đã-thấy chỉ hub ghi (C-2); code Muse không đọc/ghi nó.
        for name in ("center_fetcher.py", "center_dispatcher.py",
                     "center_ledger.py", "center_outbox.py",
                     "center_claims.py", "center_overdue.py"):
            src = (muse_side / name).read_text(encoding="utf-8")
            self.assertNotIn("seen_at", src, name)

    def test_center_dispatcher_loads_no_bot_prompt_or_memory(self):
        muse_side = Path(__file__).resolve().parents[2] / "muse-side"
        src = (muse_side / "center_dispatcher.py").read_text(
            encoding="utf-8")
        for forbidden in ("prompts/", "memory_store", "load_prompts",
                          "load_memory", "cards/"):
            self.assertNotIn(forbidden, src)

    def test_no_real_or_fake_secret_in_fetcher_logs(self):
        tmp = tempfile.TemporaryDirectory(prefix="center-guard-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _write_json(root / "center-config" / "muse-phu.json", {
            "endpoint": "http://127.0.0.1:9",
            "receiver_endpoint": "http://127.0.0.1:9",
            "fetch_secret": FETCH_WRONG,
            "push_secret": PUSH_PHU,
            "via_proxy": False,
        })
        logs = []
        center_fetcher.run_cycle(root, log_fn=logs.append,
                                 alert_fn=lambda *a: None)
        joined = "\n".join(logs)
        self.assertNotIn(FETCH_WRONG, joined)
        self.assertNotIn(PUSH_PHU, joined)
        self.assertNotIn("Noi dung viec", joined)


if __name__ == "__main__":
    unittest.main()
