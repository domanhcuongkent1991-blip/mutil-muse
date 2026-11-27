#!/usr/bin/env python3
"""Test HTTP trung tam (chặng T1): gateway GET + receiver POST.

unittest, stdlib-only, HTTP that tren localhost. Moi gia tri secret
trong test deu la mau gia dang ro rang ("TEST-FAKE-..."), khong
secret that, khong noi dung tin that.
"""

import ipaddress
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
import tempfile

from pc.gateway import gateway_main as gateway
from pc.sync_receiver import receiver_main as receiver

FETCH_CHINH = "TEST-FAKE-CENTER-FETCH-CHINH"
PUSH_CHINH = "TEST-FAKE-CENTER-PUSH-CHINH"
FETCH_PHU = "TEST-FAKE-CENTER-FETCH-PHU"
PUSH_PHU = "TEST-FAKE-CENTER-PUSH-PHU"
DUE = "2026-11-25T10:00:00+07:00"


def _build_root(tmp, participating_phu=True, phu_secrets=True):
    root = Path(tmp)
    (root / "secrets").mkdir(parents=True)
    (root / "ui-config").mkdir(parents=True)
    (root / "data" / "bot-a").mkdir(parents=True)
    (root / "registry.json").write_text(json.dumps({
        "version": 1,
        "bots": {"bot-a": {"bot_id": "bot-a", "platform": "zalo"}}}))
    (root / "secrets" / "bot-a.sync_secret").write_text(
        "TEST-FAKE-SYNC-BOT-A\n")
    (root / "ui-config" / "accounts.json").write_text(json.dumps({
        "accounts": {
            "muse-chinh": {"display_name": "Muse chinh",
                           "center_participating": True},
            "muse-phu": {"display_name": "Muse phu",
                         "center_participating": participating_phu},
            "muse-ngoai": {"display_name": "Ngoai",
                           "center_participating": False},
        }}))
    (root / "secrets" / "center-muse-chinh.fetch_secret").write_text(
        FETCH_CHINH + "\n")
    (root / "secrets" / "center-muse-chinh.push_secret").write_text(
        PUSH_CHINH + "\n")
    if phu_secrets:
        (root / "secrets" / "center-muse-phu.fetch_secret").write_text(
            FETCH_PHU + "\n")
        (root / "secrets" / "center-muse-phu.push_secret").write_text(
            PUSH_PHU + "\n")
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


def _http(port, method, path, payload=None, secret=None, raw=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if raw is not None:
        body = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
    else:
        body = None
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["Authorization"] = "Bearer %s" % secret
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), \
                resp.headers
    except urllib.error.HTTPError as exc:
        data = exc.read().decode("utf-8")
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = {"raw": data}
        return exc.code, parsed, exc.headers


class CenterHttpFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = _build_root(self.tmp.name)
        self.networks = [ipaddress.ip_network("127.0.0.0/8"),
                         ipaddress.ip_network("::1/128")]
        self.gw_ctx = gateway.GatewayContext(
            self.root, self.networks, log_fn=lambda msg: None)
        self.gw_server, self.gw_port, self.gw_thread = _serve(
            lambda: gateway.make_server(self.gw_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.gw_server, self.gw_thread)
        self.rc_ctx = receiver.ReceiverContext(
            self.root, self.networks, log_fn=lambda msg: None, min_gap=0)
        self.rc_server, self.rc_port, self.rc_thread = _serve(
            lambda: receiver.make_server(self.rc_ctx, "127.0.0.1", 0))
        self.addCleanup(_stop, self.rc_server, self.rc_thread)

    # helpers ------------------------------------------------------------
    def gw_get(self, path, secret=FETCH_PHU):
        return _http(self.gw_port, "GET", path, secret=secret)

    def rc_post(self, path, payload=None, secret=PUSH_CHINH, raw=None):
        return _http(self.rc_port, "POST", path, payload=payload,
                     secret=secret, raw=raw)

    def create_task(self, client_key="ck-1", assigned_to="muse-phu",
                    secret=PUSH_CHINH, **overrides):
        payload = {"client_key": client_key, "title": "Viec thu",
                   "body": "Noi dung viec", "kind": "nghien-cuu",
                   "assigned_to": assigned_to, "due_at": DUE}
        payload.update(overrides)
        return self.rc_post("/v1/center/muse-chinh/tasks", payload,
                            secret=secret)


class TestGatewayCenter(CenterHttpFixture):
    def test_feed_auth_matrix(self):
        # Chua co viec: dung secret van 200; sai secret 401; tai
        # khoan khong ton tai 404; mau sai 400.
        status, body, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                      secret=FETCH_PHU)
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret="TEST-FAKE-WRONG")
        self.assertEqual(status, 401)
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret=None)
        self.assertEqual(status, 401)
        status, _, _ = self.gw_get("/v1/center/muse-khong-co/feed?since=0",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 404)
        status, body, _ = self.gw_get("/v1/center/BAD_ID/feed?since=0",
                                      secret=FETCH_PHU)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_account_id")

    def test_feed_returns_events_and_records_seen(self):
        status, body, _ = self.create_task()
        self.assertEqual(status, 200)
        task_id = body["task_id"]
        # Nguoi nhan keo feed -> seen_at duoc ghi.
        status, body, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                      secret=FETCH_PHU)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 2)
        self.assertEqual(body["events"][0]["kind"], "task")
        self.assertEqual(body["events"][1]["kind"], "message")
        self.assertEqual(body["latest_seq"], 2)
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/tasks/%s" % task_id, secret=FETCH_PHU)
        first_seen = body["task"]["seen_at"]
        self.assertIsNotNone(first_seen)
        # Keo lai: seen_at khong doi.
        self.gw_get("/v1/center/muse-phu/feed?since=0", secret=FETCH_PHU)
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/tasks/%s" % task_id, secret=FETCH_PHU)
        self.assertEqual(body["task"]["seen_at"], first_seen)

    def test_feed_by_non_assignee_does_not_record_seen(self):
        status, body, _ = self.create_task()
        task_id = body["task_id"]
        status, body, _ = self.gw_get("/v1/center/muse-chinh/feed?since=0",
                                      secret=FETCH_CHINH)
        self.assertEqual(status, 200)
        status, body, _ = self.gw_get(
            "/v1/center/muse-chinh/tasks/%s" % task_id, secret=FETCH_CHINH)
        self.assertIsNone(body["task"]["seen_at"])

    def test_get_task_single_does_not_record_seen(self):
        status, body, _ = self.create_task()
        task_id = body["task_id"]
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/tasks/%s" % task_id, secret=FETCH_PHU)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["title"], "Viec thu")
        self.assertIsNone(body["task"]["seen_at"])
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/tasks/t-000000000000", secret=FETCH_PHU)
        self.assertEqual(status, 404)
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/tasks/khong-dung", secret=FETCH_PHU)
        self.assertEqual(status, 400)

    def test_feed_stale_cursor(self):
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/feed?since=999", secret=FETCH_PHU)
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])
        self.assertTrue(body["stale_center_cursor"])

    def test_health_counts(self):
        self.create_task(client_key="ck-h1")
        self.create_task(client_key="ck-h2", due_at="2026-11-01T10:00:00+07:00")
        status, body, _ = self.gw_get("/v1/center/muse-phu/health",
                                      secret=FETCH_PHU)
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"], 2)
        self.assertEqual(body["open_tasks"], 2)
        self.assertEqual(body["overdue_tasks"], 1)
        self.assertEqual(body["latest_seq"], 4)
        self.assertEqual(body["overdue_severe_hours"], 24)
        self.assertIn("hub_uptime_seconds", body)


