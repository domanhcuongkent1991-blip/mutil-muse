#!/usr/bin/env python3
"""Lớp ứng dụng Web UI — routing + xử lý request, không phụ thuộc socket.

``UiApp.handle(request)`` nhận 1 ``Request`` thuần và trả ``Response``
thuần để test được toàn bộ luồng mà không cần socket thật; adapter
HTTP nằm ở ``ui_main.py``. Mọi thao tác đổi trạng thái đều là POST +
CSRF token theo phiên + kiểm tra Origin/Sec-Fetch-Site (05 §6); CẤM
GET đổi trạng thái.

Ranh giới cứng (05 §6 + SPEC F18/S6):

- UI KHÔNG đọc nội dung file token/secret (chỉ GHI khi thêm/thay
  token); khối bàn giao K3 chỉ hiện trong response ngay sau khi tạo
  bot, không có đường xem lại.
- UI KHÔNG gọi API nền tảng, trừ ``verify_token`` (tiến trình riêng,
  chỉ getMe, token qua stdin) — ngoại lệ hẹp duy nhất của F18.
- UI KHÔNG tự nhắn tin báo (K4 là việc phía Muse); UI chỉ hiện huy
  hiệu cảnh báo từ tín hiệu phía PC.
- Điều khiển unit qua ``systemctl --user`` dạng list-args, không
  shell, bot_id phải khớp mẫu + có trong registry (allowlist).
- Audit append-only mọi thao tác đổi trạng thái: thời gian, IP,
  action, trước→sau, kết quả. Không secret, không nội dung tin.
"""

from __future__ import annotations

import contextlib
import fcntl
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from pc.center import center_store
from pc.ui import transfers, ui_auth, ui_pages, ui_static, ui_store
from shared import multipart
from shared.ids import validate_bot_id

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)

SESSION_COOKIE = "multibot_session"
LOGIN_CSRF_COOKIE = "multibot_login_csrf"
CONFIRM_TTL_SECONDS = 300  # confirm token hết hạn sau 5 phút.
MAX_CONFIRM_TOKENS = 100
BOT_LOCK_TIMEOUT_SECONDS = 5.0
MAX_FORM_FIELD_CHARS = 2000
MAX_DISPLAY_NAME_CHARS = 100

_ROUTE_BOT = re.compile(r"^/bots/([A-Za-z0-9\-_]+)(/.*)?$")
_ROUTE_ACCOUNT = re.compile(r"^/accounts/([A-Za-z0-9\-_]+)(/.*)?$")
# Trung tâm (T3): chi tiết việc + đổi trạng thái + chuyển tay + đặt hạn.
_ROUTES_CENTER_TASK = re.compile(r"^/center/tasks/(t-[0-9a-f]{12})$")
_ROUTES_CENTER_TASK_STATUS = re.compile(
    r"^/center/tasks/(t-[0-9a-f]{12})/status$")
_ROUTES_CENTER_TASK_REASSIGN = re.compile(
    r"^/center/tasks/(t-[0-9a-f]{12})/reassign$")
_ROUTES_CENTER_TASK_DUE = re.compile(
    r"^/center/tasks/(t-[0-9a-f]{12})/due$")
# GC-E05 (T3m): route moi /api/tasks/<id>/due — cung handler dat/doi
# han nhu tren (de dung ten theo thiet ke v0.6.0).
_ROUTES_API_TASK_DUE = re.compile(
    r"^/api/tasks/(t-[0-9a-f]{12})/due$")
# NS-02 (T3m): route gui lai thu chet — POST /api/bots/<id>/retry-dead.
_ROUTES_API_BOT_RETRY_DEAD = re.compile(
    r"^/api/bots/([A-Za-z0-9\-_]+)/retry-dead$")
# Trung tâm (v0.2.4, đội B): cấp lại mã bí mật — trang xác nhận (GET)
# + thực thi (POST /execute). Không có route gửi giao-viec/bao-cao tay.
_ROUTE_CENTER_ROTATE = re.compile(
    r"^/center/accounts/([A-Za-z0-9\-_]+)/rotate-secret(/execute)?$")
# Trung tâm (v0.6.0, T3j — ngoại lệ F18(3)): xem mã bí mật — trang
# nhập mật khẩu xem mã (GET) + thực thi (POST /execute). Mã chỉ xuất
# hiện trong response của route 2 sau khi mật khẩu đúng; không có
# route GET nào trả mã.
_ROUTE_CENTER_VIEW_SECRET = re.compile(
    r"^/center/accounts/([A-Za-z0-9\-_]+)/view-secret(/execute)?$")
# Trung tâm (v0.2.5, T3b): chu nhan tai file dinh kem ve (GET).
_ROUTE_CENTER_FILE = re.compile(r"^/center/files/(f-[0-9a-f]{12})$")


def _parse_int_pct(raw, field):
    """Parse so nguyen phan tram tu form cai dat (ĐX-06, T3m).

    Tra so nguyen; khong phai so nguyen ("80.5", "80.0", "abc")
    thi nem ValueError voi dung 1 cau: "Chi nhap so nguyen, vi du
    80". So ngoai khoang KHONG loi o day — tang kiem tra khoang
    giu cau loi khoang rieng nhu cu. Ca 2 handler cai dat
    (center + alerts) deu goi ham nay thay int(...) truc tiep.
    """
    text = raw if isinstance(raw, str) else ""
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        raise ValueError("Chỉ nhập số nguyên, ví dụ 80")


def _assignee_list(value):
    """Chuan hoa assigned_to (chuoi v1 / danh sach v2) -> danh sach.
    (Ban ghi v1 cu doc tuong thich nhu danh sach 1 nguoi.)"""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, str) and item]
    return []


def _valid_bot_id(bot_id):
    """True khi bot_id khớp mẫu (validate_bot_id raise khi sai)."""
    try:
        validate_bot_id(bot_id)
    except (ValueError, TypeError):
        return False
    return True


class Request:
    """Request thuần cho ``UiApp.handle`` (không phụ thuộc socket)."""

    def __init__(self, method, path, query=None, headers=None, body=b"",
                 client_ip="127.0.0.1"):
        self.method = method.upper()
        self.path = path
        self.query = query or {}
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body if isinstance(body, bytes) else bytes(body)
        self.client_ip = client_ip

    @property
    def form(self):
        """Parse body -> dict key -> [values]. Urlencoded nhu cu;
        multipart/form-data (form gui tin kem file, v0.2.5) thi chi
        lay truong text — file lay qua ``form_file``. Parse 1 lan
        (cache) de CSRF check + doc body khong parse lai."""
        if hasattr(self, "_form_cache"):
            return self._form_cache
        content_type = self.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            try:
                fields, files = multipart.parse_multipart(
                    self.body, content_type)
            except ValueError:
                fields, files = {}, []
            self._multipart_files = files
            self._form_cache = fields
            return self._form_cache
        try:
            text = self.body.decode("utf-8", errors="replace")
        except Exception:
            return {}
        self._form_cache = parse_qs(text, keep_blank_values=True)
        return self._form_cache

    def form_value(self, key, default=""):
        values = self.form.get(key)
        if not values:
            return default
        return values[0]

    def form_values(self, key):
        """Tat ca gia tri cua 1 truong form (select multiple — v0.4.0,
        chon nhieu tai khoan). Tra ve [] khi khong co."""
        values = self.form.get(key)
        if not values:
            return []
        return list(values)

    def form_file(self, name):
        """File dinh kem trong multipart: dict {name, filename,
        content_type, data}, hoac None khi khong co. Chi co y nghia
        khi Content-Type la multipart/form-data."""
        if not hasattr(self, "_multipart_files"):
            _ = self.form  # kich hoat parse (cache)
        for upload in getattr(self, "_multipart_files", []):
            if upload.get("name") == name:
                return upload
        return None


class Response:
    """Response thuần: status + headers + body bytes."""

    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.headers = dict(headers or {})

    @classmethod
    def html(cls, status, html_text, extra_headers=None):
        headers = {"Content-Type": "text/html; charset=utf-8"}
        headers.update(extra_headers or {})
        return cls(status, html_text.encode("utf-8"), headers)

    @classmethod
    def text(cls, status, text, extra_headers=None):
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        headers.update(extra_headers or {})
        return cls(status, text.encode("utf-8"), headers)

    @classmethod
    def json(cls, status, payload, extra_headers=None):
        """JSON (v0.6.0, T3i — POST /center/mark-read, GET
        /api/center/unread)."""
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(extra_headers or {})
        return cls(status, json.dumps(payload, ensure_ascii=False)
                   .encode("utf-8"), headers)

    @classmethod
    def redirect(cls, location, extra_headers=None):
        headers = {"Location": location}
        headers.update(extra_headers or {})
        return cls(302, b"", headers)


def _security_headers():
    return {
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }


