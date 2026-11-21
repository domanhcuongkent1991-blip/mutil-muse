"""Phan loai loi Telegram thanh phan loai chung (DESIGN §7, ``03`` §5).

Phan loai tu CA 2 KENH: uu tien ``error_code`` trong body; neu body
khong co/khong parse duoc thi dung HTTP status. Telegram tra HTTP
status bang ma loi (tai lieu chinh thuc), nhung phan loai van doc body
truoc de lay them ``parameters`` (``retry_after``,
``migrate_to_chat_id``) — bai hoc tu vong test that Zalo: response
that co the mang truong ma mo phong thieu.

Bang phan loai — nguon/bang chung tung dong:

- ``ok: true`` -> thanh cong. Chap nhan ca ``error_code: 0`` kem
  ``ok: true`` (bai hoc Zalo 15/11/2026: dung coi 0 la loi).
- Poll + ``result == []`` -> khong co tin moi: binh thuong, khong
  phai loi.
- 401 -> TOKEN_INVALID: token sai/bi thu hoi (tai lieu chinh thuc).
- 403 khi gui -> CHAT_GONE theo chat: bot bi chan/kick khoi chat do;
  bot van khoe, lane do dung lai toi khi co tin moi (``03`` §5).
  403 khi poll -> UNKNOWN: dien giai cua vong code nay (thiet ke
  khong dinh nghia; poll hiem khi 403) — poller tiep tuc thu voi
  backoff, khong thoat.
- 409 khi poll -> xung dot (poller/webhook khac dang chay): co
  ``is_conflict``; poller thoat ma 42, CAM retry lap (SPEC F5).
  409 khi gui -> UNKNOWN: thiet ke chi dinh nghia 409 cho poll.
- 429 -> RATE_LIMITED + ``retry_after`` (giay) lay tu
  ``parameters.retry_after`` neu co; nguoi goi phai cho DUNG so giay
  do, chan toi da 3600s (``03`` §5). Thieu ``retry_after`` thi caller
  dung backoff mac dinh.
- 400 + ``parameters.migrate_to_chat_id`` -> PERMANENT kem
  ``migrate_to_chat_id``: chat cu da doi id; nguoi gui viet lai
  chat_id va thu lai 1 lan (``03`` §5).
- 400 (gui, khong migrate) -> PERMANENT: thu chet ngay sau 1 lan.
  400 (poll) -> UNKNOWN: dien giai cua vong code nay.
- 500/502/503/504 -> RETRYABLE_NETWORK (loi phia may chu Telegram).
- Loi transport -> RETRYABLE_NETWORK.
- Tin hieu la khac -> UNKNOWN.
- Khong co khai niem quota nhu Zalo: ``quota_suspect`` luon False
  (gi nguyen truong de giao dien phan loai thong nhat).
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.errors import ErrorClass

DIRECTIONS = ("poll", "send")

# Chan tren cho retry_after (03 §5): khong cho qua 3600s.
MAX_RETRY_AFTER_SECONDS = 3600


@dataclass(frozen=True)
class Classification:
    """Ket qua phan loai mot lan goi API Telegram."""

    error_class: ErrorClass | None
    is_success: bool = False
    is_no_new_messages: bool = False
    is_conflict: bool = False
    retry_after: int | None = None
    migrate_to_chat_id: str | None = None
    quota_suspect: bool = False
    signal: str = ""

    @property
    def is_error(self):
        return self.error_class is not None


def _normalize_code(code):
    """Chuan hoa ma tin hieu ve int; khong chuan hoa duoc thi None."""
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        try:
            return int(code.strip())
        except ValueError:
            return None
    return None


def _normalize_seconds(value):
    """Chuan hoa retry_after ve int giay; khong hop le thi None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _extract_parameters(body):
    """Lay (retry_after, migrate_to_chat_id) tu body neu co."""
    retry_after = None
    migrate_to = None
    if isinstance(body, dict):
        params = body.get("parameters")
        if isinstance(params, dict):
            retry_after = _normalize_seconds(params.get("retry_after"))
            raw_migrate = params.get("migrate_to_chat_id")
            if isinstance(raw_migrate, bool):
                raw_migrate = None
            if raw_migrate is not None:
                migrate_to = str(raw_migrate)
    return retry_after, migrate_to