class TestReceiverCenterCreate(CenterHttpFixture):
    def test_create_200_with_two_consecutive_seqs(self):
        status, body, _ = self.create_task()
        self.assertEqual(status, 200)
        self.assertTrue(body["task_id"].startswith("t-"))
        self.assertFalse(body["duplicate"])
        from pc.center import center_store as store_mod
        store = store_mod.CenterStore(self.root)
        events = store.read_events()
        self.assertEqual([row["seq"] for row in events], [1, 2])
        self.assertEqual(events[0]["kind"], "task_created")
        self.assertEqual(events[1]["record"]["kind"], "giao-viec")

    def test_create_duplicate_client_key(self):
        _, first, _ = self.create_task()
        status, body, _ = self.create_task()
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])
        self.assertEqual(body["task_id"], first["task_id"])

    def test_create_same_client_key_different_content_409(self):
        self.create_task()
        status, body, _ = self.create_task(title="Tieu de khac")
        self.assertEqual(status, 409)

    def test_create_missing_due_at_400(self):
        status, body, _ = self.rc_post(
            "/v1/center/muse-chinh/tasks",
            {"client_key": "ck-m", "title": "T", "body": "B",
             "kind": "app", "assigned_to": "muse-phu"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "missing_due_at")

    def test_create_self_assign_400(self):
        status, body, _ = self.create_task(assigned_to="muse-chinh")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "self_assign_forbidden")

    def test_create_assigned_to_not_participating_400(self):
        status, body, _ = self.create_task(assigned_to="muse-ngoai")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "not_participating")

    def test_create_assigned_to_unknown_400(self):
        status, body, _ = self.create_task(assigned_to="muse-khong-co")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_assigned_to")

    def test_create_assigned_to_without_secrets_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(tmp, phu_secrets=False)
            ctx = receiver.ReceiverContext(
                root, self.networks, log_fn=lambda msg: None, min_gap=0)
            server, port, thread = _serve(
                lambda: receiver.make_server(ctx, "127.0.0.1", 0))
            try:
                status, body, _ = _http(
                    port, "POST", "/v1/center/muse-chinh/tasks",
                    payload={"client_key": "ck-s", "title": "T",
                             "body": "B", "kind": "app",
                             "assigned_to": "muse-phu", "due_at": DUE},
                    secret=PUSH_CHINH)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "no_center_secret")
            finally:
                _stop(server, thread)

    def test_create_wrong_secret_401_and_unknown_account_404(self):
        status, _, _ = self.create_task(secret="TEST-FAKE-WRONG")
        self.assertEqual(status, 401)
        status, _, _ = self.rc_post("/v1/center/muse-khong-co/tasks",
                                    {"client_key": "ck-z"}, secret=PUSH_CHINH)
        self.assertEqual(status, 404)


class TestReceiverCenterStatus(CenterHttpFixture):
    def _task_id(self):
        _, body, _ = self.create_task()
        return body["task_id"]

    def _status(self, task_id, to_status, note=None, client_key="sk-1",
                secret=PUSH_PHU):
        return self.rc_post(
            "/v1/center/muse-phu/tasks/%s/status" % task_id,
            {"to_status": to_status, "note": note, "client_key": client_key},
            secret=secret)

    def test_wrong_transition_400_writes_nothing(self):
        task_id = self._task_id()
        status, body, _ = self._status(task_id, "da-xac-nhan")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_transition")
        from pc.center import center_store as store_mod
        store = store_mod.CenterStore(self.root)
        self.assertEqual(store.get_task(task_id)["status"], "moi")
        self.assertEqual(len(store.read_events()), 2)

    def test_wrong_permission_403_writes_nothing(self):
        task_id = self._task_id()
        # Nguoi giao (muse-chinh, goi bang dung secret cua minh)
        # khong duoc tu danh dau da-nhan.
        status, body, _ = self.rc_post(
            "/v1/center/muse-chinh/tasks/%s/status" % task_id,
            {"to_status": "da-nhan", "note": None, "client_key": "p1"},
            secret=PUSH_CHINH)
        self.assertEqual(status, 403)

    def test_status_replay_duplicate_and_conflict(self):
        task_id = self._task_id()
        status, body, _ = self._status(task_id, "da-nhan")
        self.assertEqual((status, body["duplicate"]), (200, False))
        status, body, _ = self._status(task_id, "da-nhan")
        self.assertEqual((status, body["duplicate"]), (200, True))
        status, body, _ = self._status(task_id, "dang-lam")
        self.assertEqual(status, 409)

    def test_note_required_on_return(self):
        task_id = self._task_id()
        self._status(task_id, "da-nhan", client_key="a1")
        self._status(task_id, "dang-lam", client_key="a2")
        self._status(task_id, "da-bao-xong", client_key="a3")
        status, body, _ = self.rc_post(
            "/v1/center/muse-chinh/tasks/%s/status" % task_id,
            {"to_status": "dang-lam", "note": None, "client_key": "a4"},
            secret=PUSH_CHINH)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "note_required")
        status, body, _ = self.rc_post(
            "/v1/center/muse-chinh/tasks/%s/status" % task_id,
            {"to_status": "dang-lam", "note": "Chua dat",
             "client_key": "a4"},
            secret=PUSH_CHINH)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "dang-lam")

    def test_unknown_task_404(self):
        status, _, _ = self._status("t-000000000000", "da-nhan")
        self.assertEqual(status, 404)


