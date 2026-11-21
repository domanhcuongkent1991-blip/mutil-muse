"""Xác thực Web UI: mật khẩu hash scrypt + phiên phía server +
giới hạn đăng nhập (``05`` §6, ``06`` §1A, SPEC S4).

- Mật khẩu: ``hashlib.scrypt`` (N=2^17, r=8, p=1 — đúng ``05`` §6),
  muối 16 byte, hash ghi file 600. KHÔNG có mật khẩu mặc định: UI
  không chạy được khi chưa có file hash (đặt bằng CLI
  ``set_ui_password.py`` lần chạy đầu).
- Phiên: token ≥128 bit (192 bit), lưu phía server, tên file là
  SHA-256 của token (liệt kê thư mục không lộ token), hết hạn 10 giờ
  (trong khoảng 8–12h của D7 — diễn giải, ghi rõ), thu hồi được từng
  phiên hoặc toàn bộ (đổi mật khẩu -> thu hồi tất cả).
- Giới hạn đăng nhập: 5 lần sai/15 phút, theo IP VÀ theo tài khoản
  (chỉ có 1 tài khoản nên "theo tài khoản" = toàn cục).
- Cookie do tầng app đặt: HttpOnly/Secure/SameSite=Strict (05 §6).

Module này không đọc bất kỳ secret nào của bot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

# Tham số scrypt theo 05 §6 (N=2^17, r=8, p=1). maxmem phải đủ cho
# 128*N*r byte (~134MB) nếu không OpenSSL từ chối với giới hạn mặc
# định 32MB — đặt 256MB, ghi rõ.
SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 2 ** 28
SCRYPT_DKLEN = 32
SALT_BYTES = 16

# Phiên hết hạn sau 10 giờ — trong khoảng 8–12h của D7 (diễn giải).
SESSION_LIFETIME_SECONDS = 10 * 3600
# Giới hạn đăng nhập: 5 sai/15 phút (05 §6).
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60

PASSWORD_FILENAME = "password.hash"
SESSIONS_DIRNAME = "sessions"
ATTEMPTS_FILENAME = "login_attempts.json"

# Mật khẩu xem mã bí mật trung tâm (v0.6.0, T3j — ngoại lệ F18(3)):
# mật khẩu thứ 2, khác mật khẩu giao diện, 1 mật khẩu chung cho mọi
# tài khoản (Q-V060-15). Hash scrypt cùng tham số, file riêng 600,
# KHÔNG vào backup, KHÔNG đi qua đường đồng bộ /config sang phía
# Muse, không có mật khẩu mặc định. Đặt/đổi/đặt lại bằng CLI
# set_view_secret_password.py trên máy tính.
VIEW_SECRET_PASSWORD_FILENAME = "view_secret.hash"
# Độ dài tối thiểu mật khẩu xem mã (Q-V060-15 — đề xuất 8 ký tự).
VIEW_SECRET_MIN_PASSWORD_CHARS = 8


def _ui_config_dir(root):
    return Path(root) / "ui-config"


def _ensure_ui_config(root):
    directory = _ui_config_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def hash_password(password, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """Hash mật khẩu, trả về chuỗi có mang tham số::

        scrypt$<n>$<r>$<p>$<muối hex>$<hash hex>

    Tham số ghi trong chuỗi để sau này đổi tham số vẫn kiểm chứng
    được hash cũ. ``password`` phải là chuỗi không rỗng.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("mật khẩu phải là chuỗi không rỗng")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM,
    )
    return "scrypt$%d$%d$%d$%s$%s" % (
        n, r, p, salt.hex(), digest.hex())


def verify_password(password, stored):
    """Kiểm chứng mật khẩu với chuỗi hash đã lưu. Sai định dạng,
    sai tham số, hoặc sai mật khẩu -> False (không raise, không
    tiết lộ lý do)."""
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    parts = stored.strip().split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False
    if n <= 1 or (n & (n - 1)) != 0 or r <= 0 or p <= 0:
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            dklen=len(expected), maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(digest, expected)


