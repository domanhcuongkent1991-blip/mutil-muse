"""Singleton 1 tiến trình/bot (DESIGN.md §10, D6).

- flock trên ``locks/<bot_id>.lock`` (vị trí CỐ ĐỊNH, ngoài thư mục
  data của bot).
- File ``locks/<bot_id>.owner`` quyền 600 ghi
  ``{bot_id, pid, token_sha256_8}``.
- Tiến trình thứ 2 cùng ``bot_id`` HOẶC cùng hash token thì từ chối
  khởi động — kể cả khác bot_id nhưng trùng token (2 bot không được
  dùng chung 1 token).

Kiểm tra trùng hash token quét các file ``*.owner`` khác trong thư
mục locks; file owner của tiến trình đã chết được coi là cũ và không
chặn. Việc ghi owner + quét được giữ dưới 1 khóa guard chung để 2
tiến trình khởi động cùng lúc không lọt qua khe nhau.

Chỉ chạy trên Linux/WSL (dùng ``fcntl.flock``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from shared.ids import validate_bot_id

_GUARD_NAME = "singleton-guard.lock"


class SingletonError(RuntimeError):
    """Một tiến trình khác đã giữ bot_id hoặc token này."""


def token_sha256_8(token):
    """8 ký tự hex đầu của SHA-256(token) — không bao giờ lộ token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class SingletonLock:
    """Giữ flock + file owner trong suốt vòng đời tiến trình."""

    def __init__(self, locks_dir, bot_id, token_hash8):
        validate_bot_id(bot_id)
        self.locks_dir = Path(locks_dir)
        self.bot_id = bot_id
        self.token_hash8 = token_hash8
        self.lock_path = self.locks_dir / ("%s.lock" % bot_id)
        self.owner_path = self.locks_dir / ("%s.owner" % bot_id)
        self._lock_file = None
        self._acquired = False

    @classmethod
    def acquire_for(cls, locks_dir, bot_id, token):
        """Tạo khóa từ token thật (chỉ hash, không giữ token)."""
        lock = cls(locks_dir, bot_id, token_sha256_8(token))
        lock.acquire()
        return lock

    def acquire(self):
        """Chiếm singleton; raise SingletonError nếu đã có người giữ."""
        if self._acquired:
            return
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.locks_dir, 0o700)
        except OSError:
            pass
        self._lock_file = open(self.lock_path, "a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise SingletonError(
                "bot %s đã có tiến trình tai đang chạy" % self.bot_id
            )
        try:
            self._write_owner_and_check_token()
        except Exception:
            self._lock_file.close()
            self._lock_file = None
            raise
        self._acquired = True

    def _write_owner_and_check_token(self):
        guard_path = self.locks_dir / _GUARD_NAME
        with open(guard_path, "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                self._write_owner()
                conflict = self._find_token_conflict()
            finally:
                fcntl.flock(guard, fcntl.LOCK_UN)
        if conflict is not None:
            try:
                self.owner_path.unlink()
            except OSError:
                pass
            raise SingletonError(
                "token của bot %s đang được bot %s dùng (cùng hash "
                "token); 2 bot không được dùng chung 1 token"
                % (self.bot_id, conflict)
            )

    def _write_owner(self):
        owner = {
            "bot_id": self.bot_id,
            "pid": os.getpid(),
            "token_sha256_8": self.token_hash8,
        }
        tmp_path = self.owner_path.with_suffix(".owner.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(owner) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.owner_path)

    def _find_token_conflict(self):
        for owner_file in self.locks_dir.glob("*.owner"):
            if owner_file.name == self.owner_path.name:
                continue
            try:
                with open(owner_file, encoding="utf-8") as handle:
                    owner = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(owner, dict):
                continue
            if owner.get("token_sha256_8") != self.token_hash8:
                continue
            pid = owner.get("pid")
            # Không bỏ qua pid của chính tiến trình này: 2 khóa trong
            # cùng tiến trình (khác bot_id, cùng token) vẫn phải chặn.
            if _pid_alive(pid):
                return owner.get("bot_id") or owner_file.stem
        return None

    def release(self):
        """Thả singleton; chỉ xóa file owner của chính tiến trình này."""
        if not self._acquired:
            return
        self._acquired = False
        try:
            with open(self.owner_path, encoding="utf-8") as handle:
                owner = json.load(handle)
            if isinstance(owner, dict) and owner.get("pid") == os.getpid():
                self.owner_path.unlink()
        except (OSError, ValueError):
            pass
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
            self._lock_file.close()
            self._lock_file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