class TestReceiverCenterMessages(CenterHttpFixture):
    def _task_id(self):
        _, body, _ = self.create_task()
        return body["task_id"]

    def _msg(self, payload, secret=PUSH_PHU):
        return self.rc_post("/v1/center/muse-phu/messages", payload,
                            secret=secret)

    def test_reject_giao_viec_post(self):
        status, body, _ = self._msg(
            {"msg_id": "m-aaaaaaaaaa10", "kind": "giao-viec",
             "to_account": None, "task_id": None, "body": "Tu giao"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "kind_not_allowed")

    def test_bao_cao_only_from_assignee(self):
        task_id = self._task_id()
        # muse-chinh (nguoi giao, khong phai assigned_to) gui bao-cao
        # gan viec -> 403; phai goi bang dung URL + secret cua minh.
        status, body, _ = self.rc_post(
            "/v1/center/muse-chinh/messages",
            {"msg_id": "m-aaaaaaaaaa11", "kind": "bao-cao",
             "to_account": None, "task_id": task_id, "body": "Xong"},
            secret=PUSH_CHINH)
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "not_assignee")
        status, body, _ = self._msg(
            {"msg_id": "m-aaaaaaaaaa11", "kind": "bao-cao",
             "to_account": None, "task_id": task_id, "body": "Xong"})
        self.assertEqual(status, 200)

    def test_message_dedupe_by_msg_id(self):
        payload = {"msg_id": "m-aaaaaaaaaa12", "kind": "hoi",
                   "to_account": None, "task_id": None,
                   "body": "Cho hoi chut"}
        status, body, _ = self._msg(payload)
        self.assertEqual((status, body["duplicate"]), (200, False))
        status, body, _ = self._msg(payload)
        self.assertEqual((status, body["duplicate"]), (200, True))
        from pc.center import center_store as store_mod
        store = store_mod.CenterStore(self.root)
        self.assertEqual(len(store.read_events()), 1)

    def test_bao_cao_first_completion_second_progress(self):
        # V3-GC-01 qua HTTP: tin thu 2 chi la bao tien do bo sung.
        task_id = self._task_id()
        self.rc_post("/v1/center/muse-phu/tasks/%s/status" % task_id,
                     {"to_status": "da-nhan", "note": None,
                      "client_key": "b1"}, secret=PUSH_PHU)
        self.rc_post("/v1/center/muse-phu/tasks/%s/status" % task_id,
                     {"to_status": "dang-lam", "note": None,
                      "client_key": "b2"}, secret=PUSH_PHU)
        self.rc_post("/v1/center/muse-phu/tasks/%s/status" % task_id,
                     {"to_status": "da-bao-xong", "note": None,
                      "client_key": "b3"}, secret=PUSH_PHU)
        _, first, _ = self._msg(
            {"msg_id": "m-aaaaaaaaaa13", "kind": "bao-cao",
             "to_account": None, "task_id": task_id, "body": "Xong roi"})
        self.assertEqual(first["report_role"], "completion")
        _, second, _ = self._msg(
            {"msg_id": "m-aaaaaaaaaa14", "kind": "bao-cao",
             "to_account": None, "task_id": task_id,
             "body": "Bo sung chi tiet"})
        self.assertEqual(second["report_role"], "progress")
        from pc.center import center_store as store_mod
        store = store_mod.CenterStore(self.root)
        self.assertEqual(store.get_task(task_id)["status"], "da-bao-xong")


