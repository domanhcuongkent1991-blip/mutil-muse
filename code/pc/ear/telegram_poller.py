"""Vong lap poll Telegram cua 1 tai (``03`` §1 + §5, D4/D6).

May trang thai trong tien trinh, giong mau :mod:`pc.ear.poller`:

- ``starting``: kiem tra token bang getMe; kiem tra getWebhookInfo —
  webhook dang bat thi BAO RO va DUNG (ma thoat 2, CAM tu xoa
  webhook); 3 lan 401 lien tiep thi sang ``wait_token``.
- ``polling``: long-poll getUpdates theo offset ben, parse -> chong
  lap theo update_id -> tai file (neu co) -> ghi queue ben -> cap
  nhat seen + offset. 409 (xung dot poller/webhook khac) thi thoat
  ma 42 (SPEC F5), CAM retry lap.
- ``wait_token``: dung poll, tham do lai moi 60s bang cach doc lai
  file token + getMe; token dung thi tu tiep tuc, CAM restart lap.

Offset ben (``01`` §2, ``03`` §1): luu trong ``offset.txt`` (tmp +
rename), cap nhat SAU khi ghi queue thanh cong; ``offset`` = update_id
lon nhat da tieu thu + 1. File mat/hong thi khoi phuc: queue con dong
thi offset = update_id lon nhat trong queue + 1; queue rong thi reset
0 + chong lap + ghi dau hieu khoi phuc cho nguoi van hanh. CAM reset
mu ve 0 khi queue con du lieu.

Tap update_id da thay luu BEN trong ``seen_updates.txt`` (toi da 5000
id), cap nhat SAU khi ghi queue + offset.

So lieu giong baseline Zalo (R1): getUpdates timeout 50s, HTTP timeout
65s (50 + 15 dem), backoff ``min(5s×n, 60s)`` + jitter rieng tung bot
0–2s (gieo tu bot_id), heartbeat log moi 3 phut. 429 co
``parameters.retry_after`` thi cho DUNG so giay do (chan 3600s).

Anh chup nang luc (``03`` §6): getMe luc khoi dong + moi 24h (SUY
LUAN); luu NGUYEN response vao ``capability.json`` + trich truong, gom
``can_read_all_group_messages`` (``true`` = privacy da TAT). Username
lay tu capability de ap luat @suffix; chua biet username thi khong bo
qua lenh (mo an toan) + ghi canh bao.

File dinh kem (``03`` §3): tai NGAY khi nhan qua getFile + duong tai
file, luu vao ``data/<bot_id>/files/`` (quyen 600), ten file mang seq
de prune theo watermark. Qua 20MB thi giu tin + ``file_error``, khong
crash, khong bo tin. Prune: tuoi > 30 ngay (SUY LUAN, chot o GĐ2) VA
seq <= watermark "da phuc vu" cua gateway; chua co watermark thi giu
lai tat ca (than trong).

Poller nhan cac ham goi API tiem vao de test khong can mang; ham that
dung :mod:`shared.telegram_client` qua cac factory ``make_*_fn``.

KHONG log noi dung tin nhan o bat cu dau nao: log chi co seq,
event_id, chat_id rut gon (<=8 ky tu), ma/phan loai loi.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path

from shared import ids
from shared.errors import ErrorClass
from shared.record import now_iso
from shared.telegram_client import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    call_api,
    download_file,
)
from shared.telegram_errors import (
    MAX_RETRY_AFTER_SECONDS,
    classify_response,
)
from pc.ear.parse_telegram import parse_update
from pc.ear.poller import load_token

# Gia tri baseline, giong Zalo (R1).
POLL_TIMEOUT_DEFAULT = 50
HTTP_TIMEOUT_MARGIN = 15  # HTTP timeout = poll timeout + 15s dem.
HEARTBEAT_INTERVAL_S = 180
BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 60

# SUY LUAN — do/chot o GĐ2 (thiet ke ghi ro SUY LUAN).
WAIT_TOKEN_THRESHOLD = 3  # so lan 401 lien tiep thi vao WAIT_TOKEN.
WAIT_TOKEN_PROBE_S = 60  # tham do lai moi 60s khi cho token.
CAPABILITY_INTERVAL_S = 86400  # chup lai nang luc moi 24h.
CAPABILITY_RETRY_S = 60  # chup lai loi thi thu lai sau 60s.
JITTER_MAX_S = 2.0  # jitter rieng tung bot: 0..2s.
GET_ME_TIMEOUT_S = 30
GET_FILE_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 60
FILE_PRUNE_AGE_DAYS = 30  # SUY LUAN — chot o GĐ2 (03 §3).

# Ma thoat (SPEC F5): 42 = xung dot poller/webhook, systemd khong
# restart lap (RestartPreventExitStatus=42 trong tai@.service).
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CONFLICT = 42

# Gioi han tai file (tai lieu chinh thuc).
MAX_FILE_BYTES = DEFAULT_MAX_DOWNLOAD_BYTES

CHAT_LOG_LEN = 8

# Ten file mac dinh khi media khong co file_name (quy uoc dia
# phuong cua vong code nay, khong phai hanh vi API).
_DEFAULT_FILE_NAMES = {
    "photo": "photo.jpg",
    "voice": "voice.ogg",
}

_OFFSET_RECOVERY_MARKER = "offset_recovered.json"


def make_poll_fn(base_url):
    """Ham poll that dung transport chung (khong token that o day)."""

    def poll_fn(token, poll_timeout, offset, allowed_updates):
        return call_api(
            base_url,
            token,
            "getUpdates",
            {
                "timeout": poll_timeout,
                "offset": offset,
                "allowed_updates": list(allowed_updates),
            },
            timeout=poll_timeout + HTTP_TIMEOUT_MARGIN,
        )

    return poll_fn


def make_get_me_fn(base_url):
    """Ham kiem tra token/nang luc that dung transport chung."""

    def get_me_fn(token):
        return call_api(base_url, token, "getMe", {}, timeout=GET_ME_TIMEOUT_S)

    return get_me_fn


def make_get_webhook_info_fn(base_url):
    """Ham kiem tra trang thai webhook that dung transport chung."""

    def get_webhook_info_fn(token):
        return call_api(
            base_url, token, "getWebhookInfo", {}, timeout=GET_ME_TIMEOUT_S
        )

    return get_webhook_info_fn


def make_get_file_fn(base_url):
    """Ham lay duong dan tai file that dung transport chung."""

    def get_file_fn(token, file_id):
        return call_api(
            base_url, token, "getFile", {"file_id": file_id},
            timeout=GET_FILE_TIMEOUT_S,
        )

    return get_file_fn


def make_download_fn(base_url):
    """Ham tai bytes file that dung transport chung."""

    def download_fn(token, file_path):
        return download_file(
            base_url, token, file_path, timeout=DOWNLOAD_TIMEOUT_S,
            max_bytes=MAX_FILE_BYTES,
        )

    return download_fn


def _no_response(exc):
    return {
        "http_status": None,
        "body": None,
        "error_code": None,
        "transport_error": type(exc).__name__,
    }


def _jitter_rng(bot_id):
    """RNG jitter giong Poller Zalo: gieo tu bot_id, on dinh, khac
    nhau giua cac bot."""
    seed = int.from_bytes(hashlib.sha256(bot_id.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed)


class TelegramPoller:
    """May trang thai poll Telegram cua 1 bot.

    ``run_once`` = 1 buoc (tra ve so giay nen cho); ``run`` = lap toi
    khi stop_event duoc dat hoac poller tu dung (xung dot/webhook),
    tra ve ma thoat (0/2/42).
    """

    def __init__(self, *, bot_id, token_path, queue_store, seen_store,
                 state_writer, data_dir, files_dir, poll_fn, get_me_fn,
                 get_webhook_info_fn, get_file_fn, download_fn,
                 poll_timeout=POLL_TIMEOUT_DEFAULT,
                 allowed_updates=("message",), log=None, monotonic=None,
                 capability_path=None, offset_path=None):
        self.bot_id = bot_id
        self.token_path = Path(token_path)
        self.queue_store = queue_store
        self.seen_store = seen_store
        self.state_writer = state_writer
        self.data_dir = Path(data_dir)
        self.files_dir = Path(files_dir)
        self.capability_path = (
            Path(capability_path)
            if capability_path is not None
            else self.data_dir / "capability.json"
        )
        self.offset_path = (
            Path(offset_path)
            if offset_path is not None
            else self.data_dir / "offset.txt"
        )
        self.poll_fn = poll_fn
        self.get_me_fn = get_me_fn
        self.get_webhook_info_fn = get_webhook_info_fn
        self.get_file_fn = get_file_fn
        self.download_fn = download_fn
        self.poll_timeout = poll_timeout
        self.allowed_updates = tuple(allowed_updates)
        self._log = log or (lambda message: None)
        self._monotonic = monotonic or time.monotonic
        self._rng = _jitter_rng(bot_id)
        self.mode = "starting"
        self.consec_err = 0
        self.consec_401 = 0
        self.exit_code = None
        self.stopped = False
        self._last_heartbeat = self._monotonic()
        self._last_capability = None
        self._next_capability_attempt = 0.0
        self._username_warning_logged = False
        self._file_prune_warning_logged = False
        self.bot_username = self._load_username_from_capability()
        self.offset = self._load_offset()

    # -- tien ich ----------------------------------------------------

    def _backoff(self, n):
        base = min(BACKOFF_BASE_S * n, BACKOFF_CAP_S)
        return base + self._rng.uniform(0, JITTER_MAX_S)

    def _maybe_heartbeat(self):
        now = self._monotonic()
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL_S:
            self._last_heartbeat = now
            self._log(
                "heartbeat: mode=%s consec_err=%d offset=%d latest_seq=%d"
                % (self.mode, self.consec_err, self.offset,
                   self.queue_store.latest_seq)
            )

    def _write_state(self, **fields):
        fields.setdefault("queue_latest_seq", self.queue_store.latest_seq)
        fields.setdefault("queue_floor_seq", self.queue_store.floor_seq)
        self.state_writer.update(**fields)

    def _short_chat(self, chat_id):
        return str(chat_id or "")[:CHAT_LOG_LEN]

    def _stop_with_code(self, code):
        self.exit_code = code
        self.stopped = True

    # -- offset ben --------------------------------------------------

    def _load_offset(self):
        """Doc offset; file mat/hong thi khoi phuc theo 01 §2."""
        try:
            with open(self.offset_path, encoding="utf-8") as handle:
                raw = handle.read().strip()
            offset = int(raw)
            if offset < 0:
                raise ValueError("offset am")
            return offset
        except (OSError, ValueError):
            pass
        recovered = self._recover_offset_from_queue()
        if recovered is not None:
            self._log(
                "CANH BAO: offset.txt mat/hong — khoi phuc offset=%d tu "
                "queue (update_id lon nhat + 1); CAM reset mu ve 0"
                % recovered
            )
            self._write_recovery_marker(recovered, "queue")
            self._save_offset_value(recovered)
            return recovered
        self._log(
            "CANH BAO: offset.txt mat/hong va queue rong — reset offset=0, "
            "chong lap bang seen_updates.txt; NGUOI VAN HANH kiem tra "
            "tin trung/lap sau khi tai chay lai"
        )
        self._write_recovery_marker(0, "reset")
        self._save_offset_value(0)
        return 0

    def _recover_offset_from_queue(self):
        """Quet queue lay update_id lon nhat; None neu queue rong."""
        max_update_id = None
        since = 0
        while True:
            rows, _latest, _floor = self.queue_store.read_since(since, 500)
            if not rows:
                break
            for row in rows:
                record = row.get("record") if isinstance(row, dict) else None
                platform_event_id = (
                    record.get("platform_event_id")
                    if isinstance(record, dict) else None
                )
                try:
                    update_id = int(str(platform_event_id))
                except (TypeError, ValueError):
                    continue
                if max_update_id is None or update_id > max_update_id:
                    max_update_id = update_id
                seq = row.get("seq")
                if isinstance(seq, int) and seq > since:
                    since = seq
            if len(rows) < 500:
                break
        if max_update_id is None:
            return None
        return max_update_id + 1

    def _write_recovery_marker(self, offset, source):
        marker = {
            "recovered_at": now_iso(),
            "offset": offset,
            "source": source,
            "bot_id": self.bot_id,
        }
        path = self.data_dir / _OFFSET_RECOVERY_MARKER
        tmp_path = path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(marker, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError:
            self._log("khong ghi duoc dau hieu khoi phuc offset")

    def _save_offset_value(self, offset):
        tmp_path = self.offset_path.with_suffix(".txt.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write("%d\n" % offset)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.offset_path)

    def _save_offset(self):
        self._save_offset_value(self.offset)

    # -- anh chup nang luc -------------------------------------------

    def _load_username_from_capability(self):
        """Doc username tu capability cu (neu co) de ap luat @suffix."""
        try:
            with open(self.capability_path, encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError):
            return None
        if isinstance(snapshot, dict):
            username = snapshot.get("username")
            if isinstance(username, str) and username:
                return username
        return None

    def _save_capability(self, response):
        body = response.get("body")
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            result = {}
        extracted = {
            "id": result.get("id"),
            "username": result.get("username"),
            "first_name": result.get("first_name"),
            "can_read_all_group_messages": result.get(
                "can_read_all_group_messages"),
        }
        snapshot = {
            "fetched_at": now_iso(),
            "response": body,
            "id": extracted["id"],
            "username": extracted["username"],
            "first_name": extracted["first_name"],
            "can_read_all_group_messages": extracted[
                "can_read_all_group_messages"],
        }
        tmp_path = self.capability_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.capability_path)
        self._last_capability = self._monotonic()
        username = extracted["username"]
        if isinstance(username, str) and username:
            self.bot_username = username
        privacy = extracted["can_read_all_group_messages"]
        self._log(
            "capability: username=@%s can_read_all_group_messages=%s "
            "(true = privacy da TAT, bot doc duoc moi tin nhom)"
            % (self.bot_username or "?", privacy)
        )

    def _maybe_refresh_capability(self, token):
        """Chup lai nang luc khi du 24h; loi thi thu lai sau 60s."""
        now = self._monotonic()
        if self._last_capability is None:
            return
        if now - self._last_capability < CAPABILITY_INTERVAL_S:
            return
        if now < self._next_capability_attempt:
            return
        try:
            response = self.get_me_fn(token)
        except Exception as exc:  # noqa: BLE001 - loi goi ham tiem vao
            response = _no_response(exc)
        result = classify_response(response, direction="poll")
        if result.is_success:
            self._save_capability(response)
            self._log("capability: da chup lai sau 24h")
        else:
            error_class = result.error_class or ErrorClass.UNKNOWN
            self._log("capability: chup lai loi (%s), thu lai sau %ds"
                      % (error_class.value, CAPABILITY_RETRY_S))
            self._next_capability_attempt = now + CAPABILITY_RETRY_S

    # -- mot buoc cua may trang thai ---------------------------------

    def run_once(self):
        """Chay 1 buoc; tra ve so giay nen cho truoc buoc tiep theo."""
        if self.stopped:
            return 0.0
        self._maybe_heartbeat()
        if self.mode == "starting":
            return self._starting_step()
        if self.mode == "wait_token":
            return self._wait_token_step()
        return self._poll_step()

    def run(self, stop_event):
        """Lap cho toi khi stop_event duoc dat hoac poller tu dung.

        Tra ve ma thoat: 0 = dung em, 2 = webhook dang bat (cau hinh),
        42 = xung dot poller/webhook (systemd khong restart lap).
        Khong bao gio tu thoat vi loi mang/API.
        """
        while not stop_event.is_set() and not self.stopped:
            try:
                delay = self.run_once()
            except Exception as exc:  # noqa: BLE001 - tai khong duoc chet
                self._log("poller: loi khong luong truoc (%s), backoff"
                          % type(exc).__name__)
                delay = self._backoff(1)
            stop_event.wait(max(0.0, delay))
        return self.exit_code if self.exit_code is not None else EXIT_OK

    def _read_token_or_error(self):
        """Doc token; loi thi ghi state + tra (None, delay backoff)."""
        try:
            return load_token(self.token_path), None
        except (OSError, ValueError) as exc:
            self.consec_err += 1
            self._log("khong doc duoc file token (%s)" % type(exc).__name__)
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
        except Exception as exc:  # noqa: BLE001 - loi goi ham tiem vao
            response = _no_response(exc)
        result = classify_response(response, direction="poll")
        if result.is_success:
            self._save_capability(response)
            self.consec_err = 0
            self.consec_401 = 0
            return self._webhook_check_step(token)
        if result.error_class == ErrorClass.TOKEN_INVALID:
            self.consec_401 += 1
            self.consec_err += 1
            self._log("khoi dong: token khong hop le (lan %d)" % self.consec_401)
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
        self.consec_401 = 0
        self.consec_err += 1
        error_class = result.error_class or ErrorClass.UNKNOWN
        self._log("khoi dong: getMe loi (%s)" % error_class.value)
        self._write_state(
            last_poll_ok=False,
            consec_err=self.consec_err,
            last_error_class=error_class.value,
            last_error_at=now_iso(),
            token_state="starting",
        )
        return self._backoff(self.consec_err)

    def _webhook_check_step(self, token):
        """Kiem tra webhook truoc khi poll; dang bat thi dung (ma 2)."""
        try:
            response = self.get_webhook_info_fn(token)
        except Exception as exc:  # noqa: BLE001 - loi goi ham tiem vao
            response = _no_response(exc)
        result = classify_response(response, direction="poll")
        if not result.is_success:
            error_class = result.error_class or ErrorClass.UNKNOWN
            self.consec_err += 1
            self._log(
                "khoi dong: kiem tra webhook loi (%s), thu lai sau "
                "backoff — chua poll" % error_class.value)
            self._write_state(
                last_poll_ok=False,
                consec_err=self.consec_err,
                last_error_class=error_class.value,
                last_error_at=now_iso(),
                token_state="starting",
            )
            return self._backoff(self.consec_err)
        body = response.get("body")
        info = body.get("result") if isinstance(body, dict) else None
        url = info.get("url") if isinstance(info, dict) else None
        if isinstance(url, str) and url:
            self._log(
                "WEBHOOK DANG BAT (url da dat): dung tai, khong poll. "
                "Nguoi van hanh tat webhook bang tay trong console "
                "truoc khi khoi dong lai — tai CAM tu xoa webhook."
            )
            self._write_state(
                last_poll_ok=False,
                consec_err=self.consec_err,
                last_error_class=ErrorClass.UNKNOWN.value,
                last_error_at=now_iso(),
                token_state="ok",
            )
            self._stop_with_code(EXIT_CONFIG)
            return 0.0
        self.mode = "polling"
        self._log("khoi dong xong: token dung duoc, webhook tat, bat dau poll")
        self._write_state(
            consec_err=0,
            last_error_class=None,
            last_error_at=None,
            token_state="ok",
        )
        return 0.0

    def _enter_wait_token(self):
        self.mode = "wait_token"
        self._log(
            "WAIT_TOKEN: %d lan 401 lien tiep — dung poll, tham do lai "
            "moi %ds, cho token moi (khong restart)"
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
            self._log("WAIT_TOKEN: chua doc duoc file token (%s)"
                      % type(exc).__name__)
            self._write_state(token_state="WAIT_TOKEN")
            return float(WAIT_TOKEN_PROBE_S)
        try:
            response = self.get_me_fn(token)
        except Exception as exc:  # noqa: BLE001 - loi goi ham tiem vao
            response = _no_response(exc)
        result = classify_response(response, direction="poll")
        if result.is_success:
            self._save_capability(response)
            self.consec_err = 0
            self.consec_401 = 0
            self.mode = "polling"
            self._log("WAIT_TOKEN: token da dung, tiep tuc poll")
            self._write_state(
                consec_err=0,
                last_error_class=None,
                last_error_at=None,
                token_state="ok",
            )
            return 0.0
        error_class = result.error_class or ErrorClass.UNKNOWN
        self._log("WAIT_TOKEN: tham do lai van loi (%s)" % error_class.value)
        self._write_state(
            last_error_class=error_class.value,
            last_error_at=now_iso(),
            token_state="WAIT_TOKEN",
        )
        return float(WAIT_TOKEN_PROBE_S)

    def _poll_step(self):
        token, delay = self._read_token_or_error()
        if token is None:
            return delay
        self._maybe_refresh_capability(token)
        try:
            response = self.poll_fn(
                token, self.poll_timeout, self.offset, self.allowed_updates)
        except Exception as exc:  # noqa: BLE001 - loi goi ham tiem vao
            response = _no_response(exc)
        result = classify_response(response, direction="poll")
        if result.is_conflict:
            self._log(
                "XUNG DOT 409: co poller/webhook khac dang chay cho bot "
                "nay — thoat ma 42, systemd KHONG restart lap; nguoi van "
                "hanh don dep roi khoi dong lai tay"
            )
            self._write_state(
                last_poll_ok=False,
                last_poll_at=now_iso(),
                consec_err=self.consec_err + 1,
                last_error_class=ErrorClass.UNKNOWN.value,
                last_error_at=now_iso(),
                token_state="ok",
            )
            self._stop_with_code(EXIT_CONFLICT)
            return 0.0
        if result.is_success:
            self.consec_err = 0
            self.consec_401 = 0
            self._process_updates(response, token)
            self.queue_store.prune()
            self._prune_files()
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
        self._log("poll loi (%s), lan lien tiep %d"
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
        if error_class == ErrorClass.RATE_LIMITED and \
                result.retry_after is not None:
            # 429 co retry_after: cho DUNG so giay, chan 3600s.
            wait = min(result.retry_after, MAX_RETRY_AFTER_SECONDS)
            self._log("poll 429: cho %ds theo retry_after" % wait)
            return float(wait)
        return self._backoff(self.consec_err)

    def _process_updates(self, response, token):
        body = response.get("body")
        if not isinstance(body, dict):
            self._log("poll ok nhung body khong phai object, bo qua dot")
            return 0
        updates = body.get("result")
        if not isinstance(updates, list):
            self._log("poll ok nhung result khong phai danh sach, bo qua dot")
            return 0
        queued = 0
        duplicates = 0
        skipped = 0
        max_update_id = None
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int) or isinstance(update_id, bool):
                self._log("bo qua update thieu update_id (khong dinh danh duoc)")
                continue
            if max_update_id is None or update_id > max_update_id:
                max_update_id = update_id
            key = str(update_id)
            if key in self.seen_store:
                duplicates += 1
                continue
            if not self.bot_username and not self._username_warning_logged:
                self._username_warning_logged = True
                self._log(
                    "CANH BAO: chua biet username bot (chua co capability) — "
                    "luat @suffix tam mo, lenh cho bot khac van vao queue"
                )
            seq_hint = self.queue_store.next_seq()

            def _download(attachment, event_id, _token=token, _seq=seq_hint):
                return self._download_attachment(_token, attachment, event_id, _seq)

            record, reason = parse_update(
                update, self.bot_id, bot_username=self.bot_username,
                download_fn=_download, log_fn=self._log,
            )
            if record is None:
                skipped += 1
                self._log("bo qua update_id=%d: %s" % (update_id, reason))
                continue
            # Thu tu bat buoc: ghi queue ben TRUOC, cap nhat seen SAU,
            # offset luu SAU khi ghi queue (cuoi dot).
            seq = self.queue_store.append(
                record, record["event_id"],
                received_at=record["received_at"],
            )
            self.seen_store.add(key)
            queued += 1
            self._log("queued seq=%d event_id=%s chat=%s"
                      % (seq, record["event_id"],
                         self._short_chat(record["chat_id"])))
        if duplicates:
            self._log("bo %d update trung (da thay)" % duplicates)
        if skipped:
            self._log("bo qua %d update (khong xu ly)" % skipped)
        if max_update_id is not None:
            new_offset = max_update_id + 1
            if new_offset > self.offset:
                self.offset = new_offset
                self._save_offset()
        return queued

    # -- tai file dinh kem --------------------------------------------

    def _download_attachment(self, token, attachment, event_id, seq):
        """Tai 1 dinh kem ve dia; tra {"local_path", "file_error"}."""
        file_size = attachment.get("file_size")
        if isinstance(file_size, int) and file_size > MAX_FILE_BYTES:
            return {"local_path": None, "file_error": "file_too_large"}
        file_id = attachment.get("file_id")
        try:
            response = self.get_file_fn(token, file_id)
        except Exception as exc:  # noqa: BLE001 - loi goi ham tiem vao
            response = _no_response(exc)
        result = classify_response(response, direction="poll")
        file_path = None
        if result.is_success:
            body = response.get("body")
            info = body.get("result") if isinstance(body, dict) else None
            if isinstance(info, dict):
                candidate = info.get("file_path")
                if isinstance(candidate, str) and candidate:
                    file_path = candidate
                size = info.get("file_size")
                if isinstance(size, int) and size > MAX_FILE_BYTES:
                    return {"local_path": None, "file_error": "file_too_large"}
        if file_path is None:
            return {"local_path": None, "file_error": "getfile_failed"}
        try:
            downloaded = self.download_fn(token, file_path)
        except Exception:  # noqa: BLE001 - loi goi ham tiem vao
            return {"local_path": None, "file_error": "download_error"}
        if not isinstance(downloaded, dict):
            return {"local_path": None, "file_error": "download_error"}
        if downloaded.get("too_large"):
            return {"local_path": None, "file_error": "file_too_large"}
        body_bytes = downloaded.get("body_bytes")
        if body_bytes is None or downloaded.get("http_status") != 200:
            return {"local_path": None, "file_error": "download_failed"}
        file_name = attachment.get("file_name")
        if not isinstance(file_name, str) or not file_name:
            file_name = _DEFAULT_FILE_NAMES.get(
                attachment.get("kind"), "file")
        digest = hashlib.sha256(str(file_id).encode("utf-8")).hexdigest()[:8]
        safe_name = ids.sanitize_filename(file_name)
        name = "%d_%s_%s_%s" % (seq, event_id, digest, safe_name)
        try:
            self.files_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.files_dir, 0o700)
            except OSError:
                pass
            dest = self.files_dir / name
            tmp_path = dest.with_suffix(dest.suffix + ".tmp")
            with open(tmp_path, "wb") as handle:
                handle.write(body_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, dest)
        except OSError:
            return {"local_path": None, "file_error": "save_failed"}
        return {"local_path": str(dest), "file_error": None}

    # -- prune file ---------------------------------------------------

    def _prune_files(self):
        """Xoa file cu (>30 ngay) VA seq <= watermark "da phuc vu".

        Chua co watermark thi giu lai tat ca. File khong co tien to seq
        thi khong bao gio xoa (than trong). Chi log so luong, khong
        log ten file (ten file co the la thong tin nhay cam).
        """
        watermark = self.queue_store.read_watermark()
        if watermark is None:
            if not self._file_prune_warning_logged:
                self._file_prune_warning_logged = True
                self._log("file prune: chua co watermark, giu lai tat ca file")
            return 0
        try:
            entries = list(self.files_dir.iterdir())
        except OSError:
            return 0
        now = time.time()
        cutoff = now - FILE_PRUNE_AGE_DAYS * 86400
        removed = 0
        for entry in entries:
            if not entry.is_file():
                continue
            head = entry.name.split("_", 1)[0]
            try:
                seq = int(head)
            except ValueError:
                continue
            if seq > watermark:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            self._log("file prune: da xoa %d file cu (watermark=%d)"
                      % (removed, watermark))
        return removed
