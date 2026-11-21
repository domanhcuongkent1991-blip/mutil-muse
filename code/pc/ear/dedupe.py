"""Chống lặp: tập platform_event_id đã thấy, lưu bền (``01`` §2, D18).

Khóa chống lặp là ``(bot_id, platform_event_id)``; mỗi bot có file
``seen.txt`` riêng trong thư mục data của mình. File giữ tối đa 5000
id, ghi kiểu tmp+rename. Caller PHẢI cập nhật seen SAU khi ghi queue
bền (append + flush + fsync) — crash giữa chừng thì lần poll sau nhận
lại event và chống lặp chặn trùng.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

SEEN_CAP = 5000


def _fsync_dir(dir_path):
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class SeenStore:
    """Tập id đã thấy của 1 bot, bền trên đĩa."""

    def __init__(self, path, cap=SEEN_CAP):
        self.path = Path(path)
        self.cap = cap
        self.lock = threading.Lock()
        self._order = []
        self._set = set()
        self._load()

    def _load(self):
        try:
            handle = open(self.path, encoding="utf-8")
        except OSError:
            return
        with handle:
            ids = [line.strip() for line in handle if line.strip()]
        for key in ids[-self.cap:]:
            if key not in self._set:
                self._set.add(key)
                self._order.append(key)

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".txt.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(self._order[-self.cap:]) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)
        _fsync_dir(self.path.parent)

    def seen(self, key):
        """True nếu key đã thấy trước đây."""
        with self.lock:
            return key in self._set

    def __contains__(self, key):
        return self.seen(key)

    def __len__(self):
        with self.lock:
            return len(self._order)

    def add(self, key):
        """Ghi nhận key đã thấy; trả về True nếu là key mới.

        Chỉ gọi SAU khi bản ghi đã ghi bền vào queue. Key đã có thì
        không ghi lại file.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("seen key phải là chuỗi không rỗng")
        with self.lock:
            if key in self._set:
                return False
            self._set.add(key)
            self._order.append(key)
            if len(self._order) > self.cap:
                dropped = self._order[: len(self._order) - self.cap]
                self._order = self._order[len(dropped):]
                for old in dropped:
                    self._set.discard(old)
            self._save_locked()
            return True
