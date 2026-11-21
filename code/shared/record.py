"""Bản ghi chuẩn hóa mỏng v1: đầu nối -> lõi (DESIGN.md §6).

Đúng bảng trường DESIGN §6: 21 trường + ``raw`` (toàn bộ JSON gốc,
không lược). Quy tắc:

- ``chat_id``/``sender_id`` luôn là chuỗi (tránh mất chính xác số
  nguyên 64-bit); số nguyên được ép sang chuỗi, không giữ dạng số.
- ``text`` và ``caption`` trích riêng, không gộp.
- Trường vắng thì ``null``, không đoán.
- ``schema_version`` = 1; đổi ý nghĩa trường thì tăng version kèm ghi
  chú di chuyển.
"""

from __future__ import annotations

import copy
from datetime import datetime

from shared import ids

SCHEMA_VERSION = 1

RECORD_FIELDS = (
    "schema_version",
    "bot_id",
    "platform",
    "platform_event_id",
    "event_id",
    "kind",
    "chat_id",
    "chat_title",
    "sender_id",
    "sender_name",
    "message_id",
    "text",
    "caption",
    "entities",
    "caption_entities",
    "reply_to_message_id",
    "file_hint",
    "attachments",
    "from_bot",
    "platform_date",
    "received_at",
    "raw",
)

_RECORD_FIELDS_SET = frozenset(RECORD_FIELDS)

ATTACHMENT_FIELDS = (
    "kind",
    "file_id",
    "file_name",
    "mime_type",
    "file_size",
    "local_path",
    "pc_local_path",
    "muse_local_path",
    "file_error",
)

_ATTACHMENT_FIELDS_SET = frozenset(ATTACHMENT_FIELDS)

RECORD_KINDS = ("message", "migrate", "service", "unsupported")

PLATFORMS = ("zalo", "telegram")

# Trường chuỗi-hoặc-null trong bản ghi.
_NULLABLE_STR_FIELDS = (
    "chat_title",
    "sender_name",
    "message_id",
    "text",
    "caption",
    "reply_to_message_id",
    "file_hint",
)


def now_iso():
    """Giờ hiện tại dạng ISO-8601 KÈM MÚI GIỜ (đồng hồ máy cục bộ)."""
    return datetime.now().astimezone().isoformat()


def _coerce_id(value, field, required):
    """Ép id về chuỗi; số nguyên -> chuỗi, bool -> lỗi."""
    if value is None:
        if required:
            raise ValueError("trường %s bắt buộc có giá trị" % field)
        return None
    if isinstance(value, bool):
        raise ValueError("trường %s không được là boolean" % field)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError("trường %s phải là chuỗi hoặc số nguyên" % field)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
        value, bool
    )