class TestReceiverCenterR6(CenterHttpFixture):
    FAKE_PATTERNS = [
        ("center_secret",
         "center fetch secret = TEST-FAKE-CENTER-FETCH-0001"),
        ("queue_secret", "queue secret: TEST-FAKE-QUEUE-0001"),
        ("sync_secret", "sync secret = TEST-FAKE-SYNC-0001"),
        ("bot_token", "bot token = TEST-FAKE-BOT-TOKEN-0001"),
        ("ui_password", "mat khau UI = TEST-FAKE-UI-PASS-0001"),
    ]

    def test_blocked_in_title_body_note_message(self):
        from pc.center import center_store as store_mod
        for label, fake in self.FAKE_PATTERNS:
            # title
            status, body, _ = self.create_task(
                client_key="ck-r6-%s" % label, title=fake)
            self.assertEqual(status, 400, label)
            self.assertEqual(body["error"], "blocked_secret_pattern", label)
            # body
            status, body, _ = self.create_task(
                client_key="ck-r6b-%s" % label, body=fake)
            self.assertEqual(status, 400, label)
            # note qua POST /status
            _, created, _ = self.create_task(client_key="ck-ok-%s" % label)
            status, body, _ = self.rc_post(
                "/v1/center/muse-phu/tasks/%s/status" % created["task_id"],
                {"to_status": "da-nhan", "note": fake,
                 "client_key": "ck-n-%s" % label}, secret=PUSH_PHU)
            self.assertEqual(status, 400, label)
            self.assertEqual(body["error"], "blocked_secret_pattern", label)
            # body tin nhan
            status, body, _ = self.rc_post(
                "/v1/center/muse-phu/messages",
                {"msg_id": "m-aaaaaaaaab%s" % label[:2].replace("-", "0"),
                 "kind": "hoi", "to_account": None, "task_id": None,
                 "body": fake}, secret=PUSH_PHU)
            self.assertEqual(status, 400, label)
        store = store_mod.CenterStore(self.root)
        # Chi cac viec "ck-ok-*" duoc ghi (5 viec x 2 su kien); khong
        # dong nao chua mau gia dang bi ghi.
        self.assertEqual(len(store.read_events()), 10)


