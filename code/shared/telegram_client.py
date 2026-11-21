"""Transport chung goi Telegram Bot API — stdlib-only.

Giong hop dong 4 kenh cua :mod:`shared.zalo_client` (DESIGN §7) nhung
rieng cho Telegram (``03`` §2/§4)::

    {"http_status": int | None,
     "body": object | None,        # JSON da parse, hoac text tho
     "error_code": int | None,     # ma trong body (neu co)
     "transport_error": str | None}

- Loi HTTP (ke ca 4xx/5xx) KHONG phai ``transport_error``: status +
  body van tra du de bang phan loai 2 kenh chay duoc.
- Chi loi khong cham duoc toi may chu (mat mang, timeout, redirect bi
  tu choi) moi thanh ``transport_error``; khi do 3 kenh kia la None.
- Tu choi redirect: redirect co the lam lo token trong URL sang host
  khac.

``base_url`` BAT BUOC truyen vao tuong minh (khong co host mac dinh):
test dung may chu gia tren localhost, chay that dung host Telegram do
cau hinh cung cap. Token KHONG BAO GIO xuat hien trong log hay thong
bao loi — chuoi loi duoc tay token truoc khi tra ve.

Khac Zalo:

- Ngoai POST JSON (:func:`call_api`), Telegram can multipart de gui
  file tu dia (:func:`call_api_multipart`) va GET de tai file ve
  (:func:`download_file`, duong dan ``/file/bot<TOKEN>/<file_path>``).
- Duong dan tai file lay tu response ``getFile``; ham tai kiem tra
  ``file_path`` (cam ``..``, cam bat dau bang ``/``, cam scheme) de
  chong chen duong dan.
- Gioi han tai: mac dinh toi da 20MB (tai lieu chinh thuc); vuot qua
  thi dung giua chung va bao ``too_large`` thay vi tai het ve.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid

_METHOD_RE = re.compile(r"^[A-Za-z0-9_]+$")

_REDACTED = "[REDACTED]"

# Gioi han tai file cua Telegram (tai lieu chinh thuc): file > 20MB
# khong tai duoc qua Bot API.
DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Tran cung cho multipart gui file: document/video toi da 50MB (tai
# lieu chinh thuc). Transport kiem tra lai de khong bao gio gui qua
# tran nay ke ca khi caller quen kiem tra.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Tu choi moi redirect."""

    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _parse_body(raw_bytes):
    """Parse body tra ve -> (body, error_code).

    Body JSON thi tra object da parse; body khong phai JSON thi tra
    text tho va ``error_code`` la None (phan loai se dung HTTP status).
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
    """Tay token khoi chuoi loi — token khong bao gio lot ra ngoai."""
    if token and token in text:
        return text.replace(token, _REDACTED)
    return text


def _check_base_url_token_method(base_url, token, method):
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url bat buoc phai truyen vao tuong minh")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token bat buoc phai co")
    if not isinstance(method, str) or not _METHOD_RE.match(method):
        raise ValueError("method khong hop le: %r" % (method,))


def _do_request(request, token, timeout):
    """Thuc hien 1 request, tra dict 4 kenh (xem docstring module)."""
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            status = response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # May chu da tra loi (ke ca 4xx/5xx): giu du status + body,
        # KHONG gop thanh transport_error.
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - body doc loi thi coi nhu rong
            raw = b""
        body, error_code = _parse_body(raw)
        return {
            "http_status": exc.code,
            "body": body,
            "error_code": error_code,
            "transport_error": None,
        }
    except Exception as exc:  # noqa: BLE001 - moi loi transport ve 1 kenh
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


def call_api(base_url, token, method, payload=None, timeout=30):
    """Goi mot method Telegram Bot API bang POST JSON.

    :param base_url: scheme + host, vd ``http://127.0.0.1:PORT`` khi
        test voi may chu gia; chay that thi cau hinh cung cap host
        Telegram. Bat buoc truyen vao, khong co mac dinh.
    :param token: token bot, dat trong duong dan URL theo thiet ke
        cua Telegram (``/bot<TOKEN>/<method>``). Khong log, khong de
        sot vao loi.
    :param method: ten method, chi chu/so/gach duoi (chong chen
        duong dan).
    :param payload: dict JSON gui kem (mac dinh ``{}``).
    :param timeout: timeout HTTP (giay).
    :return: dict du 4 kenh ``http_status``/``body``/``error_code``/
        ``transport_error`` (xem docstring module).
    """
    _check_base_url_token_method(base_url, token, method)
    url = "%s/bot%s/%s" % (base_url.rstrip("/"), token, method)
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    return _do_request(request, token, timeout)


def build_multipart(fields, file_field, file_name, file_bytes):
    """Dung body multipart/form-data cho 1 file upload.

    Tach rieng de test duoc phan dung body ma khong can mang. Tra ve
    ``(content_type, body_bytes)``. ``fields`` la dict ten -> gia tri
    chuoi; ``file_bytes`` la noi dung file.
    """
    boundary = "----multibot-%s" % uuid.uuid4().hex
    parts = []
    for name, value in (fields or {}).items():
        parts.append(
            b"--" + boundary.encode("ascii") + b"\r\n"
            + b'Content-Disposition: form-data; name="%s"\r\n\r\n'
            % str(name).encode("utf-8")
            + str(value).encode("utf-8") + b"\r\n"
        )
    safe_name = str(file_name).replace('"', "_").replace("\r", "_").replace("\n", "_")
    parts.append(
        b"--" + boundary.encode("ascii") + b"\r\n"
        + b'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
        % (str(file_field).encode("utf-8"), safe_name.encode("utf-8"))
        + b"Content-Type: application/octet-stream\r\n\r\n"
        + bytes(file_bytes) + b"\r\n"
    )
    parts.append(b"--" + boundary.encode("ascii") + b"--\r\n")
    content_type = "multipart/form-data; boundary=%s" % boundary
    return content_type, b"".join(parts)


def call_api_multipart(base_url, token, method, fields, file_field,
                       file_path, file_name=None, timeout=60,
                       max_bytes=MAX_UPLOAD_BYTES):
    """Goi mot method Telegram kem upload 1 file (multipart).

    :param fields: dict cac truong text (vd chat_id, caption).
    :param file_field: ten truong file (vd truong anh/tai lieu/video
        cua method tuong ung).
    :param file_path: duong dan file tren dia.
    :param file_name: ten file gui kem; mac dinh lay basename cua
        ``file_path``.
    :param max_bytes: tran cung kich thuoc file; vuot qua thi raise
        ValueError truoc khi gui (caller da kiem tra gioi han rieng
        tung method; day la luoi an toan cuoi).
    :return: dict du 4 kenh nhu :func:`call_api`.
    """
    _check_base_url_token_method(base_url, token, method)
    if not isinstance(file_field, str) or not _METHOD_RE.match(file_field):
        raise ValueError("file_field khong hop le: %r" % (file_field,))
    size = os.path.getsize(file_path)
    if size > max_bytes:
        raise ValueError(
            "file %d bytes vuot tran %d bytes" % (size, max_bytes))
    with open(file_path, "rb") as handle:
        file_bytes = handle.read()
    name = file_name or os.path.basename(str(file_path))
    content_type, body = build_multipart(fields, file_field, name, file_bytes)
    url = "%s/bot%s/%s" % (base_url.rstrip("/"), token, method)
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    return _do_request(request, token, timeout)


def _validate_file_path(file_path):
    """Kiem tra file_path tu getFile: cam thoat khoi goc tai file."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("file_path bat buoc phai la chuoi khong rong")
    if file_path.startswith("/") or file_path.startswith("\\"):
        raise ValueError("file_path khong duoc bat dau bang /: %r" % (file_path,))
    if "://" in file_path or file_path.startswith(".."):
        raise ValueError("file_path khong hop le: %r" % (file_path,))
    parts = file_path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("file_path chua thanh phan rong/nguy hiem: %r" % (file_path,))
    return file_path


