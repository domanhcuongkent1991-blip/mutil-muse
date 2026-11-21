"""Định danh dùng chung: event_id, event_key, bot_id, tên file.

Nguồn: DESIGN.md §6 (định danh thống nhất) và §10 (mẫu bot_id),
BASELINE-DIFF.md R5 (giữ nguyên hàm ``event_id`` baseline) và D3
(fallback hash phải ổn định qua restart).

Hàm ``event_id`` giữ nguyên logic baseline của gói tai-PC: tiền tố
``m``/``u`` + sanitize ký tự lạ. Điểm duy nhất thay đổi là fallback
khi event không có id: baseline dùng ``hash()`` của Python (khác nhau
mỗi lần chạy) — bản này dùng ``hashlib.sha256`` (D3).
"""

from __future__ import annotations

import hashlib
import json
import re

BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def validate_bot_id(bot_id):
    """Kiểm tra bot_id theo mẫu DESIGN §10.

    Không khớp mẫu thì từ chối toàn bộ (raise), KHÔNG "làm sạch rồi
    tiếp tục". Trả về chính bot_id khi hợp lệ.
    """
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        raise ValueError(
            "bot_id không hợp lệ: %r (mẫu bắt buộc "
            "^[a-z0-9][a-z0-9-]{0,31}$)" % (bot_id,)
        )
    return bot_id


def sanitize_filename(name):
    """Làm sạch một id trước khi thành tên file (DESIGN §6).

    Chỉ giữ chữ cái, số, ``.``, ``_``, ``-``; ký tự khác thành ``_``;
    cắt dấu chấm ở đầu/cuối để tránh ``..``; rỗng thì trả về ``_``.
    """
    if not isinstance(name, str):
        name = str(name)
    cleaned = _FILENAME_UNSAFE_RE.sub("_", name).strip(".")
    return cleaned if cleaned else "_"


def stable_hash_hex(ev):
    """Hash SHA-256 ổn định qua restart của một event (D3).

    JSON đã sắp xếp khóa, không phụ thuộc thứ tự key hay lần chạy.
    """
    payload = json.dumps(
        ev, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_id(ev):
    """Định danh bản ghi — GIỮ NGUYÊN hàm baseline (R5).

    - ``update_id`` số (Telegram) -> ``u<id>``.
    - ``message.message_id`` (Zalo) -> ``m`` + id đã sanitize.
    - Không có id -> ``h`` + 12 ký tự hex đầu của SHA-256 (D3; baseline
      dùng ``hash()`` của Python, không ổn định qua restart).
    """
    uid = ev.get("update_id")
    if isinstance(uid, int):
        return "u%d" % uid
    mid = (ev.get("message") or {}).get("message_id")
    if mid:
        return "m%s" % "".join(c for c in str(mid) if c.isalnum() or c in "-_")
    return "h%s" % stable_hash_hex(ev)[:12]


def event_key(bot_id, event_id_value):
    """Khóa logic ``(bot_id, event_id)`` dạng chuỗi ``bot_id__event_id``.

    Dùng trong tên file claim và tên file queue phía Muse
    (``<bot_id>__<event_id>.json``). bot_id sai mẫu thì từ chối toàn bộ.
    """
    validate_bot_id(bot_id)
    if not isinstance(event_id_value, str) or not event_id_value:
        raise ValueError("event_id phải là chuỗi không rỗng")
    return "%s__%s" % (bot_id, event_id_value)