def validate_record(record):
    """Kiểm tra một bản ghi theo đúng bảng trường DESIGN §6.

    Trả về danh sách lỗi (rỗng = hợp lệ). Không raise.
    """
    errors = []
    if not isinstance(record, dict):
        return ["record phải là dict"]
    for field in RECORD_FIELDS:
        if field not in record:
            errors.append("thiếu trường: %s" % field)
    for key in record:
        if key not in _RECORD_FIELDS_SET:
            errors.append("trường lạ: %s" % key)

    def present(field):
        return field in record

    if present("schema_version"):
        value = record["schema_version"]
        if not _is_int(value) or value != SCHEMA_VERSION:
            errors.append("schema_version phải là số nguyên 1")
    if present("bot_id"):
        try:
            ids.validate_bot_id(record["bot_id"])
        except ValueError:
            errors.append("bot_id không hợp lệ: %r" % (record["bot_id"],))
    if present("platform") and record["platform"] not in PLATFORMS:
        errors.append("platform phải là một trong %s" % (PLATFORMS,))
    for field in ("platform_event_id", "event_id"):
        if present(field):
            value = record[field]
            if not isinstance(value, str) or not value:
                errors.append("%s phải là chuỗi không rỗng" % field)
    if present("kind") and record["kind"] not in RECORD_KINDS:
        errors.append("kind phải là một trong %s" % (RECORD_KINDS,))
    for field in ("chat_id", "sender_id"):
        if present(field) and not isinstance(record[field], str):
            errors.append("%s phải luôn là chuỗi" % field)
    for field in _NULLABLE_STR_FIELDS:
        if present(field):
            value = record[field]
            if value is not None and not isinstance(value, str):
                errors.append("%s phải là chuỗi hoặc null" % field)
    for field in ("entities", "caption_entities"):
        if present(field):
            value = record[field]
            if value is not None and not isinstance(value, list):
                errors.append("%s phải là mảng hoặc null" % field)
    if present("attachments"):
        value = record["attachments"]
        if not isinstance(value, list):
            errors.append("attachments phải là mảng")
        else:
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    errors.append("attachments[%d] phải là object" % index)
                    continue
                for key in item:
                    if key not in _ATTACHMENT_FIELDS_SET:
                        errors.append(
                            "attachments[%d] có trường lạ: %s" % (index, key)
                        )
                if "file_size" in item:
                    size = item["file_size"]
                    if size is not None and not _is_int(size):
                        errors.append(
                            "attachments[%d].file_size phải là số nguyên "
                            "hoặc null" % index
                        )
                for key in _ATTACHMENT_FIELDS_SET - {"file_size"}:
                    if key in item:
                        item_value = item[key]
                        if item_value is not None and not isinstance(
                            item_value, str
                        ):
                            errors.append(
                                "attachments[%d].%s phải là chuỗi hoặc "
                                "null" % (index, key)
                            )
    if present("from_bot") and not isinstance(record["from_bot"], bool):
        errors.append("from_bot phải là boolean")
    if present("platform_date"):
        value = record["platform_date"]
        if value is not None and not _is_number(value):
            errors.append("platform_date phải là số hoặc null")
    if present("received_at"):
        value = record["received_at"]
        if not isinstance(value, str):
            errors.append("received_at phải là chuỗi ISO-8601 kèm múi giờ")
        else:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                errors.append("received_at không parse được: %r" % (value,))
            else:
                if parsed.tzinfo is None:
                    errors.append("received_at phải kèm múi giờ")
    if present("raw") and not isinstance(record["raw"], dict):
        errors.append("raw phải là object")
    return errors


def build_record(
    *,
    bot_id,
    platform,
    platform_event_id,
    raw,
    chat_id,
    sender_id,
    event_id=None,
    kind="message",
    chat_title=None,
    sender_name=None,
    message_id=None,
    text=None,
    caption=None,
    entities=None,
    caption_entities=None,
    reply_to_message_id=None,
    file_hint=None,
    attachments=None,
    from_bot=False,
    platform_date=None,
    received_at=None,
):
    """Dựng một bản ghi chuẩn v1 và kiểm tra trước khi trả về.

    ``event_id`` vắng thì suy ra từ ``raw`` bằng hàm baseline
    (:func:`shared.ids.event_id`); ``received_at`` vắng thì lấy giờ
    hiện tại kèm múi giờ; ``attachments`` vắng thì thành mảng rỗng.
    Raise ``ValueError`` nếu bản ghi dựng xong không hợp lệ.
    """
    ids.validate_bot_id(bot_id)
    if platform not in PLATFORMS:
        raise ValueError("platform phải là một trong %s" % (PLATFORMS,))
    if not isinstance(raw, dict):
        raise ValueError("raw phải là dict (toàn bộ JSON gốc, không lược)")
    if event_id is None:
        event_id = ids.event_id(raw)
    record = {
        "schema_version": SCHEMA_VERSION,
        "bot_id": bot_id,
        "platform": platform,
        "platform_event_id": _coerce_id(platform_event_id, "platform_event_id", True),
        "event_id": event_id,
        "kind": kind,
        "chat_id": _coerce_id(chat_id, "chat_id", True),
        "chat_title": chat_title,
        "sender_id": _coerce_id(sender_id, "sender_id", True),
        "sender_name": sender_name,
        "message_id": _coerce_id(message_id, "message_id", False),
        "text": text,
        "caption": caption,
        "entities": entities,
        "caption_entities": caption_entities,
        "reply_to_message_id": _coerce_id(
            reply_to_message_id, "reply_to_message_id", False
        ),
        "file_hint": file_hint,
        "attachments": [] if attachments is None else attachments,
        "from_bot": from_bot,
        "platform_date": platform_date,
        "received_at": now_iso() if received_at is None else received_at,
        "raw": copy.deepcopy(raw),
    }
    errors = validate_record(record)
    if errors:
        raise ValueError("bản ghi không hợp lệ: %s" % "; ".join(errors))
    return record
