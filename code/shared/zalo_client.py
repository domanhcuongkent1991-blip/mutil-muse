"""Transport chung gọi Zalo Bot Platform — stdlib-only.

Chỉ có MỘT hàm gọi chung :func:`call_api`; CẤM method tên riêng trong
file này (D17: đầu nối gửi phía Muse mới đặt tên method gửi; tai chỉ
dùng transport chung này).

Trả đủ 4 kênh (D1 — cấm gộp lỗi, DESIGN §7, ``02`` §4)::

    {"http_status": int | None,
     "body": object | None,        # JSON đã parse, hoặc text thô
     "error_code": int | None,     # mã trong body (nếu có)
     "transport_error": str | None}

- Lỗi HTTP (kể cả 4xx/5xx) KHÔNG phải ``transport_error``: status +
  body vẫn trả đủ để bảng phân loại 2 kênh chạy được.
- Chỉ lỗi không chạm được tới máy chủ (mất mạng, timeout, redirect bị
  từ chối) mới thành ``transport_error``; khi đó 3 kênh kia là None.
- Từ chối redirect (mẫu baseline R6): redirect có thể làm lộ token
  trong URL sang host khác.

``base_url`` BẮT BUỘC truyền vào tường minh (không có host mặc định): test dùng
máy chủ giả trên localhost, chạy thật dùng host Zalo do cấu hình cung
cấp. Token KHÔNG BAO GIỜ xuất hiện trong log hay thông báo lỗi — chuỗi
lỗi được tẩy token trước khi trả về.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

_METHOD_RE = re.compile(r"^[A-Za-z0-9_]+$")

_REDACTED = "[REDACTED]"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Từ chối mọi redirect (mẫu baseline R6)."""

    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _parse_body(raw_bytes):
    """Parse body trả về -> (body, error_code).

    Body JSON thì trả object đã parse; body không phải JSON thì trả
    text thô và ``error_code`` là None (phân loại sẽ dùng HTTP status).
    """
    if not raw_bytes:
        return None, None
    text = raw_bytes.decode("utf-8", errors="replace")
    if not text.strip():
        return None, None
    try:
        body = json.loads(text)
    except ValueError:
        return text, None
    error_code = None
    if isinstance(body, dict):
        error_code = body.get("error_code")
        if isinstance(error_code, bool):
            error_code = None
        elif isinstance(error_code, str):
            try:
                error_code = int(error_code)
            except ValueError:
                error_code = None
        elif not isinstance(error_code, int):
            error_code = None
    return body, error_code


def _scrub(text, token):
    """Tẩy token khỏi chuỗi lỗi — token không bao giờ lọt ra ngoài."""
    if token and token in text:
        return text.replace(token, _REDACTED)
    return text


def call_api(base_url, token, method, payload=None, timeout=30):
    """Gọi một method Zalo Bot Platform bằng POST JSON.

    :param base_url: scheme + host, vd ``http://127.0.0.1:PORT`` khi
        test với máy chủ giả; chạy thật thì cấu hình cung cấp host
        Zalo. Bắt buộc truyền vào, không có mặc định.
    :param token: token bot, đặt trong đường dẫn URL theo thiết kế
        của Zalo (``/bot<TOKEN>/<method>``). Không log, không tẩy
        sót vào lỗi.
    :param method: tên method, chỉ chữ/số/gạch dưới (chống chèn
        đường dẫn).
    :param payload: dict JSON gửi kèm (mặc định ``{}``).
    :param timeout: timeout HTTP (giây).
    :return: dict đủ 4 kênh ``http_status``/``body``/``error_code``/
        ``transport_error`` (xem docstring module).
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url bắt buộc phải truyền vào tường minh")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token bắt buộc phải có")
    if not isinstance(method, str) or not _METHOD_RE.match(method):
        raise ValueError("method không hợp lệ: %r" % (method,))
    url = "%s/bot%s/%s" % (base_url.rstrip("/"), token, method)
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            status = response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Máy chủ đã trả lời (kể cả 4xx/5xx): giữ đủ status + body,
        # KHÔNG gộp thành transport_error (D1).
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - body đọc lỗi thì coi như rỗng
            raw = b""
        body, error_code = _parse_body(raw)
        return {
            "http_status": exc.code,
            "body": body,
            "error_code": error_code,
            "transport_error": None,
        }
    except Exception as exc:  # noqa: BLE001 - mọi lỗi transport về 1 kênh
        message = _scrub("%s: %s" % (type(exc).__name__, exc), token)
        return {
            "http_status": None,
            "body": None,
            "error_code": None,
            "transport_error": message,
        }
    body, error_code = _parse_body(raw)
    return {
        "http_status": status,
        "body": body,
        "error_code": error_code,
        "transport_error": None,
    }
