#!/usr/bin/env python3
"""Receiver đồng bộ trả lời trên PC (cổng 8766) — kênh K2, GĐ3b.

Một tiến trình RIÊNG (không gộp vào gateway — gateway vẫn 100%
chỉ-đọc), chỉ dùng thư viện chuẩn Python. Nhiệm vụ duy nhất: nhận
các dòng trả lời syncer phía Muse đẩy về và ghi append-only vào file
trả lời của đúng bot. Hop dong: thiet ke v0.2,
``design-docs/04-gateway-and-fetcher.md`` §4.

Endpoint (POST, Bearer secret rieng tung bot/tai khoan)::

  POST /v1/bots/<bot_id>/replies   body {"entries": [...]}
  POST /v1/center/<account_id>/tasks
  POST /v1/center/<account_id>/tasks/<task_id>/status
  POST /v1/center/<account_id>/messages
  POST /v1/center/<account_id>/files      (v0.2.5, T3b — tai file len)
  POST /v1/center/<account_id>/typing     (v0.6.0, T3h — tin hieu
                                           "dang tra loi")

Tong 6 endpoint (quyet doi quy tac 'exactly ONE endpoint' — doc 10
§9 #6; skill multibot_gateway_sync da cap nhat truoc khi code).
5 endpoint trung tam thuoc mo rong K2 (SPEC-TRUNG-TAM §2.3/TT8,
DESIGN-TRUNG-TAM §5). Muc dich duy nhat cua receiver van la nhan
du lieu day ve va ghi append-only — khong co endpoint nao khac
(dac biet khong endpoint gui tin, khong endpoint dieu khien,
khong endpoint doc). Phuong thuc khac POST tra 501 mac dinh cua
thu vien — bang chung receiver khong co endpoint nao khac.

Ranh giới cứng (kiểm chứng bằng grep ở test):

- KHÔNG đọc file token của bot, KHÔNG đọc hàng đợi của tai, KHÔNG ghi
  file trạng thái của tai, KHÔNG gọi API nền tảng.
- Auth: Bearer sync secret riêng từng bot (file 600, đọc lại mỗi
  request để xoay không cần restart), ``hmac.compare_digest``. CẤM
  dùng lại queue secret của gateway. Secret bot A ghi replies bot B
  -> 401. Bot không có trong registry -> 404.
- Endpoint trung tâm (chặng T1): Bearer = center push secret riêng
  từng tài khoản (2 secret/tài khoản: fetch cho gateway, push cho
  receiver — CẤM dùng lại secret bot, TS1). Mọi ghi vào kho trung
  tâm đều qua module store chung (``pc/center/center_store.py``)
  — receiver KHÔNG tự ghi file kho nào, KHÔNG tự tính seq, KHÔNG
  tự ghi seen (seen do store ghi khi gateway trả feed).
- Chống trùng theo ``reply_key`` (tập key trong bộ nhớ + nạp lại từ
  file lúc khởi động và khi gặp bot mới). Chống trùng trung tâm theo
  ``client_key``/``msg_id`` (do store giữ, quét log bền — sống qua
  restart receiver).
- Giới hạn tốc độ: tối đa 1 request/2s/bot (SUY LUẬN) — quá thì 429
  kèm ``Retry-After: 2``. Trung tâm: 1 request/2s/tài khoản.
- Trần bảo vệ (SUY LUẬN): body tối đa 1MB, tối đa 100 dòng/đợt, mỗi
  dòng tối đa 64KB. Syncer gửi đợt ≤50 dòng/256KB nên trần này không
  chặn luồng bình thường. Rieng endpoint tai file trung tam
  (v0.2.5; DOI 20/11): tran rieng 52 MB (file toi da 50 MB + mep multipart — de file 51 MB van toi duoc store de nhan cau loi than thien, thay vi bi cong 413 kho).
- Cắt tỉa file trả lời: cùng chính sách 30 ngày như hàng đợi (04 §4),
  chạy lúc khởi động + mỗi giờ; chỉ cắt theo ``sent_at``, giữ dòng
  không parse được. KHÔNG sua logic cat tia nay o chặng T1 (phan
  cua doi G4 dang chuan bi song song — chi thiet ke + test plan,
  khong sua code); prune kho trung tam la ham rieng trong module
  store, KHONG tu chay trong receiver.

Cấu hình (cùng mẫu gateway): tham số dòng lệnh > biến môi trường >
mặc định. Gốc dữ liệu: --root/--base-dir | MULTIBOT_BASE_DIR |
~/multibot/. Cổng: --port | MULTIBOT_RECEIVER_PORT | 8766. Bind:
--bind | RECEIVER_BIND | 0.0.0.0. Allowlist IP: --allow-cidrs |
MULTIBOT_ALLOW_CIDRS | 127.0.0.0/8,::1/128,100.64.0.0/10.

Log chỉ metadata (thời gian + bot + trạng thái + số dòng nhận/trùng).
CẤM log secret/header/nội dung trả lời (SPEC S6).
"""

import argparse
import hmac
import ipaddress
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

RECEIVER_VERSION = "0.1.0"
DEFAULT_PORT = 8766
DEFAULT_BIND = "0.0.0.0"
DEFAULT_ALLOW_CIDRS = "127.0.0.0/8,::1/128,100.64.0.0/10"
MAX_BODY_BYTES = 1024 * 1024
# Tran rieng cho POST /v1/center/<account>/files (v0.2.5): file toi
# da 50 MB + mep multipart (~1 MB). Tran 52 MB de file 51 MB van
# toi duoc store (cau loi than thien); body vuot tran nay -> 413
# file_too_large (khong doc body vao bo nho).
MAX_FILE_BODY_BYTES = 52 * 1024 * 1024
MAX_ENTRIES_PER_BATCH = 100
MAX_ENTRY_BYTES = 64 * 1024
# SUY LUAN (04 §4): toi da 1 request/2s/bot.
MIN_REQUEST_GAP_SECONDS = 2.0
# Cat tia: cung moc 30 ngay nhu hang doi (04 §4).
PRUNE_MAX_AGE_DAYS = 30
PRUNE_INTERVAL_SECONDS = 3600

BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
REPLY_STATUSES = ("sent", "failed", "dead")


def _load_center_store():
    """Import module store trung tam (pc.center.center_store).

    Chay dang script truc tiep thi sys.path khong co goc code — them
    goc code (thu muc chua pc/) roi import lai. Chi import, khong
    ghi file kho o day: moi ghi kho trung tam deu qua store.
    """
    try:
        from pc.center import center_store
        return center_store
    except ImportError:
        code_root = Path(__file__).resolve().parents[2]
        if str(code_root) not in sys.path:
            sys.path.insert(0, str(code_root))
        from pc.center import center_store
        return center_store


def _load_multipart():
    """Import module parse multipart (shared/multipart.py)."""
    try:
        from shared import multipart
        return multipart
    except ImportError:
        code_root = Path(__file__).resolve().parents[2]
        if str(code_root) not in sys.path:
            sys.path.insert(0, str(code_root))
        from shared import multipart
        return multipart


def default_log(message):
    """Ghi 1 dong log metadata ra stdout (journald thu lai)."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    print("%s %s" % (now, message), flush=True)


def parse_cidrs(text):
    """Parse chuoi CIDR phay nhau thanh danh sach network. Sai -> ValueError."""
    networks = []
    for part in text.split(","):
        part = part.strip()
        if part:
            networks.append(ipaddress.ip_network(part, strict=False))
    if not networks:
        raise ValueError("allowlist IP rong")
    return networks


def ip_allowed(networks, address_text):
    """True neu dia chi nguon nam trong allowlist. Khong parse duoc -> False."""
    try:
        addr = ipaddress.ip_address(address_text)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for net in networks:
        try:
            if addr in net:
                return True
        except TypeError:
            continue
    return False


def read_json_file(path):
    """Doc JSON khoan dung: thieu/hong/khong phai JSON -> None."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def registry_entry(root, bot_id):
    """Tim muc cua bot trong registry.json; ho tro ca dang dict va list."""
    data = read_json_file(root / "registry.json")
    if not isinstance(data, dict):
        return None
    bots = data.get("bots")
    if isinstance(bots, dict):
        entry = bots.get(bot_id)
        return entry if isinstance(entry, dict) else None
    if isinstance(bots, list):
        for entry in bots:
            if isinstance(entry, dict) and entry.get("bot_id") == bot_id:
                return entry
        return None
    entry = data.get(bot_id)
    return entry if isinstance(entry, dict) else None


def read_sync_secret(root, bot_id):
    """Doc sync secret cua bot tu file (600), moi request doc lai de
    xoay secret khong can restart. Thieu/rong -> None (khong bao gio
    chap nhan secret rong)."""
    path = root / "secrets" / ("%s.sync_secret" % bot_id)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first_line:
        return None
    return first_line.encode("utf-8")


def replies_path(root, bot_id):
    """Đường dẫn file trả lời của 1 bot."""
    return Path(root) / "data" / bot_id / "replies.jsonl"