def classify_code(code, direction="poll", retry_after=None,
                  migrate_to_chat_id=None):
    """Phan loai mot ma tin hieu (tu body hoac HTTP status)."""
    if direction not in DIRECTIONS:
        raise ValueError("direction phai la mot trong %s" % (DIRECTIONS,))
    code = _normalize_code(code)
    if code == 0:
        # 0 = khong co loi (bai hoc Zalo: ok:true kem error_code 0).
        return Classification(
            error_class=None, is_success=True, signal="code:0"
        )
    if code == 401:
        return Classification(
            error_class=ErrorClass.TOKEN_INVALID, signal="code:401"
        )
    if code == 403:
        if direction == "send":
            return Classification(
                error_class=ErrorClass.CHAT_GONE, signal="code:403/send"
            )
        return Classification(
            error_class=ErrorClass.UNKNOWN, signal="code:403/poll"
        )
    if code == 409:
        if direction == "poll":
            return Classification(
                error_class=ErrorClass.UNKNOWN, is_conflict=True,
                signal="code:409/poll",
            )
        return Classification(
            error_class=ErrorClass.UNKNOWN, signal="code:409/send"
        )
    if code == 429:
        return Classification(
            error_class=ErrorClass.RATE_LIMITED,
            retry_after=retry_after,
            signal="code:429",
        )
    if code == 400:
        if migrate_to_chat_id is not None:
            return Classification(
                error_class=ErrorClass.PERMANENT,
                migrate_to_chat_id=migrate_to_chat_id,
                signal="code:400/migrate",
            )
        if direction == "send":
            return Classification(
                error_class=ErrorClass.PERMANENT, signal="code:400/send"
            )
        return Classification(
            error_class=ErrorClass.UNKNOWN, signal="code:400/poll"
        )
    if code in (500, 502, 503, 504):
        return Classification(
            error_class=ErrorClass.RETRYABLE_NETWORK,
            signal="code:%s" % (code,),
        )
    return Classification(error_class=ErrorClass.UNKNOWN, signal="code:%s" % (code,))


def classify_response(response, direction="poll"):
    """Phan loai dict ket qua cua :func:`shared.telegram_client.call_api`.

    Thu tu: loi transport -> ``ok: true`` (kem kiem tra result rong
    khi poll) -> ``error_code`` trong body -> HTTP status -> UNKNOWN.
    ``parameters`` trong body luon duoc trich truoc de kem theo phan
    loai (retry_after / migrate_to_chat_id).
    """
    if direction not in DIRECTIONS:
        raise ValueError("direction phai la mot trong %s" % (DIRECTIONS,))
    if not isinstance(response, dict):
        return Classification(error_class=ErrorClass.UNKNOWN, signal="no-response")
    if response.get("transport_error"):
        return Classification(
            error_class=ErrorClass.RETRYABLE_NETWORK, signal="transport_error"
        )
    body = response.get("body")
    error_code = response.get("error_code")
    if error_code is None and isinstance(body, dict):
        error_code = body.get("error_code")
    retry_after, migrate_to = _extract_parameters(body)
    if isinstance(body, dict) and body.get("ok") is True:
        # ok:true la thanh cong ke ca khi error_code = 0 (bai hoc
        # Zalo). Chi khi ok:true KEM error_code khac 0 thi moi tin
        # error_code — truong hop mau thuan chua tung thay, xu ly
        # than trong: uu tien error_code.
        normalized = _normalize_code(error_code)
        if normalized in (None, 0):
            is_empty = direction == "poll" and body.get("result") == []
            return Classification(
                error_class=None, is_success=True,
                is_no_new_messages=is_empty, signal="ok:true",
            )
    if error_code is not None:
        result = classify_code(
            error_code, direction, retry_after=retry_after,
            migrate_to_chat_id=migrate_to,
        )
        return Classification(
            error_class=result.error_class,
            is_success=result.is_success,
            is_no_new_messages=result.is_no_new_messages,
            is_conflict=result.is_conflict,
            retry_after=result.retry_after,
            migrate_to_chat_id=result.migrate_to_chat_id,
            quota_suspect=result.quota_suspect,
            signal="body:%s" % result.signal,
        )
    http_status = _normalize_code(response.get("http_status"))
    if http_status is None:
        return Classification(error_class=ErrorClass.UNKNOWN, signal="no-signal")
    if http_status == 200:
        # HTTP 200 nhung body khong xac nhan ok:true: khong dam coi la
        # thanh cong.
        return Classification(
            error_class=ErrorClass.UNKNOWN, signal="http:200-unconfirmed"
        )
    result = classify_code(
        http_status, direction, retry_after=retry_after,
        migrate_to_chat_id=migrate_to,
    )
    return Classification(
        error_class=result.error_class,
        is_success=result.is_success,
        is_no_new_messages=result.is_no_new_messages,
        is_conflict=result.is_conflict,
        retry_after=result.retry_after,
        migrate_to_chat_id=result.migrate_to_chat_id,
        quota_suspect=result.quota_suspect,
        signal="http:%s" % result.signal,
    )