def _safe_compare(left, right):
    """So sánh chuỗi hằng thời gian, an toàn với chuỗi non-ASCII.

    compare_digest với str non-ASCII sẽ raise TypeError (500) — encode
    UTF-8 trước để mọi input lạ đều chỉ nhận 403.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _parse_cookies(request):
    cookies = {}
    raw = request.headers.get("cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


class UiApp:
    """Ứng dụng UI. Các runner tiêm vào được để test không cần systemd."""

    def __init__(self, root, unit_runner=None, unit_status_fn=None,
                 tailscale_fn=None, log_fn=None, verify_fn=None,
                 gateway_status_fn=None, receiver_status_fn=None):
        self.root = Path(root)
        self.unit_runner = unit_runner or self._default_unit_runner
        self.unit_status_fn = unit_status_fn or self._default_unit_status
        self.tailscale_fn = tailscale_fn or self._default_tailscale_status
        self.log_fn = log_fn or self._default_log_tail
        self.verify_fn = verify_fn or self._default_verify_token
        self.gateway_status_fn = (
            gateway_status_fn or self._default_gateway_status)
        self.receiver_status_fn = (
            receiver_status_fn or self._default_receiver_status)
        self._confirm_tokens = {}
        # Sweep hết hạn lúc khởi động (doc 09 §4.2): luồng quá 72h
        # không tiến triển được ghi nhận expired + xóa. Best-effort:
        # lỗi không được chặn UI khởi động.
        try:
            transfers.expire_stale_transfers(self.root, time.time())
        except Exception:
            pass

    # -- runner mặc định (lệnh hệ thống, list-args, không shell) -------

    def _default_unit_runner(self, action, bot_id):
        """Chạy systemctl --user cho unit tai@<bot_id>.

        Trả (returncode, stdout+stderr đã cắt). ``action`` chỉ 1 trong
        3 giá trị allowlist; bot_id đã khớp mẫu trước khi tới đây.
        """
        commands = {
            "enable": ["systemctl", "--user", "enable", "--now",
                       "tai@%s.service" % bot_id],
            "disable": ["systemctl", "--user", "disable", "--now",
                        "tai@%s.service" % bot_id],
            "restart": ["systemctl", "--user", "restart",
                        "tai@%s.service" % bot_id],
        }
        command = commands.get(action)
        if command is None:
            return 2, "action không hỗ trợ"
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 2, "không chạy được systemctl: %s" % type(exc).__name__
        output = (done.stdout or "") + (done.stderr or "")
        return done.returncode, output[:500]

    def _default_unit_status(self, bot_id):
        """Trạng thái unit: "active" / "inactive" / "unknown"."""
        try:
            done = subprocess.run(
                ["systemctl", "--user", "is-active",
                 "tai@%s.service" % bot_id],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        state = (done.stdout or "").strip()
        if done.returncode == 0 and state == "active":
            return "active"
        if state in ("inactive", "failed"):
            return "inactive"
        return "unknown"

    def _default_gateway_status(self):
        """Trạng thái unit gateway: "active"/"inactive"/"unknown".

        Chỉ dùng cho cổng lặng-fetch ở bước 1 của luồng chuyển (doc 09
        §4.3): UI đọc trạng thái unit, CẤM gọi endpoint gateway.
        """
        try:
            done = subprocess.run(
                ["systemctl", "--user", "is-active",
                 "multibot-gateway.service"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        state = (done.stdout or "").strip()
        if done.returncode == 0 and state == "active":
            return "active"
        if state in ("inactive", "failed"):
            return "inactive"
        return "unknown"

    def _default_receiver_status(self):
        """Trạng thái unit receiver: "active"/"inactive"/"unknown"
        (đợt mẫu rẻ Hades — sao y mẫu gateway, khác duy nhất tên unit).

        UI đọc trạng thái unit, CẤM đo receiver bằng cách gọi endpoint
        http://...:8766 (cùng luật đã ghi cho gateway).
        """
        try:
            done = subprocess.run(
                ["systemctl", "--user", "is-active",
                 "multibot-sync-receiver.service"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        state = (done.stdout or "").strip()
        if done.returncode == 0 and state == "active":
            return "active"
        if state in ("inactive", "failed"):
            return "inactive"
        return "unknown"

    def _status_chips_html(self):
        """Hàng chip trạng thái gateway/receiver (đợt mẫu rẻ Hades):
        luôn đủ 2 chip; mỗi chip 3 trạng thái (đang chạy/đang tắt/
        không rõ). Tái dùng class .chip sẵn có."""
        parts = []
        for label, status in (
                ("Gateway", self.gateway_status_fn()),
                ("Receiver", self.receiver_status_fn())):
            if status == "active":
                cls, text = "green", "đang chạy"
            elif status == "inactive":
                cls, text = "red", "đang tắt"
            else:
                cls, text = "gray", "không rõ"
            parts.append('<span class="chip %s">%s: %s</span>'
                         % (cls, ui_pages.esc(label), text))
        return '<div class="chips">%s</div>' % "".join(parts)

    def _default_tailscale_status(self):
        """Trạng thái Tailscale, best-effort: {"up": bool|None, "ip"}."""
        result = {"up": None, "ip": None}
        try:
            done = subprocess.run(
                ["tailscale", "status"], capture_output=True, text=True,
                timeout=3)
            if done.returncode == 0:
                result["up"] = True
            else:
                result["up"] = False
        except (OSError, subprocess.TimeoutExpired):
            return result
        try:
            done = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True,
                timeout=3)
            if done.returncode == 0:
                result["ip"] = (done.stdout or "").strip().splitlines()[0].strip()
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
        return result

    def _default_log_tail(self, bot_id, limit=50):
        """50 dòng log cuối của unit tai (journalctl, đã lược ở tầng
        hiển thị). Lỗi -> []."""
        try:
            done = subprocess.run(
                ["journalctl", "--user", "-u",
                 "tai@%s.service" % bot_id, "--no-pager", "-n", str(limit),
                 "-o", "cat"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if done.returncode != 0:
            return []
        return (done.stdout or "").splitlines()[-limit:]

    def _default_verify_token(self, platform, base_url, token):
        """Kiểm tra token qua helper riêng (tiến trình riêng, stdin).

        Trả (ok, info) như ``verify_token.verify``. Timeout 20s.
        """
        helper = Path(__file__).resolve().parent / "verify_token.py"
        try:
            done = subprocess.run(
                ["python3", str(helper), "--platform", platform,
                 "--base-url", base_url],
                input=(token + "\n").encode("utf-8"),
                capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False, {"error_code": None}
        try:
            payload = json.loads(done.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, {"error_code": None}
        if not isinstance(payload, dict):
            return False, {"error_code": None}
        if payload.get("ok") is True:
            return True, {
                "id": str(payload.get("id") or ""),
                "name": payload.get("name"),
                "username": payload.get("username"),
            }
        return False, {"error_code": payload.get("error_code")}

    # -- tiện ích -------------------------------------------------------

    def _with_security(self, response):
        for key, value in _security_headers().items():
            response.headers.setdefault(key, value)
        return self._apply_ui_theme(response)

    def _apply_ui_theme(self, response):
        """Áp theme đã lưu lên mọi trang đầy đủ (v0.6.0, T3k).

        Mọi trang đầy đủ đều qua ``layout()`` (mặc định
        ``data-theme="light"``); tầng response này viết lại giá trị
        ``data-theme`` theo khóa ``theme`` trong ``ui_prefs.json``
        (đọc 1 lần/trang đầy đủ — fragment/JSON/redirect không chạm
        file). Lưu ý (G-K2): thực tế prefs được đọc 2 lần mỗi request
        trang đầy đủ — handler đọc 1 lần + tầng này đọc thêm 1 lần —
        có chủ đích, chỉ thêm 1 lần đọc file nhỏ, không khác biệt quan
        sát được; hợp đồng frag-04 §3.2 "đọc file prefs 1 lần/request"
        được nới theo thực tế này. Theme lưu phía máy chủ là nguồn
        đúng duy nhất nên
        tầng này thắng mọi giá trị layout vẽ ra. Trang đăng nhập
        cũng theo theme đã chọn (không theo phiên). Lỗi đọc prefs
        -> giữ nguyên trang (mặc định sáng qua ``:root``).
        Vì sao không luồn theme qua ~88 điểm gọi hàm trang: tránh
        va chạm gộp với đội 1 (T3i/T3l đang sửa cùng file); hành vi
        quan sát được giống hệt thiết kế (frag-04 §3.4) — ghi rõ ở
        BAN-GIAO-T3k. ``layout(theme=...)`` vẫn là API chuẩn để
        render trực tiếp 1 trang với theme cho trước.
        """
        try:
            body = response.body
        except Exception:
            return response
        if not isinstance(body, (bytes, bytearray)):
            return response
        prefix = b'<!DOCTYPE html>\n<html lang="vi" data-theme="'
        if not body.startswith(prefix):
            # Trang đầy đủ cũ (không qua layout mới): thêm thuộc tính.
            plain = b'<!DOCTYPE html>\n<html lang="vi">'
            if not body.startswith(plain):
                return response
            try:
                theme = ui_store.load_ui_theme(self.root)
            except Exception:
                return response
            data_theme = ui_pages.ui_data_theme(theme)
            response.body = (
                b'<!DOCTYPE html>\n<html lang="vi" data-theme="'
                + data_theme.encode("ascii") + b'">'
                + body[len(plain):])
            return response
        try:
            theme = ui_store.load_ui_theme(self.root)
        except Exception:
            return response
        data_theme = ui_pages.ui_data_theme(theme)
        end = body.find(b'">', len(prefix))
        if end < 0:
            return response
        response.body = (prefix + data_theme.encode("ascii")
                         + body[end:])
        return response

    def _session(self, request):
        cookies = _parse_cookies(request)
        token = cookies.get(SESSION_COOKIE)
        if not token:
            return None, None
        session = ui_auth.get_session(self.root, token)
        if session is None:
            return None, None
        return session, token

    def _require_session(self, request):
        """Trả (session, token, error_response)."""
        session, token = self._session(request)
        if session is None:
            if request.path.startswith("/fragments/"):
                return None, None, self._with_security(
                    Response.text(401, "unauthorized"))
            return None, None, self._with_security(
                Response.redirect("/login"))
        return session, token, None

    def _origin_ok(self, request):
        """Kiểm tra yêu cầu POST đến từ cùng site.

        Tín hiệu chính là Sec-Fetch-Site (fetch metadata): trình duyệt
        hiện đại tự đặt, mã JS chéo site không thể giả mạo. Đã kiểm chứng
        thực nghiệm 15/11/2026: form navigation trong một số môi trường
        webview gửi `Origin: null` dù cùng origin (sec_site=same-origin),
        nên chỉ khi THIẾU Sec-Fetch-Site (client cũ, curl) mới so Origin
        với Host theo cách cũ.
        """
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site:
            return fetch_site in ("same-origin", "same-site", "none")
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            allowed = {"http://" + host, "https://" + host}
            if origin not in allowed:
                return False
        return True

    def _csrf_ok(self, request, session):
        """CSRF token theo phiên + kiểm tra Origin/Sec-Fetch-Site."""
        token = request.form_value("csrf_token")
        if not token:
            return False
        if not _safe_compare(token, session.get("csrf_token", "")):
            return False
        return self._origin_ok(request)

    def _csrf_error(self, session):
        return self._with_security(Response.html(
            403,
            ui_pages.simple_page_html(
                "Yêu cầu không hợp lệ",
                "<h1>Yêu cầu không hợp lệ</h1>"
                "<p>Phiên làm việc có thể đã hết hạn hoặc yêu cầu không "
                "đến từ trang này. Quay lại trang trước và thử lại.</p>"
                '<p><a href="/">Về màn hình chính</a></p>',
                csrf_token=session.get("csrf_token")),
        ))

    def _not_found(self, session=None):
        csrf = session.get("csrf_token") if session else None
        return self._with_security(Response.html(
            404,
            ui_pages.simple_page_html(
                "Không tìm thấy",
                "<h1>Không tìm thấy</h1>"
                "<p>Trang hoặc bot này không tồn tại.</p>"
                '<p><a href="/">Về màn hình chính</a></p>',
                csrf_token=csrf),
        ))

    def _method_not_allowed(self, session):
        return self._with_security(Response.html(
            405,
            ui_pages.simple_page_html(
                "Phương thức không hỗ trợ",
                "<h1>Phương thức không hỗ trợ</h1>"
                "<p>Thao tác này chỉ chấp nhận form POST.</p>"
                '<p><a href="/">Về màn hình chính</a></p>',
                csrf_token=session.get("csrf_token")),
        ))

    # -- confirm token (bộ nhớ, hết hạn 5 phút, ràng buộc phiên) --------

    def _prune_confirm_tokens(self, now):
        expired = [key for key, item in self._confirm_tokens.items()
                   if item["expires_at"] <= now]
        for key in expired:
            del self._confirm_tokens[key]
        while len(self._confirm_tokens) > MAX_CONFIRM_TOKENS:
            oldest = min(self._confirm_tokens,
                         key=lambda k: self._confirm_tokens[k]["expires_at"])
            del self._confirm_tokens[oldest]

    def _issue_confirm_token(self, session_token, payload):
        now = time.time()
        self._prune_confirm_tokens(now)
        token = secrets.token_urlsafe(32)
        import hashlib

        fingerprint = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        self._confirm_tokens[token] = {
            "fingerprint": fingerprint,
            "payload": payload,
            "expires_at": now + CONFIRM_TTL_SECONDS,
        }
        return token

    def _consume_confirm_token(self, session_token, token):
        """Trả payload nếu token còn hạn + đúng phiên, ngược lại None.
        Token chỉ dùng 1 lần."""
        import hashlib

        now = time.time()
        self._prune_confirm_tokens(now)
        item = self._confirm_tokens.pop(token, None)
        if item is None:
            return None
        fingerprint = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        if not _safe_compare(fingerprint, item["fingerprint"]):
            return None
        return item["payload"]

    # -- khóa theo bot + audit + ghi secret ------------------------------

    @contextlib.contextmanager
    def _bot_lock(self, bot_id):
        locks_dir = self.root / "ui-config" / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / ("%s.lock" % bot_id)
        handle = open(lock_path, "a", encoding="utf-8")
        deadline = time.time() + BOT_LOCK_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise TimeoutError("bot đang bận")
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()

    def _audit(self, client_ip, action, bot_id, before, after, result,
               task_id=None, account_id=None):
        """Ghi 1 dòng audit append-only (600). Best-effort: lỗi ghi
        không chặn thao tác. Không secret, không nội dung tin.
        ``task_id`` chỉ dùng cho dòng trung tâm (TS3); ``account_id``
        chỉ dùng cho dòng quản lý tài khoản / cấp-xoay secret trung tâm
        (TS3: dòng cấp/xoay ghi account_id + thời điểm, KHÔNG giá trị
        secret) — khóa chỉ thêm khi có giá trị để dòng cũ không đổi.
        (T3j: thân hàm ở ui_store.append_audit_line để CLI đặt mật
        khẩu xem mã ghi được cùng 1 định dạng.)"""
        ui_store.append_audit_line(
            self.root, client_ip, action, bot_id, before, after,
            result, task_id=task_id, account_id=account_id)

    def _write_secret_file(self, bot_id, suffix, content):
        """Ghi file secret nguyên tử (tmp + fsync + rename), quyền 600.
        UI chỉ GHI secret, không bao giờ đọc lại."""
        secrets_dir = self.root / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        path = secrets_dir / ("%s.%s" % (bot_id, suffix))
        tmp_path = secrets_dir / (".%s.%s.tmp" % (bot_id, suffix))
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)

    def _remove_secret_file(self, bot_id, suffix):
        try:
            os.unlink(self.root / "secrets" / ("%s.%s" % (bot_id, suffix)))
        except OSError:
            pass

    # -- dữ liệu chung ---------------------------------------------------

    def _summaries(self, now):
        registry = ui_store.load_registry(self.root)
        fetch_state = ui_store.read_fetch_state(self.root)
        accounts = ui_store.read_accounts(self.root)
        transfers_data = ui_store.read_transfers(self.root)
        warn_fraction = ui_store.load_alert_settings(self.root)
        summaries = []
        for bot_id in sorted(registry.get("bots", {})):
            entry = registry["bots"][bot_id]
            unit_state = self.unit_status_fn(bot_id)
            summaries.append(ui_store.bot_summary(
                self.root, bot_id, entry, now=now, unit_state=unit_state,
                fetch_state=fetch_state, accounts=accounts,
                transfers_data=transfers_data,
                quota_warn_fraction=warn_fraction))
        return summaries

    def _accounts_list(self):
        """List (account_id, display_name) xếp theo tên, cho select/lọc."""
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        return sorted(
            ((aid, record["display_name"]) for aid, record in account_map.items()),
            key=lambda pair: pair[1].lower())

    def _active_transfer(self, bot_id, now=None):
        """Luồng chuyển đang chạy (C6) của bot, hoặc None."""
        if now is None:
            now = time.time()
        return ui_store.active_transfer_for_bot(
            ui_store.read_transfers(self.root), bot_id, now)

    def _registry_entry(self, bot_id):
        registry = ui_store.load_registry(self.root)
        bots = registry.get("bots") or {}
        if not isinstance(bots, dict):
            return None
        entry = bots.get(bot_id)
        return entry if isinstance(entry, dict) else None

    # -- dispatcher ------------------------------------------------------

    def handle(self, request):
        """Điều phối request -> Response (luôn gắn header bảo mật)."""
        path = request.path
        if path == "/static/app.css" and request.method == "GET":
            return self._with_security(Response(
                200, ui_static.APP_CSS.encode("utf-8"),
                {"Content-Type": "text/css; charset=utf-8",
                 "Cache-Control": "no-store"}))
        if path == "/static/app.js" and request.method == "GET":
            return self._with_security(Response(
                200, ui_static.APP_JS.encode("utf-8"),
                {"Content-Type": "text/javascript; charset=utf-8",
                 "Cache-Control": "no-store"}))
        if path == "/favicon.ico" and request.method == "GET":
            # Trả 204 thay vì để favicon rơi vào _require_session -> 302
            # /login (trước đây trình duyệt load lại trang login trong nền,
            # xoay cookie CSRF và làm chết form đang mở).
            return self._with_security(Response(
                204, b"",
                {"Cache-Control": "public, max-age=604800"}))
        if path == "/login":
            return self._handle_login(request)
        if path == "/logout":
            return self._handle_logout(request)
        session, session_token, error = self._require_session(request)
        if error is not None:
            return error
        if path == "/" and request.method == "GET":
            return self._handle_dashboard(request, session)
        if path == "/fragments/dashboard" and request.method == "GET":
            return self._handle_dashboard_fragment(request, session)
        if path == "/fragments/history" and request.method == "GET":
            return self._handle_history_fragment(request, session)
        if path == "/fragments/log" and request.method == "GET":
            return self._handle_log_fragment(request, session)
        if path == "/fragments/center-chat" and request.method == "GET":
            return self._handle_center_chat_fragment(request, session)
        if path == "/alerts/ack":
            return self._handle_alert_ack(request, session)
        if path == "/settings":
            return self._handle_settings(request, session)
        if path == "/api/ui-prefs":
            return self._handle_ui_prefs(request, session)
        if path == "/api/settings/center":
            return self._handle_settings_center(request, session)
        if path == "/api/settings/alerts":
            return self._handle_settings_alerts(request, session)
        if path == "/api/prune-now":
            return self._handle_prune_now(request, session)
        if path == "/center/tasks":
            return self._handle_center_create_task(request, session)
        if path == "/center/messages":
            return self._handle_center_message(request, session)
        if path == "/center/mark-read":
            return self._handle_center_mark_read(request, session)
        if path == "/api/center/unread":
            return self._handle_center_unread(request, session)
        if path == "/center/room":
            return self._handle_center_room(request, session)
        match = _ROUTE_CENTER_FILE.match(path)
        if match:
            return self._handle_center_file_download(
                request, session, match.group(1))
        match = _ROUTE_CENTER_ROTATE.match(path)
        if match:
            return self._handle_center_rotate_secret(
                request, session, session_token, match.group(1),
                match.group(2) or "")
        match = _ROUTE_CENTER_VIEW_SECRET.match(path)
        if match:
            return self._handle_center_view_secret(
                request, session, session_token, match.group(1),
                match.group(2) or "")
        match = _ROUTES_CENTER_TASK_STATUS.match(path)
        if match:
            return self._handle_center_task_status(
                request, session, match.group(1))
        match = _ROUTES_CENTER_TASK_REASSIGN.match(path)
        if match:
            return self._handle_center_task_reassign(
                request, session, match.group(1))
        match = _ROUTES_CENTER_TASK_DUE.match(path)
        if match:
            return self._handle_center_task_set_due(
                request, session, match.group(1))
        match = _ROUTES_API_TASK_DUE.match(path)
        if match:
            return self._handle_center_task_set_due(
                request, session, match.group(1))
        match = _ROUTES_API_BOT_RETRY_DEAD.match(path)
        if match:
            return self._handle_retry_dead(
                request, session, session_token, match.group(1))
        match = _ROUTES_CENTER_TASK.match(path)
        if match:
            return self._handle_center_task_detail(
                request, session, match.group(1))
        if path == "/bots/new":
            return self._handle_bot_new(request, session)
        if path == "/accounts" and request.method == "GET":
            return self._handle_accounts(request, session)
        if path == "/accounts/new":
            return self._handle_account_new(request, session)
        account_match = _ROUTE_ACCOUNT.match(path)
        if account_match:
            account_id = account_match.group(1)
            rest = account_match.group(2) or ""
            return self._handle_account_route(
                request, session, session_token, account_id, rest)
        match = _ROUTE_BOT.match(path)
        if match:
            bot_id = match.group(1)
            rest = match.group(2) or ""
            return self._handle_bot_route(request, session, session_token,
                                          bot_id, rest)
        return self._not_found(session)

    # -- đăng nhập / đăng xuất --------------------------------------------

    def _render_login(self, request=None, error=None, status=200):
        """Trang đăng nhập với login_csrf + cookie khớp (double-submit).

        Nếu request đã mang cookie login_csrf còn sống thì TÁI DÙNG token
        đó cho form thay vì xoay mới: mọi lần re-render (favicon, tab thứ
        hai, back-button, submit sai mật khẩu) không làm chết form đang
        mở. Chỉ xoay token khi chưa có cookie (lần đầu mở trang, hoặc
        cookie đã hết hạn 10 phút). Khi tái dùng KHÔNG gửi Set-Cookie để
        giữ nguyên thời hạn cookie và không echo giá trị client tự đặt
        vào header.
        """
        cookies = _parse_cookies(request) if request is not None else {}
        existing = cookies.get(LOGIN_CSRF_COOKIE, "")
        if existing:
            login_csrf = existing
            extra_headers = {}
        else:
            login_csrf = secrets.token_urlsafe(32)
            extra_headers = {"Set-Cookie": (
                "%s=%s; Path=/login; HttpOnly; Secure; SameSite=Strict; "
                "Max-Age=600" % (LOGIN_CSRF_COOKIE, login_csrf))}
        return self._with_security(Response.html(
            status,
            ui_pages.login_page_html(error=error, login_csrf=login_csrf),
            extra_headers))

    def _handle_login(self, request):
        if request.method == "GET":
            return self._render_login(request)
        if request.method != "POST":
            return self._with_security(Response.text(405, "method not allowed"))
        # Double-submit CSRF: form phải khớp cookie đăng nhập.
        cookies = _parse_cookies(request)
        form_csrf = request.form_value("login_csrf")
        cookie_csrf = cookies.get(LOGIN_CSRF_COOKIE, "")
        if not form_csrf or not cookie_csrf or not _safe_compare(
                form_csrf, cookie_csrf) or not self._origin_ok(request):
            # Log chẩn đoán dài hạn (chỉ metadata, không token/cookie):
            # giúp phân biệt CSRF thật (cross-site) với môi trường gửi
            # Origin:null đã biết.
            sys.stderr.write(
                "CSRF-FAIL: has_form=%s has_cookie=%s match=%s "
                "origin_ok=%s sec_site=%s sec_mode=%s sec_dest=%s "
                "host=%s origin=%s ua=%s\n" % (
                    bool(form_csrf), bool(cookie_csrf),
                    bool(form_csrf and cookie_csrf
                         and _safe_compare(form_csrf, cookie_csrf)),
                    self._origin_ok(request),
                    request.headers.get("sec-fetch-site", "-"),
                    request.headers.get("sec-fetch-mode", "-"),
                    request.headers.get("sec-fetch-dest", "-"),
                    request.headers.get("host", "-"),
                    request.headers.get("origin", "-"),
                    request.headers.get("user-agent", "-")[:80]))
            return self._render_login(
                request,
                error="Không khớp mã bảo mật trang đăng nhập. Tải lại trang.",
                status=403)
        if not ui_auth.login_allowed(self.root, request.client_ip):
            return self._render_login(
                request,
                error="Thử quá nhiều lần. Đợi 15 phút rồi thử lại.",
                status=429)
        password = request.form_value("password")
        stored = ui_auth.load_password_hash(self.root)
        if stored is None:
            return self._with_security(Response.html(
                500,
                ui_pages.simple_page_html(
                    "Chưa đặt mật khẩu",
                    "<h1>Chưa đặt mật khẩu</h1>"
                    "<p>Giao diện chưa có mật khẩu. Chạy lệnh đặt mật khẩu "
                    "trên máy tính rồi mở lại trang này.</p>",
                    csrf_token=None)))
        if password and ui_auth.verify_password(password, stored):
            ui_auth.record_login_success(self.root, request.client_ip)
            token, _csrf = ui_auth.create_session(
                self.root, ip=request.client_ip)
            cookie = (
                "%s=%s; Path=/; HttpOnly; Secure; SameSite=Strict; "
                "Max-Age=%d" % (SESSION_COOKIE, token,
                                ui_auth.SESSION_LIFETIME_SECONDS))
            return self._with_security(Response.redirect(
                "/", {"Set-Cookie": cookie}))
        ui_auth.record_login_failure(self.root, request.client_ip)
        return self._render_login(request, error="Mật khẩu chưa đúng.")

    def _handle_logout(self, request):
        if request.method != "POST":
            session, _, error = self._require_session(request)
            if error is not None:
                return error
            return self._method_not_allowed(session)
        session, session_token, error = self._require_session(request)
        if error is not None:
            return error
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        ui_auth.revoke_session(self.root, session_token)
        clear = (
            "%s=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"
            % SESSION_COOKIE)
        return self._with_security(Response.redirect(
            "/login", {"Set-Cookie": clear}))

    # -- màn hình chính (tab V2) ------------------------------------------

    _VALID_TABS = ("all", "center", "chat", "zalo", "telegram", "alerts")
    _VALID_DIRS = ("all", "in", "out", "attention")
    _VALID_VIEWS = ("open", "history")
    _VALID_STATUSES = ("", "new", "fetched", "sent", "failed", "dead")
    _VALID_CENTER_STATUSES = ("", "moi", "da-nhan", "dang-lam",
                              "da-bao-xong", "da-xac-nhan", "huy")
    _VALID_CENTER_KINDS = ("", "app", "nghien-cuu", "viet", "vanhanh",
                           "khac")

    @staticmethod
    def _dashboard_params(request):
        """Tham số tab/lọc từ query, đã kiểm chứng giá trị."""
        query = request.query

        def _one(key, default=""):
            values = query.get(key)
            return values[0] if values else default

        tab = _one("tab", "all")
        # (L6, 21/11 — quyet chu nhan) Tab sai ten KHONG sua am
        # tham: van rot ve "all" nhung ghi co ``tab_invalid`` de
        # luot tai trang day du redirect 302 ve
        # ?tab=all&notice=tab-khong-hop-le; duong fragment (polling)
        # giu fallback lang le nhu cu (khong redirect 302).
        tab_invalid = tab not in UiApp._VALID_TABS
        if tab_invalid:
            tab = "all"
        direction = _one("dir", "all")
        if direction not in UiApp._VALID_DIRS:
            direction = "all"
        view = _one("view", "open")
        if view not in UiApp._VALID_VIEWS:
            view = "open"
        platform = _one("platform", "")
        if platform not in ("", "zalo", "telegram"):
            platform = ""
        bot = _one("bot", "")
        if bot and not _valid_bot_id(bot):
            bot = ""
        account = _one("account", "")
        if account and not ui_store.ACCOUNT_ID_RE.match(account):
            account = ""
        center_status = _one("status", "")
        if center_status not in UiApp._VALID_CENTER_STATUSES:
            center_status = ""
        center_kind = _one("kind", "")
        if center_kind not in UiApp._VALID_CENTER_KINDS:
            center_kind = ""
        overdue = _one("overdue", "")
        if overdue not in ("", "1"):
            overdue = ""
        need_owner = _one("need_owner", "")
        if need_owner not in ("", "1"):
            need_owner = ""
        try:
            chat_limit = int(_one("chat_limit", "50"))
        except ValueError:
            chat_limit = 50
        if chat_limit < 50:
            chat_limit = 50
        if chat_limit > 500:
            chat_limit = 500
        # Phong chat trung tam (v0.3.0, T3c): mac dinh "chung"; sai
        # mau -> ve khung chung (khong 400 — query cua nguoi dung).
        room = _one("room", "chung")
        if not center_store.ROOM_RE.match(room):
            room = "chung"
        return {"tab": tab, "dir": direction, "view": view,
                "platform": platform, "bot": bot, "account": account,
                "q": _one("q", "")[:200], "status": center_status,
                "kind": center_kind, "overdue": overdue,
                "need_owner": need_owner, "chat_limit": chat_limit,
                "room": room, "tab_invalid": tab_invalid}

    @staticmethod
    def _dashboard_query(params):
        """Tham số query giữ nguyên chế độ xem hiện tại (dùng chung
        cho URL fragment polling và URL trang đầy đủ)."""
        frag = {"tab": params["tab"]}
        if params["bot"]:
            frag["bot"] = params["bot"]
        if params.get("account"):
            frag["account"] = params["account"]
        if params["dir"] != "all":
            frag["dir"] = params["dir"]
        if params["q"]:
            frag["q"] = params["q"]
        if params["view"] != "open":
            frag["view"] = params["view"]
        if params["platform"]:
            frag["platform"] = params["platform"]
        if params["tab"] == "center":
            if params.get("status"):
                frag["status"] = params["status"]
            if params.get("kind"):
                frag["kind"] = params["kind"]
            if params.get("overdue"):
                frag["overdue"] = params["overdue"]
            if params.get("room") and params["room"] != "chung":
                frag["room"] = params["room"]
        if params["tab"] == "chat":
            if params.get("room") and params["room"] != "chung":
                frag["room"] = params["room"]
        if params["tab"] == "alerts" and params.get("need_owner"):
            frag["need_owner"] = params["need_owner"]
        return frag

    @staticmethod
    def _fragment_url(params):
        """URL fragment polling giữ nguyên chế độ xem hiện tại."""
        return "/fragments/dashboard?" + urlencode(
            UiApp._dashboard_query(params))

    @staticmethod
    def _dashboard_url(params):
        """URL trang đầy đủ giữ nguyên chế độ xem (đích redirect của
        toggle node — về đúng trang/tab đang xem, không về fragment)."""
        return "/?" + urlencode(UiApp._dashboard_query(params))

    def _dashboard_fragment(self, session, request, params=None):
        now = time.time()
        if params is None:
            params = self._dashboard_params(request)
        # Prefs đọc 1 lần/request (DESIGN §8.1), truyền xuống các hàm
        # render — không đọc lặp trong vòng lặp bot.
        prefs = ui_store.load_ui_prefs(self.root)
        next_url = self._dashboard_url(params)
        summaries = ui_store.sort_summaries(self._summaries(now))
        ui_store.update_alert_state(self.root, summaries, now)
        for summary in summaries:
            ui_store.record_poll_sample(self.root, summary["bot_id"],
                                        summary, now)
        unseen = ui_store.count_unseen_alerts(self.root, summaries, now)
        open_list = ui_store.open_alerts(self.root, summaries, now)
        names = {s["bot_id"]: s["display_name"] for s in summaries}
        platforms = {s["bot_id"]: s["platform"] for s in summaries}
        owners = {s["bot_id"]: s.get("owner_account") for s in summaries}
        accounts = self._accounts_list()
        center_health = self._center_health()
        # (v0.6.0, T3i) Huy hieu tab Chat: tong chua doc + "@" — tinh
        # 1 lan moi lan render fragment (cung nhip polling 5 giay).
        try:
            _center_unread = self._center_store().room_unread_counts()
            _center_mention = self._center_store() \
                .rooms_with_unread_owner_mention()
        except OSError:
            _center_unread = {}
            _center_mention = set()
        nav = ui_pages.tabs_nav_html(
            params["tab"], len(open_list),
            center_health.get("overdue_tasks", 0),
            center_unread_total=sum(_center_unread.values()),
            center_has_mention=bool(_center_mention))
        # Hàng chip trạng thái gateway/receiver (đợt mẫu rẻ Hades):
        # tính 1 lần, dùng chung cho cả 3 đường return.
        chips_html = self._status_chips_html()
        tab = params["tab"]
        account_filter = params.get("account", "")
        if account_filter:
            open_list = [a for a in open_list
                         if owners.get(a["bot_id"]) == account_filter]
            summaries = [s for s in summaries
                         if s.get("owner_account") == account_filter]
        if tab == "center":
            return chips_html + nav + self._center_fragment(params, now)
        if tab == "chat":
            # Tab Chat: phan fragment chi co thanh tab (huy hieu
            # cap nhat theo polling 5 giay); khung chat + phong +
            # o nhap render TINH o _handle_dashboard (ngoai
            # fragment) — JS tu polling delta rieng cho hop chat.
            return nav
        if tab == "alerts":
            filtered_open = [
                alert for alert in open_list
                if (not params["bot"] or alert["bot_id"] == params["bot"])
                and (not params["platform"]
                     or alert["platform"] == params["platform"])]
            if params.get("need_owner") == "1":
                filtered_open = [
                    alert for alert in filtered_open
                    if ui_store.alert_needs_owner(alert)]
            # Node Lịch sử sự kiện đóng -> bỏ qua lệnh đọc của node đó
            # (DESIGN §8.1); tóm tắt chỉ còn tên node, không số.
            if params["view"] == "history" and prefs.get(
                    "event-history-30d"):
                events = []
            else:
                events = ui_store.read_alert_events(
                    self.root, now, bot_id=params["bot"] or None)
            if params["platform"]:
                events = [event for event in events
                          if platforms.get(event["bot_id"])
                          == params["platform"]]
            if account_filter:
                events = [event for event in events
                          if owners.get(event["bot_id"]) == account_filter]
            node_summaries = {}
            if prefs.get("open-alerts"):
                node_summaries["open-alerts"] = self._open_alerts_summary(
                    filtered_open)
            if prefs.get("event-history-30d"):
                # Lech T3 (b), T3m: tom tat "N dong gan nhat ·
                # moi nhat HH:MM" — dung du lieu da doc san khi co;
                # lenh doc da bi bo qua (node dong + xem lich su)
                # thi dem nhe, khong goi read_alert_events.
                if events:
                    newest = max((e.get("ts") or 0) for e in events)
                    node_summaries["event-history-30d"] = (
                        self._history_lines_summary(len(events), newest))
                else:
                    count, newest = ui_store.alert_events_totals(
                        self.root, now, bot_id=params["bot"] or None)
                    node_summaries["event-history-30d"] = (
                        self._history_lines_summary(count, newest))
            elif events:
                node_summaries["event-history-30d"] = (
                    self._alert_events_summary(events))
            return chips_html + nav + ui_pages.alerts_fragment_html(
                filtered_open, events, names, platforms,
                session["csrf_token"], params, prefs=prefs,
                next_url=next_url, node_summaries=node_summaries,
                center_stuck=self._center_stuck_items(now),
                center_discussion_stopped=(
                    self._center_stopped_discussion_items()),
                center_view_overdue=[
                    item for item in
                    ui_store.center_view_pending_items(self.root, now)
                    if item["overdue_seconds"]
                    > ui_store.CENTER_VIEW_ROTATION_DEADLINE_SECONDS])
        if tab in ("zalo", "telegram"):
            tab_summaries = [s for s in summaries if s["platform"] == tab]
        else:
            tab_summaries = summaries
        bot_ids = [s["bot_id"] for s in tab_summaries]
        if params["bot"]:
            feed_ids = [params["bot"]] if params["bot"] in bot_ids else []
        else:
            feed_ids = bot_ids
        direction = {"in": "in", "out": "out"}.get(params["dir"])
        # Node Hoạt động đóng -> bỏ qua activity_feed (DESIGN §8.1);
        # dải số vẫn cần cho tóm tắt (count_stats là lệnh đếm nhẹ).
        if prefs.get("activity"):
            feed = []
        else:
            feed = ui_store.activity_feed(
                self.root, feed_ids, now=now, limit=ui_store.ACTIVITY_LIMIT,
                direction=direction,
                attention_only=params["dir"] == "attention",
                query=params["q"] or None)
        stats = ui_store.count_stats(self.root, bot_ids, now=now)
        node_summaries = self._dashboard_node_summaries(
            prefs, stats, tab_summaries, feed)
        # L7: 1 banner chung "đồng bộ trả lời chậm" trên node
        # bot-cards — tính 1 lần từ đúng danh sách bot của tab đang
        # render (tab Tất cả: 5/5, tab nền tảng: chỉ bot nền tảng đó).
        sync_banner = ui_pages.sync_slow_banner_html(tab_summaries)
        if tab in ("zalo", "telegram"):
            body = ui_pages.platform_fragment_html(
                tab, tab_summaries, feed, session["csrf_token"], stats,
                names, params, accounts=accounts, prefs=prefs,
                next_url=next_url, node_summaries=node_summaries,
                sync_banner=sync_banner)
        else:
            tailscale = self.tailscale_fn()
            body = ui_pages.overview_fragment_html(
                tab_summaries, feed, unseen, tailscale,
                session["csrf_token"], stats, names, params,
                accounts=accounts, prefs=prefs, next_url=next_url,
                node_summaries=node_summaries,
                sync_banner=sync_banner)
        return chips_html + nav + body

    @staticmethod
    def _hhmm(epoch):
        """Giờ:phút theo giờ địa phương của PC."""
        if epoch is None:
            return None
        try:
            return time.strftime("%H:%M", time.localtime(epoch))
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _history_lines_summary(count, newest):
        """Tom tat 2 node lich su (lech T3 (b), T3m): "N dong gan
        nhat · moi nhat HH:MM"; chua co dong nao thi cau rong."""
        if not count:
            return "Chưa có sự kiện cảnh báo nào trong 30 ngày"
        text = "%d dòng gần nhất" % count
        hhmm = UiApp._hhmm(newest) if newest else None
        if hhmm:
            text += " · mới nhất %s" % hhmm
        return text

    @staticmethod
    def _open_alerts_summary(open_list):
        """Tóm tắt node Cảnh báo đang mở: N đang mở — mới nhất HH:MM."""
        if not open_list:
            return "Không có cảnh báo nào đang mở"
        newest = max((a.get("since") or 0) for a in open_list)
        text = "%d đang mở" % len(open_list)
        hhmm = UiApp._hhmm(newest) if newest else None
        if hhmm:
            text += " — mới nhất %s" % hhmm
        return text

    @staticmethod
    def _alert_events_summary(events):
        """Tóm tắt node Lịch sử sự kiện: N sự kiện — mới nhất HH:MM."""
        if not events:
            return "Chưa có sự kiện cảnh báo nào trong 30 ngày"
        newest = max((e.get("ts") or 0) for e in events)
        text = "%d sự kiện 30 ngày" % len(events)
        hhmm = UiApp._hhmm(newest) if newest else None
        if hhmm:
            text += " — mới nhất %s" % hhmm
        return text

    def _dashboard_node_summaries(self, prefs, stats, summaries, feed):
        """Tóm tắt các node ĐÓNG ở tab chính/nền tảng — chỉ dùng dữ
        liệu đã đọc sẵn trong request (không đọc thêm file nặng).

        Lưu ý (lệch nhỏ so với bảng tóm tắt SPEC TT11): "mới nhất
        HH:MM" của Hoạt động lấy từ tin đến mới nhất trong summaries
        (tin trả lời GĐ3b có thể mới hơn nhưng không nằm trong summary
        — vẫn đúng ở GĐ3a, nơi hoạt động chỉ gồm tin đến)."""
        out = {}
        if prefs.get("five-stats"):
            out["five-stats"] = ("5 số — hôm nay %s tin đến"
                                 % ui_pages._fmt_num(
                                     stats.get("today_in", 0)))
        if prefs.get("bot-cards"):
            text = "%d bot" % len(summaries)
            newest = self._newest_bot_created()
            if newest is not None:
                name, created = newest
                out["bot-cards"] = "%s · mới nhất: %s (thêm ngày %s)" % (
                    text, name, created)
            else:
                out["bot-cards"] = text
        if prefs.get("activity"):
            text = "30 dòng gần nhất"
            newest = None
            for summary in summaries:
                last = (summary.get("last_message") or {}).get("ts")
                if isinstance(last, (int, float)) and (
                        newest is None or last > newest):
                    newest = last
            hhmm = self._hhmm(newest) if newest else None
            if hhmm:
                text += " — mới nhất %s" % hhmm
            out["activity"] = text
        return out

    def _newest_bot_created(self):
        """(tên, DD/MM) của bot thêm gần nhất theo created_at trong
        registry; None khi không có created_at nào."""
        try:
            registry = ui_store.load_registry(self.root)
        except OSError:
            return None
        best = None
        for entry in (registry.get("bots") or {}).values():
            created = entry.get("created_at") if isinstance(entry, dict) \
                else None
            name = entry.get("display_name") if isinstance(entry, dict) \
                else None
            if not isinstance(created, str) or not created:
                continue
            if best is None or created > best[0]:
                best = (created, name or "")
        if best is None:
            return None
        created, name = best
        day = created[8:10] + "/" + created[5:7] \
            if len(created) >= 10 else created
        return name, day

    def _handle_dashboard(self, request, session, center_error=None,
                          center_values=None):
        params = self._dashboard_params(request)
        # (L6, 21/11 — quyet chu nhan) Luot tai trang day du ma tab
        # sai ten: redirect 302 ve tab Tất cả kem notice 1 lan.
        # Khong redirect khi dang hien loi form tao viec
        # (center_error) de khong lam mat loi; duong fragment
        # (polling) khong qua day nen van fallback lang le ve all.
        if params.get("tab_invalid") and center_error is None:
            return self._with_security(Response.redirect(
                "/?tab=all&notice=tab-khong-hop-le"))
        # Banner chi hien khi URL co notice hop le (query param
        # 1 lan sau redirect); tai lai trang khong co notice thi
        # khong hien lai. Chi nhan dung gia tri da biet — khong
        # in bat ky gia tri notice nao khac ra HTML.
        notice_values = request.query.get("notice")
        notice = notice_values[0] if notice_values else ""
        notice_banner = ""
        if notice == "tab-khong-hop-le":
            notice_banner = (
                '<div class="banner">Tab không tồn tại — đã chuyển '
                "về tab Tất cả.</div>")
        if center_error is not None:
            # Form tạo việc lỗi: ở lại tab Trung tâm để hiện rõ lỗi.
            params = dict(params)
            params["tab"] = "center"
        fragment = self._dashboard_fragment(session, request, params=params)
        fragment_url = self._fragment_url(params)
        static_html = ""
        if params["tab"] == "center":
            static_html = self._center_static_html(
                session, params, center_error=center_error,
                center_values=center_values)
        elif params["tab"] == "chat":
            static_html = self._center_chat_static_html(
                session, params, request)
        return self._with_security(Response.html(
            200,
            ui_pages.dashboard_page_html(fragment, session["csrf_token"],
                                         fragment_url=fragment_url,
                                         static_html=static_html,
                                         notice_banner=notice_banner)))

    def _handle_dashboard_fragment(self, request, session):
        fragment = self._dashboard_fragment(session, request)
        return self._with_security(Response.html(200, fragment))

    # -- trung tâm (T3, v0.2.3) --------------------------------------------

    def _center_store(self):
        return center_store.CenterStore(self.root)

    def _center_health(self):
        """Số đếm trung tâm cho huy hiệu quá hạn; kho chưa có -> 0."""
        try:
            return self._center_store().center_health()
        except OSError:
            return {"accounts": 0, "open_tasks": 0, "overdue_tasks": 0,
                    "latest_seq": 0, "last_write_at": None}

    def _center_stuck_items(self, now):
        """Muc trung tam o tab Canh bao (v0.6.0, T3i — D4): 1 dong moi
        phong chat tac (muc "can xem"), kem ten phong de hien. Du
        lieu tu ``ui_store.center_chat_stuck_alerts`` (file T3h neu
        co, khong thi tinh truc tiep tren PC — xem ham do)."""
        try:
            stuck = ui_store.center_chat_stuck_alerts(self.root, now)
        except OSError:
            return []
        if not stuck:
            return []
        try:
            rooms = self._center_store().list_rooms(
                center_store.read_accounts(self.root))
        except OSError:
            rooms = []
        labels = {}
        for entry in rooms:
            room = entry["room"]
            if entry.get("account_id") is None:
                labels[room] = "Khung chung"
            else:
                labels[room] = "Riêng: %s" % (
                    entry.get("display_name") or entry["account_id"])
        return [{
            "room": item["room"],
            "label": labels.get(item["room"], item["room"]),
            "waiting_count": item["waiting_count"],
            "oldest_wait_seconds": item.get("oldest_wait_seconds"),
            "level": item["level"],
        } for item in stuck]

    def _center_stopped_discussion_items(self):
        """Vong thao luan da dung cho tab Canh bao (v0.6.0, T3l —
        doc 10 §3.8 §4b): 1 dong muc "thong tin" moi vong dung, kem
        ten phong + ten nguoi tham gia de hien. Du lieu tu
        ``ui_store.center_discussion_stopped_rounds`` (tu tinh lai
        tu kho hub tren PC — xem ham do); dung chung mau dong canh
        bao chat tac cua T3i, khong tao pipeline canh bao moi, khong
        vao huy hieu tab, khong am thanh."""
        try:
            stopped = ui_store.center_discussion_stopped_rounds(
                self.root)
        except OSError:
            return []
        if not stopped:
            return []
        try:
            rooms = self._center_store().list_rooms(
                center_store.read_accounts(self.root))
        except OSError:
            rooms = []
        labels = {}
        for entry in rooms:
            room = entry["room"]
            if entry.get("account_id") is None:
                labels[room] = "Khung chung"
            else:
                labels[room] = "Riêng: %s" % (
                    entry.get("display_name") or entry["account_id"])
        names = self._center_account_names()
        return [{
            "room": item["room"],
            "label": labels.get(item["room"], item["room"]),
            "names": ", ".join(
                names.get(account_id, account_id)
                for account_id in item.get("participants") or []),
            "count": item["count"],
            "level": ui_store.CENTER_DISCUSSION_STOPPED_LEVEL,
        } for item in stopped]

    def _center_live_typing(self, now=None):
        """{room: [account_id...]} dang tra loi (typing con han 90
        giay, T3h) + {room: [ten hien thi...]} — doc tu hub
        (data/center/typing.json), khong suy ra tu phia Muse."""
        try:
            data = center_store.read_typing(self.root)
        except OSError:
            return {}, {}
        if now is None:
            now = time.time()
        names = self._center_account_names()
        by_room = {}
        named = {}
        for account_id, rooms in (data or {}).items():
            if not isinstance(rooms, dict):
                continue
            for room, entry in rooms.items():
                if center_store.typing_entry_is_live(entry, now):
                    by_room.setdefault(room, []).append(account_id)
                    named.setdefault(room, []).append(
                        names.get(account_id, account_id))
        return by_room, named

    def _center_account_names(self):
        """{account_id: tên gọi} + "owner" = Chủ nhân (người giao qua UI)."""
        accounts = ui_store.read_accounts(self.root).get("accounts") or {}
        names = {}
        for account_id, record in accounts.items():
            names[account_id] = record.get("display_name") or account_id
        names["owner"] = "Chủ nhân"
        return names

    def _center_assignable(self):
        """Tài khoản được giao việc: tham gia trung tâm + có đủ secret
        (TT1/TT9)."""
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        out = []
        for account_id, record in account_map.items():
            if not record.get("center_participating"):
                continue
            if not center_store.has_center_secrets(self.root, account_id):
                continue
            out.append((account_id,
                        record.get("display_name") or account_id))
        out.sort(key=lambda pair: pair[1].lower())
        return out

    def _center_last_fetch(self):
        """{account_id: epoch} lần kéo feed trung tâm gần nhất, đọc từ
        mục "center" riêng trong gateway-fetch-state.json (DESIGN §9:
        do center gateway ghi mỗi lần feed GET 200 đã xác thực; file
        chưa có mục center thì đọc về {}). KHÔNG suy từ seen_state.json
        (chỉ ghi mốc fetch đầu tiên có việc của đúng tài khoản; tài
        khoản kéo feed nhưng không có việc của mình thì không để lại
        dấu), không suy từ mốc tạo việc."""
        return ui_store.read_center_fetch_state(self.root)

    def _center_fragment(self, params, now):
        """Fragment tab Trung tâm (trong polling 5 giây): tổng quan ->
        việc (+ lọc, quá hạn lên đầu) -> tài khoản (đủ 4 mục + mã bí
        mật, SPEC TT11 / DESIGN §9). Đếm đang làm/quá hạn trên TOÀN BỘ
        việc (không đếm trên danh sách đã lọc)."""
        store = self._center_store()
        all_tasks = list(store.list_tasks().values())
        counts = ui_store.count_center_tasks_by_account(all_tasks, now)
        tasks = all_tasks
        if params.get("status"):
            tasks = [t for t in tasks
                     if t.get("status") == params["status"]]
        if params.get("account"):
            wanted = params["account"]
            tasks = [t for t in tasks
                     if wanted in _assignee_list(t.get("assigned_to"))]
        if params.get("kind"):
            tasks = [t for t in tasks
                     if t.get("kind") == params["kind"]]
        query = (params.get("q") or "").lower()
        if query:
            tasks = [t for t in tasks
                     if query in (t.get("title") or "").lower()
                     or query in (t.get("body") or "").lower()]
        if params.get("overdue"):
            tasks = [t for t in tasks
                     if ui_pages._center_task_overdue(t, now)]

        def _sort_key(task):
            due = ui_store.parse_iso_to_epoch(task.get("due_at"))
            created = ui_store.parse_iso_to_epoch(task.get("created_at"))
            return (0 if ui_pages._center_task_overdue(task, now) else 1,
                    due if due is not None else float("inf"),
                    -(created or 0))

        tasks.sort(key=_sort_key)
        health = store.center_health()
        names = self._center_account_names()
        last_fetch = self._center_last_fetch()
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        orphan_ids = {t.get("task_id") for t in all_tasks
                      if ui_store.is_center_orphan(t, account_map)}
        # Viec chung chi 1 phan nguoi lam viec bi ngung: dem so nguoi
        # da ngung de dong viec hien "N nguoi da ngung — xem chi tiet"
        # (doc 10 §7.3; viec mo coi hoan toan da co nhac "can chuyen
        # tay" rieng, khong dem o day).
        partial_suspended = {}
        for task in all_tasks:
            task_id = task.get("task_id")
            if task_id in orphan_ids:
                continue
            assignees = _assignee_list(task.get("assigned_to"))
            stopped = sum(
                1 for account_id in assignees
                if not (account_map.get(account_id) or {}).get(
                    "center_participating"))
            if stopped:
                partial_suspended[task_id] = stopped
        accounts = []
        # (T3j) Nút "Xem mã" / dòng nhắc "đã xem — chưa xoay": mật
        # khẩu xem mã đặt chưa (1 mật khẩu chung cho mọi tài khoản)
        # + trạng thái xem theo tài khoản (đọc 1 lần cho cả tab).
        view_password_set = ui_auth.view_secret_password_is_set(self.root)
        view_state, _view_state_status = \
            ui_store.read_center_view_state(self.root)
        view_accounts = {}
        if view_state is not None:
            view_accounts = view_state.get("accounts") or {}
        for account_id, record in sorted(
                account_map.items(),
                key=lambda pair: (pair[1].get("display_name")
                                  or pair[0]).lower()):
            secret = ui_store.center_secret_status(self.root, account_id)
            count = counts.get(account_id) or {"open": 0, "overdue": 0}
            view_entry = view_accounts.get(account_id) or {}
            viewed_at = view_entry.get("viewed_at")
            accounts.append({
                "account_id": account_id,
                "display_name": record.get("display_name") or account_id,
                "participating": bool(record.get("center_participating")),
                "last_fetch_epoch": last_fetch.get(account_id),
                "open_count": count["open"],
                "overdue_count": count["overdue"],
                "secret_has": secret["has"],
                "secret_issued_epoch": secret["issued_at"],
                "view_password_set": view_password_set,
                "viewed_at": viewed_at,
                "view_overdue": viewed_at is not None and now - viewed_at
                > ui_store.CENTER_VIEW_ROTATION_DEADLINE_SECONDS,
                "center_removed": bool(record.get("center_removed")),
            })
        filters = {"status": params.get("status", ""),
                   "account": params.get("account", ""),
                   "kind": params.get("kind", ""),
                   "overdue": params.get("overdue", ""),
                   "q": params.get("q", "")}
        # (v0.6.0, T3i) Dong dan "Mo khung chat →" cuoi tab Trung tam
        # kem huy hieu tong chua doc + "@".
        try:
            _chat_unread = store.room_unread_counts()
            _chat_mention = store.rooms_with_unread_owner_mention()
        except OSError:
            _chat_unread = {}
            _chat_mention = set()
        return ui_pages.center_fragment_html(
            tasks, accounts, health, filters, names, now,
            orphan_task_ids=orphan_ids,
            partial_suspended=partial_suspended,
            chat_unread_total=sum(_chat_unread.values()),
            chat_has_mention=bool(_chat_mention))

    def _center_chat_messages(self, chat_limit, room="chung",
                              before_seq=None, after_seq=None,
                              task_id=None):
        """(messages, task_titles, has_more, max_seq, min_seq) cho
        khung chat: tin TRONG PHONG + ten viec cua tin gan viec (cat
        40 ky tu luc render). ``max_seq``/``min_seq`` = seq lon/nho
        nhat cua tin da tra (None neu phong khong co tin) — JS dung
        de neo polling delta + tai tin cu. Tin cu khong ghi ``room``
        mac dinh ``"chung"``.

        (v0.6.0, T3i — DESIGN §8.5) 3 che do, loai tru nhau:
        - mac dinh: N tin gan nhat (``has_more`` = con tin cu hon);
        - ``before_seq=B``: 50 tin lien truoc B (tai tin cu khi cuon
          len dau hop; ``has_more`` = con tin cu hon B);
        - ``after_seq=A``: chi tin moi hon A, toi da 100 tin (polling
          delta 5 giay; khong thu hep ve lai 50);
        - ``task_id=T``: chi tin gan viec T (+ tin tag goc khi viec
          tao tu tag) — dung cho trang chi tiet viec.
        BO tran 500 tin: xem lai toi het 30 ngay luu."""
        store = self._center_store()
        events = store.read_events()
        picked = []
        if task_id is not None:
            task = store.get_task(task_id)
            source_msg_id = task.get("source_msg_id") if task else None
            for row in events:
                if row.get("kind") != "message":
                    continue
                record = row.get("record") or {}
                if row.get("task_id") == task_id or (
                        source_msg_id
                        and row.get("msg_id") == source_msg_id):
                    picked.append((row["seq"], record))
            picked.sort(key=lambda pair: pair[0])
            has_more = False
        else:
            for row in events:
                if row.get("kind") != "message":
                    continue
                record = row.get("record") or {}
                if (record.get("room") or "chung") != room:
                    continue
                if before_seq is not None \
                        and row["seq"] >= before_seq:
                    continue
                if after_seq is not None \
                        and row["seq"] <= after_seq:
                    continue
                picked.append((row["seq"], record))
            picked.sort(key=lambda pair: pair[0])
            if before_seq is not None:
                has_more = len(picked) > 50
                picked = picked[-50:]
            elif after_seq is not None:
                has_more = False
                picked = picked[:100]
            else:
                has_more = len(picked) > chat_limit
                picked = picked[-chat_limit:]
        max_seq = picked[-1][0] if picked else None
        min_seq = picked[0][0] if picked else None
        # (v0.6.0, NS-K21-1 — D17) Bong bóng phải mang ``data-seq``
        # (frag-02 §2.2): ``seq`` chỉ nằm trên row của events.jsonl,
        # không nằm trong record, nên bản cũ trả ``[record for
        # _seq, record in picked]`` làm renderer luôn nhận seq=None.
        # Gắn seq vào BẢN SAO của record (``{**record, ...}`` — không
        # sửa dict gốc của store) để ``center_message_bubble_html``
        # vẽ ``data-seq``. Chỉ 2 nơi gọi hàm này (fragment chat +
        # tab Chat tĩnh), cả 2 chỉ render bong bóng — record không bị
        # so sánh/băm/lưu lại ở đâu khác nên thêm khóa "seq" không
        # đổi hành vi nào khác.
        messages = [{**record, "seq": seq} for seq, record in picked]
        task_titles = {}
        for message in messages:
            task_id_msg = message.get("task_id")
            if task_id_msg and task_id_msg not in task_titles:
                task = store.get_task(task_id_msg)
                if task is not None:
                    task_titles[task_id_msg] = task.get("title") or ""
        # Bong bóng tin tag co viec kem theo hien dong ten viec nhu
        # tin gan task_id (Q-C5, v0.4.0): map them theo source_msg_id
        # cua viec tao tu tag -> tieu de viec.
        for task in store.list_tasks().values():
            source = task.get("source_msg_id")
            if source and source not in task_titles:
                task_titles[source] = task.get("title") or ""
        return messages, task_titles, has_more, max_seq, min_seq

    def _handle_center_chat_fragment(self, request, session):
        """GET /fragments/center-chat?room=<room>&chat_limit=N —
        fragment chat rieng (v0.2.5, Q-C2): polling 5 giay tu hien
        tin moi. O nhap KHONG o day (nam ngoai fragment) nen ban
        nhap khong mat.

        (v0.6.0, T3i — DESIGN §8.5) 3 tham so moi, loai tru nhau:
        ``before_seq`` (tai 50 tin cu hon — cuon len dau hop),
        ``after_seq`` (polling delta — chi tin moi hon, toi da 100),
        ``task_id`` (chi tin gan viec do, loai tru voi ``room``).
        ``before_seq`` + ``after_seq`` cung luc -> 400;
        ``task_id`` + ``room`` (co mat ro rang) -> 400; tham so sai
        (so am/chu/task_id khong ton tai) -> 400, khong tra HTML.
        LUAT DA DOC MOI (thay NS-08 cu): phuc vu fragment KHONG tu
        danh dau da doc — chi bam vien "N tin moi" (POST
        /center/mark-read) moi danh dau."""
        if request.method != "GET":
            return self._method_not_allowed(session)
        params = self._dashboard_params(request)
        room = params["room"]

        def _one(key):
            values = request.query.get(key)
            return values[0] if values else None

        before_raw = _one("before_seq")
        after_raw = _one("after_seq")
        task_raw = _one("task_id")
        if before_raw is not None and after_raw is not None:
            return self._with_security(Response.text(
                400, "before_seq và after_seq loại trừ nhau"))
        if task_raw is not None and "room" in request.query:
            return self._with_security(Response.text(
                400, "task_id và room loại trừ nhau"))

        def _parse_seq(raw, name):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None, ("Tham số %s phải là số nguyên." % name)
            if value < 0:
                return None, ("Tham số %s phải ≥ 0." % name)
            return value, None

        before_seq = after_seq = None
        if before_raw is not None:
            before_seq, err = _parse_seq(before_raw, "before_seq")
            if err is not None:
                return self._with_security(Response.text(400, err))
        if after_raw is not None:
            after_seq, err = _parse_seq(after_raw, "after_seq")
            if err is not None:
                return self._with_security(Response.text(400, err))
        task_id = None
        if task_raw is not None:
            if not re.match(r"^t-[A-Za-z0-9_-]{1,64}$", task_raw):
                return self._with_security(Response.text(
                    400, "Tham số task_id không hợp lệ."))
            if self._center_store().get_task(task_raw) is None:
                return self._with_security(Response.text(
                    400, "Việc không tồn tại."))
            task_id = task_raw
        messages, task_titles, has_more, max_seq, min_seq = \
            self._center_chat_messages(
                params["chat_limit"], room,
                before_seq=before_seq, after_seq=after_seq,
                task_id=task_id)
        names = self._center_account_names()
        fragment = ui_pages.center_chat_fragment_html(
            messages, names, task_titles, params["chat_limit"], has_more,
            room=room, oldest_seq=min_seq, newest_seq=max_seq,
            account_colors=ui_store.center_chat_colors(self.root),
            # (SUA B1) Fragment delta (after_seq/before_seq) voi 0
            # tin moi chi tra the meta, KHONG tra o trong — neu
            # khong, moi chu ky polling 5 giay lai them 1 o trong
            # vao hop chat. Fragment thuong (mo dau/khong tham so
            # delta) van hien o trong khi phong thuc su rong.
            show_empty_notice=(before_seq is None
                               and after_seq is None))
        return self._with_security(Response.html(200, fragment))

    def _center_static_html(self, session, params, center_error=None,
                            center_values=None):
        """Form tạo việc: render TĨNH ngoài fragment 5 giây (T3-CH-06)
        — bấm toggle ở node khác không ghi đè bản nháp.

        (v0.6.0, T3i — ĐỔI Q-T3-05) Khung chat đã rời sang tab Chat
        riêng (xem ``_center_chat_static_html``); tab Trung tâm chỉ
        còn form tạo việc ở phần tĩnh này."""
        create = ui_pages.center_create_form_html(
            self._center_assignable(), session["csrf_token"],
            error=center_error, values=center_values)
        return create

    def _center_chat_static_html(self, session, params, request):
        """Tab Chat rieng, mo hinh A (v0.6.0, T3i — DOI Q-T3-05):
        cot trai danh sach phong + cot phai khung chat + o nhap.
        Render TINH ngoai fragment 5 giay (T3-CH-06) — JS tu polling
        delta rieng cho hop chat (khong qua fragment dashboard).

        Phong mo: ``?room=`` thang khi co (va duoc luu lam phong
        dang chat do); khong co ``?room=`` thi mo phong da luu
        (``chat_ui_state.json``); phong da luu khong con ton tai thi
        rot lang le ve ``"chung"`` (Q-V060-11), khong bao. Dong canh
        bao kho file (DX-01, T3m) chuyen theo sang tab Chat."""
        store = self._center_store()
        rooms = store.list_rooms(
            center_store.read_accounts(self.root))
        room_names = {entry["room"]: entry for entry in rooms}
        explicit_room = "room" in (request.query or {})
        room = params.get("room", "chung")
        if explicit_room:
            if room in room_names:
                center_store.set_last_chat_room(self.root, room)
            else:
                # ?room= chi phong khong con -> rot lang le ve phong
                # da luu (neu con) roi ve "chung" (Q-V060-11).
                room = center_store.get_last_chat_room(self.root)
                if room not in room_names:
                    room = "chung"
        else:
            room = center_store.get_last_chat_room(self.root)
            if room not in room_names:
                room = "chung"
        chat_limit = params.get("chat_limit", 50)
        messages, task_titles, has_more, max_seq, min_seq = \
            self._center_chat_messages(chat_limit, room)
        names = self._center_account_names()
        unread = store.room_unread_counts()
        mention_rooms = store.rooms_with_unread_owner_mention()
        typing_by_room, typing_named = self._center_live_typing()
        room_entry = room_names.get(room)
        archived = bool(room_entry and room_entry.get("archived"))
        draft = "" if archived else store.get_draft(room)
        # (L5, 21/11 — quyet chu nhan) Header phong dang mo luon
        # mang tien to "Phòng đang mở:" de phan biet voi danh sach
        # phong (danh sach da bo "Riêng:").
        if room_entry is None:
            room_label = "Phòng đang mở: Khung chung"
        elif room_entry.get("account_id") is None:
            room_label = "Phòng đang mở: Khung chung"
        else:
            room_label = "Phòng đang mở: Riêng: %s" % (
                room_entry.get("display_name")
                or room_entry["account_id"])
        # ĐX-01 (T3m): canh bao kho file ngay tren khung chat khi da
        # dung >= nguong tu don trong center.json (mac dinh 80%).
        # Tinh 1 lan moi lan render trang (HTML tinh, KHONG dua vao
        # fragment polling 5 giay). Duoi nguong thi khong hien gi.
        # (T3i chuyen dong nay tu tab Trung tam sang tab Chat.)
        storage_warn = ""
        try:
            used_bytes = store.store_total_bytes()
            fraction = center_store.load_center_config(
                self.root)["prune_near_full_fraction"]
            if used_bytes >= fraction \
                    * center_store.MAX_FILE_STORE_BYTES:
                pct = used_bytes * 100 \
                    // center_store.MAX_FILE_STORE_BYTES
                storage_warn = (
                    '<p class="error">Kho file đã dùng %d%% — nên dọn '
                    'trước khi gửi file lớn '
                    '<a href="/settings#ket-qua-don">Dọn ngay</a></p>'
                    % pct)
        except OSError:
            storage_warn = ""
        return ui_pages.center_chat_tab_html(
            rooms, room, room_label, messages, names, task_titles,
            chat_limit, has_more, min_seq, max_seq, unread,
            mention_rooms, set(typing_by_room),
            typing_named.get(room) or [], session["csrf_token"],
            draft=draft, archived=archived,
            client_key=secrets.token_hex(16),
            storage_warn=storage_warn,
            recipients=self._center_assignable(),
            filter_senders=self._accounts_list()
            + [("owner", "Chủ nhân")],
            account_colors=ui_store.center_chat_colors(self.root),
            stopped_rounds=[
                item for item in
                ui_store.center_discussion_stopped_rounds(self.root)
                if item["room"] == room])

    def _handle_center_mark_read(self, request, session):
        """POST /center/mark-read (v0.6.0, T3i — luat da doc moi):
        CHI bam vien "N tin moi" (hoac ngoai le sat day Q-V060-07)
        moi danh dau da doc. Form POST thuong + CSRF bat buoc; body
        ``room`` + ``seq`` (so nguyen >= 0); tra JSON
        {"ok": true, "room": ..., "read_seq": <moc moi>}. Phong luu
        tru -> 400 (khong danh dau phong luu tru); seq nho hon moc
        hien tai thi moc giu nguyen, van tra OK. Khong ghi audit —
        danh dau da doc la trang thai hien thi, nhu bat/tat node."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        try:
            room = center_store.parse_room(
                request.form_value("room").strip())
        except center_store.CenterError:
            return self._with_security(Response.json(
                400, {"ok": False, "error": "bad_room"}))
        try:
            seq = int(request.form_value("seq"))
        except (TypeError, ValueError):
            return self._with_security(Response.json(
                400, {"ok": False, "error": "bad_seq"}))
        if seq < 0:
            return self._with_security(Response.json(
                400, {"ok": False, "error": "bad_seq"}))
        store = self._center_store()
        rooms = store.list_rooms(center_store.read_accounts(self.root))
        room_entry = next((entry for entry in rooms
                           if entry["room"] == room), None)
        if room_entry is not None and room_entry.get("archived"):
            return self._with_security(Response.json(
                400, {"ok": False, "error": "room_archived"}))
        read_seq = store.mark_room_read(room, seq)
        return self._with_security(Response.json(
            200, {"ok": True, "room": room, "read_seq": read_seq}))

    def _handle_center_unread(self, request, session):
        """GET /api/center/unread (v0.6.0, T3i): chi doc (GET), phien
        dang nhap, khong CSRF. Tra JSON {"ok": true, "total": <tong
        chua doc moi phong>, "has_mention": <co tin tag chu nhan
        chua doc>, "rooms": {room: so}, "mention_rooms": [room],
        "typing": {room: [account_id...]}} — tin hieu "dang tra
        loi" con han 90 giay (T3h). JS goi cung nhip 5 giay voi
        polling fragment, khong tao nhip rieng."""
        if request.method != "GET":
            return self._method_not_allowed(session)
        store = self._center_store()
        try:
            counts = store.room_unread_counts()
            mention_rooms = store.rooms_with_unread_owner_mention()
        except OSError:
            counts = {}
            mention_rooms = set()
        typing_by_room, _named = self._center_live_typing()
        return self._with_security(Response.json(200, {
            "ok": True,
            "total": sum(counts.values()),
            "has_mention": bool(mention_rooms),
            "rooms": counts,
            "mention_rooms": sorted(mention_rooms),
            "typing": typing_by_room,
            "typing_names": _named,
        }))

    def _handle_center_file_download(self, request, session, file_id):
        """GET /center/files/<file_id> — chu nhan tai file dinh kem ve
        (v0.2.5, Q-C7). Chi doc (GET), phien dang nhap, khong CSRF.
        File khong ton tai / bytes lech sidecar -> 404 chung. Tra
        octet-stream + attachment + nosniff (tai ve, khong mo inline).
        Khong log ten file."""
        if request.method != "GET":
            return self._method_not_allowed(session)
        store = self._center_store()
        meta = store.get_file_meta(file_id)
        if meta is None:
            return self._not_found(session)
        path = store.file_bytes_path(file_id)
        try:
            data = path.read_bytes()
        except OSError:
            return self._not_found(session)
        if len(data) != meta.get("file_size"):
            return self._not_found(session)
        return self._with_security(Response(
            200, data,
            {"Content-Type": "application/octet-stream",
             "Content-Length": str(len(data)),
             "Content-Disposition":
                 center_store.attachment_disposition(
                     meta.get("file_name") or "file"),
             "Cache-Control": "no-store",
             "X-Content-Type-Options": "nosniff"}))

    def _center_error_page(self, session, task_id, text, status=400,
                           back=None):
        """Trang lỗi thao tác trung tâm (kèm link quay lại). ``back``
        tuy chon: khi goi tu khung chat thi ve dung phong dang mo
        (``/?tab=chat&room=<room>#center-chat``)."""
        if back is None:
            back = ("/center/tasks/%s" % task_id) if task_id \
                else "/?tab=center"
        body = (
            "<h1>Không làm được</h1>"
            "<p>%s</p>"
            '<p><a href="%s">Quay lại</a></p>'
        ) % (ui_pages.esc(text), ui_pages.esc(back))
        return self._with_security(Response.html(
            status,
            ui_pages.simple_page_html("Không làm được", body,
                                      csrf_token=session["csrf_token"])))

    @staticmethod
    def _parse_center_due(raw):
        """datetime-local (giờ PC, Asia/Saigon +07:00, không DST) ->
        ISO-8601 kèm offset; sai định dạng -> None."""
        text = (raw or "").strip()
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            aware = parsed.replace(
                tzinfo=timezone(timedelta(hours=7)))
            return aware.isoformat()
        return None

    def _create_center_task(self, request, values):
        """Tạo việc qua store (caller="owner"). Trả (task_id, None) khi
        xong, (None, lỗi) khi bị từ chối — không ghi gì khi lỗi.
        ``values["assigned_to"]`` là danh sách 1..5 (v0.4.0) hoặc
        chuỗi 1 người (form cũ)."""
        title = values["title"]
        body = values["body"]
        kind = values["kind"]
        assigned_to = values["assigned_to"]
        due_raw = values["due_at"]
        if not title:
            return None, "Thiếu tiêu đề việc."
        if len(title) > 200:
            return None, "Tiêu đề quá 200 ký tự."
        if not body:
            return None, "Thiếu nội dung việc."
        if kind not in ("app", "nghien-cuu", "viet", "vanhanh", "khac"):
            return None, "Loại việc không hợp lệ."
        if isinstance(assigned_to, str):
            assigned_to = [assigned_to] if assigned_to else []
        seen = set()
        assignees = []
        for item in assigned_to or []:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                assignees.append(item)
        if not assignees or len(assignees) > 5:
            return None, ("Chọn 1–5 tài khoản nhận việc.")
        assignable = {a for a, _ in self._center_assignable()}
        bad = [a for a in assignees if a not in assignable]
        if bad:
            return None, ("Tài khoản nhận việc chưa tham gia trung tâm "
                          "hoặc chưa có đủ secret.")
        if not due_raw:
            return None, "Thiếu hạn xong (due_at bắt buộc)."
        due_at = self._parse_center_due(due_raw)
        if due_at is None:
            return None, "Hạn xong không đúng định dạng ngày giờ."
        for text in (title, body):
            label = center_store.find_secret_like(text)
            if label is not None:
                return None, ("Nội dung giống mẫu secret (%s) — không "
                              "được ghi secret vào việc (TS2)." % label)
        try:
            task, _dup = self._center_store().create_task(
                "owner", title, body, kind, assignees, due_at,
                secrets.token_hex(16))
        except center_store.CenterError as exc:
            return None, "Không tạo được việc: %s" % exc.error
        self._audit(request.client_ip, "center_task_create", None, None,
                    task["task_id"], "ok", task_id=task["task_id"])
        return task["task_id"], None

    def _handle_center_create_task(self, request, session):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        values = {"title": request.form_value("title").strip(),
                  "body": request.form_value("body").strip(),
                  "kind": request.form_value("kind"),
                  "assigned_to": request.form_values("assigned_to"),
                  "due_at": request.form_value("due_at")}
        task_id, error = self._create_center_task(request, values)
        if error is not None:
            return self._handle_dashboard(
                request, session, center_error=error,
                center_values=values)
        return self._with_security(Response.redirect(
            "/center/tasks/%s" % task_id))

    def _handle_center_task_detail(self, request, session, task_id):
        if request.method != "GET":
            return self._method_not_allowed(session)
        store = self._center_store()
        task = store.get_task(task_id)
        if task is None:
            return self._not_found(session)
        events = store.read_events()
        # (v0.6.0, NS-K21-1 — D17) Cùng luật như _center_chat_messages:
        # bong bóng trang chi tiết việc cũng mang data-seq (frag-02
        # §2.2) — gắn seq vào bản sao record, không sửa dict gốc.
        messages = [{**(row.get("record") or {}), "seq": row["seq"]}
                    for row in events
                    if row.get("kind") == "message"
                    and (row.get("task_id") == task_id
                         or (task.get("source_msg_id")
                             and row.get("msg_id")
                             == task.get("source_msg_id")))]
        # Trang thai bao cao theo tung nguoi (co "chua khop" — §6.4)
        # + tai khoan nao dang tam ngung (de hien "da ngung").
        account_map = ui_store.read_accounts(self.root).get("accounts") \
            or {}
        return self._with_security(Response.html(
            200,
            ui_pages.center_task_detail_html(
                task, messages, self._center_account_names(),
                session["csrf_token"], self._center_assignable(),
                time.time(),
                report_match=store.task_report_match(task_id),
                accounts=account_map,
                account_colors=ui_store.center_chat_colors(
                    self.root))))

    def _handle_center_task_status(self, request, session, task_id):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        to_status = request.form_value("to_status")
        note = request.form_value("note") or None
        if to_status not in ("da-xac-nhan", "dang-lam", "huy"):
            return self._center_error_page(
                session, task_id, "Trạng thái đích không hợp lệ.")
        store = self._center_store()
        task = store.get_task(task_id)
        if task is None:
            return self._not_found(session)
        before = task.get("status")
        # Từ chối việc (moi -> huy, R4) và trả lại (da-bao-xong ->
        # dang-lam) bắt buộc có note.
        if to_status == "huy" and before == "moi" \
                and not (note or "").strip():
            return self._center_error_page(
                session, task_id,
                "Từ chối việc cần ghi rõ lý do (note bắt buộc).")
        if to_status == "dang-lam" and before == "da-bao-xong" \
                and not (note or "").strip():
            return self._center_error_page(
                session, task_id,
                "Trả lại việc cần ghi rõ lý do (note bắt buộc).")
        if note:
            label = center_store.find_secret_like(note)
            if label is not None:
                return self._center_error_page(
                    session, task_id,
                    "Ghi chú giống mẫu secret (%s) — không được ghi "
                    "secret (TS2)." % label)
        try:
            _task, _dup = store.change_status(
                task_id, to_status, caller="owner", caller_role="owner",
                note=note, client_key=secrets.token_hex(16))
        except center_store.CenterError as exc:
            return self._center_error_page(
                session, task_id,
                "Không đổi được trạng thái: %s" % exc.error)
        # Lech T3 (d), T3m: 2 nut "Tu choi viec" (viec moi) va "Huy
        # viec" (viec dang lam) di chung to_status=huy nhung audit
        # khac nhau — nut nao bam thi ghi action cua nut do.
        if to_status == "huy" and before == "moi":
            action = "center_task_refuse"
        else:
            action = {"da-xac-nhan": "center_task_confirm",
                      "dang-lam": "center_task_return",
                      "huy": "center_task_cancel"}[to_status]
        self._audit(request.client_ip, action, None, before, to_status,
                    "ok", task_id=task_id)
        return self._with_security(Response.redirect(
            "/center/tasks/%s" % task_id))

    def _handle_center_task_reassign(self, request, session, task_id):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        new_assigned = request.form_values("assigned_to")
        store = self._center_store()
        task = store.get_task(task_id)
        if task is None:
            return self._not_found(session)
        assignable = {a for a, _ in self._center_assignable()}
        seen = set()
        assignees = []
        for item in new_assigned:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                assignees.append(item)
        if not assignees or len(assignees) > 5:
            return self._center_error_page(
                session, task_id, "Chọn 1–5 tài khoản nhận việc.")
        if any(a not in assignable for a in assignees):
            return self._center_error_page(
                session, task_id,
                "Tài khoản nhận chuyển chưa tham gia trung tâm hoặc "
                "chưa có đủ secret.")
        current = task.get("assigned_to") or []
        if isinstance(current, str):
            current = [current]
        if set(assignees) == set(current):
            return self._center_error_page(
                session, task_id,
                "Danh sách người nhận chuyển trùng với người đang giữ "
                "việc.")
        before = list(current)
        try:
            store.reassign_task(task_id, assignees, by="owner")
        except center_store.CenterError as exc:
            return self._center_error_page(
                session, task_id,
                "Không chuyển được việc: %s" % exc.error)
        self._audit(request.client_ip, "center_task_reassign", None,
                    before, assignees, "ok", task_id=task_id)
        return self._with_security(Response.redirect(
            "/center/tasks/%s" % task_id))

    def _handle_center_task_set_due(self, request, session, task_id):
        """POST /center/tasks/<id>/due (va /api/tasks/<id>/due) —
        dat han cho viec chua co han (Q-V040-3, nut "Dat han") hoac
        DOI han da dat (GC-E05, nut "Doi han"). Chi chu nhan tren
        UI; khong cho xoa han ve null (Q-V060-37); han moi phai o
        tuong lai, sai thi 400 cau tieng Viet, khong ghi gi."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        store = self._center_store()
        task = store.get_task(task_id)
        if task is None:
            return self._not_found(session)
        old_due = task.get("due_at")
        due_at = self._parse_center_due(
            request.form_value("due_at"))
        if due_at is None:
            return self._center_error_page(
                session, task_id,
                "Hạn xong không đúng định dạng ngày giờ.")
        try:
            store.set_task_due(task_id, due_at, by="owner",
                               allow_change=True)
        except center_store.CenterError as exc:
            message = {
                "due_in_past": "Hạn xong phải ở tương lai.",
                "due_already_set": "Việc này đã có hạn rồi.",
                "bad_due_at": "Hạn xong không đúng định dạng ngày giờ.",
                "bad_transition": ("Việc đã xong hoặc đã hủy nên "
                                   "không đổi được hạn."),
            }.get(exc.error, "Không đặt được hạn: %s" % exc.error)
            return self._center_error_page(session, task_id, message)
        if old_due is None:
            self._audit(request.client_ip, "center_task_set_due", None,
                        None, due_at, "ok", task_id=task_id)
        else:
            self._audit(request.client_ip, "center_task_due_changed",
                        None, old_due, due_at, "ok", task_id=task_id)
        return self._with_security(Response.redirect(
            "/center/tasks/%s" % task_id))

    def _handle_center_message(self, request, session):
        """POST /center/messages: CHỈ tin hoi của chủ nhân (ngoại lệ
        F18). Mọi trường kind trong form (nếu có) đều bị bỏ qua —
        không tồn tại route gửi giao-viec/bao-cao tay.

        (v0.6.0, T3l — D8 + schema v4) Form co o chon "Gửi cho:"
        chon NHIEU tai khoan (``to_account``: rong = Tat ca, danh
        sach 1-5 nguoi; "owner"/khong ton tai/qua 5 -> 400).

        (v0.2.5, T3b) Form multipart, kem file dinh kem: luu file qua
        store TRUOC khi ghi tin (lay ``file_id``); ghi tin loi thi
        XOA file vua luu (GC-03). Audit dong tai len CHI sau khi tin
        da ghi xong (GC-03) — chi ghi metadata (file_id/ten/dung
        luong), khong ghi noi dung. Chi co file (khong co body) cung
        gui duoc; ca 2 trong thi tu choi ro."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        body = request.form_value("body").strip()
        # (v0.3.0, T3c) Phong gui tin toi (truong an trong form);
        # sai mau hoac phong khong ton tai -> trang loi, khong ghi.
        try:
            room = center_store.parse_room(
                request.form_value("room").strip())
        except center_store.CenterError:
            return self._center_error_page(
                session, None, "Phòng không hợp lệ.")
        back = "/?tab=chat&room=%s#center-chat" % quote(room)
        store = self._center_store()
        # (v0.6.0, T3l — D8 + schema v4, C-18) O chon "Gửi cho:"
        # chon NHIEU tai khoan cung luc (select multiple): doc TAT CA
        # gia tri da chon; rong / chi chon "Tất cả" ("") = gui cho
        # tat ca (to_account=None); neu khong thi truyen DANH SACH
        # (toi da 5 nguoi, giu nguyen thu tu chon, bo trung lap).
        # Kiem tra TUNG gia tri phai ton tai trong accounts.json;
        # "owner" -> 400 (khong gui cho chinh chu nhan duoc, nhu
        # T3i); khong ton tai -> 400; qua 5 nguoi -> 400. Truyen
        # danh sach cho store (store/receiver nang v4 do thanh vien
        # schema lam — UI chi truyen dung dinh dang v4).
        to_account = None
        raw_values = [value.strip() for value in
                      request.form_values("to_account")]
        chosen = []
        seen = set()
        for value in raw_values:
            if not value or value in seen:
                continue
            seen.add(value)
            chosen.append(value)
        if chosen:
            if "owner" in chosen:
                return self._center_error_page(
                    session, None,
                    "Không gửi cho chính chủ nhân được.", back=back)
            account_map = ui_store.read_accounts(
                self.root).get("accounts") or {}
            missing = [value for value in chosen
                       if value not in account_map]
            if missing:
                return self._center_error_page(
                    session, None, "Tài khoản nhận không tồn tại.",
                    back=back)
            if len(chosen) > 5:
                return self._center_error_page(
                    session, None,
                    "Chỉ gửi cho tối đa 5 tài khoản cùng lúc.",
                    back=back)
            to_account = chosen
        rooms = store.list_rooms(center_store.read_accounts(self.root))
        room_entry = next((entry for entry in rooms
                           if entry["room"] == room), None)
        if room_entry is None:
            return self._center_error_page(
                session, None, "Phòng không tồn tại.", back=back)
        if room_entry.get("archived"):
            return self._center_error_page(
                session, None,
                "Phòng lưu trữ — chỉ xem lại tin cũ, không gửi thêm "
                "được.", status=403, back=back)
        upload = request.form_file("file")
        file_data = None
        file_name = None
        if upload is not None:
            file_data = upload.get("data") or b""
            file_name = (upload.get("filename") or "").strip()
            # Trinh duyet gui truong file rong khi khong chon file:
            # coi nhu khong dinh kem.
            if not file_name and not file_data:
                upload = None
        if not body and upload is None:
            return self._center_error_page(
                session, None, "Tin nhắn trống.", back=back)
        if len(body) > 5000:
            return self._center_error_page(
                session, None, "Tin nhắn quá 5.000 ký tự.", back=back)
        label = center_store.find_secret_like(body)
        if label is not None:
            return self._center_error_page(
                session, None,
                "Tin nhắn giống mẫu secret (%s) — không được ghi "
                "secret (TS2)." % label, back=back)
        file_id = None
        if upload is not None:
            try:
                meta = self._center_store().save_file(
                    "owner", file_name, file_data)
            except center_store.CenterError as exc:
                return self._center_error_page(
                    session, None, self._center_file_error_text(exc),
                    back=back)
            file_id = meta["file_id"]
        try:
            _msg, _dup = self._center_store().append_message(
                "owner", "hoi", to_account, None, body,
                "m-" + secrets.token_hex(6), file_id=file_id, room=room,
                client_key=request.form_value("client_key").strip()
                or None)
        except center_store.CenterError as exc:
            if file_id is not None:
                # Ghi tin loi -> xoa file vua luu (GC-03).
                self._center_store().delete_file(file_id)
            return self._center_error_page(
                session, None, self._center_message_error_text(exc),
                back=back)
        if _dup and file_id is not None:
            # Gui lai form (cung client_key): tin da co — file vua
            # tai len khong duoc tham chieu, xoa de khoi rac kho.
            stored_file = (_msg.get("file") or {}).get("file_id")
            if stored_file != file_id:
                self._center_store().delete_file(file_id)
        # Gui xong: xoa ban nhap cua phong do (P9).
        self._center_store().delete_draft(room)
        if file_id is not None:
            # Audit CHI sau khi tin da ghi xong (GC-03).
            self._audit(request.client_ip, "center_file_upload", None,
                        None, {"file_id": file_id,
                               "file_name": file_name,
                               "file_size": len(file_data)},
                        "ok")
        return self._with_security(Response.redirect(back))

    def _handle_center_room(self, request, session):
        """POST /center/room — chuyen phong chat (v0.3.0, T3c,
        CHANG-5): form POST thuong + CSRF (KHONG link GET thuan).
        Truong an ``draft`` do APP_JS chep tu textarea hien tai truoc
        khi gui (chi khi ``draft_copied`` = "1" — neu JS tat thi
        truong an trong, GIU NGUYEN ban nhap da luu cua phong cu,
        khong xoa); luu nhap phong cu vao drafts.json roi redirect
        ve phong dich (giu ``?room=``). Phong dich khong ton tai ->
        trang loi. Phong luu tru van mo duoc de xem lai (chi doc;
        form nhap khong render, phong khong gui them duoc)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        store = self._center_store()
        try:
            target = center_store.parse_room(
                request.form_value("room").strip())
        except center_store.CenterError:
            return self._center_error_page(
                session, None, "Phòng không hợp lệ.")
        try:
            from_room = center_store.parse_room(
                request.form_value("from_room").strip())
        except center_store.CenterError:
            from_room = "chung"
        rooms = store.list_rooms(center_store.read_accounts(self.root))
        if target != "chung" and not any(
                entry["room"] == target for entry in rooms):
            return self._center_error_page(
                session, None, "Phòng không tồn tại.")
        if request.form_value("draft_copied") == "1":
            store.save_draft(from_room, request.form_value("draft"))
        # (v0.6.0, T3i) Doi phong thi luu lam phong dang chat do
        # (mo lai tab Chat se ve dung phong nay khi khong co ?room=).
        center_store.set_last_chat_room(self.root, target)
        return self._with_security(Response.redirect(
            "/?tab=chat&room=%s#center-chat" % quote(target)))

    @staticmethod
    @staticmethod
    def _center_message_error_text(exc):
        """Cau chu loi gui tin de hien cho chu nhan (v0.3.0, T3d —
        NS-07: qua MAX_MENTIONS thi bao ro cach tach tin)."""
        if exc.error == "too_many_mentions":
            return ("Tag quá 5 tài khoản trong 1 tin — tách thành "
                    "nhiều tin, mỗi tin tối đa 5 tag rồi gửi lại.")
        return "Không gửi được tin: %s" % exc.error

    @staticmethod
    def _center_file_error_text(exc):
        """Cau chu loi tai file len de hien cho chu nhan (khong lo
        ten file that — co the chua secret)."""
        if exc.error == "empty_file":
            return "File trống — chọn file có nội dung rồi gửi lại."
        if exc.error == "file_too_large":
            return "File quá 50 MB — chia nhỏ hoặc nén rồi gửi lại."
        if exc.error == "store_full":
            return ("Kho file đã đầy (2 GB) — dọn kho rồi gửi lại "
                    "(file chưa tải lên được).")
        if exc.error == "bad_file_name":
            return "Tên file không hợp lệ — đổi tên rồi gửi lại."
        if exc.error == "blocked_secret_pattern":
            return ("Tên file giống mẫu secret — đổi tên rồi gửi lại "
                    "(không tải file chứa mật khẩu/mã bí mật).")
        return "Không tải file lên được: %s" % exc.error

    # -- quản lý tài khoản trung tâm (v0.2.4, doc 10 §5.6) ---------------
    # 2 mức dừng (Q-AM-05): tạm ngưng = chỉ tắt cờ, giữ mã; gỡ hẳn =
    # tắt cờ + xoay mã. Tham gia lại (Q-AM-04) = cấp mã mới, làm lại
    # như tham gia mới. Thứ tự tham gia (Q-AM-01): cấp mã TRƯỚC, bật
    # cờ SAU. UI KHÔNG hiện/tạo secret trong trình duyệt (TS3/F18):
    # giá trị secret chỉ ghi file 600 trên PC, không trả về response,
    # không ghi audit/log.

    def _set_center_participating(self, account_id, value, removed=None):
        """Bật/tắt cờ center_participating (đúng 1 nơi: accounts.json).

        (T3j) ``removed``: khi gỡ hẳn thì đặt True, khi tham gia
        (lại) thì đặt False — cờ ``center_removed`` phân biệt "gỡ
        hẳn" với "tạm ngưng" (cả 2 đều tắt cờ tham gia) để route xem
        mã từ chối đúng tài khoản đã gỡ (DESIGN §3.11 §4.2). None =
        không đổi cờ này."""
        def _set(data):
            if account_id in data["accounts"]:
                data["accounts"][account_id]["center_participating"] = value
                if removed is not None:
                    data["accounts"][account_id]["center_removed"] = removed

        ui_store.modify_accounts(self.root, _set)

    def _rotate_center_secrets(self, account_id):
        """Tạo cặp secret trung tâm MỚI (fetch+push), ghi file 600 qua
        _write_secret_file. KHÔNG trả giá trị secret cho caller, KHÔNG
        hiện hay log giá trị. Mã cũ chết ngay khi ghi đè xong. Mẫu tạo
        secret như khi tạo bot (token_urlsafe(32), file 600).

        (T3j) Trả True khi lần xoay này gỡ cờ nhắc "đã xem — chưa
        xoay" của tài khoản (đã có lượt xem chờ xoay); False khi xoay
        mà không qua xem. Sau 1 lần xem, không khôi phục mã cũ — xoay
        tiếp cặp mới (Q-V060-24)."""
        fetch_secret = secrets.token_urlsafe(32)
        push_secret = secrets.token_urlsafe(32)
        self._write_secret_file("center-%s" % account_id, "fetch_secret",
                                fetch_secret + "\n")
        self._write_secret_file("center-%s" % account_id, "push_secret",
                                push_secret + "\n")
        return ui_store.mark_center_view_rotated(self.root, account_id)

    def _audit_center_secret(self, request, account_id,
                             rotated_after_view=False):
        """1 dòng audit cho mỗi lần cấp/xoay secret (TS3): account_id +
        thời điểm, KHÔNG giá trị secret. (T3j) Xoay sau 1 lần xem thì
        ghi thêm "rotated_after_view": true vào after (SPEC TS3)."""
        after = {"account_id": account_id}
        if rotated_after_view:
            after["rotated_after_view"] = True
        self._audit(request.client_ip, "center_secret_rotate", None,
                    None, after, "ok",
                    account_id=account_id)

    def _center_account_error(self, session, text):
        """Trang lỗi thao tác tài khoản trung tâm (kèm link quay lại)."""
        body = (
            "<h1>Không làm được</h1>"
            "<p>%s</p>"
            '<p><a href="/accounts">Về danh sách tài khoản</a></p>'
        ) % ui_pages.esc(text)
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html("Không làm được", body,
                                      csrf_token=session["csrf_token"])))

    def _center_confirm_expired(self, session):
        """Trang báo xác nhận 2 bước đã hết hạn (mẫu gỡ tài khoản)."""
        return self._with_security(Response.html(
            403,
            ui_pages.simple_page_html(
                "Xác nhận đã hết hạn",
                "<h1>Xác nhận đã hết hạn</h1>"
                "<p>Quay lại danh sách tài khoản và bấm lại.</p>"
                '<p><a href="/accounts">Về danh sách tài khoản</a></p>',
                csrf_token=session["csrf_token"])))

    def _handle_center_pause(self, request, session, account_id):
        """POST: tạm ngưng (Q-AM-05 mức 1) — chỉ tắt cờ, GIỮ mã."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map.get(account_id) or {}
        if not record.get("center_participating"):
            return self._center_account_error(
                session, "Tài khoản chưa tham gia trung tâm nên không "
                         "tạm ngưng được.")
        self._set_center_participating(account_id, False)
        # Q-V040-2: go tai khoan khoi danh sach nguoi lam viec cua moi
        # viec chua xong (giu lich su + bao cao da lam; viec khong con
        # ai thi mo coi "can chuyen tay"). Trang thai viec khong doi,
        # khong tu chuyen cho ai khac.
        removed = self._center_store().remove_assignee(account_id,
                                                       by="owner")
        self._audit(request.client_ip, "center_pause", None,
                    {"account_id": account_id, "participating": True},
                    {"account_id": account_id, "participating": False,
                     "removed_from_tasks": len(
                         removed["removed_from"])},
                    "ok", account_id=account_id)
        return self._with_security(Response.redirect("/accounts"))

    def _handle_center_discussion(self, request, session, account_id):
        """POST: bat/tat thao luan cua 1 tai khoan (v0.6.0, T3l —
        doc 10 §3.8 §3c, quyet D10).

        Chi chu nhan bat/tat: giao dien chi co 1 mat khau chu nhan
        (khong co nguoi dung khac), nhu nut "Tam ngung tham gia" —
        khong them phan quyen rieng. Form POST + CSRF, truong
        ``enabled`` = "1" (bat) / "0" (tat); gia tri khac -> trang
        loi, khong ghi. Ghi 1 dong audit ``center_discussion_toggle``
        (before/after: account_id + co, KHONG ghi noi dung tin).
        Tat thi hub tu choi ngay 403 ``discussion_disabled`` (phan
        receiver do thanh vien schema lam); UI chi bat/tat co + doc
        co de hien dung trang thai."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        raw = request.form_value("enabled").strip()
        if raw not in ("0", "1"):
            return self._center_account_error(
                session, "Giá trị bật/tắt thảo luận không hợp lệ.")
        value = raw == "1"
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map.get(account_id) or {}
        before = bool(record.get("center_discussion_enabled", True))

        def _set(data):
            if account_id in data["accounts"]:
                data["accounts"][account_id][
                    "center_discussion_enabled"] = value

        ui_store.modify_accounts(self.root, _set)
        self._audit(request.client_ip, "center_discussion_toggle",
                    None,
                    {"account_id": account_id,
                     "center_discussion_enabled": before},
                    {"account_id": account_id,
                     "center_discussion_enabled": value},
                    "ok", account_id=account_id)
        return self._with_security(Response.redirect("/accounts"))

    def _handle_center_resume(self, request, session, account_id):
        """POST: bật lại sau tạm ngưng — chỉ bật cờ, GIỮ mã cũ (bật
        lại là chạy ngay). Từ chối khi chưa có mã (tránh cờ true mà
        không có secret — lỗi thứ tự Q-AM-01)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map.get(account_id) or {}
        if record.get("center_participating"):
            return self._center_account_error(
                session, "Tài khoản đã tham gia trung tâm.")
        if not center_store.has_center_secrets(self.root, account_id):
            return self._center_account_error(
                session, "Tài khoản chưa có mã bí mật trung tâm — dùng "
                         "“Tham gia lại — cấp mã mới” thay vì bật lại.")
        self._set_center_participating(account_id, True)
        self._audit(request.client_ip, "center_resume", None,
                    {"account_id": account_id, "participating": False},
                    {"account_id": account_id, "participating": True},
                    "ok", account_id=account_id)
        return self._with_security(Response.redirect("/accounts"))

    def _handle_center_remove_confirm(self, request, session, session_token,
                                      account_id):
        """GET: trang xác nhận gỡ hẳn (Q-AM-05 mức 2) — tắt cờ + xoay
        mã. Không đổi trạng thái ở bước này."""
        if request.method != "GET":
            return self._method_not_allowed(session)
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map.get(account_id) or {}
        confirm_token = self._issue_confirm_token(
            session_token, {"kind": "center_remove",
                            "account_id": account_id})
        open_count = sum(
            1 for task in self._center_store().list_tasks().values()
            if account_id in _assignee_list(task.get("assigned_to"))
            and task.get("status") in ui_store.CENTER_OPEN_STATUSES)
        intro = (
            "<p>Gỡ hẳn tài khoản <strong>%s</strong> (%s) khỏi trung "
            "tâm?</p>"
            "<p><strong>Tắt cờ tham gia + xoay mã bí mật:</strong> mã cũ "
            "chết ngay, tài khoản này không kéo được tin nữa cho tới khi "
            "tham gia lại với mã mới.</p>"
            "<p>Tài khoản này sẽ được gỡ khỏi danh sách người làm việc "
            "của %d việc chưa xong (việc tiếp tục với người còn lại, "
            "không tự chuyển; việc nào không còn ai sẽ hiện nhắc “cần "
            "chuyển tay”).</p>"
            "<p>Tham gia lại sau này = cấp mã mới, làm lại như tham gia "
            "mới (không dùng lại mã cũ).</p>"
        ) % (ui_pages.esc(record.get("display_name") or account_id),
             ui_pages.esc(account_id), open_count)
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                "Gỡ hẳn khỏi trung tâm", intro,
                "/accounts/%s/center/remove/execute" % account_id,
                {"confirm_token": confirm_token},
                session["csrf_token"],
                "Gỡ hẳn %s" % (record.get("display_name") or account_id),
                danger=True, back_url="/accounts")))

    def _handle_center_remove_execute(self, request, session, session_token,
                                      account_id):
        """POST: thực thi gỡ hẳn — tắt cờ rồi xoay mã (mã cũ chết)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "center_remove"
                or payload.get("account_id") != account_id):
            return self._center_confirm_expired(session)
        self._set_center_participating(account_id, False, removed=True)
        rotated_after_view = self._rotate_center_secrets(account_id)
        # (T3j) Tài khoản đã gỡ hẳn thì xóa luôn mục trạng thái xem
        # mã (không còn lượt xem chờ xoay nào).
        ui_store.clear_center_view_entry(self.root, account_id)
        # Q-V040-2 (nhu tam ngung): go khoi danh sach nguoi lam viec.
        removed = self._center_store().remove_assignee(account_id,
                                                       by="owner")
        self._audit(request.client_ip, "center_remove", None,
                    {"account_id": account_id, "participating": True},
                    {"account_id": account_id, "participating": False,
                     "removed_from_tasks": len(
                         removed["removed_from"])},
                    "ok", account_id=account_id)
        self._audit_center_secret(request, account_id,
                                  rotated_after_view)
        body = (
            "<h1>Đã gỡ hẳn tài khoản %s</h1>"
            "<p>Cờ tham gia đã tắt, mã bí mật cũ đã chết (mã mới đã tạo "
            "trên máy, chưa giao cho tài khoản nào).</p>"
            "<p>Tài khoản này đã được gỡ khỏi danh sách người làm việc "
            "của %d việc chưa xong — việc tiếp tục với người còn lại; "
            "việc nào không còn ai thì chuyển tay trên tab Trung tâm "
            "nếu cần.</p>"
            '<p><a href="/accounts">Về danh sách tài khoản</a> · '
            '<a href="/?tab=center">Tab Trung tâm</a></p>'
        ) % (ui_pages.esc(account_id),
             len(removed["removed_from"]))
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html("Đã gỡ hẳn", body,
                                      csrf_token=session["csrf_token"])))

    def _handle_center_join_confirm(self, request, session, session_token,
                                    account_id):
        """GET: trang xác nhận tham gia (lại) trung tâm — cấp mã mới
        rồi bật cờ (Q-AM-01/Q-AM-04). Không đổi trạng thái ở đây."""
        if request.method != "GET":
            return self._method_not_allowed(session)
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map.get(account_id) or {}
        if record.get("center_participating"):
            return self._center_account_error(
                session, "Tài khoản đã tham gia trung tâm — muốn xoay mã "
                         "thì dùng nút “Cấp lại mã mới” trên tab Trung tâm.")
        confirm_token = self._issue_confirm_token(
            session_token, {"kind": "center_join",
                            "account_id": account_id})
        intro = (
            "<p>Cho tài khoản <strong>%s</strong> (%s) tham gia trung "
            "tâm?</p>"
            "<p><strong>Cấp mã bí mật mới rồi bật cờ tham gia</strong> "
            "(cấp mã trước, bật cờ sau). Mã cũ (nếu có) chết ngay.</p>"
            "<p>Mã mới chỉ tạo trên máy tính, <strong>không hiện trên "
            "giao diện</strong>. Nhờ agent PC giao mã mới cho tài khoản "
            "qua kênh an toàn; tài khoản ghi lại center-config rồi kiểm "
            "chứng fetch 200 ngay bằng mã mới. Tới khi tài khoản kéo thử "
            "thành công, tab Trung tâm mới hiện “đang hoạt động”.</p>"
        ) % (ui_pages.esc(record.get("display_name") or account_id),
             ui_pages.esc(account_id))
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                "Tham gia trung tâm", intro,
                "/accounts/%s/center/join/execute" % account_id,
                {"confirm_token": confirm_token},
                session["csrf_token"],
                "Cấp mã mới và tham gia",
                back_url="/accounts")))

    def _handle_center_join_execute(self, request, session, session_token,
                                    account_id):
        """POST: thực thi tham gia — cấp mã mới TRƯỚC, bật cờ SAU."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "center_join"
                or payload.get("account_id") != account_id):
            return self._center_confirm_expired(session)
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        if (account_map.get(account_id) or {}).get("center_participating"):
            return self._center_account_error(
                session, "Tài khoản đã tham gia trung tâm.")
        rotated_after_view = self._rotate_center_secrets(account_id)
        self._set_center_participating(account_id, True, removed=False)
        self._audit(request.client_ip, "center_join", None,
                    {"account_id": account_id, "participating": False},
                    {"account_id": account_id, "participating": True},
                    "ok", account_id=account_id)
        self._audit_center_secret(request, account_id,
                                  rotated_after_view)
        body = (
            "<h1>Đã cấp mã mới cho tài khoản %s</h1>"
            "<p>Cờ tham gia đã bật. Mã mới chỉ nằm trên máy tính, "
            "<strong>không hiện ở đây</strong>.</p>"
            "<p>Bước tiếp theo (agent PC làm): giao mã mới cho tài khoản "
            "qua kênh an toàn; tài khoản ghi lại center-config rồi kiểm "
            "chứng fetch 200 ngay bằng mã mới. Tới khi tài khoản kéo thử "
            "thành công, tab Trung tâm mới hiện “đang hoạt động”.</p>"
            '<p><a href="/accounts">Về danh sách tài khoản</a> · '
            '<a href="/?tab=center">Tab Trung tâm</a></p>'
        ) % ui_pages.esc(account_id)
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html("Đã tham gia trung tâm", body,
                                      csrf_token=session["csrf_token"])))

    def _handle_center_rotate_secret(self, request, session, session_token,
                                     account_id, rest):
        """Cấp/cấp lại mã mới (Q-AM-07, TS4): GET trang xác nhận, POST
        /execute xoay cặp secret. UI chỉ kích hoạt luồng xoay — không
        hiện/tạo secret trong trình duyệt, không hiện giá trị mã.

        (NS-02) Cau chu doi theo trang thai secret: chua co ma ->
        "Cấp mã mới" + cau hoi cap lan dau; da co ma -> "Cấp lại mã
        mới" + canh bao ma cu chet ngay."""
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map.get(account_id)
        if record is None:
            return self._not_found(session)
        secret_has = ui_store.center_secret_status(
            self.root, account_id)["has"]
        title = "Cấp lại mã mới" if secret_has else "Cấp mã mới"
        if rest == "":
            if request.method != "GET":
                return self._method_not_allowed(session)
            confirm_token = self._issue_confirm_token(
                session_token, {"kind": "center_rotate_secret",
                                "account_id": account_id})
            participating = bool(record.get("center_participating"))
            warn = ""
            if participating:
                warn = (
                    "<p><strong>Tài khoản đang tham gia:</strong> mã cũ "
                    "chết ngay khi bấm xác nhận, tài khoản mất kết nối "
                    "tới khi nhận mã mới.</p>")
            # (T3j, frag-03 §6.7) Xoay sau 1 lần xem: trang xác nhận
            # ghi rõ mã cũ vừa được xem lúc nào (xoay xong thì xem
            # lại mã mới, không xem lại mã cũ).
            _view_state, _view_status = \
                ui_store.read_center_view_state(self.root)
            if _view_state is not None:
                _viewed = (_view_state.get("accounts") or {}).get(
                    account_id) or {}
                if _viewed.get("viewed_at") is not None:
                    warn += (
                        "<p>Mã cũ vừa được xem lúc %s — xoay xong thì "
                        "xem lại mã mới, mã cũ không hiện lại được nữa."
                        "</p>"
                        % ui_pages.esc(ui_store.format_day_time_saigon(
                            _viewed["viewed_at"])))
            if secret_has:
                intro = (
                    "<p>Cấp mã mới sẽ làm mã cũ chết ngay. Tài khoản "
                    "<strong>%s</strong> (%s) phải ghi lại cấu hình mới "
                    "rồi mới kéo tin tiếp được. Cấp lại mã?</p>"
                    "%s"
                    "<p>Mã mới chỉ tạo trên máy tính, "
                    "<strong>không hiện trên giao diện</strong>. Nhờ "
                    "agent PC giao mã mới cho tài khoản qua kênh an "
                    "toàn; tài khoản ghi lại center-config rồi kiểm "
                    "chứng fetch 200 ngay bằng mã mới.</p>"
                    "<p>Ngày cấp trên tab Trung tâm đổi sang ngày cấp "
                    "mới sau khi xoay.</p>"
                ) % (ui_pages.esc(record.get("display_name") or account_id),
                     ui_pages.esc(account_id), warn)
            else:
                intro = (
                    "<p>Tài khoản <strong>%s</strong> (%s) chưa có mã "
                    "bí mật trung tâm. Cấp mã mới cho tài khoản này?</p>"
                    "%s"
                    "<p>Mã mới chỉ tạo trên máy tính, "
                    "<strong>không hiện trên giao diện</strong>. Nhờ "
                    "agent PC giao mã mới cho tài khoản qua kênh an "
                    "toàn; tài khoản ghi lại center-config rồi kiểm "
                    "chứng fetch 200 ngay bằng mã mới.</p>"
                ) % (ui_pages.esc(record.get("display_name") or account_id),
                     ui_pages.esc(account_id), warn)
            return self._with_security(Response.html(
                200,
                ui_pages.confirm_page_html(
                    title, intro,
                    "/center/accounts/%s/rotate-secret/execute"
                    % account_id,
                    {"confirm_token": confirm_token},
                    session["csrf_token"],
                    title,
                    danger=True, back_url="/?tab=center")))
        if rest != "/execute":
            return self._not_found(session)
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None
                or payload.get("kind") != "center_rotate_secret"
                or payload.get("account_id") != account_id):
            return self._with_security(Response.html(
                403,
                ui_pages.simple_page_html(
                    "Xác nhận đã hết hạn",
                    "<h1>Xác nhận đã hết hạn</h1>"
                    "<p>Quay lại tab Trung tâm và bấm lại.</p>"
                    '<p><a href="/?tab=center">Tab Trung tâm</a></p>',
                    csrf_token=session["csrf_token"])))
        rotated_after_view = self._rotate_center_secrets(account_id)
        self._audit_center_secret(request, account_id,
                                  rotated_after_view)
        old_dead = ("<p>Mã cũ đã chết, mã mới chỉ nằm trên máy tính, "
                    "<strong>không hiện ở đây</strong>.</p>"
                    if secret_has else
                    "<p>Mã mới chỉ nằm trên máy tính, "
                    "<strong>không hiện ở đây</strong>.</p>")
        body = (
            "<h1>Đã cấp mã mới cho tài khoản %s</h1>"
            "%s"
            "<p>Bước tiếp theo (agent PC làm): giao mã mới cho tài khoản "
            "qua kênh an toàn; tài khoản ghi lại center-config rồi kiểm "
            "chứng fetch 200 ngay bằng mã mới.</p>"
            '<p><a href="/?tab=center">Về tab Trung tâm</a></p>'
        ) % (ui_pages.esc(account_id), old_dead)
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(title, body,
                                      csrf_token=session["csrf_token"])))

    # -- xem mã bí mật trung tâm (v0.6.0, T3j — ngoại lệ F18(3)) --------

    def _view_secret_blocked_page(self, session, text):
        """Trang 403 khi lượt xem bị chặn (đã hiện 1 lần / quá hạn
        xoay / file trạng thái hỏng-thiếu / sau khôi phục backup):
        ghi rõ phải xoay mã mới, không hiện mã, không lộ mật khẩu."""
        body = (
            "<h1>Không xem được mã</h1>"
            "<p>%s</p>"
            "<p>Mã cũ không hiện lại được nữa — xoay mã mới rồi xem "
            "mã mới.</p>"
            '<p><a href="/?tab=center">Về tab Trung tâm</a></p>'
        ) % ui_pages.esc(text)
        return self._with_security(Response.html(
            403,
            ui_pages.simple_page_html("Không xem được mã", body,
                                      csrf_token=session["csrf_token"])))

    def _handle_center_view_secret(self, request, session, session_token,
                                   account_id, rest):
        """Xem mã bí mật trung tâm (DESIGN §3.11 bước 1–5).

        Route 1 (GET): trang nhập mật khẩu xem mã — chỉ kiểm tra tài
        khoản tồn tại + chưa gỡ + đã có mã (404 chung nếu không);
        kiểm tra cờ "đã xem" nằm ở route 2, không nằm ở route 1.
        Route 2 (POST /execute): CSRF + confirm token 1 lần -> kiểm
        tra khóa nhập sai -> kiểm tra mật khẩu -> kiểm tra cờ "đã
        xem" TRƯỚC KHI đọc mã -> đọc cặp mã -> đặt cờ + audit ->
        trả trang hiện mã (Cache-Control: no-store). Mã chỉ xuất hiện
        trong response này, không có route GET nào trả mã."""
        now = time.time()
        if rest == "/execute":
            if request.method != "POST":
                return self._method_not_allowed(session)
            if not self._csrf_ok(request, session):
                return self._csrf_error(session)
            payload = self._consume_confirm_token(
                session_token, request.form_value("confirm_token"))
            if (payload is None
                    or payload.get("kind") != "center_view_secret"
                    or payload.get("account_id") != account_id):
                return self._with_security(Response.html(
                    403,
                    ui_pages.simple_page_html(
                        "Xác nhận đã hết hạn",
                        "<h1>Xác nhận đã hết hạn</h1>"
                        "<p>Quay lại tab Trung tâm và bấm lại.</p>"
                        '<p><a href="/?tab=center">Tab Trung tâm</a></p>',
                        csrf_token=session["csrf_token"])))
            # Tài khoản không tồn tại / đã gỡ hẳn -> 404 chung, không
            # ghi audit (frag-03 §4.2/§4.5). Chưa có mã thì 404 ở
            # bước sau kiểm tra cờ "đã xem" (để lượt xem lại của mã
            # đã xem vẫn trả trang chặn thay vì 404 khi file mã đã
            # bị xóa giữa chừng).
            account_map = ui_store.read_accounts(
                self.root).get("accounts") or {}
            record = account_map.get(account_id)
            if record is None or record.get("center_removed"):
                return self._not_found(session)
            secret_status = ui_store.center_secret_status(
                self.root, account_id)
            # File trạng thái hỏng -> fail-closed ngay (không đánh giá
            # được khóa/cờ "đã xem"); chỉ lộ tình trạng hệ thống,
            # không lộ mã. File thiếu thì xử lý ở bước 6 sau khi mật
            # khẩu đúng (tránh lộ lịch sử xem cho người chưa có mật
            # khẩu).
            state, state_status = ui_store.read_center_view_state(
                self.root)
            if state_status == "corrupt":
                self._audit(
                    request.client_ip, "center_secret_view_blocked",
                    None, {"has_secret": True},
                    {"reason": "view_state_corrupt"}, "failed",
                    account_id=account_id)
                return self._view_secret_blocked_page(
                    session, "File trạng thái xem mã bị hỏng — xoay mã "
                             "mới trước khi xem lại.")
            if state is None:
                state = ui_store.new_center_view_state()
            # Khóa nhập sai (Q-V060-20): 5 lần sai/15 phút -> khóa
            # 900 giây. Lượt xem trong lúc khóa ghi 1 dòng audit
            # center_secret_view_locked (không kiểm tra mật khẩu).
            locked_until = state.get("locked_until")
            if isinstance(locked_until, (int, float)) \
                    and locked_until > now:
                self._audit(
                    request.client_ip, "center_secret_view_locked",
                    None, {},
                    {"reason": "too_many_failures",
                     "locked_until": locked_until}, "failed",
                    account_id=account_id)
                body = (
                    "<h1>Thử quá nhiều lần</h1>"
                    "<p>Thử quá nhiều lần. Đợi 15 phút rồi thử lại.</p>"
                    '<p><a href="/?tab=center">Về tab Trung tâm</a></p>'
                )
                return self._with_security(Response.html(
                    403,
                    ui_pages.simple_page_html(
                        "Thử quá nhiều lần", body,
                        csrf_token=session["csrf_token"])))
            # Kiểm tra mật khẩu xem mã (rỗng / quá 2.000 ký tự / sai
            # đều 403 như sai mật khẩu, không phân biệt, không ghi
            # log mật khẩu).
            password = request.form_value("view_password")
            if (not isinstance(password, str) or not password
                    or len(password) > MAX_FORM_FIELD_CHARS
                    or not ui_auth.verify_view_secret_password(
                        self.root, password)):
                def _fail(view_state):
                    attempts = view_state.setdefault(
                        "failed_attempts", {}).setdefault(
                            request.client_ip, [])
                    attempts[:] = [
                        stamp for stamp in attempts
                        if isinstance(stamp, (int, float))
                        and now - stamp
                        < ui_store.CENTER_VIEW_FAILURE_WINDOW_SECONDS]
                    attempts.append(now)
                    if len(attempts) \
                            >= ui_store.CENTER_VIEW_MAX_FAILURES:
                        view_state["locked_until"] = (
                            now + ui_store.CENTER_VIEW_LOCK_SECONDS)

                ui_store.modify_center_view_state(self.root, _fail)
                self._audit(
                    request.client_ip, "center_secret_view_failed",
                    None, {"has_secret": True},
                    {"reason": "wrong_view_password"}, "failed",
                    account_id=account_id)
                body = (
                    "<h1>Mật khẩu xem mã chưa đúng</h1>"
                    "<p>Mật khẩu xem mã chưa đúng. Thử lại.</p>"
                    '<p><a href="/center/accounts/%s/view-secret">'
                    "Thử lại</a></p>"
                ) % ui_pages.esc(account_id)
                return self._with_security(Response.html(
                    403,
                    ui_pages.simple_page_html(
                        "Mật khẩu xem mã chưa đúng", body,
                        csrf_token=session["csrf_token"])))
            # Mật khẩu đúng: xóa lịch sử sai của IP này (cùng mẫu
            # record_login_success) + khóa đã hết hạn.
            def _clear_failures(view_state):
                view_state.get("failed_attempts", {}).pop(
                    request.client_ip, None)
                old_lock = view_state.get("locked_until")
                if isinstance(old_lock, (int, float)) \
                        and old_lock <= now:
                    view_state["locked_until"] = None

            ui_store.modify_center_view_state(self.root, _clear_failures)
            # File trạng thái thiếu (chưa từng tồn tại): phân biệt
            # bằng audit — đã có dòng center_secret_view mà file mất
            # thì fail-closed (từ chối xem cho tới khi xoay mã);
            # chưa có dòng nào thì coi là chưa từng xem (cho xem lần
            # đầu — trường hợp nâng cấp thông thường). C-25: sau khôi
            # phục toàn bộ ui-config/ từ backup thì KHÔNG kết luận
            # "chưa từng xem" bằng audit — dấu khôi phục
            # (center_view_restored.json) + mã cũ hơn mốc khôi phục
            # thì từ chối xem cho tới khi xoay mã.
            if state_status == "missing" and ui_store.audit_has_center_view(
                    self.root, account_id):
                self._audit(
                    request.client_ip, "center_secret_view_blocked",
                    None, {"has_secret": True},
                    {"reason": "view_state_missing"}, "failed",
                    account_id=account_id)
                return self._view_secret_blocked_page(
                    session, "File trạng thái xem mã đã mất — xoay mã "
                             "mới trước khi xem lại.")
            restored_at = ui_store.center_view_restored_at(self.root)
            issued_at = secret_status["issued_at"]
            if (restored_at is not None and issued_at is not None
                    and issued_at < restored_at):
                self._audit(
                    request.client_ip, "center_secret_view_blocked",
                    None, {"has_secret": True},
                    {"reason": "restored_from_backup"}, "failed",
                    account_id=account_id)
                return self._view_secret_blocked_page(
                    session, "Đã khôi phục backup — xoay mã mới trước "
                             "khi xem lại.")
            # Kiểm tra cờ "đã xem" TRƯỚC KHI đọc mã (Q-V060-17):
            # đã xem rồi thì trả trang "đã hiện 1 lần" + audit lần
            # xem lại bị chặn, KHÔNG đọc mã. Kiểm tra + đặt cờ làm
            # trong cùng 1 khóa flock (modify_center_view_state) để
            # 2 tab bấm cùng lúc không hiện lại mã cũ lần 2.
            def _check_and_mark(view_state):
                entry = (view_state.get("accounts") or {}).get(
                    account_id) or {}
                viewed = entry.get("viewed_at")
                if viewed is not None:
                    return viewed
                view_state.setdefault("accounts", {})[account_id] = {
                    "viewed_at": now, "rotated_after_view": False}
                return None

            already_viewed_at = ui_store.modify_center_view_state(
                self.root, _check_and_mark)
            if already_viewed_at is not None:
                if now - already_viewed_at > (
                        ui_store.CENTER_VIEW_ROTATION_DEADLINE_SECONDS):
                    self._audit(
                        request.client_ip,
                        "center_secret_view_blocked",
                        None, {"has_secret": True},
                        {"reason": "rotation_overdue",
                         "viewed_at": already_viewed_at}, "failed",
                        account_id=account_id)
                    return self._view_secret_blocked_page(
                        session, "Mã này đã xem quá 24 giờ mà chưa xoay "
                                 "— xoay mã mới trước khi xem lại.")
                self._audit(
                    request.client_ip, "center_secret_view_blocked",
                    None, {"has_secret": True},
                    {"reason": "already_viewed"}, "failed",
                    account_id=account_id)
                return self._view_secret_blocked_page(
                    session, "Mã này đã hiện 1 lần — xoay mã mới rồi "
                             "xem mã mới.")
            # Chưa xem: tài khoản chưa có mã -> 404 chung (frag-03
            # §4.5); có mã thì đọc cặp mã (đường đọc duy nhất trong
            # UI — ngoại lệ F18(3)), ghi audit, trả trang hiện mã.
            # Cờ "đã xem" đã đặt ở bước trên; nếu mã biến mất giữa
            # chừng (đọc được None) thì trả 404 — cờ vẫn giữ
            # (fail-closed: phải xoay mã mới).
            if not secret_status["has"]:
                return self._not_found(session)
            fetch_secret = center_store.read_center_secret(
                self.root, account_id, "fetch")
            push_secret = center_store.read_center_secret(
                self.root, account_id, "push")
            if fetch_secret is None or push_secret is None:
                return self._not_found(session)

            self._audit(
                request.client_ip, "center_secret_view",
                None, {"has_secret": True},
                {"viewed": True,
                 "auto_hide_seconds":
                     ui_store.CENTER_VIEW_AUTO_HIDE_SECONDS,
                 "rotation_pending": True}, "ok",
                account_id=account_id)
            transfer_active = self._account_transfer_block(
                account_id, now) is not None
            html = ui_pages.view_secret_page_html(
                account_id, record.get("display_name") or account_id,
                fetch_secret.decode("utf-8"),
                push_secret.decode("utf-8"), now,
                transfer_active=transfer_active)
            return self._with_security(Response.html(
                200, html,
                extra_headers={"Cache-Control": "no-store",
                               "Pragma": "no-cache"}))
        if rest != "":
            return self._not_found(session)
        if request.method != "GET":
            return self._method_not_allowed(session)
        account_map = ui_store.read_accounts(
            self.root).get("accounts") or {}
        record = account_map.get(account_id)
        if record is None or record.get("center_removed"):
            return self._not_found(session)
        if not ui_store.center_secret_status(
                self.root, account_id)["has"]:
            return self._not_found(session)
        confirm_token = self._issue_confirm_token(
            session_token, {"kind": "center_view_secret",
                            "account_id": account_id})
        state, _state_status = ui_store.read_center_view_state(self.root)
        locked = bool(
            state is not None
            and isinstance(state.get("locked_until"), (int, float))
            and state["locked_until"] > now)
        viewed_at = None
        overdue = False
        if state is not None:
            entry = (state.get("accounts") or {}).get(account_id) or {}
            viewed_at = entry.get("viewed_at")
            overdue = viewed_at is not None and now - viewed_at > (
                ui_store.CENTER_VIEW_ROTATION_DEADLINE_SECONDS)
        return self._with_security(Response.html(
            200,
            ui_pages.view_secret_password_page_html(
                account_id, record.get("display_name") or account_id,
                session["csrf_token"], confirm_token,
                paused=not record.get("center_participating"),
                transfer_active=self._account_transfer_block(
                    account_id, now) is not None,
                locked=locked, overdue=overdue, viewed_at=viewed_at)))

    # -- cài đặt giao diện + prefs node (T3) -------------------------------

    def _handle_settings(self, request, session):
        if request.method != "GET":
            return self._method_not_allowed(session)
        prefs, theme = ui_store.load_ui_prefs_full(self.root)
        config = center_store.load_center_config(self.root)
        prune_state = center_store.read_prune_state(self.root)
        alert_fraction = ui_store.load_alert_settings(self.root)
        try:
            file_used = self._center_store().store_total_bytes()
        except OSError:
            file_used = None
        return self._with_security(Response.html(
            200,
            ui_pages.ui_settings_page_html(
                prefs, session["csrf_token"],
                center_config=config, prune_state=prune_state,
                file_used_bytes=file_used,
                alert_settings=alert_fraction, theme=theme)))

    # -- cài đặt chung (v0.5.0, T3f — SPEC TT19/TT20, DESIGN §11) ------
    _SETTING_LABELS = {
        "prune_day": ("Ngày chạy dọn", lambda v: dict(
            ui_pages.PRUNE_DAY_LABELS).get(v, v)),
        "prune_retention_days": ("Thời gian giữ lại",
                                 lambda v: "%d ngày" % v),
        "prune_near_full_fraction": ("Ngưỡng tự dọn khi kho gần đầy",
                                     lambda v: "%d%%" % round(v * 100)),
        "overdue_severe_hours": ("Quá hạn bao lâu thì tính là nặng",
                                 lambda v: "%d giờ" % v),
    }

    def _handle_settings_center(self, request, session):
        """POST /api/settings/center: đổi 1 hoặc nhiều khóa trong
        ui-config/center.json. CSRF bắt buộc. Khóa lạ/giá trị ngoài
        khoảng -> 400, KHÔNG ghi gì (từ chối toàn bộ — SPEC TT19).
        Mỗi khóa đổi ghi 1 dòng audit (bot_id "center", giờ, mục nào,
        cũ -> mới — Q7). Đổi là có hiệu lực ngay (Q5)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        raw = {}
        for key in ("prune_day", "prune_retention_days",
                    "prune_near_full_pct", "overdue_severe_hours"):
            value = request.form_value(key, None)
            if value is not None and value != "":
                raw[key] = value
        # Khóa lạ trong form -> từ chối toàn bộ (kể cả khóa của đợt
        # sau chưa thiết kế — tránh ghi lặng lẽ giá trị không ai đọc).
        for key in request.form:
            if key in ("csrf_token",):
                continue
            if key not in ("prune_day", "prune_retention_days",
                           "prune_near_full_pct", "overdue_severe_hours"):
                return self._with_security(Response.text(
                    400, "khóa không hợp lệ: %s" % key))
        updates = {}
        try:
            if "prune_day" in raw:
                updates["prune_day"] = raw["prune_day"]
            if "prune_retention_days" in raw:
                updates["prune_retention_days"] = _parse_int_pct(
                    raw["prune_retention_days"], "prune_retention_days")
            if "prune_near_full_pct" in raw:
                updates["prune_near_full_fraction"] = _parse_int_pct(
                    raw["prune_near_full_pct"],
                    "prune_near_full_pct") / 100.0
            if "overdue_severe_hours" in raw:
                updates["overdue_severe_hours"] = _parse_int_pct(
                    raw["overdue_severe_hours"], "overdue_severe_hours")
        except ValueError as exc:
            return self._with_security(Response.text(400, str(exc)))
        clean, error = center_store.validate_center_config_updates(updates)
        if error is not None:
            return self._with_security(Response.text(400, error))
        # ĐX-07 (T3m): luu xong ve neo nhom chua khoa vua doi —
        # khoa prune_* -> #nhom-don-dep (nhom 1, dung truoc nhat),
        # chi overdue_severe_hours -> #nhom-qua-han; khong khoa nao
        # doi (gui lai dung gia tri cu) -> #nhom-don-dep vi form
        # trung tam nam trong nhom Don dep. Loi 400/403 khong doi
        # redirect (tra trang loi nhu cu).
        if not clean:
            return self._with_security(Response.redirect(
                "/settings#nhom-don-dep"))
        before = center_store.load_center_config(self.root)
        center_store.save_center_config(self.root, clean)
        for key, value in clean.items():
            old = before.get(key)
            if old == value:
                continue
            label, fmt = self._SETTING_LABELS[key]
            self._audit(request.client_ip,
                        "center_settings_%s" % key, "center",
                        "%s: %s" % (label, fmt(old)),
                        "%s: %s" % (label, fmt(value)), "ok")
        anchor = "#nhom-qua-han" if set(clean) == {
            "overdue_severe_hours"} else "#nhom-don-dep"
        return self._with_security(Response.redirect(
            "/settings%s" % anchor))

    _ALERT_SETTING_LABELS = {
        "quota_warn_fraction": ("Ngưỡng cảnh báo quota",
                                lambda v: "%d%%" % round(v * 100)),
    }

    def _handle_settings_alerts(self, request, session):
        """POST /api/settings/alerts: đổi ngưỡng cảnh báo quota trong
        ui-config/alert_settings.json. CSRF bắt buộc. Khóa lạ/giá trị
        ngoài khoảng -> 400, KHÔNG ghi gì (từ chối toàn bộ — SPEC
        TT15/TT20). Mỗi lần đổi ghi 1 dòng audit (bot_id "center",
        giờ, mục nào, cũ -> mới — Q7). Đổi là có hiệu lực ngay (Q5):
        lần tính cảnh báo sau dùng ngưỡng mới (UI đọc file thay hằng
        số — DESIGN §6)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        raw = {}
        value = request.form_value("quota_warn_pct", None)
        if value is not None and value != "":
            raw["quota_warn_pct"] = value
        # Khóa lạ trong form -> từ chối toàn bộ (cùng quy tắc POST
        # /api/settings/center).
        for key in request.form:
            if key in ("csrf_token",):
                continue
            if key != "quota_warn_pct":
                return self._with_security(Response.text(
                    400, "khóa không hợp lệ: %s" % key))
        updates = {}
        try:
            if "quota_warn_pct" in raw:
                updates["quota_warn_fraction"] = _parse_int_pct(
                    raw["quota_warn_pct"], "quota_warn_pct") / 100.0
        except ValueError as exc:
            return self._with_security(Response.text(400, str(exc)))
        clean, error = ui_store.validate_alert_settings_updates(updates)
        if error is not None:
            return self._with_security(Response.text(400, error))
        if not clean:
            # ĐX-06: form canh bao de trong (thieu han truong hoac
            # chuoi rong) -> 400 ro rang, khong redirect lang le,
            # khong ghi audit, khong doi file.
            return self._with_security(Response.text(
                400, "chưa nhập ngưỡng"))
        before = ui_store.load_alert_settings(self.root)
        ui_store.save_alert_settings(self.root, clean)
        new = clean["quota_warn_fraction"]
        if before != new:
            label, fmt = self._ALERT_SETTING_LABELS[
                "quota_warn_fraction"]
            self._audit(request.client_ip,
                        "alert_settings_quota_warn_fraction", "center",
                        "%s: %s" % (label, fmt(before)),
                        "%s: %s" % (label, fmt(new)), "ok")
        # ĐX-07: luu xong (ke ca gui lai dung gia tri cu) ve neo
        # nhom Canh bao.
        return self._with_security(Response.redirect(
            "/settings#nhom-canh-bao"))

    def _handle_prune_now(self, request, session):
        """POST /api/prune-now: nút "Dọn ngay" (Q3). CSRF bắt buộc.
        Chạy 1 lần dọn trong tiến trình UI (giữ store.lock — UI không
        gọi lệnh bọc qua shell, không gọi CLI). Kết quả hiện ở dòng
        "Lần dọn cuối" trên trang Cài đặt chung (đọc prune_state.json
        sau redirect). Lần dọn KHÔNG ghi audit (chỉ ghi prune_state +
        log — DESIGN §3.8)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        # GC-F02: truyen cursor nhu 2 duong don con lai.
        self._center_store().prune_all(
            trigger="manual",
            cursors=center_store.load_center_cursors(self.root))
        # ĐX-04: ve neo dong ket qua don.
        return self._with_security(Response.redirect(
            "/settings#ket-qua-don"))

    def _ui_prefs_response(self, is_json, next_url, collapsed):
        if is_json:
            return self._with_security(Response.text(
                200,
                json.dumps({"ok": True, "collapsed": collapsed},
                           ensure_ascii=False),
                {"Content-Type": "application/json; charset=utf-8"}))
        return self._with_security(Response.redirect(next_url))

    def _handle_ui_prefs(self, request, session):
        """POST /api/ui-prefs: toggle 1 node / mở-đóng tất cả trong
        tab / về mặc định / đổi theme (v0.6.0, T3k). Chấp nhận cả
        form-urlencoded (form POST thường + redirect) và JSON. CSRF
        bắt buộc. KHÔNG audit từng lần toggle; chỉ audit "Về mặc
        định" đúng 1 dòng + đổi theme đúng 1 dòng (D-Q-V060-44,
        chỉ khi giá trị thật sự đổi)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        content_type = request.headers.get("content-type", "")
        is_json = "application/json" in content_type
        if is_json:
            try:
                data = json.loads(request.body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._with_security(Response.text(
                    400, "JSON không hợp lệ"))
            if not isinstance(data, dict):
                return self._with_security(Response.text(
                    400, "JSON không hợp lệ"))
            token = data.get("csrf_token") or request.headers.get(
                "x-csrf-token", "")
            if not token or not _safe_compare(
                    token, session.get("csrf_token", "")) \
                    or not self._origin_ok(request):
                return self._csrf_error(session)
        else:
            if not self._csrf_ok(request, session):
                return self._csrf_error(session)
            data = {"node": request.form_value("node"),
                    "collapsed": request.form_value("collapsed"),
                    "action": request.form_value("action"),
                    "tab": request.form_value("tab"),
                    "theme": request.form_value("theme"),
                    "next": request.form_value("next")}
        action = data.get("action") or ""
        next_url = data.get("next") or ""
        if not next_url.startswith("/"):
            next_url = "/"
        if action == "set-theme":
            # Công tắc theme (v0.6.0, T3k — frag-04 §3.3): chỉ nhận
            # "light"/"dark" (POST theme=hades KHÔNG hợp lệ); giá trị
            # lạ -> 400, không ghi gì. Lưu qua set_ui_theme (flock +
            # tmp+rename, 600, giữ nguyên collapsed + khóa lạ).
            theme = data.get("theme") or ""
            if theme not in ui_store.UI_THEME_VALUES:
                return self._with_security(Response.text(
                    400, "theme không hợp lệ"))
            old_theme, _new_theme = ui_store.set_ui_theme(
                self.root, theme)
            if old_theme != theme:
                # D-Q-V060-44: đổi theme ghi 1 dòng audit (giờ, mục
                # "theme", cũ -> mới; bot_id "center" như audit đổi
                # cài đặt trung tâm). Lưu lại cùng giá trị thì không
                # phải "đổi" nên không ghi.
                self._audit(request.client_ip, "ui-prefs-theme",
                            "center", old_theme, theme, "ok")
            collapsed = ui_store.load_ui_prefs(self.root)
            return self._ui_prefs_response(is_json, next_url, collapsed)
        action = data.get("action") or ""
        next_url = data.get("next") or ""
        if not next_url.startswith("/"):
            next_url = "/"
        if action == "reset-defaults":
            ui_store.reset_ui_prefs(self.root)
            self._audit(request.client_ip, "ui-prefs-reset", None, None,
                        "defaults", "ok")
            collapsed = ui_store.load_ui_prefs(self.root)
            return self._ui_prefs_response(is_json, next_url, collapsed)
        if action in ("open-all", "close-all"):
            tab = data.get("tab") or ""
            keys = ui_store.UI_PREF_TAB_KEYS.get(tab)
            if keys is None:
                return self._with_security(Response.text(
                    400, "tab không hợp lệ"))
            closed = action == "close-all"
            _result, collapsed = ui_store.modify_ui_prefs(
                self.root,
                lambda prefs: [prefs.__setitem__(key, closed)
                               for key in keys])
            return self._ui_prefs_response(is_json, next_url, collapsed)
        node = data.get("node") or ""
        if node not in ui_store.UI_PREF_KEYS:
            return self._with_security(Response.text(
                400, "node không hợp lệ"))
        raw = data.get("collapsed")
        if isinstance(raw, bool):
            closed = raw
        elif isinstance(raw, str) and raw.lower() in ("true", "false"):
            closed = raw.lower() == "true"
        else:
            return self._with_security(Response.text(
                400, "collapsed không hợp lệ"))
        _result, collapsed = ui_store.modify_ui_prefs(
            self.root, lambda prefs: prefs.__setitem__(node, closed))
        return self._ui_prefs_response(is_json, next_url, collapsed)

    def _handle_alert_ack(self, request, session):
        """POST /alerts/ack: xác nhận 1 cảnh báo đang mở (V2 §5).

        Chỉ POST + CSRF; bot phải có trong registry, khóa phải thuộc
        ALERT_KEYS. Xong thì quay lại tab Cảnh báo giữ nguyên bộ lọc.
        """
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        bot_id = request.form_value("bot_id")
        key = request.form_value("key")
        entry = self._registry_entry(bot_id) if _valid_bot_id(bot_id) else None
        if entry is None or key not in ui_store.ALERT_KEYS:
            return self._not_found(session)
        now = time.time()
        summary = ui_store.bot_summary(
            self.root, bot_id, entry, now=now,
            unit_state=self.unit_status_fn(bot_id),
            quota_warn_fraction=ui_store.load_alert_settings(self.root))
        text = None
        for alert in summary.get("alerts") or []:
            if alert["key"] == key:
                text = alert.get("text")
                break
        ui_store.ack_alert(self.root, bot_id, key, now, text=text)
        self._audit(request.client_ip, "ack-alert", bot_id, key, "acked",
                    "ok")
        view = request.form_value("view") or "open"
        if view not in self._VALID_VIEWS:
            view = "open"
        redirect_params = {"tab": "alerts", "view": view}
        for field in ("bot", "platform"):
            value = request.form_value(field)
            if value:
                redirect_params[field] = value
        need_owner = request.form_value("need_owner")
        if need_owner == "1":
            redirect_params["need_owner"] = "1"
        return self._with_security(Response.redirect(
            "/?" + urlencode(redirect_params)))

    def _handle_history_fragment(self, request, session):
        bot_id = request.query.get("bot", [""])[0]
        if not _valid_bot_id(bot_id) or self._registry_entry(bot_id) is None:
            return self._with_security(Response.text(404, "not found"))
        page = self._parse_page(request)
        chat_filter = request.query.get("chat", [""])[0]
        status_filter = request.query.get("status", [""])[0]
        if status_filter not in self._VALID_STATUSES:
            status_filter = ""
        now = time.time()
        items, total, page, pages, sync = ui_store.merged_history(
            self.root, bot_id, now=now, page=page,
            per_page=ui_store.HISTORY_PER_PAGE,
            chat_filter=ui_store.resolve_chat_filter(
                self.root, bot_id, chat_filter),
            status_filter=status_filter or None)
        allowlist = ui_store.read_allowlist(self.root, bot_id) or {}
        fragment = ui_pages.history_fragment_html(
            items, sync, allowlist.get("allowed_chats") or [],
            session["csrf_token"], bot_id)
        return self._with_security(Response.html(200, fragment))

    def _handle_log_fragment(self, request, session):
        bot_id = request.query.get("bot", [""])[0]
        if not _valid_bot_id(bot_id) or self._registry_entry(bot_id) is None:
            return self._with_security(Response.text(404, "not found"))
        lines = [ui_store.redact_log_line(line)
                 for line in self.log_fn(bot_id, 50)]
        return self._with_security(Response.html(
            200, ui_pages.log_fragment_html(lines)))

    @staticmethod
    def _parse_page(request):
        raw = request.query.get("page", ["1"])[0]
        try:
            page = int(raw)
        except (TypeError, ValueError):
            page = 1
        return max(1, page)

    # -- route theo bot ----------------------------------------------------

    def _append_retry_dead_request(self, root, bot_id, reply_key):
        """Ghi 1 yeu cau gui lai thu chet (NS-02, T3m — quyet D11 (b))
        vao ``data/<bot_id>/retry_dead_requests.jsonl``: append duoi
        khoa flock rieng, quyen 600. ``seq`` tang dan de gateway
        phuc vu theo cursor; ``new_reply_key`` do UI sinh ngay luc
        bam (de audit ghi duoc ma lan thu moi, va de phia Muse
        dung dung ma nay lam delivery_id — chong gui trung khi keo
        lap). Yeu cau KHONG mang noi dung tin. Tra ve request dict.
        """
        bot_dir = Path(root) / "data" / bot_id
        bot_dir.mkdir(parents=True, exist_ok=True)
        path = bot_dir / "retry_dead_requests.jsonl"
        lock_path = bot_dir / ".retry_dead_requests.lock"
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            max_seq = 0
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    seq = row.get("seq")
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        max_seq = max(max_seq, seq)
            request = {
                "seq": max_seq + 1,
                "request_id": secrets.token_hex(16),
                "reply_key": reply_key,
                "new_reply_key": secrets.token_hex(16),
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o600)
            try:
                os.write(fd, (json.dumps(request, ensure_ascii=False)
                              + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            return request
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _handle_retry_dead(self, request, session, session_token,
                           bot_id):
        """POST /api/bots/<bot_id>/retry-dead: bam "Gui lai" / "Gui lai
        tat ca" o muc Thu chet (NS-02, T3m — quyet D11 (b)).

        Chi ghi yeu cau vao file phia PC; gateway phuc vu qua endpoint
        moi, fetcher keo moi chu ky, retry_dead xu ly phia Muse. Moi
        thu duoc gui lai ghi 1 dong audit (action ``bot_retry_dead``;
        before = reply_key thu cu, after = reply_key lan thu moi —
        khong ghi noi dung tin). Chi thu lai thu con ``dead`` tai
        thoi diem bam; thu da gui thanh cong thi bo qua (khong gui
        trung). Khong gioi han cung so lan bam tay (Q-V060-36)."""
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        if not _valid_bot_id(bot_id):
            return self._not_found(session)
        if self._registry_entry(bot_id) is None:
            return self._not_found(session)
        form = request.form
        retry_all = form.get("retry_all", [""])[0] == "1"
        live_tips, _total, _page, _pages = ui_store.dead_letters(
            self.root, bot_id, now=time.time(), page=1, per_page=200)
        live = {tip["root_key"] for tip in live_tips}
        if retry_all:
            # G-M1 (AUDIT-V060): lap het cac trang (moi trang toi da
            # 200) — truoc day chi quet trang 1 nen thu chet ngoai
            # 200 bi bo sot im lang, UI khong bao gi.
            root_keys = [tip["root_key"] for tip in live_tips]
            for page in range(2, _pages + 1):
                more, _t, _p, _pp = ui_store.dead_letters(
                    self.root, bot_id, now=time.time(), page=page,
                    per_page=200)
                live.update(tip["root_key"] for tip in more)
                root_keys.extend(tip["root_key"] for tip in more)
        else:
            root_key = form.get("reply_key", [""])[0]
            if not root_key:
                return self._with_security(Response.html(
                    400,
                    ui_pages.simple_page_html(
                        "Thiếu mã thư",
                        "<h1>Thiếu mã thư</h1>"
                        "<p>Không rõ thư nào cần gửi lại.</p>"
                        '<p><a href="/bots/%s">Về trang bot</a></p>'
                        % bot_id)))
            root_keys = [root_key]
        # (v0.6.0, NS-R1 — D17) Dem so yeu cau ghi that (chi thu con
        # dead moi dem) de bao lai so N dung tren trang bot.
        written = 0
        for root_key in root_keys:
            if root_key not in live:
                continue
            request_row = self._append_retry_dead_request(
                self.root, bot_id, root_key)
            self._audit(request.client_ip, "bot_retry_dead", bot_id,
                        root_key, request_row["new_reply_key"], "ok")
            written += 1
        # (v0.6.0, NS-R1 — D17) Cau xac nhan 1 lan (flash trong
        # session): trang bot doc roi xoa ngay, tai lai khong hien
        # lai. Redirect GIU NGUYEN "/bots/<id>" (khong them
        # "?retry=N" — test t3m bat Location ket thuc dung bang
        # "/bots/bot-a"; UI hien chua co co che flash/param san nen
        # dung session + helper save_session moi o ui_auth).
        session["retry_notice"] = {"bot_id": bot_id, "count": written}
        ui_auth.save_session(self.root, session_token, session)
        return self._with_security(Response.redirect(
            "/bots/%s" % bot_id))

    def _handle_bot_route(self, request, session, session_token, bot_id, rest):
        if not _valid_bot_id(bot_id):
            return self._not_found(session)
        entry = self._registry_entry(bot_id)
        if rest in ("", "/"):
            if request.method != "GET":
                return self._method_not_allowed(session)
            if entry is None:
                return self._not_found(session)
            return self._handle_bot_detail(request, session, session_token,
                                           bot_id, entry)
        if entry is None:
            return self._not_found(session)
        if rest == "/edit":
            return self._handle_bot_edit(request, session, bot_id, entry)
        if rest == "/control":
            return self._handle_control_confirm(request, session,
                                                session_token, bot_id, entry)
        if rest == "/control/execute":
            return self._handle_control_execute(request, session,
                                                session_token, bot_id, entry)
        if rest == "/replace-token":
            return self._handle_replace_token(request, session, bot_id, entry)
        if rest == "/confirm-vault":
            return self._handle_confirm_vault(request, session, bot_id, entry)
        if rest == "/transfer":
            return self._handle_transfer(request, session, bot_id, entry)
        if rest == "/transfer/start":
            return self._handle_transfer_start(request, session, bot_id, entry)
        if rest == "/transfer/step":
            return self._handle_transfer_step(request, session, bot_id, entry)
        if rest == "/transfer/retry-step7":
            return self._handle_transfer_retry_step7(
                request, session, bot_id, entry)
        if rest == "/transfer/rollback":
            return self._handle_transfer_rollback(
                request, session, bot_id, entry)
        if rest == "/transfer/cancel":
            return self._handle_transfer_cancel(
                request, session, session_token, bot_id, entry)
        if rest == "/transfer/cancel/execute":
            return self._handle_transfer_cancel_execute(
                request, session, session_token, bot_id, entry)
        if rest == "/transfer/dismiss-reminder":
            return self._handle_transfer_dismiss_reminder(
                request, session, bot_id, entry)
        if rest == "/delete":
            return self._handle_delete_confirm(request, session,
                                               session_token, bot_id, entry)
        if rest == "/delete/execute":
            return self._handle_delete_execute(request, session,
                                               session_token, bot_id, entry)
        if rest == "/add-chat":
            return self._handle_add_chat_confirm(request, session,
                                                 session_token, bot_id, entry)
        if rest == "/add-chat/execute":
            return self._handle_add_chat_execute(request, session,
                                                 session_token, bot_id, entry)
        return self._not_found(session)

    def _handle_bot_detail(self, request, session, session_token,
                           bot_id, entry):
        now = time.time()
        # (v0.6.0, NS-R1 — D17) Doc + xoa flash "gui lai" 1 lan:
        # chi hien o lan mo trang ngay sau cu bam, tai lai thi het.
        retry_notice = None
        raw_notice = session.pop("retry_notice", None)
        if raw_notice is not None:
            ui_auth.save_session(self.root, session_token, session)
            if isinstance(raw_notice, dict) \
                    and raw_notice.get("bot_id") == bot_id:
                count = raw_notice.get("count")
                if isinstance(count, int) and not isinstance(count, bool) \
                        and count >= 0:
                    retry_notice = count
        prefs = ui_store.load_ui_prefs(self.root)
        detail_url = "/bots/%s" % bot_id
        unit_state = self.unit_status_fn(bot_id)
        summary = ui_store.bot_summary(self.root, bot_id, entry, now=now,
                                       unit_state=unit_state,
                                       quota_warn_fraction=
                                       ui_store.load_alert_settings(
                                           self.root))
        ui_store.record_poll_sample(self.root, bot_id, summary, now)
        page = self._parse_page(request)
        chat_filter = request.query.get("chat", [""])[0]
        status_filter = request.query.get("status", [""])[0]
        if status_filter not in self._VALID_STATUSES:
            status_filter = ""
        # NS-02 (T3m): muc Thu chet — phan trang rieng (dead_page),
        # khong dung chung page cua lich su.
        try:
            dead_page = int(request.query.get("dead_page", ["1"])[0])
        except (TypeError, ValueError):
            dead_page = 1
        dead_letters = ui_store.dead_letters(
            self.root, bot_id, now=now, page=max(1, dead_page),
            per_page=20)
        # Node đóng -> bỏ qua lệnh đọc của node đó (DESIGN §8.1).
        # Chỉ giữ lại lệnh đếm nhẹ cho tóm tắt (allowlist: 1 file JSON
        # nhỏ cho "N chat được phép"; sync: trạng thái đồng bộ).
        if prefs.get("message-history"):
            items, total, page, pages = [], 0, 1, 1
            sync = ui_store.sync_status(self.root, bot_id, now)
        else:
            items, total, page, pages, sync = ui_store.merged_history(
                self.root, bot_id, now=now, page=page,
                per_page=ui_store.HISTORY_PER_PAGE,
                chat_filter=ui_store.resolve_chat_filter(
                    self.root, bot_id, chat_filter),
                status_filter=status_filter or None)
        if prefs.get("config"):
            config = {}
        else:
            config = ui_store.read_bot_config(self.root, bot_id) or {}
        allowlist = ui_store.read_allowlist(self.root, bot_id) or {}
        if prefs.get("ear-log"):
            log_lines = []
        else:
            log_lines = [ui_store.redact_log_line(line)
                         for line in self.log_fn(bot_id, 50)]
        history_fragment = ui_pages.history_fragment_html(
            items, sync, allowlist.get("allowed_chats") or [],
            session["csrf_token"], bot_id)
        ui_store.mark_alerts_seen(
            self.root, bot_id, [a["key"] for a in summary.get("alerts", [])],
            now)
        accounts = ui_store.read_accounts(self.root)
        reminder = ui_store.persistent_reminder_for_bot(
            ui_store.read_transfer_log(self.root), bot_id)
        if prefs.get("stats-7d"):
            daily = []
        else:
            daily = ui_store.daily_stats(self.root, bot_id, now)
        if prefs.get("alerts-history-detail"):
            alert_events = []
        else:
            alert_events = ui_store.read_alert_events(
                self.root, now, bot_id=bot_id)
        if prefs.get("health"):
            poll_samples = []
        else:
            poll_samples = ui_store.read_poll_samples(
                self.root, bot_id, now)
        if prefs.get("message-history"):
            chats = []
        else:
            chats = ui_store.list_chats(self.root, bot_id)
        node_summaries = self._detail_node_summaries(
            prefs, summary, allowlist, now)
        return self._with_security(Response.html(
            200,
            ui_pages.bot_detail_page_html(
                summary, history_fragment, total, page, pages, sync, config,
                allowlist, ui_pages.log_fragment_html(log_lines),
                session["csrf_token"], chat_filter=chat_filter,
                status_filter=status_filter,
                daily=daily,
                alert_events=alert_events,
                poll_samples=poll_samples,
                chats=chats,
                accounts=accounts, reminder=reminder,
                dual_hidden=ui_store.summary_dual_fetch_hidden(
                    summary, now),
                prefs=prefs, next_url=detail_url,
                node_summaries=node_summaries,
                dead_letters=dead_letters,
                retry_notice=retry_notice)))

    def _detail_node_summaries(self, prefs, summary, allowlist, now=None):
        """Tóm tắt các node ĐÓNG ở trang chi tiết — chỉ dùng dữ liệu
        đã đọc sẵn (summary/allowlist), không đọc thêm file nặng.

        Lech T3 (b), T3m: 2 node lich su + Lich su tin nay co du
        so lieu — Lich su canh bao: "N dong gan nhat · moi nhat
        HH:MM" (dem nhe, khong goi read_alert_events); Lich su tin:
        "N muc/trang · tong N muc — moi nhat HH:MM" (dem nhe, khong
        goi merged_history). Log tai nghe giu "50 dòng cuối" (gio
        dong cuoi chi co khi chay journalctl — lenh doc cua node
        dong bi bo qua nen khong lay duoc, ghi ro o BAN-GIAO-T3m).
        """
        if now is None:
            now = time.time()
        out = {}
        if prefs.get("health"):
            poll_text = "lần poll cuối chưa rõ"
            age = summary.get("state_age_seconds")
            if age is not None:
                minutes = int(age // 60)
                if minutes <= 0:
                    poll_text = "lấy tin lần cuối: dưới 1 phút trước"
                elif minutes == 1:
                    poll_text = "lấy tin lần cuối: 1 phút trước"
                else:
                    poll_text = ("lấy tin lần cuối: %d phút trước"
                                 % minutes)
            elif summary.get("last_poll_at"):
                poll_text = "lấy tin lần cuối: %s" % (
                    summary["last_poll_at"])
            out["health"] = "%d mẫu · %s" % (
                ui_store.POLL_SAMPLE_LIMIT, poll_text)
        if prefs.get("controls"):
            if summary.get("paused"):
                state = "Tạm dừng"
            elif summary.get("enabled"):
                state = "Bật"
            else:
                state = "Tắt"
            out["controls"] = "Trạng thái hiện tại: %s" % state
        if prefs.get("config"):
            chats_allowed = allowlist.get("allowed_chats") or []
            out["config"] = "5 trường · %d chat được phép" % len(
                chats_allowed)
        if prefs.get("stats-7d"):
            out["stats-7d"] = "7 ngày · hôm nay %s tin" % ui_pages._fmt_num(
                summary.get("today_in", 0))
        if prefs.get("alerts-history-detail"):
            count, newest = ui_store.alert_events_totals(
                self.root, now, bot_id=summary.get("bot_id"))
            out["alerts-history-detail"] = (
                self._history_lines_summary(count, newest))
        if prefs.get("fetches"):
            state = ui_store.label_display_state(
                summary.get("fetch_entry"))
            labels = state.get("labels") or []
            total = sum(int(label.get("fetch_count") or 0)
                        for label in labels)
            unlab = state.get("unlabelled")
            if unlab is not None:
                total += int(unlab.get("fetch_count") or 0)
            newest = None
            for label in labels:
                seen = label.get("last_seen_at")
                if seen and (newest is None or seen > newest):
                    newest = seen
            if unlab is not None and unlab.get("last_seen_at"):
                seen = unlab["last_seen_at"]
                if newest is None or seen > newest:
                    newest = seen
            text = "%d lượt" % total
            if isinstance(newest, (int, float)):
                hhmm = self._hhmm(newest)
                if hhmm:
                    text += " — mới nhất %s" % hhmm
            out["fetches"] = text
        if prefs.get("message-history"):
            total, newest = ui_store.history_totals(
                self.root, summary.get("bot_id"), now)
            text = "%d mục/trang · tổng %d mục" % (
                ui_store.HISTORY_PER_PAGE, total)
            hhmm = self._hhmm(newest) if newest else None
            if hhmm:
                text += " — mới nhất %s" % hhmm
            out["message-history"] = text
        if prefs.get("ear-log"):
            out["ear-log"] = "50 dòng cuối"
        return out

    # -- điều khiển bot (UI2) ----------------------------------------------

    _CONTROL_ACTIONS = ("enable", "disable", "restart", "pause", "resume")

    def _control_consequence(self, action, entry):
        platform = entry.get("platform", "zalo")
        name = entry.get("display_name") or entry.get("bot_id", "")
        if platform == "zalo":
            offline_note = (
                "Tin nhắn gửi tới trong lúc bot tắt sẽ MẤT — Zalo không "
                "giữ lại tin khi tai không nghe.")
        else:
            offline_note = (
                "Tin nhắn gửi tới trong lúc bot tắt sẽ chờ trên máy chủ "
                "Telegram tối đa 24 giờ rồi mới mất.")
        texts = {
            "enable": (
                "Bật bot <strong>%s</strong>: tai sẽ bắt đầu nghe tin "
                "mới. Bot chỉ trả lời các chat trong danh sách cho phép."
                % ui_pages.esc(name)),
            "disable": (
                "Tắt bot <strong>%s</strong>: tai ngừng nghe tin. %s"
                % (ui_pages.esc(name), offline_note)),
            "restart": (
                "Khởi động lại tai nghe của bot <strong>%s</strong>. "
                "Trong lúc khởi động lại: %s"
                % (ui_pages.esc(name), offline_note)),
            "pause": (
                "Tạm dừng bot <strong>%s</strong>: bot vẫn nghe và giữ "
                "tin, nhưng KHÔNG trả lời. Khi bật lại, tin tồn quá 30 "
                "phút sẽ được trả lời gộp, có câu thừa nhận chậm trễ "
                "(quyết định D1)." % ui_pages.esc(name)),
            "resume": (
                "Bật lại bot <strong>%s</strong>: bot trả lời trở lại. "
                "Tin tồn quá 30 phút sẽ được trả lời gộp, có câu thừa "
                "nhận chậm trễ (quyết định D1)." % ui_pages.esc(name)),
        }
        return texts[action]

    _CONTROL_LABELS = {
        "enable": "Bật bot",
        "disable": "Tắt bot",
        "restart": "Khởi động lại",
        "pause": "Tạm dừng",
        "resume": "Bật lại",
    }

    def _handle_control_confirm(self, request, session, session_token,
                                bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        action = request.form_value("action")
        if action not in self._CONTROL_ACTIONS:
            return self._not_found(session)
        confirm_token = self._issue_confirm_token(
            session_token,
            {"kind": "control", "bot_id": bot_id, "action": action})
        intro = "<p>%s</p>" % self._control_consequence(action, entry)
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                self._CONTROL_LABELS[action], intro,
                "/bots/%s/control/execute" % bot_id,
                {"confirm_token": confirm_token},
                session["csrf_token"],
                self._CONTROL_LABELS[action],
                danger=(action == "disable"),
                back_url="/bots/%s" % bot_id)))

    def _handle_control_execute(self, request, session, session_token,
                                bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "control"
                or payload.get("bot_id") != bot_id):
            return self._with_security(Response.html(
                403,
                ui_pages.simple_page_html(
                    "Xác nhận đã hết hạn",
                    "<h1>Xác nhận đã hết hạn</h1>"
                    "<p>Quay lại trang bot và bấm lại nút điều khiển.</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))
        action = payload["action"]
        try:
            with self._bot_lock(bot_id):
                return self._execute_control(
                    request, session, bot_id, action)
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1>"
                    "<p>Một thao tác khác đang chạy trên bot này. "
                    "Đợi vài giây rồi thử lại.</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))

    def _execute_control(self, request, session, bot_id, action):
        entry = self._registry_entry(bot_id)
        if entry is None:
            return self._not_found(session)
        before = {"enabled": entry.get("enabled"),
                  "paused": entry.get("paused")}
        if action == "enable":
            if ui_store.read_allowlist(self.root, bot_id) is None:
                return self._control_error(
                    session, bot_id,
                    "Chưa bật được: bot chưa có danh sách chat được phép "
                    "(D5). Thêm danh sách rồi mới bật.")
            if ui_store.is_awaiting_muse_sync(
                    entry, ui_store.read_fetch_state(self.root), bot_id):
                return self._control_error(
                    session, bot_id,
                    "Chưa bật được: đang chờ đồng bộ về Muse. Nút Bật mở "
                    "khi phía Muse lấy tin lần đầu thành công.")
            if entry.get("enabled"):
                return self._control_result(
                    session, bot_id, "Bot đã bật rồi — không làm gì thêm.")
            code, output = self.unit_runner("enable", bot_id)
            if code != 0:
                self._audit(request.client_ip, "control_enable", bot_id,
                            before, before, "failed")
                return self._control_error(
                    session, bot_id,
                    "Không bật được unit (systemctl báo lỗi). Xem log tai "
                    "nghe ở trang chi tiết.")
            # Dọn lười cờ chờ: fetch đầu đã thành công (kiểm tra ở
            # trên) nên cờ trong registry không còn đúng nữa; xóa
            # cùng lần ghi này để registry phản ánh đúng sự thật.
            ui_store.modify_registry(self.root, lambda data: data["bots"][bot_id].update({"enabled": True, "awaiting_muse_sync": False}))
            after = {"enabled": True, "paused": entry.get("paused")}
            self._audit(request.client_ip, "control_enable", bot_id,
                        before, after, "ok")
            return self._control_result(
                session, bot_id, "Đã bật bot. " + self._unit_state_text(bot_id))
        if action == "disable":
            if not entry.get("enabled"):
                return self._control_result(
                    session, bot_id, "Bot đã tắt rồi — không làm gì thêm.")
            code, output = self.unit_runner("disable", bot_id)
            if code != 0:
                self._audit(request.client_ip, "control_disable", bot_id,
                            before, before, "failed")
                return self._control_error(
                    session, bot_id,
                    "Không tắt được unit (systemctl báo lỗi).")
            ui_store.modify_registry(self.root, lambda data: data["bots"][bot_id].update({"enabled": False}))
            after = {"enabled": False, "paused": entry.get("paused")}
            self._audit(request.client_ip, "control_disable", bot_id,
                        before, after, "ok")
            return self._control_result(
                session, bot_id, "Đã tắt bot. " + self._unit_state_text(bot_id))
        if action == "restart":
            code, output = self.unit_runner("restart", bot_id)
            if code != 0:
                self._audit(request.client_ip, "control_restart", bot_id,
                            before, before, "failed")
                return self._control_error(
                    session, bot_id,
                    "Không khởi động lại được unit (systemctl báo lỗi).")
            self._audit(request.client_ip, "control_restart", bot_id,
                        before, before, "ok")
            return self._control_result(
                session, bot_id,
                "Đã khởi động lại tai nghe. " + self._unit_state_text(bot_id))
        if action == "pause":
            if entry.get("paused"):
                return self._control_result(
                    session, bot_id, "Bot đã tạm dừng rồi — không làm gì thêm.")
            ui_store.modify_registry(self.root, lambda data: data["bots"][bot_id].update({"paused": True}))
            after = {"enabled": entry.get("enabled"), "paused": True}
            self._audit(request.client_ip, "control_pause", bot_id,
                        before, after, "ok")
            return self._control_result(
                session, bot_id,
                "Đã tạm dừng: bot vẫn nghe và giữ tin, không trả lời. "
                "Phía Muse nhận cờ tạm dừng ở chu kỳ lấy tin tiếp theo.")
        if action == "resume":
            if not entry.get("paused"):
                return self._control_result(
                    session, bot_id, "Bot không ở trạng thái tạm dừng.")
            ui_store.modify_registry(self.root, lambda data: data["bots"][bot_id].update({"paused": False}))
            after = {"enabled": entry.get("enabled"), "paused": False}
            self._audit(request.client_ip, "control_resume", bot_id,
                        before, after, "ok")
            return self._control_result(
                session, bot_id,
                "Đã bật lại: bot trả lời trở lại. Tin tồn quá 30 phút sẽ "
                "được trả lời gộp theo D1.")
        return self._not_found(session)

    def _unit_state_text(self, bot_id):
        state = self.unit_status_fn(bot_id)
        return {"active": "Trạng thái unit hiện tại: đang chạy.",
                "inactive": "Trạng thái unit hiện tại: đang dừng.",
                "unknown": "Không đọc được trạng thái unit."}.get(state, "")

    def _control_result(self, session, bot_id, message):
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Xong",
                "<h1>Xong</h1><p>%s</p>"
                '<p><a href="/bots/%s">Về trang bot</a> · '
                '<a href="/">Màn hình chính</a></p>'
                % (ui_pages.esc(message), bot_id),
                csrf_token=session["csrf_token"])))

    def _control_error(self, session, bot_id, message):
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Chưa làm được",
                "<h1>Chưa làm được</h1>"
                '<div class="field-error">%s</div>'
                '<p><a href="/bots/%s">Về trang bot</a></p>'
                % (ui_pages.esc(message), bot_id),
                csrf_token=session["csrf_token"])))

    # -- thêm / sửa bot (UI3) ----------------------------------------------

    def _parse_list_field(self, request, key):
        values = []
        raw = request.form_value(key)
        for line in raw.splitlines():
            item = line.strip()
            if item and len(item) <= MAX_FORM_FIELD_CHARS:
                values.append(item)
        seen = set()
        out = []
        for value in values:
            if value not in seen:
                seen.add(value)
                out.append(value)
        return out

    def _validate_bot_fields(self, request, is_new):
        """Trả (values, errors). Không bao giờ trả token về phía hiển thị."""
        values = {
            "bot_id": request.form_value("bot_id").strip(),
            "display_name": request.form_value("display_name").strip(),
            "platform": request.form_value("platform").strip(),
            "bot_kind": request.form_value("bot_kind").strip() or "one_to_one",
            "owner_account": request.form_value("owner_account").strip(),
            "allowed_chats": self._parse_list_field(request, "allowed_chats"),
            "owner_sender_ids": self._parse_list_field(
                request, "owner_sender_ids"),
            "default_sender_policy": request.form_value(
                "default_sender_policy").strip() or "deny",
            "group_privacy": request.form_value("group_privacy").strip() or "on",
        }
        errors = []
        if values["owner_account"]:
            account_map = ui_store.read_accounts(
                self.root).get("accounts") or {}
            if values["owner_account"] not in account_map:
                errors.append(
                    "Tài khoản quản lý không có trong danh sách tài khoản.")
        else:
            values["owner_account"] = ""
        if is_new:
            if not _valid_bot_id(values["bot_id"]):
                errors.append(
                    "ID bot chỉ gồm chữ thường, số, gạch ngang; bắt đầu "
                    "bằng chữ/số; tối đa 32 ký tự.")
            elif self._registry_entry(values["bot_id"]) is not None:
                errors.append("ID bot đã tồn tại. Chọn ID khác.")
        if not values["display_name"]:
            errors.append("Tên hiển thị không được để trống.")
        elif len(values["display_name"]) > MAX_DISPLAY_NAME_CHARS:
            errors.append("Tên hiển thị tối đa %d ký tự."
                          % MAX_DISPLAY_NAME_CHARS)
        if values["platform"] not in ("zalo", "telegram"):
            errors.append("Nền tảng phải là Zalo hoặc Telegram.")
        if values["bot_kind"] not in ("one_to_one", "group"):
            errors.append("Vai trò không hợp lệ.")
        if values["default_sender_policy"] not in ("deny", "allow"):
            errors.append("Chính sách người lạ không hợp lệ.")
        if values["group_privacy"] not in ("on", "off"):
            errors.append("Privacy nhóm không hợp lệ.")
        return values, errors

    def _duplicate_bot_id(self, platform, platform_bot_id, exclude_bot_id=None):
        """True nếu platform_bot_id đã thuộc về 1 bot khác (trùng token).

        So sánh theo nền tảng của bản ghi registry (ảnh chụp getMe
        không mang trường platform) + id trích từ ảnh chụp.
        """
        registry = ui_store.load_registry(self.root)
        for bot_id, other in (registry.get("bots") or {}).items():
            if bot_id == exclude_bot_id:
                continue
            if not isinstance(other, dict):
                continue
            if other.get("platform") != platform:
                continue
            other_id = ui_store.capability_bot_id(
                ui_store.read_capability(self.root, bot_id))
            if other_id is not None and str(other_id) == str(platform_bot_id):
                return True
        return False

    def _handle_bot_new(self, request, session):
        accounts = self._accounts_list()
        if request.method == "GET":
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html("new", {}, [], session["csrf_token"],
                                       accounts=accounts)))
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        values, errors = self._validate_bot_fields(request, is_new=True)
        token = request.form_value("token")
        if not token:
            errors.append("Token không được để trống.")
        elif len(token) > MAX_FORM_FIELD_CHARS:
            errors.append("Token quá dài.")
        form_values = dict(values)
        form_values["allowed_chats"] = "\n".join(values["allowed_chats"])
        form_values["owner_sender_ids"] = "\n".join(values["owner_sender_ids"])
        if errors:
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html("new", form_values, errors,
                                       session["csrf_token"],
                                       accounts=accounts)))
        endpoints = ui_store.read_platform_endpoints(self.root)
        base_url = endpoints.get(values["platform"])
        if not base_url:
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html(
                    "new", form_values,
                    ["Chưa cấu hình địa chỉ API cho nền tảng này trên máy "
                     "tính (file platform_endpoints.json). Nhờ người quản "
                     "lý máy điền địa chỉ rồi thử lại. Chưa lưu gì."],
                    session["csrf_token"], accounts=accounts)))
        ok, info = self.verify_fn(values["platform"], base_url, token)
        if not ok:
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html(
                    "new", form_values,
                    ["Token không đúng hoặc không kết nối được tới nền "
                     "tảng. Kiểm tra lại token rồi thử lại. Chưa lưu gì."],
                    session["csrf_token"], accounts=accounts)))
        platform_bot_id = info.get("id") or ""
        if platform_bot_id and self._duplicate_bot_id(
                values["platform"], platform_bot_id):
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html(
                    "new", form_values,
                    ["Token này thuộc về 1 bot đã có trên hệ thống. "
                     "Mỗi bot chỉ có 1 bản ghi. Chưa lưu gì."],
                    session["csrf_token"], accounts=accounts)))
        bot_id = values["bot_id"]
        try:
            with self._bot_lock(bot_id):
                if self._registry_entry(bot_id) is not None:
                    return self._with_security(Response.html(
                        200,
                        ui_pages.bot_form_html(
                            "new", form_values,
                            ["ID bot vừa bị tạo bởi 1 thao tác khác. "
                             "Chọn ID khác."],
                            session["csrf_token"], accounts=accounts)))
                queue_secret = secrets.token_urlsafe(32)
                sync_secret = secrets.token_urlsafe(32)
                self._write_secret_file(bot_id, "token", token)
                self._write_secret_file(bot_id, "queue_secret", queue_secret)
                self._write_secret_file(bot_id, "sync_secret", sync_secret)
                config = {
                    "bot_id": bot_id,
                    "platform": values["platform"],
                    "display_name": values["display_name"],
                    "bot_kind": values["bot_kind"],
                    "group_privacy": values["group_privacy"],
                    "default_sender_policy": values["default_sender_policy"],
                    # Tai nghe BẮT BUỘC đọc api_base_url từ config này;
                    # thiếu thì tai thoát lỗi lặp (79 lần sáng 16/11).
                    # Lấy đúng địa chỉ vừa dùng để kiểm tra token (từ
                    # platform_endpoints.json do agent PC cấu hình) —
                    # KHÔNG hard-code host thật (rào chắn cấm).
                    "api_base_url": base_url,
                }
                ui_store.write_bot_config(self.root, bot_id, config)
                if values["allowed_chats"] or values["owner_sender_ids"]:
                    ui_store.write_allowlist(self.root, bot_id, {
                        "allowed_chats": values["allowed_chats"],
                        "owner_sender_ids": values["owner_sender_ids"],
                        "default_sender_policy": values["default_sender_policy"],
                    })
                entry = {
                    "bot_id": bot_id,
                    "platform": values["platform"],
                    "display_name": values["display_name"],
                    "enabled": False,
                    "paused": False,
                    "awaiting_muse_sync": True,
                    "token_pending_verification": False,
                    "owner_account": values["owner_account"] or None,
                    "created_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                }
                ui_store.modify_registry(
                    self.root,
                    lambda data: data["bots"].update({bot_id: entry}))
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                    csrf_token=session["csrf_token"])))
        self._audit(request.client_ip, "bot_create", bot_id, None,
                    {"platform": values["platform"], "enabled": False,
                     "owner_account": values["owner_account"] or None}, "ok")
        tailscale = self.tailscale_fn()
        host = tailscale.get("ip") or "<địa chỉ Tailscale của PC>"
        gateway_endpoint = "http://%s:8765" % host
        receiver_endpoint = "http://%s:8766" % host
        account_name = None
        if values["owner_account"]:
            account_name = ui_store.account_display_name(
                ui_store.read_accounts(self.root).get("accounts") or {},
                values["owner_account"])
        return self._with_security(Response.html(
            200,
            ui_pages.handoff_page_html(
                bot_id, gateway_endpoint, receiver_endpoint, queue_secret,
                sync_secret, session["csrf_token"],
                account_id=values["owner_account"] or None,
                account_name=account_name)))

    def _handle_bot_edit(self, request, session, bot_id, entry):
        config = ui_store.read_bot_config(self.root, bot_id) or {}
        allowlist = ui_store.read_allowlist(self.root, bot_id) or {}
        accounts = self._accounts_list()
        transfer_locked = self._active_transfer(bot_id) is not None
        current_owner = ui_store.owner_account_of(entry) or ""
        if request.method == "GET":
            values = {
                "bot_id": bot_id,
                "display_name": config.get("display_name")
                or entry.get("display_name", ""),
                "platform": config.get("platform")
                or entry.get("platform", "zalo"),
                "bot_kind": config.get("bot_kind", "one_to_one"),
                "owner_account": current_owner,
                "allowed_chats": "\n".join(
                    allowlist.get("allowed_chats") or []),
                "owner_sender_ids": "\n".join(
                    allowlist.get("owner_sender_ids") or []),
                "default_sender_policy": allowlist.get(
                    "default_sender_policy",
                    config.get("default_sender_policy", "deny")),
                "group_privacy": config.get("group_privacy", "on"),
            }
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html("edit", values, [],
                                       session["csrf_token"], bot_id=bot_id,
                                       accounts=accounts,
                                       transfer_locked=transfer_locked)))
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        values, errors = self._validate_bot_fields(request, is_new=False)
        current_platform = config.get("platform") or entry.get("platform")
        if values["platform"] != current_platform:
            errors.append(
                "Không đổi được nền tảng của bot đã tạo. Muốn đổi nền "
                "tảng thì xóa bot rồi tạo lại.")
        # Khóa trường Tài khoản quản lý trong lúc đang chuyển (doc 09
        # §4.5): form khóa (disabled) không gửi trường này — khi đó giữ
        # nguyên nhãn; gửi kèm giá trị khác nhãn hiện tại thì từ chối.
        if transfer_locked:
            if "owner_account" in request.form \
                    and values["owner_account"] != current_owner:
                errors.append(
                    "Bot đang chuyển — không đổi được trường Tài khoản "
                    "quản lý. Đổi nhãn ở bước 6 của luồng chuyển.")
            values["owner_account"] = current_owner
        form_values = dict(values)
        form_values["bot_id"] = bot_id
        form_values["allowed_chats"] = "\n".join(values["allowed_chats"])
        form_values["owner_sender_ids"] = "\n".join(values["owner_sender_ids"])
        if errors:
            return self._with_security(Response.html(
                200,
                ui_pages.bot_form_html("edit", form_values, errors,
                                       session["csrf_token"], bot_id=bot_id,
                                       accounts=accounts,
                                       transfer_locked=transfer_locked)))
        try:
            with self._bot_lock(bot_id):
                # Kiểm tra lại khóa BÊN TRONG khóa bot lúc thực thi
                # (S8): luồng chuyển bắt đầu sau khi form render vẫn
                # chặn được lần ghi này.
                if self._active_transfer(bot_id) is not None \
                        and "owner_account" in request.form \
                        and values["owner_account"] != \
                        (ui_store.owner_account_of(
                            self._registry_entry(bot_id)) or ""):
                    return self._with_security(Response.html(
                        200,
                        ui_pages.bot_form_html(
                            "edit", form_values,
                            ["Bot đang chuyển — không đổi được trường Tài "
                             "khoản quản lý. Đổi nhãn ở bước 6 của luồng "
                             "chuyển."],
                            session["csrf_token"], bot_id=bot_id,
                            accounts=accounts, transfer_locked=True)))
                before = {
                    "display_name": config.get("display_name"),
                    "bot_kind": config.get("bot_kind"),
                    "group_privacy": config.get("group_privacy"),
                    "owner_account": current_owner or None,
                }
                new_config = dict(config)
                new_config.update({
                    "bot_id": bot_id,
                    "platform": current_platform,
                    "display_name": values["display_name"],
                    "bot_kind": values["bot_kind"],
                    "group_privacy": values["group_privacy"],
                    "default_sender_policy": values["default_sender_policy"],
                })
                ui_store.write_bot_config(self.root, bot_id, new_config)
                if values["allowed_chats"] or values["owner_sender_ids"]:
                    ui_store.write_allowlist(self.root, bot_id, {
                        "allowed_chats": values["allowed_chats"],
                        "owner_sender_ids": values["owner_sender_ids"],
                        "default_sender_policy": values["default_sender_policy"],
                    })
                else:
                    try:
                        os.unlink(self.root / "config"
                                  / ("%s.allowlist.json" % bot_id))
                    except OSError:
                        pass
                ui_store.modify_registry(
                    self.root,
                    lambda data: data["bots"][bot_id].update(
                        {"display_name": values["display_name"],
                         "owner_account": values["owner_account"] or None}))
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                    csrf_token=session["csrf_token"])))
        self._audit(request.client_ip, "bot_edit", bot_id, before,
                    {"display_name": values["display_name"],
                     "bot_kind": values["bot_kind"],
                     "group_privacy": values["group_privacy"],
                     "owner_account": values["owner_account"] or None}, "ok")
        return self._with_security(Response.redirect("/bots/%s" % bot_id))

    # -- khu vực quản lý tài khoản (doc 08 §4, MA6) -----------------------

    def _handle_accounts(self, request, session):
        accounts = ui_store.read_accounts(self.root)
        registry = ui_store.load_registry(self.root)
        grouped = ui_store.bots_by_account(registry)
        bot_names = {}
        for bot_id, entry in (registry.get("bots") or {}).items():
            config = ui_store.read_bot_config(self.root, bot_id)
            bot_names[bot_id] = config.get("display_name") \
                or entry.get("display_name") or bot_id
        now = time.time()
        transfers_data = ui_store.read_transfers(self.root)
        transfers_by_bot = {}
        for record in (transfers_data.get("transfers") or {}).values():
            if ui_store.is_transfer_active(record, now):
                transfers_by_bot[record["bot_id"]] = record
        # Mục Trung tâm trong từng thẻ tài khoản (v0.2.4, doc 10 §5.6).
        center_info = {}
        for account_id, record in (
                accounts.get("accounts") or {}).items():
            secret = ui_store.center_secret_status(self.root, account_id)
            center_info[account_id] = {
                "participating": bool(record.get("center_participating")),
                "secret_has": secret["has"],
                "secret_issued_text": (
                    ui_store.format_date_saigon(secret["issued_at"])
                    if secret["issued_at"] is not None else None),
                # (v0.6.0, T3l) Cong tac thao luan + mau chat rieng
                # cho muc Trung tam tren trang Tai khoan.
                "discussion_enabled": bool(
                    record.get("center_discussion_enabled", True)),
                "chat_color": record.get("chat_color") or "",
            }
        return self._with_security(Response.html(
            200,
            ui_pages.accounts_page_html(
                accounts, grouped, bot_names, session["csrf_token"],
                transfers_by_bot=transfers_by_bot,
                center_info=center_info)))

    def _handle_account_new(self, request, session):
        if request.method == "GET":
            return self._with_security(Response.html(
                200,
                ui_pages.account_form_html({}, [], session["csrf_token"])))
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        display_name = request.form_value("display_name").strip()
        notes = request.form_value("notes").strip()[:500]
        values = {"display_name": display_name, "notes": notes}
        errors = []
        if not display_name:
            errors.append("Tên gọi không được để trống.")
        elif len(display_name) > 100:
            errors.append("Tên gọi tối đa 100 ký tự.")
        if errors:
            return self._with_security(Response.html(
                200,
                ui_pages.account_form_html(
                    values, errors, session["csrf_token"])))
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        preview_id = ui_store.unique_account_id(
            account_map, ui_store.slugify_account_id(display_name))
        action = request.form_value("action")
        if action == "preview":
            return self._with_security(Response.html(
                200,
                ui_pages.account_form_html(
                    values, [], session["csrf_token"],
                    preview_id=preview_id)))
        if action != "create":
            return self._not_found(session)
        # Bắt buộc xem trước ID trước khi lưu: lần POST tạo phải mang
        # đúng ID đã xem trước cho tên gọi này.
        if request.form_value("previewed_id") != preview_id:
            return self._with_security(Response.html(
                200,
                ui_pages.account_form_html(
                    values,
                    ["Bấm \"Xem trước ID\" để xem ID tài khoản sẽ lưu "
                     "trước khi lưu."],
                    session["csrf_token"], preview_id=preview_id)))

        def _create(data):
            final_id = ui_store.unique_account_id(
                data["accounts"], ui_store.slugify_account_id(display_name))
            data["accounts"][final_id] = {
                "display_name": display_name,
                "notes": notes,
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            }
            return final_id

        created_id, _data = ui_store.modify_accounts(self.root, _create)
        self._audit(request.client_ip, "account_create", None, None,
                    {"account_id": created_id,
                     "display_name": display_name}, "ok")
        return self._with_security(Response.redirect("/accounts"))

    def _handle_account_route(self, request, session, session_token,
                              account_id, rest):
        if not ui_store.ACCOUNT_ID_RE.match(account_id):
            return self._not_found(session)
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        if account_id not in account_map:
            return self._not_found(session)
        if rest == "/rename":
            return self._handle_account_rename(request, session, account_id)
        if rest == "/delete":
            return self._handle_account_delete_confirm(
                request, session, session_token, account_id)
        if rest == "/delete/execute":
            return self._handle_account_delete_execute(
                request, session, session_token, account_id)
        if rest == "/center/pause":
            return self._handle_center_pause(request, session, account_id)
        if rest == "/center/discussion":
            return self._handle_center_discussion(
                request, session, account_id)
        if rest == "/center/resume":
            return self._handle_center_resume(request, session, account_id)
        if rest == "/center/remove":
            return self._handle_center_remove_confirm(
                request, session, session_token, account_id)
        if rest == "/center/remove/execute":
            return self._handle_center_remove_execute(
                request, session, session_token, account_id)
        if rest == "/center/join":
            return self._handle_center_join_confirm(
                request, session, session_token, account_id)
        if rest == "/center/join/execute":
            return self._handle_center_join_execute(
                request, session, session_token, account_id)
        return self._not_found(session)

    def _handle_account_rename(self, request, session, account_id):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        display_name = request.form_value("display_name").strip()
        if not display_name or len(display_name) > 100:
            return self._with_security(Response.html(
                200,
                ui_pages.simple_page_html(
                    "Tên gọi chưa đúng",
                    "<h1>Tên gọi chưa đúng</h1>"
                    "<p>Tên gọi phải có 1–100 ký tự.</p>"
                    '<p><a href="/accounts">Về danh sách tài khoản</a></p>',
                    csrf_token=session["csrf_token"])))
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        before = (account_map.get(account_id) or {}).get("display_name")

        def _rename(data):
            if account_id in data["accounts"]:
                data["accounts"][account_id]["display_name"] = display_name

        ui_store.modify_accounts(self.root, _rename)
        self._audit(request.client_ip, "account_rename", None,
                    {"account_id": account_id, "display_name": before},
                    {"account_id": account_id,
                     "display_name": display_name}, "ok")
        return self._with_security(Response.redirect("/accounts"))

    def _account_transfer_block(self, account_id, now=None):
        """Luồng đang chạy nào chạm tới tài khoản này (from/to)? Trả về
        transfer record đầu tiên, hoặc None."""
        if now is None:
            now = time.time()
        transfers_data = ui_store.read_transfers(self.root)
        for record in (transfers_data.get("transfers") or {}).values():
            if not ui_store.is_transfer_active(record, now):
                continue
            if record.get("from_account_id") == account_id \
                    or record.get("to_account_id") == account_id:
                return record
        return None

    def _handle_account_delete_confirm(self, request, session, session_token,
                                       account_id):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        if self._account_transfer_block(account_id) is not None:
            return self._with_security(Response.html(
                200,
                ui_pages.simple_page_html(
                    "Chưa gỡ được tài khoản",
                    "<h1>Chưa gỡ được tài khoản</h1>"
                    "<p>Tài khoản này đang là tài khoản cũ/mới của 1 luồng "
                    "chuyển bot đang chạy. Chờ luồng chuyển xong rồi mới "
                    "gỡ.</p>"
                    '<p><a href="/accounts">Về danh sách tài khoản</a></p>',
                    csrf_token=session["csrf_token"])))
        registry = ui_store.load_registry(self.root)
        grouped = ui_store.bots_by_account(registry)
        bot_ids = grouped.get(account_id) or []
        account_map = ui_store.read_accounts(self.root).get("accounts") or {}
        record = account_map[account_id]
        confirm_token = self._issue_confirm_token(
            session_token, {"kind": "account_delete",
                            "account_id": account_id})
        bot_list = "".join(
            "<li>%s</li>" % ui_pages.esc(bot_id) for bot_id in bot_ids)
        only_warning = ""
        if len(account_map) == 1:
            only_warning = (
                "<p><strong>Đây là tài khoản duy nhất trong danh sách.</strong> "
                "Sau khi gỡ, mọi bot sẽ về trạng thái chưa gắn tài khoản.</p>"
            )
        intro = (
            "<p>Gỡ tài khoản <strong>%s</strong> (%s)?</p>"
            "%s"
            "<p>Các bot sau sẽ về trạng thái <strong>chưa gắn tài "
            "khoản</strong> (không tự gắn sang tài khoản khác):</p>"
            "<ul>%s</ul>"
            "<p>Tai nghe trên máy tính của các bot đó vẫn chạy, vẫn nghe "
            "và giữ tin. Gỡ khỏi danh sách không xóa secret/cấu hình ở "
            "phía tài khoản Muse đó — dọn dẹp phía Muse làm riêng.</p>"
            "<p>Sau khi gỡ, các thẻ bot hiện nhãn \"Chưa gắn tài khoản\" "
            "+ nhắc \"chưa có tài khoản nào lấy tin — gắn lại theo quy "
            "trình §6\".</p>"
        ) % (ui_pages.esc(record["display_name"]), ui_pages.esc(account_id),
             only_warning, bot_list or "<li>Không có bot nào.</li>")
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                "Gỡ tài khoản", intro,
                "/accounts/%s/delete/execute" % account_id,
                {"confirm_token": confirm_token},
                session["csrf_token"],
                "Gỡ tài khoản %s" % record["display_name"], danger=True,
                back_url="/accounts")))

    def _handle_account_delete_execute(self, request, session, session_token,
                                       account_id):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "account_delete"
                or payload.get("account_id") != account_id):
            return self._with_security(Response.html(
                403,
                ui_pages.simple_page_html(
                    "Xác nhận đã hết hạn",
                    "<h1>Xác nhận đã hết hạn</h1>"
                    "<p>Quay lại danh sách tài khoản và bấm lại.</p>"
                    '<p><a href="/accounts">Về danh sách tài khoản</a></p>',
                    csrf_token=session["csrf_token"])))
        # Kiểm tra lại khóa lúc thực thi: luồng chuyển bắt đầu sau khi
        # phát hành confirm vẫn chặn được lần gỡ này.
        if self._account_transfer_block(account_id) is not None:
            return self._with_security(Response.html(
                200,
                ui_pages.simple_page_html(
                    "Chưa gỡ được tài khoản",
                    "<h1>Chưa gỡ được tài khoản</h1>"
                    "<p>Tài khoản này đang là tài khoản cũ/mới của 1 luồng "
                    "chuyển bot đang chạy.</p>"
                    '<p><a href="/accounts">Về danh sách tài khoản</a></p>',
                    csrf_token=session["csrf_token"])))
        registry = ui_store.load_registry(self.root)
        grouped = ui_store.bots_by_account(registry)
        bot_ids = grouped.get(account_id) or []

        def _unassign(data):
            for bot_id in bot_ids:
                entry = data["bots"].get(bot_id)
                if isinstance(entry, dict):
                    entry["owner_account"] = None

        # NS5: registry hỏng thì TỪ CHỐI ghi (không rebuild rỗng) và
        # KHÔNG gỡ tài khoản — tài khoản vẫn còn nguyên để xử lý sau.
        try:
            ui_store.modify_registry_checked(self.root, _unassign)
        except ValueError:
            return self._with_security(Response.html(
                200,
                ui_pages.simple_page_html(
                    "Chưa gỡ được tài khoản",
                    "<h1>Chưa gỡ được tài khoản</h1>"
                    "<p>registry.json hỏng — từ chối ghi. Tài khoản vẫn "
                    "còn nguyên, chưa gỡ gì.</p>"
                    '<p><a href="/accounts">Về danh sách tài khoản</a></p>',
                    csrf_token=session["csrf_token"])))
        ui_store.modify_accounts(
            self.root,
            lambda data: data["accounts"].pop(account_id, None))
        self._audit(request.client_ip, "account_remove", None,
                    {"account_id": account_id, "bots": bot_ids}, None, "ok")
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Đã gỡ tài khoản",
                "<h1>Đã gỡ tài khoản %s</h1>"
                "<p>%d bot đã về trạng thái chưa gắn tài khoản.</p>"
                '<p><a href="/accounts">Về danh sách tài khoản</a></p>'
                % (ui_pages.esc(account_id), len(bot_ids)),
                csrf_token=session["csrf_token"])))

    # -- thay token đã lưu (luồng 6 bước, SPEC §1.4) -----------------------

    def _handle_replace_token(self, request, session, bot_id, entry):
        display_name = entry.get("display_name") or bot_id
        if request.method == "GET":
            if self._active_transfer(bot_id) is not None:
                return self._transfer_locked_response(
                    session, bot_id, "Thay token")
            return self._with_security(Response.html(
                200,
                ui_pages.replace_token_form_html(
                    bot_id, display_name, [], session["csrf_token"])))
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        if self._active_transfer(bot_id) is not None:
            return self._transfer_locked_response(
                session, bot_id, "Thay token")
        token1 = request.form_value("token1")
        token2 = request.form_value("token2")
        confirmed = request.form_value("confirm") == "yes"
        errors = []
        if not token1:
            errors.append("Token mới không được để trống.")
        elif token1 != token2:
            errors.append("2 lần dán token không khớp nhau.")
        elif len(token1) > MAX_FORM_FIELD_CHARS:
            errors.append("Token quá dài.")
        if not confirmed:
            errors.append("Cần tick xác nhận đã lấy token mới.")
        if errors:
            return self._with_security(Response.html(
                200,
                ui_pages.replace_token_form_html(
                    bot_id, display_name, errors, session["csrf_token"])))
        platform = entry.get("platform", "zalo")
        endpoints = ui_store.read_platform_endpoints(self.root)
        base_url = endpoints.get(platform)
        if not base_url:
            return self._with_security(Response.html(
                200,
                ui_pages.replace_token_form_html(
                    bot_id, display_name,
                    ["Chưa cấu hình địa chỉ API cho nền tảng này trên máy "
                     "tính. Chưa lưu gì."],
                    session["csrf_token"])))
        ok, info = self.verify_fn(platform, base_url, token1)
        if not ok:
            return self._with_security(Response.html(
                200,
                ui_pages.replace_token_form_html(
                    bot_id, display_name,
                    ["Token mới không đúng hoặc không kết nối được tới nền "
                     "tảng. Token cũ vẫn nguyên. Chưa lưu gì."],
                    session["csrf_token"])))
        platform_bot_id = info.get("id") or ""
        if platform_bot_id and self._duplicate_bot_id(
                platform, platform_bot_id, exclude_bot_id=bot_id):
            return self._with_security(Response.html(
                200,
                ui_pages.replace_token_form_html(
                    bot_id, display_name,
                    ["Token này thuộc về 1 bot khác đã có trên hệ thống. "
                     "Chưa lưu gì."],
                    session["csrf_token"])))
        try:
            with self._bot_lock(bot_id):
                # Kiểm tra lại BÊN TRONG khóa lúc thực thi (S8): luồng
                # chuyển có thể bắt đầu sau lúc kiểm tra đầu.
                if self._active_transfer(bot_id) is not None:
                    return self._transfer_locked_response(
                        session, bot_id, "Thay token")
                self._write_secret_file(bot_id, "token", token1)
                ui_store.modify_registry(
                    self.root,
                    lambda data: data["bots"][bot_id].update({
                        "token_pending_verification": True,
                        "pending_since": time.strftime(
                            "%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                    }))
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                    csrf_token=session["csrf_token"])))
        self._audit(request.client_ip, "token_replace", bot_id,
                    {"token_pending_verification": False},
                    {"token_pending_verification": True}, "ok")
        return self._with_security(Response.html(
            200,
            ui_pages.pending_verification_html(
                bot_id, display_name, session["csrf_token"])))

    def _handle_confirm_vault(self, request, session, bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        try:
            with self._bot_lock(bot_id):
                # Khóa xác nhận Vault 2 đầu trong lúc chuyển (§4.5),
                # kiểm tra lại bên trong khóa (S8).
                if self._active_transfer(bot_id) is not None:
                    return self._transfer_locked_response(
                        session, bot_id, "Xác nhận Vault")
                ui_store.modify_registry(
                    self.root,
                    lambda data: data["bots"][bot_id].update(
                        {"token_pending_verification": False}))
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                    csrf_token=session["csrf_token"])))
        self._audit(request.client_ip, "token_vault_confirmed", bot_id,
                    {"token_pending_verification": True},
                    {"token_pending_verification": False}, "ok")
        return self._with_security(Response.redirect("/bots/%s" % bot_id))

    # -- luồng chuyển bot (doc 09 §4) ------------------------------------

    def _transfer_error_response(self, session, bot_id, message, status=200):
        return self._with_security(Response.html(
            status,
            ui_pages.simple_page_html(
                "Chuyển bot",
                "<h1>Chuyển bot</h1><p>%s</p>"
                '<p><a href="/bots/%s/transfer">Về luồng chuyển</a></p>'
                % (ui_pages.esc(message), bot_id),
                csrf_token=session["csrf_token"])))

    def _transfer_locked_response(self, session, bot_id, action_text):
        """Trang khóa thao tác §4.5: bot đang chuyển."""
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Bot đang chuyển",
                "<h1>Bot đang chuyển</h1><p>%s bị khóa trong lúc bot đang "
                "chuyển sang tài khoản khác. Hoàn tất, quay lại hoặc hủy "
                "luồng chuyển rồi thử lại.</p>"
                '<p><a href="/bots/%s">Về trang bot</a></p>'
                % (ui_pages.esc(action_text), bot_id),
                csrf_token=session["csrf_token"])))

    def _bot_busy_response(self, session):
        return self._with_security(Response.html(
            409,
            ui_pages.simple_page_html(
                "Bot đang bận",
                "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                csrf_token=session["csrf_token"])))

    def _transfer_load(self, request, bot_id, now, session):
        """Đọc luồng theo transfer_id trong form + xử lý hết hạn.

        Trả (record, error_response): error_response khác None khi
        luồng không còn hoặc đã hết hạn (đã ghi nhận expired).
        """
        transfer_id = request.form_value("transfer_id")
        data = ui_store.read_transfers(self.root)
        record = (data.get("transfers") or {}).get(transfer_id)
        if record is None or record.get("bot_id") != bot_id:
            return None, self._transfer_error_response(
                session, bot_id,
                "Luồng chuyển không còn (đã hoàn tất, đã hủy hoặc đã hết "
                "hạn).")
        if not ui_store.is_transfer_active(record, now):
            try:
                transfers.expire_stale_transfers(self.root, now)
            except OSError:
                return None, self._transfer_error_response(
                    session, bot_id,
                    "Không ghi được nhật ký chuyển — chưa thay đổi gì, "
                    "thử lại sau.")
            return None, self._transfer_error_response(
                session, bot_id,
                "Luồng chuyển đã hết hạn (quá 72 giờ không tiến triển). "
                "Bắt đầu luồng mới nếu vẫn cần chuyển.")
        return record, None

    def _transfer_revision_error(self, request, record, session, bot_id):
        """Kiểm tra revision kỳ vọng; trả response lỗi hoặc None."""
        try:
            expected = int(request.form_value("revision"))
        except (TypeError, ValueError):
            expected = None
        if expected != int(record.get("revision") or 0):
            return self._transfer_error_response(
                session, bot_id,
                "Trang đã cũ (luồng chuyển đã thay đổi). Tải lại trang "
                "rồi thử lại.",
                status=409)
        return None

    def _render_transfer_step(self, session, bot_id, entry, transfer,
                              errors=None):
        """Render trang mốc hiện tại (chỉ đọc — GET không đổi trạng
        thái, C7)."""
        now = time.time()
        step = int(transfer.get("current_step") or 1)
        context = {}
        eval_result = None
        fetch_state = ui_store.read_fetch_state(self.root)
        if step == 1:
            try:
                gateway_status = self.gateway_status_fn()
            except Exception:
                gateway_status = "unknown"
            context["gateway_status"] = gateway_status
            context["quiet"] = transfers.fetch_quiet_info(
                fetch_state, bot_id, now)
        elif step == 2:
            state = ui_store.read_state(self.root, bot_id)
            latest = state.get("queue_latest_seq")
            context["latest_seq"] = latest if isinstance(latest, int) else None
            context["latest_sample_at"] = time.strftime(
                "%H:%M:%S", time.localtime(now))
        elif step == 3:
            context["capability_present"] = (
                ui_store.capability_bot_id(
                    ui_store.read_capability(self.root, bot_id)) is not None)
        elif step == 7:
            eval_result = transfers.evaluate_step7(
                self.root, bot_id, transfer, now, fetch_state=fetch_state,
                registry_entry=entry,
                allowlist=ui_store.read_allowlist(self.root, bot_id))
        return self._with_security(Response.html(
            200,
            ui_pages.transfer_page_html(
                bot_id, entry.get("display_name") or bot_id, transfer,
                step, context, session["csrf_token"],
                self._accounts_list(), errors=errors,
                eval_result=eval_result)))

    def _handle_transfer(self, request, session, bot_id, entry):
        if request.method != "GET":
            return self._method_not_allowed(session)
        record = self._active_transfer(bot_id)
        if record is None:
            return self._with_security(Response.html(
                200,
                ui_pages.transfer_start_page_html(
                    bot_id, entry.get("display_name") or bot_id,
                    self._accounts_list(), session["csrf_token"],
                    ui_store.owner_account_of(entry))))
        return self._render_transfer_step(session, bot_id, entry, record)

    def _handle_transfer_start(self, request, session, bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        now = time.time()
        accounts = dict(self._accounts_list())
        from_id = request.form_value("from_account_id") or None
        to_id = request.form_value("to_account_id") or None
        errors = []
        if to_id is None or to_id not in accounts:
            errors.append("Tài khoản đích phải có sẵn trong danh sách "
                          "tài khoản.")
        if from_id is not None and from_id not in accounts:
            errors.append("Tài khoản cũ không có trong danh sách tài "
                          "khoản.")
        if to_id is not None and from_id == to_id:
            errors.append("Tài khoản đích phải khác tài khoản cũ.")
        if errors:
            return self._with_security(Response.html(
                200,
                ui_pages.transfer_start_page_html(
                    bot_id, entry.get("display_name") or bot_id,
                    self._accounts_list(), session["csrf_token"],
                    ui_store.owner_account_of(entry), errors=errors)))
        try:
            with self._bot_lock(bot_id):
                if self._active_transfer(bot_id, now) is not None:
                    return self._transfer_error_response(
                        session, bot_id,
                        "Bot đang chuyển dở — tiếp tục luồng hiện tại, "
                        "không tạo luồng thứ 2.")
                record = transfers.new_transfer_record(
                    bot_id, from_id, ui_store.owner_account_of(entry),
                    to_id, now)
                # Nhật ký trước, trạng thái sau (B4). Sự kiện `started`
                # (kèm from/to) ghi trước `step_done` mốc 0 — xem doc 09
                # §4.3 mốc 0, §6.1 (B4).
                transfers.append_transfer_event(
                    self.root,
                    transfers.make_event(
                        "started", record, 0,
                        {"from": record.get("from_account_id"),
                         "to": record.get("to_account_id")}, now))
                transfers.append_transfer_event(
                    self.root,
                    transfers.make_event(
                        "step_done", record, 0,
                        {"note": "Bắt đầu luồng chuyển"}, now))

                def insert(data):
                    active = ui_store.active_transfer_for_bot(
                        data, bot_id, now)
                    if active is not None:
                        raise transfers.TransferError(
                            "Bot đang chuyển dở — tiếp tục luồng hiện "
                            "tại.")
                    data.setdefault("transfers", {})[
                        record["transfer_id"]] = record

                transfers.modify_transfers(self.root, insert)
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — chưa tạo luồng, thử "
                "lại sau.")
        except transfers.TransferError as exc:
            return self._transfer_error_response(session, bot_id, str(exc))
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_start", bot_id,
                    {"owner_account": ui_store.owner_account_of(entry)},
                    {"to_account_id": to_id}, "ok")
        return self._with_security(Response.redirect(
            "/bots/%s/transfer" % bot_id))

    def _handle_transfer_step(self, request, session, bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        now = time.time()
        record, error = self._transfer_load(request, bot_id, now, session)
        if error is not None:
            return error
        revision_error = self._transfer_revision_error(
            request, record, session, bot_id)
        if revision_error is not None:
            return revision_error
        try:
            step = int(request.form_value("step"))
        except (TypeError, ValueError):
            step = -1
        current = int(record.get("current_step") or 1)
        if step != current:
            if step < current:
                message = ("Mốc %d đã xong — cấm quay lại sửa mốc đã "
                           "xong." % step)
            else:
                message = ("Mốc %d chưa mở — hoàn tất các mốc trước đã."
                           % step)
            return self._render_transfer_step(
                session, bot_id, entry, record, errors=[message])
        if step == 6:
            return self._apply_step6(request, session, bot_id, entry,
                                     record, now)
        if step == 7:
            return self._apply_step7(request, session, bot_id, entry,
                                     record, now)
        if step == 9:
            return self._apply_step9(request, session, bot_id, entry,
                                     record, now)
        form = {key: values[0].strip() if values else ""
                for key, values in request.form.items()}
        context = {"now": now}
        if step == 2:
            state = ui_store.read_state(self.root, bot_id)
            latest = state.get("queue_latest_seq")
            context["latest_seq"] = latest if isinstance(latest, int) else None
        elif step == 3:
            context["capability_id"] = ui_store.capability_bot_id(
                ui_store.read_capability(self.root, bot_id))
        elif step == 5:
            context["fetch_state"] = ui_store.read_fetch_state(self.root)
        gateway_status = None
        if step == 1:
            try:
                gateway_status = self.gateway_status_fn()
            except Exception:
                gateway_status = "unknown"
            quiet, _last = transfers.fetch_quiet_info(
                ui_store.read_fetch_state(self.root), bot_id, now)
            if gateway_status == "active" and not quiet:
                return self._render_transfer_step(
                    session, bot_id, entry, record,
                    errors=["Chưa đủ 90 giây lặng lấy tin — đợi thêm rồi "
                            "thử lại."])
            if gateway_status == "inactive":
                # I5 (doc 09 §4.3 mốc 1): đo được unit gateway inactive
                # thì KHÔNG cho qua — lặng-fetch không đáng tin khi
                # gateway chết. Chỉ đường "không đo được" mới cho qua
                # bằng xác nhận của chủ nhân.
                return self._render_transfer_step(
                    session, bot_id, entry, record,
                    errors=["Unit gateway đang không active — không kiểm "
                            "chứng được lặng-fetch. Bật lại gateway rồi "
                            "thử lại."])
        try:
            detail, updates = transfers.validate_step_fields(
                step, form, record, context)
        except transfers.TransferError as exc:
            return self._render_transfer_step(
                session, bot_id, entry, record, errors=[str(exc)])
        if step == 1 and gateway_status != "active":
            # Chỉ tới đây khi KHÔNG đo được trạng thái unit (inactive đã
            # bị chặn ở trên — I5): ghi rõ chỉ dựa vào xác nhận.
            detail["note"] = ("Không đo được trạng thái unit gateway — "
                              "chỉ dựa vào xác nhận của chủ nhân.")
        try:
            with self._bot_lock(bot_id):
                fresh = self._active_transfer(bot_id, now)
                if fresh is None \
                        or fresh.get("transfer_id") != record["transfer_id"]:
                    return self._transfer_error_response(
                        session, bot_id, "Luồng chuyển đã thay đổi — tải "
                        "lại trang rồi thử lại.")
                if int(fresh.get("revision") or 0) != int(
                        record.get("revision") or 0):
                    return self._transfer_error_response(
                        session, bot_id,
                        "Trang đã cũ — tải lại trang rồi thử lại.",
                        status=409)
                transfers.append_transfer_event(
                    self.root,
                    transfers.make_event("step_done", record, step,
                                         detail, now))

                def apply_state(data):
                    rec = (data.get("transfers") or {}).get(
                        record["transfer_id"])
                    if rec is None or int(rec.get("revision") or 0) != int(
                            record.get("revision") or 0):
                        raise transfers.RevisionMismatch(
                            "revision lệch trong lúc ghi")
                    rec.update(updates)
                    rec.setdefault("steps", {})[str(step)] = {
                        "status": "done", "at": transfers.now_iso(now),
                        "note": detail.get("note", "")}
                    if step < 9:
                        rec["steps"][str(step + 1)] = {
                            "status": "current", "at": None, "note": ""}
                        rec["current_step"] = step + 1
                    transfers.refresh_progress(rec, now)

                transfers.modify_transfers(self.root, apply_state)
        except transfers.RevisionMismatch:
            return self._transfer_error_response(
                session, bot_id,
                "Trang đã cũ — tải lại trang rồi thử lại.", status=409)
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — chưa thay đổi gì, thử "
                "lại sau.")
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_step", bot_id,
                    {"step": step, "transfer_id": record["transfer_id"]},
                    {"step": step + 1}, "ok")
        return self._with_security(Response.redirect(
            "/bots/%s/transfer" % bot_id))

    def _apply_step6(self, request, session, bot_id, entry, record, now):
        """Mốc 6: UI tự ghi owner_account. Thứ tự: registry trước,
        nhật ký sau, trạng thái sau cùng; kiểm tra đồng thời BÊN TRONG
        modify_registry_checked bằng raise-để-hủy (B5/S8)."""
        try:
            marker = transfers.generate_step7_marker(self.root, bot_id, now)
        except transfers.TransferError as exc:
            return self._render_transfer_step(
                session, bot_id, entry, record, errors=[str(exc)])
        baseline = ui_store.fetch_state_entry(
            ui_store.read_fetch_state(self.root), bot_id).get("fetch_count")
        if not isinstance(baseline, int):
            baseline = 0
        to_id = record.get("to_account_id")
        expected_from = record.get("from_label_at_start")
        changed = []
        try:
            with self._bot_lock(bot_id):
                with transfers.transfers_lock(self.root):
                    data = ui_store.read_transfers(self.root)
                    rec = (data.get("transfers") or {}).get(
                        record["transfer_id"])
                    if rec is None or not ui_store.is_transfer_active(
                            rec, now):
                        return self._transfer_error_response(
                            session, bot_id,
                            "Luồng chuyển đã thay đổi — tải lại trang rồi "
                            "thử lại.")
                    if int(rec.get("revision") or 0) != int(
                            record.get("revision") or 0):
                        return self._transfer_error_response(
                            session, bot_id,
                            "Trang đã cũ — tải lại trang rồi thử lại.",
                            status=409)
                    if int(rec.get("current_step") or 0) != 6:
                        return self._transfer_error_response(
                            session, bot_id,
                            "Mốc 6 không còn là mốc hiện tại — tải lại "
                            "trang.")

                    def mutate_registry(registry_data):
                        bot_entry = registry_data["bots"].get(bot_id)
                        if not isinstance(bot_entry, dict):
                            raise transfers.TransferError(
                                "Bot không còn trong registry.")
                        current = ui_store.owner_account_of(bot_entry)
                        if current == to_id:
                            return  # Vào lại sau crash: nhãn đã đổi.
                        if current != expected_from:
                            raise transfers.TransferError(
                                "Nhãn chủ quản hiện tại không khớp nhãn "
                                "lúc bắt đầu luồng — hủy luồng và bắt đầu "
                                "lại, không ghi đè.")
                        bot_entry["owner_account"] = to_id
                        changed.append(True)

                    try:
                        ui_store.modify_registry_checked(
                            self.root, mutate_registry)
                    except transfers.TransferError as exc:
                        # Bắt trước ValueError: TransferError là subclass
                        # của ValueError, lệch nhãn phải báo đúng lỗi.
                        return self._transfer_error_response(
                            session, bot_id, str(exc))
                    except ValueError:
                        return self._transfer_error_response(
                            session, bot_id,
                            "registry.json hỏng — từ chối ghi. Kiểm tra "
                            "file registry rồi thử lại.")
                    if not transfers.transfer_log_has_kind(
                            self.root, record["transfer_id"],
                            "label_changed"):
                        label_detail = {"from": expected_from, "to": to_id}
                        if not changed:
                            label_detail["note"] = (
                                "label_changed đã xảy ra trước khi crash")
                        transfers.append_transfer_event(
                            self.root,
                            transfers.make_event(
                                "label_changed", rec, 6, label_detail, now))
                    transfers.append_transfer_event(
                        self.root,
                        transfers.make_event(
                            "step_done", rec, 6,
                            {"note": "Đổi nhãn chủ quản sang tài khoản "
                                     "đích"}, now))
                    rec.setdefault("steps", {})["6"] = {
                        "status": "done", "at": transfers.now_iso(now),
                        "note": ""}
                    rec["steps"]["7"] = {"status": "current", "at": None,
                                         "note": ""}
                    rec["current_step"] = 7
                    rec["step7_marker"] = marker
                    rec["step7_started_at"] = transfers.now_iso(now)
                    rec["step7_fetch_count_at_start"] = baseline
                    rec["step7_marker_found_at"] = None
                    transfers.refresh_progress(rec, now)
                    transfers.write_transfers_data(self.root, data)
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — thử lại sau. Nếu nhãn "
                "đã đổi, mốc 6 cho xác nhận tiếp (không ghi đè).")
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_label_change", bot_id,
                    {"owner_account": expected_from},
                    {"owner_account": to_id}, "ok")
        self._audit(request.client_ip, "transfer_step", bot_id,
                    {"step": 6, "transfer_id": record["transfer_id"]},
                    {"step": 7}, "ok")
        return self._with_security(Response.redirect(
            "/bots/%s/transfer" % bot_id))

    def _apply_step7(self, request, session, bot_id, entry, record, now):
        """Mốc 7: POST hoàn tất chạy lại đủ 4 kiểm tra phía server.
        Lần thử bị chặn do nhãn (ma trận b/c/d) hoặc hết 15 phút thì
        ghi step_failed + đánh dấu failed; thiếu bằng chứng khác thì
        chỉ hiện thông báo, không ghi gì."""
        dm_tick = request.form_value("dm_confirm") == "yes"
        attested_tick = request.form_value("attested") == "yes"
        adjudication = request.form_value("adjudication") or None
        if adjudication not in ("single_multi_part", "duplicate_rollback"):
            adjudication = None
        result = transfers.evaluate_step7(
            self.root, bot_id, record, now,
            fetch_state=ui_store.read_fetch_state(self.root),
            registry_entry=self._registry_entry(bot_id) or entry,
            allowlist=ui_store.read_allowlist(self.root, bot_id),
            dm_tick=dm_tick, attested_tick=attested_tick,
            adjudication=adjudication)
        if result["all_pass"]:
            detail = {
                "reply_tier": result["reply_tier"],
                "label_tier": result["label_tier"],
                "dm_path": result["check3"]["dm_path"],
                "labels_seen": result["labels_seen"],
                "marker_event_key": result["check3"]["marker_event_key"],
            }
            if adjudication:
                detail["adjudication"] = adjudication
            try:
                with self._bot_lock(bot_id):
                    fresh = self._active_transfer(bot_id, now)
                    if fresh is None or fresh.get("transfer_id") != \
                            record["transfer_id"]:
                        return self._transfer_error_response(
                            session, bot_id,
                            "Luồng chuyển đã thay đổi — tải lại trang rồi "
                            "thử lại.")
                    if int(fresh.get("revision") or 0) != int(
                            record.get("revision") or 0):
                        return self._transfer_error_response(
                            session, bot_id,
                            "Trang đã cũ — tải lại trang rồi thử lại.",
                            status=409)
                    transfers.append_transfer_event(
                        self.root,
                        transfers.make_event("step_done", record, 7,
                                             detail, now))

                    def apply_state(data):
                        rec = (data.get("transfers") or {}).get(
                            record["transfer_id"])
                        if rec is None or int(rec.get("revision") or 0) \
                                != int(record.get("revision") or 0):
                            raise transfers.RevisionMismatch(
                                "revision lệch trong lúc ghi")
                        rec["step7_reply_tier"] = result["reply_tier"]
                        rec["step7_label_tier"] = result["label_tier"]
                        rec["step7_dm_path"] = result["check3"]["dm_path"]
                        rec["step7_labels_seen"] = result["labels_seen"]
                        rec["step7_marker_event_key"] = \
                            result["check3"]["marker_event_key"]
                        rec["step7_adjudication"] = adjudication
                        rec.setdefault("steps", {})["7"] = {
                            "status": "done", "at": transfers.now_iso(now),
                            "note": ""}
                        rec["steps"]["8"] = {"status": "current",
                                             "at": None, "note": ""}
                        rec["current_step"] = 8
                        transfers.refresh_progress(rec, now)

                    transfers.modify_transfers(self.root, apply_state)
            except transfers.RevisionMismatch:
                return self._transfer_error_response(
                    session, bot_id,
                    "Trang đã cũ — tải lại trang rồi thử lại.", status=409)
            except OSError:
                return self._transfer_error_response(
                    session, bot_id,
                    "Không ghi được nhật ký chuyển — chưa thay đổi gì, "
                    "thử lại sau.")
            except TimeoutError:
                return self._bot_busy_response(session)
            self._audit(request.client_ip, "transfer_step", bot_id,
                        {"step": 7, "transfer_id": record["transfer_id"]},
                        {"step": 8}, "ok")
            return self._with_security(Response.redirect(
                "/bots/%s/transfer" % bot_id))
        errors = [check["message"] for check in (
            result["check1"], result["check2"], result["check3"],
            result["check4"]) if not check["pass"]]
        case = result["check1"].get("case")
        if case in ("b", "c", "d") or result["timed_out"]:
            try:
                with self._bot_lock(bot_id):
                    transfers.append_transfer_event(
                        self.root,
                        transfers.make_event(
                            "step_failed", record, 7,
                            {"label_tier": result["label_tier"],
                             "labels_seen": result["labels_seen"],
                             "case": case,
                             "timed_out": bool(result["timed_out"])}, now))

                    def mark_failed(data):
                        # N2: kiểm tra lại bản ghi MỚI trong khóa — chỉ
                        # đánh failed khi revision khớp và mốc 7 chưa
                        # done (bản ghi có thể đã đổi sau lúc đánh giá).
                        rec = (data.get("transfers") or {}).get(
                            record["transfer_id"])
                        if rec is None:
                            return
                        if int(rec.get("revision") or 0) != int(
                                record.get("revision") or 0):
                            return
                        if ui_store.transfer_step_done(rec, 7):
                            return
                        rec.setdefault("steps", {})["7"] = {
                            "status": "failed",
                            "at": transfers.now_iso(now), "note": ""}
                        rec["revision"] = int(rec.get("revision") or 0) + 1

                    transfers.modify_transfers(self.root, mark_failed)
            except OSError:
                return self._transfer_error_response(
                    session, bot_id,
                    "Không ghi được nhật ký chuyển — chưa thay đổi gì, "
                    "thử lại sau.")
            except TimeoutError:
                return self._bot_busy_response(session)
            record = self._active_transfer(bot_id, now) or record
        return self._render_transfer_step(
            session, bot_id, entry, record, errors=errors)

    def _apply_step9(self, request, session, bot_id, entry, record, now):
        """Mốc 9: thứ tự N19 — ghi completed (fsync) → xóa khỏi
        transfers.json → audit. Bấm lại không ghi trùng."""
        try:
            with self._bot_lock(bot_id):
                if transfers.transfer_log_has_kind(
                        self.root, record["transfer_id"], "completed"):
                    transfers.modify_transfers(
                        self.root,
                        lambda data: data.get("transfers", {}).pop(
                            record["transfer_id"], None))
                    return self._with_security(Response.html(
                        200,
                        ui_pages.simple_page_html(
                            "Đã hoàn tất chuyển",
                            "<h1>Đã hoàn tất chuyển bot</h1>"
                            '<p><a href="/bots/%s">Về trang bot</a></p>'
                            % bot_id,
                            csrf_token=session["csrf_token"])))
                fresh = self._active_transfer(bot_id, now)
                if fresh is None or fresh.get("transfer_id") != \
                        record["transfer_id"]:
                    return self._transfer_error_response(
                        session, bot_id,
                        "Luồng chuyển đã thay đổi — tải lại trang rồi thử "
                        "lại.")
                if int(fresh.get("revision") or 0) != int(
                        record.get("revision") or 0):
                    return self._transfer_error_response(
                        session, bot_id,
                        "Trang đã cũ — tải lại trang rồi thử lại.",
                        status=409)
                transfers.append_transfer_event(
                    self.root,
                    transfers.make_event(
                        "completed", record, 9,
                        {"actor": "ui-owner",
                         "history_choice": fresh.get("history_choice"),
                         "cursor_step2": fresh.get("cursor_step2"),
                         "cursor_step5": fresh.get("cursor_step5"),
                         "label_tier": fresh.get("step7_label_tier"),
                         "reply_tier": fresh.get("step7_reply_tier")},
                        now))

                def drop(data):
                    rec = (data.get("transfers") or {}).get(
                        record["transfer_id"])
                    if rec is None:
                        return
                    if int(rec.get("revision") or 0) != int(
                            record.get("revision") or 0):
                        raise transfers.RevisionMismatch(
                            "revision lệch trong lúc ghi")
                    del data["transfers"][record["transfer_id"]]

                transfers.modify_transfers(self.root, drop)
        except transfers.RevisionMismatch:
            return self._transfer_error_response(
                session, bot_id,
                "Trang đã cũ — tải lại trang rồi thử lại.", status=409)
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — chưa thay đổi gì, thử "
                "lại sau.")
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_complete", bot_id,
                    {"transfer_id": record["transfer_id"]},
                    {"owner_account": record.get("to_account_id")}, "ok")
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Đã hoàn tất chuyển",
                "<h1>Đã hoàn tất chuyển bot</h1>"
                "<p>Bot đã chuyển sang tài khoản mới. Nhật ký chuyển "
                "được giữ lại để đối chiếu.</p>"
                '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                csrf_token=session["csrf_token"])))

    def _handle_transfer_retry_step7(self, request, session, bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        now = time.time()
        record, error = self._transfer_load(request, bot_id, now, session)
        if error is not None:
            return error
        revision_error = self._transfer_revision_error(
            request, record, session, bot_id)
        if revision_error is not None:
            return revision_error
        if int(record.get("current_step") or 0) != 7:
            return self._transfer_error_response(
                session, bot_id, "Mốc 7 không phải mốc hiện tại.")
        try:
            marker = transfers.generate_step7_marker(self.root, bot_id, now)
        except transfers.TransferError as exc:
            return self._render_transfer_step(
                session, bot_id, entry, record, errors=[str(exc)])
        baseline = ui_store.fetch_state_entry(
            ui_store.read_fetch_state(self.root), bot_id).get("fetch_count")
        if not isinstance(baseline, int):
            baseline = 0
        try:
            with self._bot_lock(bot_id):

                def retry(data):
                    rec = (data.get("transfers") or {}).get(
                        record["transfer_id"])
                    if rec is None or int(rec.get("revision") or 0) != int(
                            record.get("revision") or 0):
                        raise transfers.RevisionMismatch(
                            "revision lệch trong lúc ghi")
                    rec["step7_marker"] = marker
                    rec["step7_started_at"] = transfers.now_iso(now)
                    rec["step7_fetch_count_at_start"] = baseline
                    rec["step7_attempt"] = int(
                        rec.get("step7_attempt") or 1) + 1
                    rec["step7_marker_found_at"] = None
                    rec.setdefault("steps", {})["7"] = {
                        "status": "current", "at": None, "note": ""}
                    # Thử lại KHÔNG làm mới hết hạn 72h (C6: chỉ hoàn
                    # tất bước / quay lại / hủy làm mới); chỉ tăng
                    # revision.
                    rec["revision"] = int(rec.get("revision") or 0) + 1

                transfers.modify_transfers(self.root, retry)
        except transfers.RevisionMismatch:
            return self._transfer_error_response(
                session, bot_id,
                "Trang đã cũ — tải lại trang rồi thử lại.", status=409)
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_step", bot_id,
                    {"step": 7, "transfer_id": record["transfer_id"],
                     "action": "retry_step7"},
                    {"step": 7}, "ok")
        return self._with_security(Response.redirect(
            "/bots/%s/transfer" % bot_id))

    def _handle_transfer_rollback(self, request, session, bot_id, entry):
        now = time.time()
        record = self._active_transfer(bot_id, now)
        if record is None:
            return self._with_security(Response.redirect(
                "/bots/%s/transfer" % bot_id))
        if ui_store.transfer_step_done(record, 8):
            return self._transfer_error_response(
                session, bot_id,
                "Mốc 8 đã xong — đường quay lại đã đóng.")
        if request.method == "GET":
            return self._with_security(Response.html(
                200,
                ui_pages.transfer_rollback_page_html(
                    bot_id, entry.get("display_name") or bot_id, record,
                    session["csrf_token"], transfers.REASON_PRESETS)))
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        revision_error = self._transfer_revision_error(
            request, record, session, bot_id)
        if revision_error is not None:
            return revision_error
        errors = []
        for key in ("stop_fetcher_y", "no_pending_y", "reenable_x"):
            if request.form_value(key) != "yes":
                errors.append("Tick đủ checklist quay lại (dừng fetcher "
                              "ở tài khoản mới, hết tin tồn, bật lại lấy "
                              "tin ở tài khoản cũ).")
                break
        vault_decision = request.form_value("vault_decision")
        if vault_decision not in ("removed", "kept"):
            errors.append("Chọn đã gỡ hay giữ cấu hình ở tài khoản mới.")
        try:
            reason = transfers.combine_reason(
                request.form_value("reason_choice"),
                request.form_value("reason_note"))
        except transfers.TransferError as exc:
            errors.append(str(exc))
            reason = None
        if errors:
            return self._with_security(Response.html(
                200,
                ui_pages.transfer_rollback_page_html(
                    bot_id, entry.get("display_name") or bot_id, record,
                    session["csrf_token"], transfers.REASON_PRESETS,
                    errors=errors)))
        vault_kept = vault_decision == "kept"
        try:
            with self._bot_lock(bot_id):
                fresh = self._active_transfer(bot_id, now)
                if fresh is None or fresh.get("transfer_id") != \
                        record["transfer_id"]:
                    return self._transfer_error_response(
                        session, bot_id,
                        "Luồng chuyển đã thay đổi — tải lại trang rồi thử "
                        "lại.")
                if int(fresh.get("revision") or 0) != int(
                        record.get("revision") or 0):
                    return self._transfer_error_response(
                        session, bot_id,
                        "Trang đã cũ — tải lại trang rồi thử lại.",
                        status=409)
                if ui_store.transfer_step_done(fresh, 6):
                    to_id = fresh.get("to_account_id")
                    from_id = fresh.get("from_account_id")

                    def restore(registry_data):
                        bot_entry = registry_data["bots"].get(bot_id)
                        if not isinstance(bot_entry, dict):
                            raise transfers.TransferError(
                                "Bot không còn trong registry.")
                        current = ui_store.owner_account_of(bot_entry)
                        if current == from_id:
                            return
                        if current != to_id:
                            raise transfers.TransferError(
                                "Nhãn chủ quản hiện tại không khớp tài "
                                "khoản đích — không trả nhãn tự động "
                                "được.")
                        bot_entry["owner_account"] = from_id

                    try:
                        ui_store.modify_registry_checked(
                            self.root, restore)
                    except transfers.TransferError as exc:
                        return self._transfer_error_response(
                            session, bot_id, str(exc))
                    except ValueError:
                        return self._transfer_error_response(
                            session, bot_id,
                            "registry.json hỏng — từ chối ghi.")
                detail = {"reason": reason,
                          "cleanup_owed": vault_kept,
                          "vault_kept": vault_kept}
                # N8: ghi rõ bản sao lịch sử tại Y vẫn còn (chỉ khi bước
                # 5 đã chọn có-chuyển — không có bản sao thì không ghi).
                notes = []
                if vault_kept:
                    notes.append("Y vẫn giữ secret — nợ dọn")
                if fresh.get("history_choice") == "with_history":
                    notes.append(
                        "Bản sao lịch sử tại Y vẫn còn — chủ nhân tự "
                        "quyết giữ/xóa; nếu chuyển lại sau thì bước 5 "
                        "phải làm lại từ đầu vì cursor ở X có thể đã "
                        "tiến.")
                if notes:
                    detail["note"] = ". ".join(notes)
                transfers.append_transfer_event(
                    self.root,
                    transfers.make_event(
                        "rolled_back", fresh,
                        int(fresh.get("current_step") or 0), detail, now))
                transfers.modify_transfers(
                    self.root,
                    lambda data: data.get("transfers", {}).pop(
                        record["transfer_id"], None))
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — chưa thay đổi gì, thử "
                "lại sau.")
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_rollback", bot_id,
                    {"transfer_id": record["transfer_id"]},
                    {"owner_account": record.get("from_account_id")}, "ok")
        return self._with_security(Response.redirect("/bots/%s" % bot_id))

    def _handle_transfer_cancel(self, request, session, session_token,
                                bot_id, entry):
        now = time.time()
        record = self._active_transfer(bot_id, now)
        if record is None:
            return self._with_security(Response.redirect(
                "/bots/%s/transfer" % bot_id))
        step6_done = ui_store.transfer_step_done(record, 6)
        step8_done = ui_store.transfer_step_done(record, 8)
        if request.method == "GET":
            return self._with_security(Response.html(
                200,
                ui_pages.transfer_cancel_page_html(
                    bot_id, entry.get("display_name") or bot_id, record,
                    step6_done, step8_done, session["csrf_token"],
                    transfers.REASON_PRESETS)))
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        revision_error = self._transfer_revision_error(
            request, record, session, bot_id)
        if revision_error is not None:
            return revision_error
        try:
            reason = transfers.combine_reason(
                request.form_value("reason_choice"),
                request.form_value("reason_note"))
        except transfers.TransferError as exc:
            return self._with_security(Response.html(
                200,
                ui_pages.transfer_cancel_page_html(
                    bot_id, entry.get("display_name") or bot_id, record,
                    step6_done, step8_done, session["csrf_token"],
                    transfers.REASON_PRESETS, errors=[str(exc)])))
        confirm_token = self._issue_confirm_token(
            session_token,
            {"kind": "transfer_cancel", "bot_id": bot_id,
             "transfer_id": record["transfer_id"], "reason": reason,
             "revision": int(record.get("revision") or 0)})
        intro = (
            "<p>Hủy luồng chuyển bot <strong>%s</strong>?</p>"
            "<p>Lý do: %s</p>"
            % (ui_pages.esc(entry.get("display_name") or bot_id),
               ui_pages.esc(reason))
        )
        if step6_done:
            intro += (
                "<p><strong>Nhãn chủ quản đã đổi sang tài khoản mới và sẽ "
                "GIỮ NGUYÊN sau khi hủy.</strong></p>")
            if not step8_done:
                intro += (
                    "<p>Mốc 8 CHƯA làm — tài khoản cũ vẫn giữ secret và "
                    "cấu hình, cần dọn thủ công sau khi hủy.</p>")
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                "Hủy luồng chuyển", intro,
                "/bots/%s/transfer/cancel/execute" % bot_id,
                {"confirm_token": confirm_token}, session["csrf_token"],
                "Hủy luồng chuyển", danger=True,
                back_url="/bots/%s/transfer" % bot_id)))

    def _handle_transfer_cancel_execute(self, request, session,
                                        session_token, bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "transfer_cancel"
                or payload.get("bot_id") != bot_id):
            return self._transfer_error_response(
                session, bot_id,
                "Xác nhận đã hết hạn — quay lại và bấm lại.", status=403)
        now = time.time()
        data = ui_store.read_transfers(self.root)
        record = (data.get("transfers") or {}).get(payload.get("transfer_id"))
        if record is None or record.get("bot_id") != bot_id:
            return self._transfer_error_response(
                session, bot_id, "Luồng chuyển không còn.")
        if not ui_store.is_transfer_active(record, now):
            try:
                transfers.expire_stale_transfers(self.root, now)
            except OSError:
                pass
            return self._transfer_error_response(
                session, bot_id, "Luồng chuyển đã hết hạn.")
        if int(record.get("revision") or 0) != int(
                payload.get("revision") or -1):
            return self._transfer_error_response(
                session, bot_id,
                "Luồng chuyển đã thay đổi sau lúc xác nhận — làm lại từ "
                "đầu.", status=409)
        step6_done = ui_store.transfer_step_done(record, 6)
        step8_done = ui_store.transfer_step_done(record, 8)
        try:
            with self._bot_lock(bot_id):
                # N17: kiểm tra lại bản ghi MỚI + revision trong khóa —
                # bản ghi có thể đã đổi sau lúc kiểm tra ngoài khóa
                # (cùng mẫu N2). Không gọi expire_stale_transfers ở
                # đây (sẽ khóa chồng transfers_lock).
                fresh_data = ui_store.read_transfers(self.root)
                fresh = (fresh_data.get("transfers") or {}).get(
                    payload.get("transfer_id"))
                if fresh is None or fresh.get("bot_id") != bot_id:
                    return self._transfer_error_response(
                        session, bot_id, "Luồng chuyển không còn.")
                if not ui_store.is_transfer_active(fresh, now):
                    return self._transfer_error_response(
                        session, bot_id, "Luồng chuyển đã hết hạn.")
                if int(fresh.get("revision") or 0) != int(
                        payload.get("revision") or -1):
                    return self._transfer_error_response(
                        session, bot_id,
                        "Luồng chuyển đã thay đổi sau lúc xác nhận — làm "
                        "lại từ đầu.", status=409)
                step6_done = ui_store.transfer_step_done(fresh, 6)
                step8_done = ui_store.transfer_step_done(fresh, 8)
                transfers.append_transfer_event(
                    self.root,
                    transfers.make_event(
                        "cancelled", fresh,
                        int(fresh.get("current_step") or 0),
                        {"reason": payload.get("reason"),
                         "cleanup_owed": bool(step6_done and not step8_done)},
                        now))
                transfers.modify_transfers(
                    self.root,
                    lambda data: data.get("transfers", {}).pop(
                        record["transfer_id"], None))
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — chưa thay đổi gì, thử "
                "lại sau.")
        except TimeoutError:
            return self._bot_busy_response(session)
        self._audit(request.client_ip, "transfer_cancel", bot_id,
                    {"transfer_id": record["transfer_id"]}, None, "ok")
        return self._with_security(Response.redirect("/bots/%s" % bot_id))

    def _handle_transfer_dismiss_reminder(self, request, session, bot_id,
                                          entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        events = [event for event in ui_store.read_transfer_log(self.root)
                  if isinstance(event, dict)
                  and event.get("bot_id") == bot_id]
        if not events:
            return self._with_security(Response.redirect(
                "/bots/%s" % bot_id))
        # N11b: chỉ ghi `cleanup_confirmed` khi đang có nhắc dở thật
        # (suy từ sự kiện cuối — cùng predicate với dòng nhắc bền R5).
        # Không có nhắc thì chỉ chuyển hướng, không ghi thêm sự kiện.
        if ui_store.persistent_reminder_for_bot(events, bot_id) is None:
            return self._with_security(Response.redirect(
                "/bots/%s" % bot_id))
        last = events[-1]
        try:
            transfers.append_transfer_event(
                self.root,
                {"ts": int(time.time()),
                 "kind": "cleanup_confirmed",
                 "transfer_id": last.get("transfer_id"),
                 "bot_id": bot_id,
                 "from_account_id": last.get("from_account_id"),
                 "to_account_id": last.get("to_account_id"),
                 "step": last.get("step"),
                 "detail": {"cleanup_owed": False, "actor": "ui-owner"}})
        except OSError:
            return self._transfer_error_response(
                session, bot_id,
                "Không ghi được nhật ký chuyển — thử lại sau.")
        return self._with_security(Response.redirect("/bots/%s" % bot_id))

    # -- xóa bot ------------------------------------------------------------

    def _handle_delete_confirm(self, request, session, session_token,
                               bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        if self._active_transfer(bot_id) is not None:
            return self._transfer_locked_response(
                session, bot_id, "Xóa bot")
        confirm_token = self._issue_confirm_token(
            session_token, {"kind": "delete", "bot_id": bot_id})
        name = entry.get("display_name") or bot_id
        intro = (
            "<p>Xóa bot <strong>%s</strong> (%s) sẽ:</p>"
            "<ul>"
            "<li>Tắt unit tai nghe và gỡ bot khỏi danh sách.</li>"
            "<li>XÓA file token và file secret của bot này trên máy tính.</li>"
            "<li>GIỮ LẠI dữ liệu hàng đợi/trả lời cũ (không xóa).</li>"
            "</ul>"
            "<p>Sau khi xóa, cần báo cho phía Muse gỡ cấu hình lấy tin "
            "và cấu hình đồng bộ của bot này, nếu không phía Muse vẫn "
            "thử lấy tin và báo lỗi.</p>"
        ) % (ui_pages.esc(name), ui_pages.esc(bot_id))
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                "Xóa bot", intro, "/bots/%s/delete/execute" % bot_id,
                {"confirm_token": confirm_token}, session["csrf_token"],
                "Xóa bot %s" % name, danger=True,
                back_url="/bots/%s" % bot_id)))

    def _handle_delete_execute(self, request, session, session_token,
                               bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "delete"
                or payload.get("bot_id") != bot_id):
            return self._with_security(Response.html(
                403,
                ui_pages.simple_page_html(
                    "Xác nhận đã hết hạn",
                    "<h1>Xác nhận đã hết hạn</h1>"
                    "<p>Quay lại trang bot và bấm lại.</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))
        try:
            with self._bot_lock(bot_id):
                # Kiểm tra lại BÊN TRONG khóa lúc thực thi (S8):
                # confirm phát hành trước khi luồng bắt đầu vẫn bị chặn.
                if self._active_transfer(bot_id) is not None:
                    return self._transfer_locked_response(
                        session, bot_id, "Xóa bot")
                self.unit_runner("disable", bot_id)
                ui_store.modify_registry(
                    self.root,
                    lambda data: data["bots"].pop(bot_id, None))
                for suffix in ("token", "queue_secret", "sync_secret"):
                    self._remove_secret_file(bot_id, suffix)
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                    csrf_token=session["csrf_token"])))
        self._audit(request.client_ip, "bot_delete", bot_id,
                    {"display_name": entry.get("display_name")}, None, "ok")
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Đã xóa bot",
                "<h1>Đã xóa bot %s</h1>"
                "<p>File token và secret của bot đã xóa. Dữ liệu cũ vẫn "
                "giữ lại trên máy.</p>"
                "<p><strong>Nhớ báo cho phía Muse</strong> gỡ cấu hình lấy "
                "tin và cấu hình đồng bộ của bot này.</p>"
                '<p><a href="/">Về màn hình chính</a></p>'
                % ui_pages.esc(entry.get("display_name") or bot_id),
                csrf_token=session["csrf_token"])))

    # -- thêm chat vào allowlist ---------------------------------------------

    def _handle_add_chat_confirm(self, request, session, session_token,
                                 bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        try:
            seq = int(request.form_value("seq"))
        except (TypeError, ValueError):
            seq = 0
        rows = ui_store.read_jsonl_rows(
            self.root / "data" / bot_id / "queue.jsonl")
        target = None
        for row in rows:
            if row.get("seq") == seq:
                target = row
                break
        if target is None:
            return self._with_security(Response.html(
                404,
                ui_pages.simple_page_html(
                    "Không tìm thấy tin",
                    "<h1>Không tìm thấy tin</h1>"
                    "<p>Tin này không còn trong hàng đợi (có thể đã bị "
                    "cắt tỉa).</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))
        chat_id = ""
        record = target.get("record")
        if isinstance(record, dict):
            chat_id = str(record.get("chat_id") or "")
            chat_title = record.get("chat_title")
            sender = record.get("sender_name")
            sender_id = record.get("sender_id")
            sample_text = record.get("text") or record.get("caption") or ""
        else:
            chat_title = None
            sender = None
            sender_id = None
            sample_text = ""
        if not chat_id:
            return self._with_security(Response.html(
                404,
                ui_pages.simple_page_html(
                    "Không tìm thấy tin",
                    "<h1>Không tìm thấy tin</h1>"
                    "<p>Tin này không có chat_id hợp lệ.</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))
        allowlist = ui_store.read_allowlist(self.root, bot_id) or {}
        if chat_id in (allowlist.get("allowed_chats") or []):
            return self._with_security(Response.html(
                200,
                ui_pages.simple_page_html(
                    "Chat đã có trong danh sách",
                    "<h1>Chat đã có trong danh sách</h1>"
                    "<p>Chat này đã được phép từ trước.</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))
        confirm_token = self._issue_confirm_token(
            session_token,
            {"kind": "add_chat", "bot_id": bot_id, "chat_id": chat_id})
        chat_title = chat_title or ui_store.short_id(chat_id)
        sender = sender or ui_store.short_id(sender_id)
        intro = (
            "<p>Thêm chat sau vào danh sách cho phép của bot "
            "<strong>%s</strong>?</p>"
            "<ul>"
            "<li>Chat: %s (id rút gọn: %s)</li>"
            "<li>Người gửi tin: %s</li>"
            "<li>Tin mẫu: %s</li>"
            "</ul>"
            "<p>Sau khi thêm, bot sẽ trả lời chat này (phía Muse nhận "
            "danh sách mới ở chu kỳ lấy cấu hình tiếp theo).</p>"
        ) % (
            ui_pages.esc(entry.get("display_name") or bot_id),
            ui_pages.esc(chat_title),
            ui_pages.esc(ui_store.short_id(chat_id)),
            ui_pages.esc(sender),
            ui_pages.esc(ui_store.preview_text(sample_text, 120)),
        )
        return self._with_security(Response.html(
            200,
            ui_pages.confirm_page_html(
                "Thêm chat vào danh sách cho phép", intro,
                "/bots/%s/add-chat/execute" % bot_id,
                {"confirm_token": confirm_token}, session["csrf_token"],
                "Thêm chat này", back_url="/bots/%s" % bot_id)))

    def _handle_add_chat_execute(self, request, session, session_token,
                                 bot_id, entry):
        if request.method != "POST":
            return self._method_not_allowed(session)
        if not self._csrf_ok(request, session):
            return self._csrf_error(session)
        payload = self._consume_confirm_token(
            session_token, request.form_value("confirm_token"))
        if (payload is None or payload.get("kind") != "add_chat"
                or payload.get("bot_id") != bot_id):
            return self._with_security(Response.html(
                403,
                ui_pages.simple_page_html(
                    "Xác nhận đã hết hạn",
                    "<h1>Xác nhận đã hết hạn</h1>"
                    "<p>Quay lại trang bot và bấm lại.</p>"
                    '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                    csrf_token=session["csrf_token"])))
        chat_id = payload["chat_id"]
        try:
            with self._bot_lock(bot_id):
                allowlist = ui_store.read_allowlist(self.root, bot_id) or {}
                chats = list(allowlist.get("allowed_chats") or [])
                if chat_id not in chats:
                    chats.append(chat_id)
                allowlist["allowed_chats"] = chats
                allowlist.setdefault("owner_sender_ids", [])
                allowlist.setdefault("default_sender_policy", "deny")
                ui_store.write_allowlist(self.root, bot_id, allowlist)
        except TimeoutError:
            return self._with_security(Response.html(
                409,
                ui_pages.simple_page_html(
                    "Bot đang bận",
                    "<h1>Bot đang bận</h1><p>Thử lại sau vài giây.</p>",
                    csrf_token=session["csrf_token"])))
        self._audit(request.client_ip, "allowlist_add_chat", bot_id,
                    None, {"chat_id_short": ui_store.short_id(chat_id)}, "ok")
        return self._with_security(Response.html(
            200,
            ui_pages.simple_page_html(
                "Đã thêm chat",
                "<h1>Đã thêm chat</h1>"
                "<p>Bot sẽ trả lời chat này sau khi phía Muse lấy cấu hình "
                "mới (chu kỳ tiếp theo, trong vòng ~1 phút).</p>"
                '<p><a href="/bots/%s">Về trang bot</a></p>' % bot_id,
                csrf_token=session["csrf_token"])))
