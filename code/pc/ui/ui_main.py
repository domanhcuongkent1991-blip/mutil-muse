#!/usr/bin/env python3
"""Máy chủ HTTP của Web UI (cổng 8787) — adapter mỏng quanh UiApp.

Chạy trên PC, CHỈ bind loopback (127.0.0.1/::1) — truy cập từ xa qua
Tailscale Serve HTTPS (05 §6). CẤM bind 0.0.0.0: tham số bind khác
loopback bị từ chối ngay lúc khởi động.

Từ chối chạy khi chưa có file hash mật khẩu (05 §6: không mật khẩu
mặc định, đặt lần đầu bằng ``set_ui_password.py``).

Log chỉ metadata (giờ + IP + method + path + status). CẤM log query,
header, body, cookie.

LỆCH THIẾT KẾ (ghi rõ, xem WEB-UI.md): 05 §6 bắt Flask + Waitress và
cấm stdlib http.server; vòng này triển khai bằng stdlib
ThreadingHTTPServer theo yêu cầu stdlib-only của nhiệm vụ, giữ đủ mọi
thuộc tính bảo mật của thiết kế (hash scrypt, phiên phía server, CSRF
+ Origin/Sec-Fetch-Site, CSP, header, escape, bind loopback,
rate-limit). UC-08 (cài Flask trong WSL) vẫn còn mở, kiểm chứng trên
PC thật trước khi chốt stack cuối.
"""

import argparse
import ipaddress
import os
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Cho phép chạy trực tiếp dạng script: thêm thư mục gốc chứa shared/
# và pc/ vào sys.path.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from pc.ui import ui_app, ui_auth  # noqa: E402
from pc.config import default_base_dir  # noqa: E402

DEFAULT_PORT = 8787
DEFAULT_BIND = "127.0.0.1"
# Tran body UI (v0.2.5, T3b; DOI 20/11): 52 MB de form gui tin trung
# tam tai duoc file 50 MB + mep multipart (de file 51 MB van toi duoc
# store de nhan cau loi than thien, thay vi bi cong 413 kho). Cac form
# khac van nho hon tran nay rat nhieu; file qua 50 MB bi store tu choi
# 413 o lop duoi.
MAX_BODY_BYTES = 52 * 1024 * 1024
ALLOWED_BINDS = ("127.0.0.1", "::1", "localhost")

_IPV4_WITH_PORT_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+):\d+$")
_IPV6_BRACKETED_RE = re.compile(r"^\[([^\]]+)\](?::\d+)?$")


def _parse_ip_entry(entry):
    """Phân tích 1 mục IP (có thể kèm cổng); không phân tích được
    thì None. Trả về dạng chuẩn của ipaddress (cùng 1 bucket cho
    cùng 1 IP viết khác nhau)."""
    text = entry.strip().strip("\"'")
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    match = _IPV4_WITH_PORT_RE.match(text)
    if match:
        try:
            return str(ipaddress.ip_address(match.group(1)))
        except ValueError:
            return None
    match = _IPV6_BRACKETED_RE.match(text)
    if match:
        try:
            return str(ipaddress.ip_address(match.group(1)))
        except ValueError:
            return None
    return None


def resolve_client_ip(peer_ip, headers):
    """IP dùng cho giới hạn đăng nhập theo IP.

    UI chỉ bind loopback, truy cập từ xa qua Tailscale Serve (proxy
    nội bộ) nên peer TCP luôn là 127.0.0.1/::1 — dùng peer làm khóa
    thì mọi IP thật gộp chung 1 bucket, giới hạn theo IP thành hư
    (đúng hiện tượng quan sát được: by_ip không tách được ai với ai).

    Khi peer là loopback và proxy gửi ``X-Forwarded-For``, lấy mục
    CUỐI CÙNG phân tích được trong chuỗi — mục do proxy gần nhất
    thêm vào; mục đầu chuỗi có thể do client tự bịa. Peer không phải
    loopback thì BỎ QUA header (không tin header từ kết nối trực
    tiếp). Không phân tích được mục nào thì dùng peer (giữ hành vi
    cũ; bucket toàn cục vẫn hãm brute force kể cả khi header bịa).

    CHƯA KIỂM CHỨNG trên PC thật (U-UI3): Tailscale Serve có gửi
    X-Forwarded-For hay không. Nếu không gửi, hàm này trả về peer
    như cũ — không tệ hơn hiện tại.
    """
    peer = str(peer_ip)
    try:
        peer_is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_is_loopback = False
    if not peer_is_loopback:
        return _parse_ip_entry(peer) or peer
    forwarded = None
    if isinstance(headers, dict):
        forwarded = headers.get("x-forwarded-for")
    if not isinstance(forwarded, str) or not forwarded.strip():
        return _parse_ip_entry(peer) or peer
    resolved = None
    for entry in forwarded.split(","):
        parsed = _parse_ip_entry(entry)
        if parsed is not None:
            resolved = parsed
    return resolved or _parse_ip_entry(peer) or peer


