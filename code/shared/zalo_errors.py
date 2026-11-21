"""Phân loại lỗi Zalo thành phân loại chung (DESIGN §7, ``02`` §4).

Phân loại từ CẢ 2 KÊNH: ưu tiên ``error_code`` trong body; nếu body
không có/không parse được thì dùng HTTP status. Hai nguồn thứ cấp mâu
thuẫn nhau (một nói mọi lỗi về HTTP 200 + body, một nói burst trả HTTP
429) nên CẤM khẳng định tuyệt đối rằng phân loại chỉ dùng body.

Bảng phân loại — nguồn/bằng chứng từng dòng:

- 0 -> thành công: ĐÃ KIỂM CHỨNG (traffic thật 15/11/2026: mọi
  response thành công đều mang ``error_code: 0`` kèm ``ok: true``;
  thiếu dòng này tai không bao giờ rời trạng thái ``starting``).
- 408 (poll) -> bình thường, KHÔNG phải lỗi: ĐÃ KIỂM CHỨNG (traffic
  thật — không có tin mới trong thời gian chờ).
- 401 -> TOKEN_INVALID: ý nghĩa ĐÃ KIỂM CHỨNG (token sai/bị reset).
- 403 khi poll -> TOKEN_INVALID: ý nghĩa ĐÃ KIỂM CHỨNG.
- 403 khi gửi -> UNKNOWN theo chat: CHƯA KIỂM CHỨNG — không gộp với
  401, chờ quan sát.
- 429 -> RATE_LIMITED + cờ ``quota_suspect``: nguồn thứ cấp mâu thuẫn
  (UC-24) — CẤM giả định burst hay hết quota; cờ chỉ ghi "nghi".
- 410 -> CHAT_GONE: CHƯA quan sát bao giờ.
- 400 (gửi) -> PERMANENT theo delivery: nguồn thứ cấp.
- Tín hiệu lạ khác -> UNKNOWN: 1 lần thử lại sau 60s (SUY LUẬN).
- Lỗi transport (không chạm được máy chủ) -> RETRYABLE_NETWORK: suy
  luận từ hành vi baseline (retry vô hạn với lỗi mạng/API, backoff
  tối đa 60s) — chưa có bảng chính thức của Zalo.
- HTTP 200 + ``ok: true`` trong body -> thành công: ĐÃ KIỂM CHỨNG.
- Dạng "HTTP 200 + mã lỗi trong body": ĐÃ KIỂM CHỨNG với mã 404
  (getWebhookInfo, 15/11/2026: HTTP 200 + ``error_code: 404``); các
  mã khác vẫn theo nguồn thứ cấp, chờ quan sát.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.errors import ErrorClass

DIRECTIONS = ("poll", "send")


@dataclass(frozen=True)
class Classification:
    """Kết quả phân loại một lần gọi API Zalo."""

    error_class: ErrorClass | None
    is_success: bool = False
    is_no_new_messages: bool = False
    quota_suspect: bool = False
    signal: str = ""

    @property
    def is_error(self):
        return self.error_class is not None


def _normalize_code(code):
    """Chuẩn hóa mã tín hiệu về int; không chuẩn hóa được thì None."""
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


def classify_code(code, direction="poll"):
    """Phân loại một mã tín hiệu (từ body hoặc HTTP status)."""
    if direction not in DIRECTIONS:
        raise ValueError("direction phải là một trong %s" % (DIRECTIONS,))
    code = _normalize_code(code)
    if code == 0:
        # 0 = khong co loi: moi response thanh cong cua Zalo deu mang
        # error_code 0 kem ok:true (DA KIEM CHUNG, traffic that
        # 15/11/2026). Khong co HTTP status 0 nen nhanh nay khong anh
        # huong duong fallback HTTP status.
        return Classification(
            error_class=None, is_success=True, signal="code:0"
        )
    if code == 408:
        # 408 = không có tin mới: bình thường, không phải lỗi.
        return Classification(
            error_class=None,
            is_no_new_messages=True,
            signal="code:408",
        )
    if code == 401:
        return Classification(
            error_class=ErrorClass.TOKEN_INVALID, signal="code:401"
        )
    if code == 403:
        if direction == "poll":
            return Classification(
                error_class=ErrorClass.TOKEN_INVALID, signal="code:403/poll"
            )
        return Classification(
            error_class=ErrorClass.UNKNOWN, signal="code:403/send"
        )
    if code == 429:
        return Classification(
            error_class=ErrorClass.RATE_LIMITED,
            quota_suspect=True,
            signal="code:429",
        )
    if code == 410:
        return Classification(error_class=ErrorClass.CHAT_GONE, signal="code:410")
    if code == 400:
        return Classification(error_class=ErrorClass.PERMANENT, signal="code:400")
    return Classification(error_class=ErrorClass.UNKNOWN, signal="code:%s" % (code,))


def classify_response(response, direction="poll"):
    """Phân loại dict kết quả của :func:`shared.zalo_client.call_api`.

    Thứ tự: lỗi transport -> ``ok: true`` -> ``error_code`` trong body
    -> HTTP status -> UNKNOWN.
    """
    if direction not in DIRECTIONS:
        raise ValueError("direction phải là một trong %s" % (DIRECTIONS,))
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
    if isinstance(body, dict) and body.get("ok") is True and error_code is None:
        return Classification(error_class=None, is_success=True, signal="ok:true")
    if error_code is not None:
        result = classify_code(error_code, direction)
        return Classification(
            error_class=result.error_class,
            is_success=result.is_success,
            is_no_new_messages=result.is_no_new_messages,
            quota_suspect=result.quota_suspect,
            signal="body:%s" % result.signal,
        )
    http_status = _normalize_code(response.get("http_status"))
    if http_status is None:
        return Classification(error_class=ErrorClass.UNKNOWN, signal="no-signal")
    if http_status == 200:
        # HTTP 200 nhưng body không xác nhận ok:true: không dám coi là
        # thành công.
        return Classification(
            error_class=ErrorClass.UNKNOWN, signal="http:200-unconfirmed"
        )
    result = classify_code(http_status, direction)
    return Classification(
        error_class=result.error_class,
        is_success=result.is_success,
        is_no_new_messages=result.is_no_new_messages,
        quota_suspect=result.quota_suspect,
        signal="http:%s" % result.signal,
    )