def download_file(base_url, token, file_path, timeout=60,
                  max_bytes=DEFAULT_MAX_DOWNLOAD_BYTES):
    """Tai 1 file tu Telegram ve (GET ``/file/bot<TOKEN>/<file_path>``).

    Tra ve dict::

        {"http_status": int | None,
         "body_bytes": bytes | None,
         "transport_error": str | None,
         "too_large": bool}

    ``too_large=True`` khi file vuot ``max_bytes``: ham dung doc giua
    chung, ``body_bytes`` la None (khong giu ban tai do). Loi HTTP
    tra ``http_status`` + ``body_bytes=None`` (body loi khong can
    thiet cho viec tai file).
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url bat buoc phai truyen vao tuong minh")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token bat buoc phai co")
    _validate_file_path(file_path)
    url = "%s/file/bot%s/%s" % (base_url.rstrip("/"), token, file_path)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            status = response.getcode()
            chunks = []
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return {
                        "http_status": status,
                        "body_bytes": None,
                        "transport_error": None,
                        "too_large": True,
                    }
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        return {
            "http_status": exc.code,
            "body_bytes": None,
            "transport_error": None,
            "too_large": False,
        }
    except Exception as exc:  # noqa: BLE001 - moi loi transport ve 1 kenh
        message = _scrub("%s: %s" % (type(exc).__name__, exc), token)
        return {
            "http_status": None,
            "body_bytes": None,
            "transport_error": message,
            "too_large": False,
        }
    return {
        "http_status": status,
        "body_bytes": b"".join(chunks),
        "transport_error": None,
        "too_large": False,
    }