def resolve_settings(argv=None):
    """Tham số dòng lệnh > biến môi trường > mặc định."""
    parser = argparse.ArgumentParser(description="May chu Web UI da-bot.")
    parser.add_argument("--root", "--base-dir", dest="root", default=None,
                        help="thu muc goc du lieu (mac dinh ~/multibot/)")
    parser.add_argument("--port", type=int, default=None,
                        help="cong HTTP (mac dinh 8787)")
    parser.add_argument("--bind", default=None,
                        help="chi chap nhan 127.0.0.1/::1/localhost")
    args = parser.parse_args(argv)
    root = args.root or os.environ.get("MULTIBOT_BASE_DIR")
    root = Path(root).expanduser() if root else default_base_dir()
    port = args.port
    if port is None:
        raw = os.environ.get("MULTIBOT_UI_PORT", "")
        try:
            port = int(raw) if raw.strip() else DEFAULT_PORT
        except ValueError:
            port = DEFAULT_PORT
    bind = args.bind or os.environ.get("UI_BIND") or DEFAULT_BIND
    return root, bind, port


def make_handler(app):
    """Tạo lớp handler gắn với 1 UiApp (1 app cho mọi request)."""

    class UiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MultibotUI/1.0"

        def log_message(self, fmt, *args):  # noqa: N802 - tên của stdlib
            # Log chỉ metadata: method + path (không query) + status.
            try:
                path = urlparse(self.path).path
            except ValueError:
                path = "-"
            sys.stderr.write(
                "%s %s %s %s\n" % (
                    self.client_address[0], self.command, path,
                    getattr(self, "_last_status", "-")))

        def _handle(self):
            try:
                parsed = urlparse(self.path)
            except ValueError:
                self._send(ui_app.Response.text(400, "bad request"))
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            length = 0
            raw_length = self.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except ValueError:
                    self._send(ui_app.Response.text(400, "bad request"))
                    return
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(ui_app.Response.text(413, "request too large"))
                return
            body = self.rfile.read(length) if length > 0 else b""
            headers = {key.lower(): value
                       for key, value in self.headers.items()}
            request = ui_app.Request(
                method=self.command, path=parsed.path, query=query,
                headers=headers, body=body,
                client_ip=resolve_client_ip(
                    self.client_address[0], headers))
            try:
                response = app.handle(request)
            except Exception as exc:  # noqa: BLE001 - không lộ lỗi ra ngoài
                sys.stderr.write("ui error: %s\n" % type(exc).__name__)
                response = ui_app.Response.text(500, "internal error")
                for key, value in ui_app._security_headers().items():
                    response.headers.setdefault(key, value)
            self._send(response)

        def _send(self, response):
            self._last_status = response.status
            self.send_response(response.status)
            self.send_header("Content-Length", str(len(response.body)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        def do_GET(self):  # noqa: N802 - tên của stdlib
            self._handle()

        def do_POST(self):  # noqa: N802 - tên của stdlib
            self._handle()

        def do_PUT(self):  # noqa: N802 - tên của stdlib
            self._send(ui_app.Response.text(405, "method not allowed"))

        def do_DELETE(self):  # noqa: N802 - tên của stdlib
            self._send(ui_app.Response.text(405, "method not allowed"))

        def do_PATCH(self):  # noqa: N802 - tên của stdlib
            self._send(ui_app.Response.text(405, "method not allowed"))

    return UiHandler


def main(argv=None):
    root, bind, port = resolve_settings(argv)
    if bind not in ALLOWED_BINDS:
        sys.stderr.write(
            "TU CHOI: UI chi bind loopback (127.0.0.1/::1). "
            "Truy cap tu xa qua Tailscale Serve HTTPS.\n")
        return 2
    if ui_auth.load_password_hash(root) is None:
        sys.stderr.write(
            "TU CHOI: chua co mat khau UI. Chay set_ui_password.py "
            "tren may tinh truoc.\n")
        return 2
    app = ui_app.UiApp(root)
    server = ThreadingHTTPServer((bind, port), make_handler(app))
    server.daemon_threads = True

    def _stop(signum, frame):
        # GC-T3f-01 (T3m): shutdown() CHO vong lap serve_forever()
        # thoat; goi ngay trong handler (chay tren luong chinh) thi
        # vong lap khong bao gio thoat -> treo vinh vien. Chi dat
        # lenh dung trong luong phu roi tra ve ngay.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sys.stderr.write("UI dang chay tai http://%s:%d (loopback only)\n"
                     % (bind, port))
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
