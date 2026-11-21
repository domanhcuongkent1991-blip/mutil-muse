"""Parse event thô Zalo -> bản ghi chuẩn v1 (``02`` §1–§2, D21).

Tai parse NGAY khi nhận: đầu nối biết Zalo, lõi chỉ thấy bản ghi
chuẩn. ``raw`` giữ nguyên toàn bộ JSON gốc, không lược.

Hành vi đã kiểm chứng (QUIRKS.md):

- ``result`` là MỘT object duy nhất (dict), nhưng code chấp nhận cả
  dict lẫn list (phòng Zalo đổi).
- Event KHÔNG có ``update_id`` số; định danh bằng
  ``message.message_id``. Thiếu ``message_id`` thì fallback hash
  ``hashlib`` ổn định qua restart.
- ``text``/``caption`` trích riêng; Zalo không có entities (null).
- Tin forward về dạng ``message.unsupported.received`` hoàn toàn
  không có nội dung -> kind ``unsupported``, text null, giữ raw.
- File/ảnh chỉ về text mô tả ``[File] ...`` -> ``attachments`` rỗng
  + ``file_hint``; CẤM bịa URL tải file.
- ``platform_date`` = ``message.date`` (epoch mili-giây, giữ nguyên
  đơn vị).
"""

from __future__ import annotations

from shared import ids
from shared.record import build_record, now_iso


def iter_events(result):
    """Chuẩn hóa ``result`` của getUpdates thành danh sách event dict.

    Dict -> 1 event; list -> các phần tử dict; còn lại -> rỗng.
    Event dict rỗng bị bỏ (không tạo bản ghi ma).
    """
    if isinstance(result, dict):
        return [result] if result else []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict) and item]
    return []


def platform_event_id_of(event):
    """Id gốc của nền tảng: ``message.message_id``, thiếu thì hash."""
    message = event.get("message")
    if isinstance(message, dict):
        mid = message.get("message_id")
        if mid is not None and str(mid) != "":
            return str(mid)
    return "h%s" % ids.stable_hash_hex(event)[:12]


def _as_str_or_none(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _sender_name(sender):
    for key in ("display_name", "name", "first_name"):
        value = sender.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def parse_event(bot_id, event, received_at=None):
    """Biến 1 event thô Zalo thành bản ghi chuẩn v1."""
    message = event.get("message")
    if not isinstance(message, dict):
        message = {}
    chat = message.get("chat")
    if not isinstance(chat, dict):
        chat = {}
    sender = message.get("from")
    if not isinstance(sender, dict):
        sender = message.get("sender")
        if not isinstance(sender, dict):
            sender = {}

    chat_id = chat.get("id")
    chat_id = "" if chat_id is None else str(chat_id)
    sender_id = sender.get("id")
    sender_id = "" if sender_id is None else str(sender_id)

    unsupported = "unsupported" in message or not message
    if unsupported:
        kind = "unsupported"
        text = None
        caption = None
    else:
        kind = "message"
        text = _as_str_or_none(message.get("text"))
        caption = _as_str_or_none(message.get("caption"))

    file_hint = None
    if text is not None and text.lstrip().startswith("[File]"):
        file_hint = text

    reply_to = None
    reply_msg = message.get("reply_to_message")
    if isinstance(reply_msg, dict) and reply_msg.get("message_id") is not None:
        reply_to = str(reply_msg["message_id"])
    elif message.get("reply_to_message_id") is not None:
        reply_to = str(message["reply_to_message_id"])

    message_id = message.get("message_id")
    message_id = None if message_id is None else str(message_id)

    platform_date = message.get("date")
    if isinstance(platform_date, bool) or not isinstance(
        platform_date, (int, float)
    ):
        platform_date = None

    return build_record(
        bot_id=bot_id,
        platform="zalo",
        platform_event_id=platform_event_id_of(event),
        event_id=ids.event_id(event),
        kind=kind,
        chat_id=chat_id,
        chat_title=_as_str_or_none(chat.get("title")),
        sender_id=sender_id,
        sender_name=_sender_name(sender),
        message_id=message_id,
        text=text,
        caption=caption,
        entities=None,
        caption_entities=None,
        reply_to_message_id=reply_to,
        file_hint=file_hint,
        attachments=[],
        from_bot=sender.get("is_bot") is True,
        platform_date=platform_date,
        received_at=now_iso() if received_at is None else received_at,
        raw=event,
    )