def parse_iso_timestamp(text):
    """Parse ISO-8601 thanh datetime co mui gio; khong parse duoc -> None."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def validate_reply_entry(entry, bot_id):
    """Kiểm tra 1 dòng trả lời theo schema 04 §4.

    Trả về danh sách lỗi (rỗng = hợp lệ). Trường lạ được giữ nguyên
    (tương thích tới) nhưng dòng quá lớn bị từ chối ở tầng trên.
    """
    errors = []
    if not isinstance(entry, dict):
        return ["dòng trả lời phải là dict"]
    for field in ("reply_key", "bot_id", "chat_id", "sent_at", "status",
                  "kind"):
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            errors.append("%s phải là chuỗi không rỗng" % field)
    if entry.get("bot_id") != bot_id:
        errors.append("bot_id trong dòng không khớp bot trên URL")
    if entry.get("status") not in REPLY_STATUSES:
        errors.append("status phải là một trong %s" % (REPLY_STATUSES,))
    for field in ("text_full", "caption", "message_id", "error_class",
                  "chat_title", "in_reply_to_event_key"):
        value = entry.get(field)
        if value is not None and not isinstance(value, str):
            errors.append("%s phải là chuỗi hoặc null" % field)
    retry_count = entry.get("retry_count")
    if retry_count is not None and (
        not isinstance(retry_count, int) or isinstance(retry_count, bool)
        or retry_count < 0
    ):
        errors.append("retry_count phải là số nguyên >= 0 hoặc null")
    return errors


def load_reply_keys(path):
    """Nạp tập reply_key đã có trong file trả lời (dedupe lúc khởi
    động). Dòng hỏng bị bỏ qua. File thiếu -> tập rỗng."""
    keys = set()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return keys
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            key = row.get("reply_key")
            if isinstance(key, str) and key:
                keys.add(key)
    return keys


def _wait_test_pause(pause):
    """Seam chi dung trong test (G4): tam dung giua luc doc xong file
    va luc os.replace. Nhan tuple (read_done, release): bao read_done
    roi cho release toi da 10 s; hoac 1 Event don: set roi cho test
    clear (toi da 10 s). Production truyen None = khong dung."""
    if isinstance(pause, (tuple, list)) and len(pause) == 2:
        read_done, release = pause
        read_done.set()
        release.wait(timeout=10)
        return
    pause.set()
    deadline = time.monotonic() + 10
    while pause.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)


def prune_replies_file(path, max_age_days=PRUNE_MAX_AGE_DAYS, now=None,
                       _test_pause_after_read=None):
    """Cắt tỉa file trả lời theo tuổi ``sent_at`` (04 §4).

    Giữ dòng không parse được và dòng còn trong hạn; cắt dòng quá
    ``max_age_days``. Ghi lại kiểu tmp+rename khi có cắt. Trả về dict
    thống kê {"cut", "kept", "unparseable_kept"}.

    CHỈ gọi khi đã giữ khóa của ReceiverContext (xem ``prune_all``);
    không gọi trực tiếp từ luồng nền. Hàm này tự nó không khóa.
    """
    if now is None:
        now = datetime.now().astimezone()
    cutoff = now - timedelta(days=max_age_days)
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return {"cut": 0, "kept": 0, "unparseable_kept": 0}
    keep_lines = []
    unparseable = 0
    cut = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            keep_lines.append(line)
            unparseable += 1
            continue
        if not isinstance(row, dict):
            keep_lines.append(line)
            unparseable += 1
            continue
        sent = parse_iso_timestamp(row.get("sent_at"))
        if sent is not None and sent < cutoff:
            cut += 1
            continue
        keep_lines.append(line)
    if cut == 0:
        return {"cut": 0, "kept": len(keep_lines),
                "unparseable_kept": unparseable}
    if _test_pause_after_read is not None:
        _wait_test_pause(_test_pause_after_read)
    tmp_path = path.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.writelines(keep_lines)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    return {"cut": cut, "kept": len(keep_lines),
            "unparseable_kept": unparseable}


class ReceiverContext:
    """Trạng thái dùng chung của receiver: gốc dữ liệu, allowlist IP,
    tập dedupe theo bot, mốc giới hạn tốc độ, hàm log. Không giữ
    secret trong bộ nhớ."""

    def __init__(self, root, networks, log_fn=None,
                 min_gap=MIN_REQUEST_GAP_SECONDS, clock=None):
        self.root = Path(root)
        self.networks = list(networks)
        self.log = log_fn or default_log
        self.min_gap = min_gap
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._known_keys = {}
        self._last_request_at = {}
        self._loaded_bots = set()

    def _ensure_loaded(self, bot_id):
        if bot_id not in self._loaded_bots:
            self._known_keys[bot_id] = load_reply_keys(
                replies_path(self.root, bot_id))
            self._loaded_bots.add(bot_id)

    def _resync_known_keys_locked(self, bot_id):
        """Đọc lại _known_keys[bot_id] từ file thật sau prune.

        Hậu tố _locked (mẫu NS-01): giả định đã giữ self._lock, không
        tự khóa. Chỉ làm khi bot đã load; bot chưa load thì lần append
        đầu sẽ nạp từ file đã prune."""
        if bot_id in self._loaded_bots:
            self._known_keys[bot_id] = load_reply_keys(
                replies_path(self.root, bot_id))

    def check_rate_limit(self, bot_id):
        """True nếu được phép xử lý request này (đủ giãn cách). Ghi
        dấu mốc khi cho qua; request bị chặn không ghi dấu (để đợt
        thử lại sau không bị đẩy lùi thêm)."""
        now = self._clock()
        with self._lock:
            last = self._last_request_at.get(bot_id)
            if last is not None and now - last < self.min_gap:
                return False
            self._last_request_at[bot_id] = now
            return True

    def append_entries(self, bot_id, entries):
        """Ghi các dòng mới vào file trả lời, dedupe theo reply_key.

        Trả về (accepted, duplicates). Ghi append + flush + fsync từng
        dòng; file tạo mới đặt quyền 600. Toàn bộ kiểm tra + ghi dưới
        1 khóa để 2 request cùng bot không ghi trùng nhau.
        """
        path = replies_path(self.root, bot_id)
        accepted = 0
        duplicates = 0
        with self._lock:
            self._ensure_loaded(bot_id)
            known = self._known_keys[bot_id]
            fresh = []
            for entry in entries:
                key = entry.get("reply_key")
                if key in known:
                    duplicates += 1
                else:
                    known.add(key)
                    fresh.append(entry)
            if not fresh:
                return 0, duplicates
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            existed = path.exists()
            with open(path, "a", encoding="utf-8") as handle:
                for entry in fresh:
                    handle.write(
                        json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            accepted = len(fresh)
        return accepted, duplicates

    def prune_all(self, now=None, _test_pause_after_read=None):
        """Cắt tỉa file trả lời của mọi bot trong registry.

        Giữ self._lock theo từng bot, bao trọn 1 chu trình đọc-sửa-ghi
        của bot đó (D-G4-2). Giả định: 1 tiến trình receiver duy nhất
        trên 1 root dữ liệu — đua là đua trong tiến trình nên
        threading.Lock chung là đủ; nếu sau này chạy 2 receiver cùng
        root thì phải nâng lên flock (lúc đó sửa thiết kế, không sửa
        lén). KHÔNG gọi prune_all từ bất kỳ hàm nào đang giữ self._lock
        (append_entries, check_rate_limit đều giữ khóa); _prune_loop chỉ
        gọi từ thread riêng, main gọi lúc khởi động trước khi server
        nhận request — không lồng khóa (NS-01).
        """
        data = read_json_file(self.root / "registry.json")
        bot_ids = []
        if isinstance(data, dict):
            bots = data.get("bots")
            if isinstance(bots, dict):
                bot_ids = list(bots.keys())
            elif isinstance(bots, list):
                bot_ids = [b.get("bot_id") for b in bots
                           if isinstance(b, dict) and b.get("bot_id")]
        for bot_id in bot_ids:
            if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
                continue
            with self._lock:
                stats = prune_replies_file(
                    replies_path(self.root, bot_id), now=now,
                    _test_pause_after_read=_test_pause_after_read)
                if stats["cut"]:
                    self._resync_known_keys_locked(bot_id)
                    self.log("receiver prune bot=%s cut=%d kept=%d"
                             % (bot_id, stats["cut"], stats["kept"]))


def make_handler(ctx):
    """Tạo handler HTTP. Chỉ định nghĩa do_POST; mọi phương thức khác
    trả 501 mặc định — bằng chứng receiver không có endpoint nào khác."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "multibot-sync-receiver/%s" % RECEIVER_VERSION
        sys_version = ""

        def log_message(self, *args):
            pass

        def _log(self, bot_label, status, extra=""):
            parts = ["receiver", "bot=%s" % bot_label,
                     "status=%d" % status]
            if extra:
                parts.append(extra)
            ctx.log(" ".join(parts))

        def _send_json(self, code, obj, extra_headers=None):
            try:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                body = b'{"error": "internal_error"}'
                code = 500
            try:
                self.send_response(code)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if extra_headers:
                    for key, value in extra_headers.items():
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def _authenticated(self, bot_id):
            expected = read_sync_secret(ctx.root, bot_id)
            if expected is None:
                return False
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            given = auth[len("Bearer "):].strip()
            if not given or any(ch.isspace() for ch in given):
                return False
            return hmac.compare_digest(given.encode("utf-8"), expected)

        # -- Endpoint trung tam (chặng T1, mo rong K2) -------------------
        # Moi ghi kho deu qua module store chung; receiver KHONG tu ghi
        # file kho, KHONG tu tinh seq, KHONG tu ghi seen. Ten file kho
        # chi nam trong center_store (grep "chi ghi qua store").
        def _center_log(self, account_label, endpoint, status, extra=""):
            parts = ["receiver", "center", "account=%s" % account_label,
                     "endpoint=%s" % endpoint, "status=%d" % status]
            if extra:
                parts.append(extra)
            ctx.log(" ".join(parts))

        def _authenticated_center(self, account_id):
            """Xac thuc Bearer = center push secret cua dung tai khoan
            (TS1). Secret doc lai moi request de xoay khong can restart.
            """
            store = _load_center_store()
            expected = store.read_center_secret(ctx.root, account_id,
                                                "push")
            if expected is None:
                return False
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            given = auth[len("Bearer "):].strip()
            if not given or any(ch.isspace() for ch in given):
                return False
            return hmac.compare_digest(given.encode("utf-8"), expected)

        def _read_json_body(self):
            """Doc body JSON (tran 1MB). Tra ve (payload, None) hoac
            (None, (ma_http, ma_loi))."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                return None, (413, "body_too_large")
            try:
                raw = self.rfile.read(length) if length else b""
            except OSError:
                return None, (400, "bad_body")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None, (400, "bad_json")
            if not isinstance(payload, dict):
                return None, (400, "bad_body")
            return payload, None

        def _handle_center(self, parts):
            """Dinh tuyen POST trung tam. Thu tu kiem tra nhu duong bot:
            mau account -> ton tai -> secret -> gioi han toc do -> body.

            Duong dan (parts = path.split("/")):
              /v1/center/<account>/tasks                len 5 -> tao viec
              /v1/center/<account>/tasks/<id>/status   len 7 -> doi trang thai
              /v1/center/<account>/messages            len 5 -> gui tin
              /v1/center/<account>/files               len 5 -> tai file len
              /v1/center/<account>/typing              len 5 -> tin hieu
                                                        "dang tra loi"
            """
            store = _load_center_store()
            account_id = parts[3]
            endpoint = parts[4]
            task_id = None
            if endpoint == "tasks" and len(parts) == 7:
                task_id = parts[5]
                if parts[6] != "status":
                    self._send_json(404, {"error": "not_found"})
                    self._center_log("none", "tasks", 404)
                    return
            elif endpoint == "tasks" and len(parts) == 5:
                pass  # tao viec
            elif endpoint == "messages" and len(parts) == 5:
                pass  # gui tin
            elif endpoint == "files" and len(parts) == 5:
                pass  # tai file len (v0.2.5)
            elif endpoint == "typing" and len(parts) == 5:
                pass  # tin hieu "dang tra loi" (v0.6.0, T3h)
            else:
                self._send_json(404, {"error": "not_found"})
                self._center_log("none", "unknown", 404)
                return
            if not BOT_ID_RE.match(account_id):
                self._send_json(400, {"error": "bad_account_id"})
                self._center_log("invalid", endpoint, 400)
                return
            if account_id not in store.read_accounts(ctx.root):
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, endpoint, 404)
                return
            if not self._authenticated_center(account_id):
                self._send_json(401, {"error": "unauthorized"})
                self._center_log(account_id, endpoint, 401)
                return
            # Gioi han 1 request/2s/tai khoan (SUY LUAN — khoa rieng
            # "center:<account>", khong lan voi khoa bot).
            if not ctx.check_rate_limit("center:" + account_id):
                self._send_json(429, {"error": "rate_limited"},
                                extra_headers={"Retry-After": "2"})
                self._center_log(account_id, endpoint, 429,
                                 extra="rate_limited")
                return
            # Co nguoi gui (v0.3.0, T3c — NS-03): nguoi gui da tam
            # ngung/go (co center_participating tat) thi POST
            # /messages + POST /tasks/<id>/status -> 403
            # not_participating (nhu /files). Tao viec (/tasks) khong
            # doi co nguoi gui — thiet ke chi yeu cau 2 duong nay.
            # (v0.6.0, T3h) Tin hieu typing cung doi co nguoi gui —
            # tai khoan da ngung khong duoc bao "dang tra loi".
            if endpoint in ("messages", "typing") or task_id is not None:
                accounts = store.read_accounts(ctx.root)
                sender = accounts.get(account_id) or {}
                if not sender.get("center_participating"):
                    self._send_json(403,
                                    {"error": "not_participating"})
                    self._center_log(account_id, endpoint, 403,
                                     extra="not_participating")
                    return
            if endpoint == "files":
                # Body multipart (tran rieng 11 MB) — doc trong
                # _center_upload, khong qua _read_json_body (tran 1 MB).
                self._center_upload(account_id, store)
                return
            payload, error = self._read_json_body()
            if error is not None:
                code, code_name = error
                self._send_json(code, {"error": code_name})
                self._center_log(account_id, endpoint, code)
                return
            try:
                if endpoint == "messages":
                    self._center_message(account_id, payload, store)
                elif endpoint == "typing":
                    self._center_typing(account_id, payload, store)
                elif task_id is not None:
                    self._center_status(account_id, task_id, payload,
                                        store)
                else:
                    self._center_create(account_id, payload, store)
            except Exception as exc:  # noqa: BLE001 - chi log ten lop loi
                self._send_json(500, {"error": "internal_error"})
                self._center_log(account_id, endpoint, 500,
                                 extra="error_class=%s"
                                 % type(exc).__name__)

        def _center_error(self, account_id, endpoint, exc):
            """Anh xa CenterError cua store sang HTTP.

            (FIXL C3) Gui {"error": exc.error} va them "code" CHI
            KHI exc.code co mat — cac 403 khac giu nguyen body.
            """
            body = {"error": exc.error}
            if getattr(exc, "code", None) is not None:
                body["code"] = exc.code
            self._send_json(exc.http_status, body)
            self._center_log(account_id, endpoint, exc.http_status)

        def _center_upload(self, account_id, store):
            """POST /v1/center/<account_id>/files — tai 1 file len
            (v0.2.5, Q-C7). Multipart, dung 1 file, truong ``file``.

            Chi tai khoan DA tham gia moi duoc tai len (TT16): co
            tham gia bi tat -> 403 ``not_participating`` (cac endpoint
            /messages + /status cung kiem tra co nguoi goi tu T3c —
            NS-03, khong chi /files). Ghi kho qua store (bytes
            tmp+rename + sidecar). Loi: rong -> 400 ``empty_file``;
            qua 50 MB/file
            -> 413 ``file_too_large`` (PERMANENT); tong kho 2 GB
            day -> 413 ``store_full`` (RETRYABLE); ten file sai hoac
            khop mau R6 -> 400. Khong log ten file (co the chua
            secret) — chi log dung luong.
            """
            accounts = store.read_accounts(ctx.root)
            account = accounts.get(account_id) or {}
            if not account.get("center_participating"):
                self._send_json(403, {"error": "not_participating"})
                self._center_log(account_id, "files", 403,
                                 extra="not_participating")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_FILE_BODY_BYTES:
                self._send_json(413, {"error": "file_too_large"})
                self._center_log(account_id, "files", 413,
                                 extra="body_too_large")
                return
            try:
                raw = self.rfile.read(length) if length else b""
            except OSError:
                self._send_json(400, {"error": "bad_body"})
                self._center_log(account_id, "files", 400)
                return
            multipart = _load_multipart()
            try:
                _fields, files = multipart.parse_multipart(
                    raw, self.headers.get("Content-Type") or "")
            except ValueError:
                self._send_json(400, {"error": "bad_multipart"})
                self._center_log(account_id, "files", 400)
                return
            if len(files) != 1 or files[0].get("name") != "file":
                self._send_json(400, {"error": "bad_file_field"})
                self._center_log(account_id, "files", 400)
                return
            upload = files[0]
            center = store.CenterStore(ctx.root)
            try:
                meta = center.save_file(account_id,
                                        upload.get("filename") or "",
                                        upload.get("data") or b"")
            except store.CenterError as exc:
                self._center_error(account_id, "files", exc)
                return
            self._send_json(200, {"file_id": meta["file_id"],
                                  "file_name": meta["file_name"],
                                  "file_size": meta["file_size"],
                                  "sha256": meta["sha256"]})
            self._center_log(account_id, "files", 200,
                             extra="file_size=%d" % meta["file_size"])

        def _center_create(self, account_id, payload, store):
            title = payload.get("title")
            body = payload.get("body")
            kind = payload.get("kind")
            assigned_to = payload.get("assigned_to")
            due_at = payload.get("due_at")
            client_key = payload.get("client_key")
            # Chan mau R6 truoc khi ghi (title/body).
            for text in (title, body):
                if isinstance(text, str) \
                        and store.find_secret_like(text) is not None:
                    self._send_json(400,
                                    {"error": "blocked_secret_pattern"})
                    self._center_log(account_id, "tasks", 400,
                                     extra="r6_blocked")
                    return
            # assigned_to nhan chuoi (v1) hoac danh sach 1..5 (v2):
            # moi nguoi phai ton tai + tham gia + co du cap secret.
            accounts = store.read_accounts(ctx.root)
            assignees = None
            if isinstance(assigned_to, str):
                assignees = [assigned_to]
            elif isinstance(assigned_to, list):
                assignees = assigned_to
            if not assignees \
                    or not all(isinstance(item, str)
                               and BOT_ID_RE.match(item)
                               for item in assignees) \
                    or len(set(assignees)) != len(assignees) \
                    or len(assignees) > store.MAX_MENTIONS:
                self._send_json(400, {"error": "bad_assigned_to"})
                self._center_log(account_id, "tasks", 400)
                return
            for target_id in assignees:
                target = accounts.get(target_id)
                if target is None:
                    self._send_json(400, {"error": "bad_assigned_to"})
                    self._center_log(account_id, "tasks", 400)
                    return
                if not target.get("center_participating"):
                    self._send_json(400, {"error": "not_participating"})
                    self._center_log(account_id, "tasks", 400)
                    return
                if not store.has_center_secrets(ctx.root, target_id):
                    self._send_json(400, {"error": "no_center_secret"})
                    self._center_log(account_id, "tasks", 400)
                    return
            center = store.CenterStore(ctx.root)
            try:
                task, duplicate = center.create_task(
                    account_id, title, body, kind, assignees, due_at,
                    client_key)
            except store.CenterError as exc:
                self._center_error(account_id, "tasks", exc)
                return
            self._send_json(200, {"task_id": task["task_id"],
                                  "duplicate": duplicate})
            self._center_log(account_id, "tasks", 200,
                             extra="duplicate=%s" % duplicate)

        def _center_status(self, account_id, task_id, payload, store):
            if not isinstance(task_id, str) \
                    or not store.TASK_ID_RE.match(task_id):
                self._send_json(400, {"error": "bad_task_id"})
                self._center_log(account_id, "tasks", 400)
                return
            note = payload.get("note")
            # Chan mau R6 cho note (V2-NS-08).
            if isinstance(note, str) \
                    and store.find_secret_like(note) is not None:
                self._send_json(400,
                                {"error": "blocked_secret_pattern"})
                self._center_log(account_id, "tasks", 400,
                                 extra="r6_blocked")
                return
            center = store.CenterStore(ctx.root)
            task = center.get_task(task_id)
            if task is None:
                self._send_json(404, {"error": "task_not_found"})
                self._center_log(account_id, "tasks", 404)
                return
            if account_id in store._as_assignee_list(
                    task.get("assigned_to")):
                role = "assigned_to"
            elif account_id == task.get("assigned_from"):
                role = "assigned_from"
            else:
                # (C-7, 22/11 — theo mau C3 cua FIXL) Them ma rieng
                # "not_assignee" de phia Muse phan biet 403 nay voi
                # cac 403 khac va bo dong (tai khoan khong con la
                # assignee/assigner thi khong bao gio doi duoc trang
                # thai nua). Khong doi error "forbidden" de tuong
                # thich nguoc.
                self._send_json(403, {"error": "forbidden",
                                      "code": "not_assignee"})
                self._center_log(account_id, "tasks", 403)
                return
            try:
                task, duplicate = center.change_status(
                    task_id, payload.get("to_status"), account_id, role,
                    note, payload.get("client_key"))
            except store.CenterError as exc:
                self._center_error(account_id, "tasks", exc)
                return
            self._send_json(200, {"task_id": task["task_id"],
                                  "status": task["status"],
                                  "duplicate": duplicate})
            self._center_log(account_id, "tasks", 200,
                             extra="duplicate=%s" % duplicate)

        def _center_typing(self, account_id, payload, store):
            """POST /v1/center/<account_id>/typing — tin hieu "dang
            tra loi" (v0.6.0, T3h — SPEC TT8, DESIGN §3.9 buoc 8).

            Body {room, started_at} -> data/center/typing.json (qua
            store, dung chung khoa cua store); TTL 90 giay do GIAO DIEN
            kiem tra khi doc (receiver khong tu xoa theo gio);
            receiver xoa muc typing khi tin hoi that vao dung phong
            (trong store.append_message). Body sai -> 4xx, khong ghi
            gi. Gioi han 1 request/2s/tai khoan ap dung nhu moi
            endpoint trung tam khac (kiem tra o _handle_center).
            """
            try:
                room = store.parse_room(payload.get("room"))
            except store.CenterError:
                self._send_json(400, {"error": "bad_room"})
                self._center_log(account_id, "typing", 400)
                return
            started_at = payload.get("started_at")
            if isinstance(started_at, bool) \
                    or not isinstance(started_at, (int, float)) \
                    or started_at <= 0:
                self._send_json(400, {"error": "bad_body"})
                self._center_log(account_id, "typing", 400)
                return
            center = store.CenterStore(ctx.root)
            center.set_typing(account_id, room, started_at)
            self._send_json(200, {"ok": True})
            self._center_log(account_id, "typing", 200)

        def _center_message(self, account_id, payload, store):
            body = payload.get("body")
            kind = payload.get("kind")
            msg_id = payload.get("msg_id")
            task_id = payload.get("task_id")
            to_account = payload.get("to_account")
            file_id = payload.get("file_id")
            # (NS-L3, AUDIT-V060 §2) Danh sach rong [] = gui chung:
            # chuan hoa ve None NGAY tai diem vao, truoc khoi kiem
            # tra to_account ben duoi. KHONG sua trong
            # _as_to_account_list() — khoi kiem tra dung None lam
            # tin hieu loi (ValueError -> targets=None -> 400).
            if to_account == []:
                to_account = None
            # Hub tu choi moi POST /messages co kind=giao-viec (chi hub
            # tu sinh — NS-18); kind la cung bi tu choi.
            if kind not in store.POSTABLE_MESSAGE_KINDS:
                self._send_json(400, {"error": "kind_not_allowed"})
                self._center_log(account_id, "messages", 400)
                return
            # Phong (v0.3.0, T3c — SPEC TT8, DESIGN §3.7 buoc 2):
            # room mac dinh "chung"; sai mau -> 400; bao-cao ngoai
            # khung chung -> 400 (P5); gui vao phong rieng cua tai
            # khoan khac -> 403; gui vao phong luu tru (co cua tai
            # khoan trong phong da tat) -> 403 not_participating.
            # Phong khong ton tai -> 403 room_forbidden (khong lo
            # danh sach tai khoan). Phong rieng cua chinh nguoi gui
            # (dm:<chinh minh>) duoc phep — co nguoi gui da kiem tra
            # o _handle_center (NS-03).
            try:
                room = store.parse_room(payload.get("room"))
            except store.CenterError:
                self._send_json(400, {"error": "bad_room"})
                self._center_log(account_id, "messages", 400)
                return
            if kind == "bao-cao" and room != store.ROOM_CHUNG:
                self._send_json(400, {"error": "bad_room"})
                self._center_log(account_id, "messages", 400,
                                 extra="bao_cao_in_room")
                return
            if room.startswith("dm:"):
                target = room[3:]
                if target != account_id:
                    accounts = store.read_accounts(ctx.root)
                    target_rec = accounts.get(target)
                    if target_rec is not None \
                            and not target_rec.get("center_participating"):
                        error = "not_participating"  # phong luu tru
                    else:
                        error = "room_forbidden"
                    self._send_json(403, {"error": error})
                    self._center_log(account_id, "messages", 403,
                                     extra=error)
                    return
            # Chan mau R6 cho body tin nhan.
            if isinstance(body, str) \
                    and store.find_secret_like(body) is not None:
                self._send_json(400,
                                {"error": "blocked_secret_pattern"})
                self._center_log(account_id, "messages", 400,
                                 extra="r6_blocked")
                return
            if task_id is not None and not isinstance(task_id, str):
                self._send_json(400, {"error": "bad_task_id"})
                self._center_log(account_id, "messages", 400)
                return
            # file_id: neu co thi phai la chuoi dung mau; store kiem
            # tra ton tai + dung nguoi tai len (NS-06) — sai thi 400
            # unknown_file_id, tin chua ghi nen gui lai duoc.
            if file_id is not None and (
                    not isinstance(file_id, str)
                    or not store.FILE_ID_RE.match(file_id)):
                self._send_json(400, {"error": "unknown_file_id"})
                self._center_log(account_id, "messages", 400)
                return
            # (v0.6.0, T3l — D1(j), SPEC TT8) ``to_account``: chuoi
            # cu van chap nhan nhu danh sach 1 nguoi; danh sach v4
            # toi da 5 — qua 5 -> 400 ``bad_to_account``. Kiem tra
            # tung phan tu theo thu tu: ton tai -> 400
            # ``bad_to_account``; dang tham gia -> 403
            # ``to_account_not_participating``; dang bat thao luan
            # -> 403 ``discussion_disabled`` (D10, quyet qua dem
            # 20/11: tu choi ngay, khong im lang). Nguoi gui tu tat
            # thao luan ma van ghi ``to_account`` (tin thao luan)
            # cung bi 403 ``discussion_disabled`` (frag-05 §4.2).
            # Tu choi toan bo tin (khong gui cho nguoi con lai).
            if to_account is not None:
                accounts = store.read_accounts(ctx.root)
                try:
                    targets = store._as_to_account_list(to_account)
                except ValueError:
                    targets = None
                if targets is None \
                        or len(targets) > store.MAX_MENTIONS:
                    self._send_json(400, {"error": "bad_to_account"})
                    self._center_log(account_id, "messages", 400)
                    return
                for target in targets:
                    if not isinstance(target, str) \
                            or target not in accounts:
                        self._send_json(400,
                                        {"error": "bad_to_account"})
                        self._center_log(account_id, "messages", 400)
                        return
                # (G-L3, AUDIT-V060) Kiem tra thao luan/tham gia chi
                # ap cho tin thao luan (kind == "hoi"): tin bao-cao
                # kem to_account khong phai tin thao luan, khong bi
                # 403 oan vi nguoi gui/tai khoan dich tat thao luan
                # hay tam ngung. Kiem tra ton tai + tran 5 o tren
                # van ap cho moi kind co to_account.
                if kind == "hoi":
                    sender = accounts.get(account_id) or {}
                    if not sender.get("center_discussion_enabled", True):
                        self._send_json(
                            403, {"error": "discussion_disabled"})
                        self._center_log(account_id, "messages", 403,
                                         extra="discussion_disabled")
                        return
                    for target in targets:
                        if not accounts[target].get("center_participating"):
                            self._send_json(
                                403,
                                {"error": "to_account_not_participating"})
                            self._center_log(
                                account_id, "messages", 403,
                                extra="to_account_not_participating")
                            return
                    for target in targets:
                        if not accounts[target].get(
                                "center_discussion_enabled", True):
                            self._send_json(
                                403, {"error": "discussion_disabled"})
                            self._center_log(account_id, "messages", 403,
                                             extra="discussion_disabled")
                            return
            center = store.CenterStore(ctx.root)
            if task_id is not None:
                task = center.get_task(task_id)
                if task is None:
                    self._send_json(404, {"error": "task_not_found"})
                    self._center_log(account_id, "messages", 404)
                    return
                # Tin bao-cao gan task_id chi chap nhan tu assigned_to
                # (NS-19); bao-cao chung (task_id rong) thi ai cung duoc.
                if kind == "bao-cao" \
                        and account_id not in store._as_assignee_list(
                            task.get("assigned_to")):
                    self._send_json(403, {"error": "forbidden",
                                          "code": "not_assignee"})
                    self._center_log(account_id, "messages", 403,
                                     extra="not_assignee")
                    return
            try:
                message, duplicate = center.append_message(
                    account_id, kind, to_account, task_id, body, msg_id,
                    file_id=file_id, room=room)
            except store.CenterError as exc:
                self._center_error(account_id, "messages", exc)
                return
            self._send_json(200, {"msg_id": message["msg_id"],
                                  "seq": message.get("seq"),
                                  "duplicate": duplicate,
                                  "report_role": message.get(
                                      "report_role")})
            self._center_log(account_id, "messages", 200,
                             extra="duplicate=%s" % duplicate)

        def do_POST(self):
            # 1. Allowlist IP nguồn — ngoài allowlist -> 403, trước mọi thứ.
            client_ip = self.client_address[0]
            if not ip_allowed(ctx.networks, client_ip):
                self._send_json(403, {"error": "forbidden"})
                self._log("none", 403)
                return
            parsed = urlparse(self.path)
            parts = parsed.path.split("/")
            # 1b. Dinh tuyen trung tam (chặng T1): /v1/center/<account>/...
            if len(parts) >= 5 and parts[0] == "" and parts[1] == "v1" \
                    and parts[2] == "center":
                self._handle_center(parts)
                return
            # 2. Định tuyến nghiêm: POST /v1/bots/<bot_id>/replies.
            if (len(parts) != 5 or parts[0] != "" or parts[1] != "v1"
                    or parts[2] != "bots" or parts[4] != "replies"):
                self._send_json(404, {"error": "not_found"})
                self._log("none", 404)
                return
            bot_id = parts[3]
            # 3. bot_id sai mẫu -> từ chối toàn bộ.
            if not BOT_ID_RE.match(bot_id):
                self._send_json(400, {"error": "bad_bot_id"})
                self._log("invalid", 400)
                return
            # 4. Bot không có trong registry -> 404 chung chung.
            if registry_entry(ctx.root, bot_id) is None:
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, 404)
                return
            # 5. Sync secret của ĐÚNG bot; sai/thiếu -> 401.
            if not self._authenticated(bot_id):
                self._send_json(401, {"error": "unauthorized"})
                self._log(bot_id, 401)
                return
            # 6. Giới hạn tốc độ 1 request/2s/bot.
            if not ctx.check_rate_limit(bot_id):
                self._send_json(429, {"error": "rate_limited"},
                                extra_headers={"Retry-After": "2"})
                self._log(bot_id, 429, extra="rate_limited")
                return
            # 7. Đọc body (trần 1MB) + parse JSON.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "body_too_large"})
                self._log(bot_id, 413)
                return
            try:
                raw = self.rfile.read(length) if length else b""
            except OSError:
                self._send_json(400, {"error": "bad_body"})
                self._log(bot_id, 400)
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {"error": "bad_json"})
                self._log(bot_id, 400)
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "bad_body"})
                self._log(bot_id, 400)
                return
            entries = payload.get("entries")
            if not isinstance(entries, list):
                self._send_json(400, {"error": "bad_entries"})
                self._log(bot_id, 400)
                return
            if len(entries) > MAX_ENTRIES_PER_BATCH:
                self._send_json(400, {"error": "batch_too_large"})
                self._log(bot_id, 400)
                return
            # 8. Kiểm tra từng dòng; dòng lỗi -> từ chối CẢ đợt (syncer
            # giữ cursor, thử lại sau — không ghi nửa vời).
            for entry in entries:
                try:
                    size = len(json.dumps(
                        entry, ensure_ascii=False).encode("utf-8"))
                except (TypeError, ValueError):
                    size = MAX_ENTRY_BYTES + 1
                if size > MAX_ENTRY_BYTES:
                    self._send_json(400, {"error": "entry_too_large"})
                    self._log(bot_id, 400)
                    return
                errors = validate_reply_entry(entry, bot_id)
                if errors:
                    self._send_json(400, {"error": "bad_entry"})
                    self._log(bot_id, 400)
                    return
            try:
                accepted, duplicates = ctx.append_entries(bot_id, entries)
            except Exception as exc:  # noqa: BLE001 - chi log ten lop loi
                self._send_json(500, {"error": "internal_error"})
                self._log(bot_id, 500,
                          extra="error_class=%s" % type(exc).__name__)
                return
            self._send_json(200, {"ok": True, "accepted": accepted,
                                  "duplicates": duplicates})
            self._log(bot_id, 200, "accepted=%d duplicates=%d"
                      % (accepted, duplicates))

    return Handler


class ReceiverHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(ctx, bind, port):
    """Tạo server (chưa chạy). Port 0 = cổng tạm thời do hệ điều hành cấp."""
    return ReceiverHTTPServer((bind, port), make_handler(ctx))


def resolve_settings(argv=None):
    """Gộp cấu hình: dòng lệnh > môi trường > mặc định."""
    parser = argparse.ArgumentParser(
        description="Receiver dong bo tra loi he da-bot V2 (cong 8766).")
    parser.add_argument("--root", "--base-dir", dest="root", default=None,
                        help="thu muc goc multibot (mac dinh ~/multibot/)")
    parser.add_argument("--port", type=int, default=None,
                        help="cong lang nghe (mac dinh 8766)")
    parser.add_argument("--bind", default=None,
                        help="dia chi bind (mac dinh 0.0.0.0)")
    parser.add_argument("--allow-cidrs", default=None,
                        help="allowlist IP nguon, phay nhau")
    args = parser.parse_args(argv)

    root_text = (
        args.root
        or os.environ.get("MULTIBOT_BASE_DIR")
        or str(Path.home() / "multibot")
    )
    root = Path(str(root_text)).expanduser()
    port_text = args.port
    if port_text is None:
        port_text = os.environ.get("MULTIBOT_RECEIVER_PORT", DEFAULT_PORT)
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise SystemExit("cong receiver khong hop le: %r" % (port_text,))
    if not 1 <= port <= 65535:
        raise SystemExit("cong receiver phai trong 1..65535: %d" % port)
    bind = str(args.bind or os.environ.get("RECEIVER_BIND") or DEFAULT_BIND)
    cidrs_text = str(
        args.allow_cidrs
        or os.environ.get("MULTIBOT_ALLOW_CIDRS")
        or DEFAULT_ALLOW_CIDRS
    )
    try:
        networks = parse_cidrs(cidrs_text)
    except ValueError as exc:
        raise SystemExit("allowlist IP khong hop le: %s" % exc)
    return root, bind, port, networks


def main(argv=None):
    root, bind, port, networks = resolve_settings(argv)
    if not root.is_dir():
        raise SystemExit(
            "thu muc goc khong ton tai: %s — chay install.sh truoc" % root)
    ctx = ReceiverContext(root, networks)
    ctx.prune_all()
    try:
        server = make_server(ctx, bind, port)
    except OSError as exc:
        print("khong mo duoc cong %d (%s): %s" % (
            port, bind, type(exc).__name__), file=sys.stderr)
        print("Co the mot receiver khac dang chay, hoac cong dang ban.",
              file=sys.stderr)
        return 1

    stop_event = threading.Event()

    def _prune_loop():
        while not stop_event.wait(PRUNE_INTERVAL_SECONDS):
            try:
                ctx.prune_all()
            except Exception as exc:  # noqa: BLE001 - prune loi khong chet receiver
                ctx.log("receiver prune loi class=%s" % type(exc).__name__)

    prune_thread = threading.Thread(target=_prune_loop, daemon=True,
                                    name="receiver-prune")
    prune_thread.start()

    def _shutdown(signum, _frame):
        del signum, _frame
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    ctx.log("receiver started bind=%s port=%d root=%s" % (bind, port, root))
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
    ctx.log("receiver stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
