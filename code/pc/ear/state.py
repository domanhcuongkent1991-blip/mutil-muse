"""Ghi ``state.json`` của tai sau MỖI lần poll (``04`` §1.1, D5).

Schema tối thiểu bắt buộc::

    {bot_id, updated_at, last_poll_ok, last_poll_at, consec_err,
     last_error_class, last_error_at, token_state, queue_latest_seq,
     queue_floor_seq, started_at, version}

Ghi kiểu tmp+rename (ghi bền trước khi đổi tên). Gateway/UI chỉ đọc
file này; không có file sức khỏe riêng nào khác.

``token_state`` nhận các giá trị: ``starting`` (đang khởi động, chờ
lần kiểm tra đầu tiên), ``ok`` (token dùng được), ``WAIT_TOKEN``
(token hỏng, đang chờ token mới — SPEC F5), ``unknown`` (chưa xác
định). Thiết kế v0.2 không liệt kê cứng các giá trị này; 4 giá trị
trên là diễn giải của vòng code này, ghi rõ tại đây.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from shared.record import now_iso

STATE_VERSION = 1

STATE_FIELDS = (
    "bot_id",
    "updated_at",
    "last_poll_ok",
    "last_poll_at",
    "consec_err",
    "last_error_class",
    "last_error_at",
    "token_state",
    "queue_latest_seq",
    "queue_floor_seq",
    "started_at",
    "version",
)

TOKEN_STATES = ("starting", "ok", "WAIT_TOKEN", "unknown")


def new_state(bot_id, started_at=None):
    """Trạng thái khởi tạo cho 1 bot."""
    return {
        "bot_id": bot_id,
        "updated_at": now_iso(),
        "last_poll_ok": False,
        "last_poll_at": None,
        "consec_err": 0,
        "last_error_class": None,
        "last_error_at": None,
        "token_state": "starting",
        "queue_latest_seq": 0,
        "queue_floor_seq": 0,
        "started_at": now_iso() if started_at is None else started_at,
        "version": STATE_VERSION,
    }


def validate_state(state):
    """Kiểm tra state đủ trường đúng kiểu; trả về danh sách lỗi."""
    errors = []
    if not isinstance(state, dict):
        return ["state phải là dict"]
    for field in STATE_FIELDS:
        if field not in state:
            errors.append("thiếu trường: %s" % field)
    if errors:
        return errors
    if not isinstance(state["bot_id"], str) or not state["bot_id"]:
        errors.append("bot_id phải là chuỗi không rỗng")
    if not isinstance(state["updated_at"], str) or not state["updated_at"]:
        errors.append("updated_at phải là chuỗi ISO-8601")
    if not isinstance(state["last_poll_ok"], bool):
        errors.append("last_poll_ok phải là boolean")
    if state["last_poll_at"] is not None and not isinstance(
        state["last_poll_at"], str
    ):
        errors.append("last_poll_at phải là chuỗi hoặc null")
    if not isinstance(state["consec_err"], int) or state["consec_err"] < 0:
        errors.append("consec_err phải là số nguyên >= 0")
    if state["last_error_class"] is not None and not isinstance(
        state["last_error_class"], str
    ):
        errors.append("last_error_class phải là chuỗi hoặc null")
    if state["last_error_at"] is not None and not isinstance(
        state["last_error_at"], str
    ):
        errors.append("last_error_at phải là chuỗi hoặc null")
    if state["token_state"] not in TOKEN_STATES:
        errors.append("token_state phải là một trong %s" % (TOKEN_STATES,))
    for field in ("queue_latest_seq", "queue_floor_seq"):
        if not isinstance(state[field], int) or state[field] < 0:
            errors.append("%s phải là số nguyên >= 0" % field)
    if not isinstance(state["started_at"], str) or not state["started_at"]:
        errors.append("started_at phải là chuỗi ISO-8601")
    if state["version"] != STATE_VERSION:
        errors.append("version phải là %d" % STATE_VERSION)
    return errors


def write_state(state_path, state):
    """Ghi state ra đĩa kiểu tmp+rename; cập nhật ``updated_at``.

    Raise ``ValueError`` nếu state không hợp lệ. Trả về state đã ghi.
    """
    state = dict(state)
    state["updated_at"] = now_iso()
    errors = validate_state(state)
    if errors:
        raise ValueError("state không hợp lệ: %s" % "; ".join(errors))
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return state


class StateWriter:
    """Giữ state trong bộ nhớ của tai và ghi ra đĩa sau mỗi lần poll."""

    def __init__(self, state_path, bot_id, started_at=None):
        self.state_path = Path(state_path)
        self.state = new_state(bot_id, started_at=started_at)

    def update(self, **fields):
        """Cập nhật trường rồi ghi ngay ra đĩa. Trường lạ thì raise."""
        for key in fields:
            if key not in STATE_FIELDS:
                raise ValueError("trường state lạ: %s" % key)
            if key in ("bot_id", "started_at", "version"):
                raise ValueError("trường state không được sửa: %s" % key)
        self.state.update(fields)
        return self.write()

    def write(self):
        """Ghi state hiện tại ra đĩa."""
        self.state = write_state(self.state_path, self.state)
        return self.state