class TestReceiverCenterGuards(CenterHttpFixture):
    def test_ip_outside_allowlist_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(tmp)
            ctx = receiver.ReceiverContext(
                root, [ipaddress.ip_network("10.0.0.0/8")],
                log_fn=lambda msg: None, min_gap=0)
            server, port, thread = _serve(
                lambda: receiver.make_server(ctx, "127.0.0.1", 0))
            try:
                status, _, _ = _http(
                    port, "POST", "/v1/center/muse-chinh/tasks",
                    payload={"client_key": "ck-ip"}, secret=PUSH_CHINH)
                self.assertEqual(status, 403)
            finally:
                _stop(server, thread)

    def test_rate_limit_429_per_account(self):
        clock = [1000.0]
        with tempfile.TemporaryDirectory() as tmp:
            root = _build_root(tmp)
            ctx = receiver.ReceiverContext(
                root, self.networks, log_fn=lambda msg: None, min_gap=2.0,
                clock=lambda: clock[0])
            server, port, thread = _serve(
                lambda: receiver.make_server(ctx, "127.0.0.1", 0))
            try:
                def post(key):
                    return _http(
                        port, "POST", "/v1/center/muse-chinh/tasks",
                        payload={"client_key": key, "title": "T",
                                 "body": "B", "kind": "app",
                                 "assigned_to": "muse-phu", "due_at": DUE},
                        secret=PUSH_CHINH)
                status, _, _ = post("ck-rl-1")
                self.assertEqual(status, 200)
                status, _, headers = post("ck-rl-2")
                self.assertEqual(status, 429)
                self.assertEqual(headers.get("Retry-After"), "2")
                clock[0] += 2.5
                status, _, _ = post("ck-rl-2")
                self.assertEqual(status, 200)
            finally:
                _stop(server, thread)

    def test_bot_replies_endpoint_unaffected(self):
        entry = {"reply_key": "k1", "bot_id": "bot-a", "chat_id": "chat1",
                 "chat_title": None, "in_reply_to_event_key": "bot-a__ev1",
                 "sent_at": "2026-11-15T08:00:00+00:00", "status": "sent",
                 "message_id": "m1", "kind": "text",
                 "text_full": "tra loi day", "caption": None,
                 "retry_count": 0}
        status, body, _ = _http(
            self.rc_port, "POST", "/v1/bots/bot-a/replies",
            payload={"entries": [entry]},
            secret="TEST-FAKE-SYNC-BOT-A")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_gateway_has_no_post_and_store_writes_only_via_store(self):
        from pathlib import Path as _Path
        code_root = _Path(__file__).resolve().parents[2]
        gw_text = (code_root / "pc" / "gateway"
                   / "gateway_main.py").read_text(encoding="utf-8")
        self.assertNotIn("def do_POST", gw_text)
        self.assertIn("def do_GET", gw_text)
        for name in ("events.jsonl", "tasks_state.json", "seen_state.json",
                     "store.lock"):
            self.assertNotIn(name, gw_text, name)
        rc_text = (code_root / "pc" / "sync_receiver"
                   / "receiver_main.py").read_text(encoding="utf-8")
        for name in ("events.jsonl", "tasks_state.json", "seen_state.json",
                     "store.lock"):
            self.assertNotIn(name, rc_text, name)
        self.assertIn("center_store", rc_text)
        self.assertIn("center_store", gw_text)


class TestGatewayCenterNotParticipating(CenterHttpFixture):
    """Co tat (v0.2.4 CHAN-1): secret dung nhung co = false -> 403."""

    def setUp(self):
        super().setUp()
        (self.root / "ui-config" / "accounts.json").write_text(json.dumps({
            "accounts": {
                "muse-chinh": {"display_name": "Muse chinh",
                               "center_participating": True},
                "muse-phu": {"display_name": "Muse phu",
                             "center_participating": False},
            }}))

    def _fetch_state(self):
        path = self.root / "gateway-fetch-state.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def test_flag_off_feed_health_tasks_all_403(self):
        for path in ("/v1/center/muse-phu/feed?since=0",
                     "/v1/center/muse-phu/health",
                     "/v1/center/muse-phu/tasks/t-000000000000"):
            status, body, _ = self.gw_get(path, secret=FETCH_PHU)
            self.assertEqual(status, 403, path)
            self.assertEqual(body["error"], "not_participating", path)

    def test_flag_off_wrong_secret_still_401_and_unknown_404(self):
        # Thu tu bat buoc 404 -> 401 -> 403: sai secret van 401 du co tat.
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret="TEST-FAKE-WRONG")
        self.assertEqual(status, 401)
        status, _, _ = self.gw_get("/v1/center/muse-khong-co/feed?since=0",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 404)

    def test_flag_off_errors_write_nothing(self):
        self.gw_get("/v1/center/muse-phu/feed?since=0", secret=FETCH_PHU)
        self.gw_get("/v1/center/muse-phu/feed?since=0",
                    secret="TEST-FAKE-WRONG")
        self.gw_get("/v1/center/muse-khong-co/feed?since=0",
                    secret=FETCH_PHU)
        self.assertIsNone(self._fetch_state())