def password_hash_path(root):
    return _ui_config_dir(root) / PASSWORD_FILENAME


def load_password_hash(root):
    """Đọc chuỗi hash mật khẩu; thiếu/rỗng -> None (UI không chạy)."""
    try:
        text = password_hash_path(root).read_text(encoding="utf-8")
    except OSError:
        return None
    stripped = text.strip()
    return stripped or None


def set_password(root, password, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """Đặt mật khẩu mới (ghi hash 600, tmp+rename) + thu hồi TOÀN BỘ
    phiên đang có (05 §6: đổi mật khẩu -> thu hồi tất cả session)."""
    directory = _ensure_ui_config(root)
    hashed = hash_password(password, n=n, r=r, p=p)
    path = directory / PASSWORD_FILENAME
    tmp_path = path.with_suffix(".hash.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (hashed + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    revoke_all_sessions(root)
    return True


def view_secret_password_hash_path(root):
    """Đường dẫn file hash mật khẩu xem mã (T3j)."""
    return _ui_config_dir(root) / VIEW_SECRET_PASSWORD_FILENAME


def load_view_secret_password_hash(root):
    """Đọc chuỗi hash mật khẩu xem mã; thiếu/rỗng -> None (chưa đặt)."""
    try:
        text = view_secret_password_hash_path(root).read_text(
            encoding="utf-8")
    except OSError:
        return None
    stripped = text.strip()
    return stripped or None


def view_secret_password_is_set(root):
    """True khi đã đặt mật khẩu xem mã (file hash tồn tại, không rỗng)."""
    return load_view_secret_password_hash(root) is not None


def set_view_secret_password(root, password, n=SCRYPT_N, r=SCRYPT_R,
                             p=SCRYPT_P):
    """Đặt/đổi/đặt lại mật khẩu xem mã (ghi hash 600, tmp+rename).

    KHÁC set_password: KHÔNG thu hồi phiên đăng nhập giao diện (2 mật
    khẩu độc lập — đổi mật khẩu xem mã không vô hiệu lượt xem đang mở,
    Q-V060-22, và không đăng xuất chủ nhân khỏi giao diện)."""
    directory = _ensure_ui_config(root)
    hashed = hash_password(password, n=n, r=r, p=p)
    path = directory / VIEW_SECRET_PASSWORD_FILENAME
    tmp_path = path.with_suffix(".hash.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (hashed + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    return True


def verify_view_secret_password(root, password):
    """Kiểm chứng mật khẩu xem mã với hash đã lưu. Chưa đặt hash,
    mật khẩu rỗng, hoặc sai -> False (không raise, không lộ lý do)."""
    stored = load_view_secret_password_hash(root)
    if stored is None:
        return False
    if not isinstance(password, str) or not password:
        return False
    return verify_password(password, stored)


# --- Phiên phía server ---

def _sessions_dir(root):
    directory = _ui_config_dir(root) / SESSIONS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _session_file(root, token):
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return _sessions_dir(root) / ("%s.json" % digest)


def create_session(root, ip=None, now=None, lifetime=SESSION_LIFETIME_SECONDS):
    """Tạo phiên mới. Trả về (token, csrf_token).

    Token 192 bit (token_urlsafe 32 byte ~ 256 bit entropy — vượt mức
    tối thiểu 128 bit). CSRF token riêng từng phiên, lưu cùng phiên.
    """
    if now is None:
        now = time.time()
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    session = {
        "created_at": now,
        "expires_at": now + lifetime,
        "ip": ip,
        "csrf_token": csrf_token,
    }
    path = _session_file(root, token)
    tmp_path = path.with_suffix(".json.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(session).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    return token, csrf_token


def save_session(root, token, session):
    """Ghi lại dict phiên đã sửa (vd flash 1 lần — NS-R1, v0.6.0).

    Ghi nguyên tử tmp+rename, quyền 0600, đúng mẫu
    ``create_session``. Token không hợp lệ / phiên không phải dict
    thì không ghi gì, trả False; ghi xong trả True.
    """
    if not isinstance(token, str) or not token:
        return False
    if not isinstance(session, dict):
        return False
    path = _session_file(root, token)
    tmp_path = path.with_suffix(".json.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(session).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    return True


def get_session(root, token, now=None):
    """Đọc phiên theo token; hết hạn/không có -> None (xóa file hết
    hạn). Trả về dict phiên (có csrf_token) khi còn hạn."""
    if not isinstance(token, str) or not token:
        return None
    if now is None:
        now = time.time()
    path = _session_file(root, token)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    expires_at = data.get("expires_at")
    if not isinstance(expires_at, (int, float)) or now >= expires_at:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return data


def revoke_session(root, token):
    """Thu hồi 1 phiên. Trả về True nếu đã có phiên đó."""
    if not isinstance(token, str) or not token:
        return False
    path = _session_file(root, token)
    existed = path.exists()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return existed


def revoke_all_sessions(root):
    """Thu hồi toàn bộ phiên (đổi mật khẩu). Trả về số phiên đã xóa."""
    directory = _ui_config_dir(root) / SESSIONS_DIRNAME
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.json"):
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


# --- Giới hạn đăng nhập ---

# Khóa tiến trình cho chu trình đọc-sửa-ghi login_attempts.json:
# ThreadingHTTPServer chạy nhiều luồng, không khóa thì 2 lần ghi
# thất bại cùng lúc sẽ đọc cùng 1 bản cũ và ghi đè mất nhau (mất
# lượt đếm). Đọc kiểm tra (login_allowed) không cần khóa: ghi dùng
# tmp+rename nên lần đọc luôn thấy toàn bộ bản cũ hoặc bản mới.
_ATTEMPTS_LOCK = threading.Lock()


def _attempts_path(root):
    return _ui_config_dir(root) / ATTEMPTS_FILENAME


def _load_attempts(root):
    try:
        data = json.loads(_attempts_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {"by_ip": {}, "global": []}
    if not isinstance(data, dict):
        return {"by_ip": {}, "global": []}
    by_ip = data.get("by_ip")
    glob = data.get("global")
    return {
        "by_ip": by_ip if isinstance(by_ip, dict) else {},
        "global": glob if isinstance(glob, list) else [],
    }


def _save_attempts(root, data):
    directory = _ensure_ui_config(root)
    path = directory / ATTEMPTS_FILENAME
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _recent(timestamps, now):
    cutoff = now - LOGIN_WINDOW_SECONDS
    return [t for t in timestamps
            if isinstance(t, (int, float)) and t > cutoff]


def login_allowed(root, ip, now=None):
    """True nếu IP này còn được thử đăng nhập (chưa đủ 5 sai/15 phút
    theo IP lẫn toàn cục)."""
    if now is None:
        now = time.time()
    data = _load_attempts(root)
    by_ip = data["by_ip"].get(str(ip))
    if isinstance(by_ip, list) \
            and len(_recent(by_ip, now)) >= LOGIN_MAX_FAILURES:
        return False
    if len(_recent(data["global"], now)) >= LOGIN_MAX_FAILURES:
        return False
    return True


def record_login_failure(root, ip, now=None):
    """Ghi nhận 1 lần đăng nhập sai (theo IP + toàn cục)."""
    if now is None:
        now = time.time()
    with _ATTEMPTS_LOCK:
        data = _load_attempts(root)
        key = str(ip)
        entries = data["by_ip"].get(key)
        if not isinstance(entries, list):
            entries = []
        entries = _recent(entries, now) + [now]
        data["by_ip"][key] = entries
        data["global"] = _recent(data["global"], now) + [now]
        _save_attempts(root, data)


def record_login_success(root, ip, now=None):
    """Đăng nhập đúng: xóa lịch sử sai của IP này (không xóa của IP
    khác)."""
    if now is None:
        now = time.time()
    with _ATTEMPTS_LOCK:
        data = _load_attempts(root)
        key = str(ip)
        if key in data["by_ip"]:
            del data["by_ip"][key]
            _save_attempts(root, data)
