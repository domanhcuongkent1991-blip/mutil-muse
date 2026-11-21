"""Vòng lặp poll Zalo của 1 tai (``02`` §1 + §5, D10–D13).

Máy trạng thái trong tiến trình (không bao giờ tự thoát vì lỗi
mạng/API — bài học daemon v2):

- ``starting``: kiểm tra lần đầu bằng getMe; lỗi thì retry backoff;
  3 lần 401 liên tiếp thì sang ``wait_token``.
- ``polling``: long-poll getUpdates, parse -> chống lặp -> ghi queue
  bền -> cập nhật seen. 408 reset lỗi liên tiếp. 3 lần 401 liên tiếp
  thì sang ``wait_token``.
- ``wait_token`` (SPEC F5, D11): dừng poll, thăm dò lại mỗi 60s
  (SUY LUẬN) bằng cách đọc lại file token + getMe; token đúng thì tự
  tiếp tục, CẤM restart lặp.

Số liệu giữ nguyên baseline (R1): getUpdates timeout 50s, HTTP
timeout 65s (50 + 15 đệm), backoff ``min(5s×n, 60s)``, heartbeat log
mỗi 3 phút. Jitter riêng từng bot (D10): 0–2s (SUY LUẬN — biên độ
jitter chưa chốt trong thiết kế), gieo từ bot_id nên ổn định và khác
nhau giữa các bot.

Ảnh chụp năng lực (D13, ``02`` §5): getMe lúc khởi động + mỗi 24h
(SUY LUẬN); lưu NGUYÊN response vào ``capability.json`` + trích
trường. Mất quyền (``can_join_groups`` false) thì ghi cờ hạ cấp
chỉ-DM để dispatcher thi hành, CẤM crash.

Khởi động ghi cảnh báo "chưa kiểm chứng trạng thái webhook" (D12);
không tự ý thay đổi cấu hình webhook.

Poller nhận ``poll_fn``/``get_me_fn`` tiêm vào để test không cần
mạng; hàm thật dùng :mod:`shared.zalo_client` qua
:func:`make_poll_fn`/:func:`make_get_me_fn`.

KHÔNG log nội dung tin nhắn ở bất cứ đâu (D4): log chỉ có seq,
event_id, chat_id rút gọn (<=8 ký tự), mã/phân loại lỗi.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import stat
import time
from pathlib import Path

from shared.errors import ErrorClass
from shared.record import now_iso
from shared.zalo_client import call_api
from shared.zalo_errors import classify_response
from pc.ear.parse_zalo import iter_events, parse_event

# Giá trị baseline, giữ nguyên (R1).
POLL_TIMEOUT_DEFAULT = 50
HTTP_TIMEOUT_MARGIN = 15  # HTTP timeout = poll timeout + 15s đệm.
HEARTBEAT_INTERVAL_S = 180
BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 60

# SUY LUẬN — đo/chốt ở GĐ1/GĐ2 (thiết kế ghi rõ SUY LUẬN).
WAIT_TOKEN_THRESHOLD = 3  # số lần 401 liên tiếp thì vào WAIT_TOKEN.
WAIT_TOKEN_PROBE_S = 60  # thăm dò lại mỗi 60s khi chờ token.
CAPABILITY_INTERVAL_S = 86400  # chụp lại năng lực mỗi 24h.
CAPABILITY_RETRY_S = 60  # chụp lại lỗi thì thử lại sau 60s.
JITTER_MAX_S = 2.0  # jitter riêng từng bot: 0..2s.
GET_ME_TIMEOUT_S = 30

WEBHOOK_WARNING = (
    "CẢNH BÁO: chưa kiểm chứng trạng thái webhook — kiểm tra bằng tay "
    "trong console"
)

CHAT_LOG_LEN = 8


def load_token(token_path):
    """Đọc token từ file tại lúc cần (file 600, 1 dòng).

    Đọc lại mỗi lần poll/thăm dò để nhận token mới mà không cần
    restart. Không bao giờ log giá trị token.
    """
    with open(token_path, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise ValueError("file token rỗng: %s" % (token_path,))
    return token


def check_token_file_permissions(token_path, log):
    """Cảnh báo (không chặn) nếu file token để quyền rộng hơn 600."""
    try:
        mode = stat.S_IMODE(os.stat(token_path).st_mode)
    except OSError:
        return
    if mode & 0o077:
        log(
            "cảnh báo: file token %s đang có quyền %o, nên đặt 600"
            % (token_path, mode)
        )


def make_poll_fn(base_url):
    """Hàm poll thật dùng transport chung (không token thật ở đây)."""

    def poll_fn(token, poll_timeout):
        return call_api(
            base_url,
            token,
            "getUpdates",
            {"timeout": poll_timeout},
            timeout=poll_timeout + HTTP_TIMEOUT_MARGIN,
        )

    return poll_fn


def make_get_me_fn(base_url):
    """Hàm kiểm tra token/năng lực thật dùng transport chung."""

    def get_me_fn(token):
        return call_api(base_url, token, "getMe", {}, timeout=GET_ME_TIMEOUT_S)

    return get_me_fn


def _jitter_rng(bot_id):
    seed = int.from_bytes(hashlib.sha256(bot_id.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed)


class Poller:
    """Máy trạng thái poll của 1 bot. ``run_once`` = 1 bước, ``run`` = lặp."""

    def __init__(self, *, bot_id, token_path, queue_store, seen_store,
                 state_writer, data_dir, poll_fn, get_me_fn,
                 poll_timeout=POLL_TIMEOUT_DEFAULT, log=None,
                 monotonic=None, capability_path=None, dm_only_path=None):
        self.bot_id = bot_id
        self.token_path = Path(token_path)
        self.queue_store = queue_store
        self.seen_store = seen_store
        self.state_writer = state_writer
        self.data_dir = Path(data_dir)
        self.capability_path = (
            Path(capability_path)
            if capability_path is not None
            else self.data_dir / "capability.json"
        )
        self.dm_only_path = (
            Path(dm_only_path)
            if dm_only_path is not None
            else self.data_dir / "dm_only.json"
        )
        self.poll_fn = poll_fn
        self.get_me_fn = get_me_fn
        self.poll_timeout = poll_timeout
        self._log = log or (lambda message: None)
        self._monotonic = monotonic or time.monotonic
        self._rng = _jitter_rng(bot_id)
        self.mode = "starting"
        self.consec_err = 0
        self.consec_401 = 0
        self._webhook_warning_logged = False
        self._last_heartbeat = self._monotonic()
        self._last_capability = None
        self._next_capability_attempt = 0.0

    # -- tiện ích ----------------------------------------------------

    def _backoff(self, n):
        base = min(BACKOFF_BASE_S * n, BACKOFF_CAP_S)
        return base + self._rng.uniform(0, JITTER_MAX_S)

    def _maybe_heartbeat(self):
        now = self._monotonic()
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL_S:
            self._last_heartbeat = now
            self._log(
                "heartbeat: mode=%s consec_err=%d latest_seq=%d"
                % (self.mode, self.consec_err, self.queue_store.latest_seq)
            )

    def _write_state(self, **fields):
        fields.setdefault("queue_latest_seq", self.queue_store.latest_seq)
        fields.setdefault("queue_floor_seq", self.queue_store.floor_seq)
        self.state_writer.update(**fields)

    def _short_chat(self, chat_id):
        return str(chat_id or "")[:CHAT_LOG_LEN]

    # -- ảnh chụp năng lực -------------------------------------------

    def _save_capability(self, response):
        body = response.get("body")
        source = body
        if isinstance(body, dict) and isinstance(body.get("result"), dict):
            source = body["result"]
        extracted = {}
        if isinstance(source, dict):
            for key in ("id", "account_name", "account_type", "can_join_groups"):
                extracted[key] = source.get(key)
        else:
            extracted = {
                "id": None,
                "account_name": None,
                "account_type": None,
                "can_join_groups": None,
            }
        snapshot = {
            "fetched_at": now_iso(),
            "response": body,
            "id": extracted["id"],
            "account_name": extracted["account_name"],
            "account_type": extracted["account_type"],
            "can_join_groups": extracted["can_join_groups"],
        }
        tmp_path = self.capability_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.capability_path)
        self._last_capability = self._monotonic()
        self._evaluate_dm_only(extracted["can_join_groups"])

    def _evaluate_dm_only(self, can_join_groups):
        """Mất quyền vào nhóm -> ghi cờ hạ cấp chỉ-DM, cấm crash.

        Diễn giải (SUY LUẬN): chỉ khi getMe nói rõ
        ``can_join_groups`` là false mới hạ cấp; true thì gỡ cờ; thiếu
        trường thì giữ nguyên (không đoán).
        """
        if can_join_groups is False:
            flag = {
                "dm_only": True,
                "reason": "can_join_groups=false (getMe)",
                "updated_at": now_iso(),
            }
            tmp_path = self.dm_only_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(flag, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.dm_only_path)
            self._log("hạ cấp chỉ-DM: can_join_groups=false (dispatcher thi hành)")
        elif can_join_groups is True and self.dm_only_path.exists():
            try:
                self.dm_only_path.unlink()
            except OSError:
                pass
            self._log("gỡ cờ chỉ-DM: can_join_groups=true trở lại")

    # -- một bước của máy trạng thái ---------------------------------

    def run_once(self):
        """Chạy 1 bước; trả về số giây nên chờ trước bước tiếp theo."""
        if not self._webhook_warning_logged:
            self._webhook_warning_logged = True
            self._log(WEBHOOK_WARNING)
        self._maybe_heartbeat()
        if self.mode == "starting":
            return self._starting_step()
        if self.mode == "wait_token":
            return self._wait_token_step()
        return self._poll_step()

    def run(self, stop_event):
        """Lặp cho tới khi stop_event được đặt. Không tự thoát vì lỗi."""
        while not stop_event.is_set():
            try:
                delay = self.run_once()
            except Exception as exc:  # noqa: BLE001 - tai không được chết
                self._log("poller: lỗi không lường trước (%s), backoff"
                          % type(exc).__name__)
                delay = self._backoff(1)
            stop_event.wait(max(0.0, delay))

    def _read_token_or_error(self):
        """Đọc token; lỗi thì ghi state + trả (None, delay backoff)."""
        try:
            return load_token(self.token_path), None
        except (OSError, ValueError) as exc:
            self.consec_err += 1
            self._log("không đọc được file token (%s)" % type(exc).__name__)
            self._write_state(
                last_poll_ok=False,
                consec_err=self.consec_err,
                last_error_class=ErrorClass.UNKNOWN.value,
                last_error_at=now_iso(),
            )
            return None, self._backoff(self.consec_err)

    def _starting_step(self):
        token, delay = self._read_token_or_error()
        if token is None:
            return delay
        try:
            response = self.get_me_fn(token)
        except Exception as exc:  # noqa: BLE001 - lỗi gọi hàm tiêm vào
            response = {
                "http_status": None,
                "body": None,
                "error_code": None,
                "transport_error": type(exc).__name__,
            }
        result = classify_response(response, direction="poll")
        if result.is_success:
            self._save_capability(response)
            self.consec_err = 0
            self.consec_401 = 0
            self.mode = "polling"
            self._log("khởi động xong: token dùng được, bắt đầu poll")
            self._write_state(
                consec_err=0,
                last_error_class=None,
                last_error_at=None,
                token_state="ok",
            )
            return 0.0
        if result.error_class == ErrorClass.TOKEN_INVALID:
            self.consec_401 += 1
            self.consec_err += 1
            self._log("khởi động: token không hợp lệ (lần %d)" % self.consec_401)
            if self.consec_401 >= WAIT_TOKEN_THRESHOLD:
                self._enter_wait_token()
                return float(WAIT_TOKEN_PROBE_S)
            self._write_state(
                last_poll_ok=False,
                consec_err=self.consec_err,
                last_error_class=ErrorClass.TOKEN_INVALID.value,
                last_error_at=now_iso(),
                token_state="starting",
            )
            return self._backoff(self.consec_err)
        # Lỗi khác (kể cả 408 lạ từ getMe): ở lại starting, retry.
        self.consec_401 = 0
        self.consec_err += 1
        error_class = result.error_class or ErrorClass.UNKNOWN
        self._log("khởi động: getMe lỗi (%s)" % error_class.value)
        self._write_state(
            last_poll_ok=False,
            consec_err=self.consec_err,
            last_error_class=error_class.value,
            last_error_at=now_iso(),
            token_state="starting",
        )
        return self._backoff(self.consec_err)

    def _enter_wait_token(self):
        self.mode = "wait_token"
        self._log(
            "WAIT_TOKEN: %d lần 401 liên tiếp — dừng poll, thăm dò lại "
            "mỗi %ds, chờ token mới (không restart)"
            % (WAIT_TOKEN_THRESHOLD, WAIT_TOKEN_PROBE_S)
        )
        self._write_state(
            last_poll_ok=False,
            consec_err=self.consec_err,
            last_error_class=ErrorClass.TOKEN_INVALID.value,
            last_error_at=now_iso(),
            token_state="WAIT_TOKEN",
        )

    def _wait_token_step(self):
        try:
            token = load_token(self.token_path)
        except (OSError, ValueError) as exc:
            self._log("WAIT_TOKEN: chưa đọc được file token (%s)"
                      % type(exc).__name__)
            self._write_state(token_state="WAIT_TOKEN")
            return float(WAIT_TOKEN_PROBE_S)
        try:
            response = self.get_me_fn(token)
        except Exception as exc:  # noqa: BLE001 - lỗi gọi hàm tiêm vào
            response = {
                "http_status": None,
                "body": None,
                "error_code": None,
                "transport_error": type(exc).__name__,
            }
        result = classify_response(response, direction="poll")
        if result.is_success:
            self._save_capability(response)
            self.consec_err = 0
            self.consec_401 = 0
            self.mode = "polling"
            self._log("WAIT_TOKEN: token đã đúng, tiếp tục poll")
            self._write_state(
                consec_err=0,
                last_error_class=None,
                last_error_at=None,
                token_state="ok",
            )
            return 0.0
        error_class = result.error_class or ErrorClass.UNKNOWN
        self._log("WAIT_TOKEN: thăm dò lại vẫn lỗi (%s)" % error_class.value)
        self._write_state(
            last_error_class=error_class.value,
            last_error_at=now_iso(),
            token_state="WAIT_TOKEN",
        )
        return float(WAIT_TOKEN_PROBE_S)

    def _maybe_refresh_capability(self, token):
        """Chụp lại năng lực khi đủ 24h; lỗi thì thử lại sau 60s."""
        now = self._monotonic()
        if self._last_capability is None:
            return
        if now - self._last_capability < CAPABILITY_INTERVAL_S:
            return
        if now < self._next_capability_attempt:
            return
        try:
            response = self.get_me_fn(token)
        except Exception as exc:  # noqa: BLE001 - lỗi gọi hàm tiêm vào
            response = {
                "http_status": None,
                "body": None,
                "error_code": None,
                "transport_error": type(exc).__name__,
            }
        result = classify_response(response, direction="poll")
        if result.is_success:
            self._save_capability(response)
            self._log("capability: đã chụp lại sau 24h")
        else:
            error_class = result.error_class or ErrorClass.UNKNOWN
            self._log("capability: chụp lại lỗi (%s), thử lại sau %ds"
                      % (error_class.value, CAPABILITY_RETRY_S))
            self._next_capability_attempt = now + CAPABILITY_RETRY_S

    def _poll_step(self):
        token, delay = self._read_token_or_error()
        if token is None:
            return delay
        self._maybe_refresh_capability(token)
        try:
            response = self.poll_fn(token, self.poll_timeout)
        except Exception as exc:  # noqa: BLE001 - lỗi gọi hàm tiêm vào
            response = {
                "http_status": None,
                "body": None,
                "error_code": None,
                "transport_error": type(exc).__name__,
            }
        result = classify_response(response, direction="poll")
        if result.is_success:
            self.consec_err = 0
            self.consec_401 = 0
            self._process_events(response)
            self.queue_store.prune()
            self._write_state(
                last_poll_ok=True,
                last_poll_at=now_iso(),
                consec_err=0,
                last_error_class=None,
                last_error_at=None,
                token_state="ok",
            )
            return 0.0
        if result.is_no_new_messages:
            # 408: bình thường, reset lỗi liên tiếp (R1).
            self.consec_err = 0
            self.consec_401 = 0
            self._write_state(
                last_poll_ok=True,
                last_poll_at=now_iso(),
                consec_err=0,
                last_error_class=None,
                last_error_at=None,
                token_state="ok",
            )
            return 0.0
        error_class = result.error_class or ErrorClass.UNKNOWN
        self.consec_err += 1
        if error_class == ErrorClass.TOKEN_INVALID:
            self.consec_401 += 1
        else:
            self.consec_401 = 0
        self._log("poll lỗi (%s), lần liên tiếp %d"
                  % (error_class.value, self.consec_err))
        if error_class == ErrorClass.TOKEN_INVALID and \
                self.consec_401 >= WAIT_TOKEN_THRESHOLD:
            self._enter_wait_token()
            return float(WAIT_TOKEN_PROBE_S)
        self._write_state(
            last_poll_ok=False,
            last_poll_at=now_iso(),
            consec_err=self.consec_err,
            last_error_class=error_class.value,
            last_error_at=now_iso(),
            token_state="ok",
        )
        return self._backoff(self.consec_err)

    def _process_events(self, response):
        body = response.get("body")
        if not isinstance(body, dict):
            self._log("poll ok nhưng body không phải object, bỏ qua đợt")
            return 0
        events = iter_events(body.get("result"))
        queued = 0
        duplicates = 0
        for event in events:
            record = parse_event(self.bot_id, event)
            platform_event_id = record["platform_event_id"]
            if platform_event_id in self.seen_store:
                duplicates += 1
                continue
            # Thứ tự bắt buộc: ghi queue bền TRƯỚC, cập nhật seen SAU.
            seq = self.queue_store.append(
                record, record["event_id"],
                received_at=record["received_at"],
            )
            self.seen_store.add(platform_event_id)
            queued += 1
            self._log("queued seq=%d event_id=%s chat=%s"
                      % (seq, record["event_id"],
                         self._short_chat(record["chat_id"])))
        if duplicates:
            self._log("bỏ %d event trùng (đã thấy)" % duplicates)
        return queued