class TestGatewayCenterFetchState(CenterHttpFixture):
    """Muc center rieng trong gateway-fetch-state.json (v0.2.4 CHAN-2)."""

    def _fetch_state(self):
        return json.loads(
            (self.root / "gateway-fetch-state.json").read_text(
                encoding="utf-8"))

    def test_feed_records_center_entry_and_increments(self):
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 200)
        state = self._fetch_state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["bots"], {})
        self.assertNotIn("muse-phu", state["bots"])
        entry = state["center"]["muse-phu"]
        self.assertEqual(entry["fetch_count"], 1)
        first = entry["last_fetch_at"]
        self.assertIsNotNone(datetime.fromisoformat(first).tzinfo)
        mode = (self.root / "gateway-fetch-state.json").stat().st_mode
        self.assertEqual(mode & 0o777, 0o600)
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 200)
        entry = self._fetch_state()["center"]["muse-phu"]
        self.assertEqual(entry["fetch_count"], 2)
        self.assertGreaterEqual(entry["last_fetch_at"], first)

    def test_center_keys_are_per_account(self):
        self.gw_get("/v1/center/muse-chinh/feed?since=0",
                    secret=FETCH_CHINH)
        self.gw_get("/v1/center/muse-phu/feed?since=0", secret=FETCH_PHU)
        center = self._fetch_state()["center"]
        self.assertEqual(set(center), {"muse-chinh", "muse-phu"})
        self.assertEqual(center["muse-chinh"]["fetch_count"], 1)
        self.assertEqual(center["muse-phu"]["fetch_count"], 1)

    def test_health_and_task_do_not_record_center_fetch(self):
        self.gw_get("/v1/center/muse-phu/feed?since=0", secret=FETCH_PHU)
        before = self._fetch_state()["center"]["muse-phu"]
        _, created, _ = self.create_task()
        status, _, _ = self.gw_get("/v1/center/muse-phu/health",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 200)
        status, _, _ = self.gw_get(
            "/v1/center/muse-phu/tasks/%s" % created["task_id"],
            secret=FETCH_PHU)
        self.assertEqual(status, 200)
        after = self._fetch_state()["center"]["muse-phu"]
        self.assertEqual(after, before)

    def test_error_responses_do_not_record_center_fetch(self):
        self.gw_get("/v1/center/muse-phu/feed?since=0", secret=FETCH_PHU)
        before = self._fetch_state()["center"]["muse-phu"]
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret="TEST-FAKE-WRONG")
        self.assertEqual(status, 401)
        status, _, _ = self.gw_get("/v1/center/muse-khong-co/feed?since=0",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 404)
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=-1",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 400)
        after = self._fetch_state()["center"]["muse-phu"]
        self.assertEqual(after, before)

    def test_stale_cursor_feed_still_records(self):
        status, body, _ = self.gw_get(
            "/v1/center/muse-phu/feed?since=999", secret=FETCH_PHU)
        self.assertEqual(status, 200)
        self.assertTrue(body["stale_center_cursor"])
        self.assertEqual(
            self._fetch_state()["center"]["muse-phu"]["fetch_count"], 1)

    def test_old_file_without_center_still_reads_and_writes(self):
        (self.root / "gateway-fetch-state.json").write_text(json.dumps({
            "version": 1,
            "bots": {"bot-a": {
                "last_fetch_at": "2026-11-19T10:00:00+00:00",
                "fetch_count": 7, "served_seq": 3}}}))
        status, _, _ = self.gw_get("/v1/center/muse-phu/feed?since=0",
                                   secret=FETCH_PHU)
        self.assertEqual(status, 200)
        state = self._fetch_state()
        self.assertEqual(state["bots"]["bot-a"]["fetch_count"], 7)
        self.assertEqual(state["center"]["muse-phu"]["fetch_count"], 1)
        self.assertNotIn("muse-phu", state["bots"])


if __name__ == "__main__":
    unittest.main()
