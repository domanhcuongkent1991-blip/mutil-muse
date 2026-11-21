"""Parse update Telegram thanh ban ghi chuan v1 (DESIGN §6, ``03`` §2).

Dang update Telegram (khac Zalo): ``getUpdates`` tra DANH SACH update,
moi update co ``update_id`` so tang dan; chong lap theo ``update_id``
(``platform_event_id`` = ``str(update_id)``, ``event_id`` = ``u<id>``
qua ham baseline :func:`shared.ids.event_id`).

Quy tac parse (``03`` §2):

- Chi xu ly update co ``message``. Loai update khac (``edited_message``,
  ``callback_query``, ``my_chat_member``, ...) thi BO QUA an toan: tra
  ``None`` kem ly do, khong crash. Poller van tien offset qua update
  da bo qua (da tieu thu).
- ``text``/``caption`` trich rieng; anh + caption thi ``text = null``,
  giu ``caption``; CAM gop, CAM bo caption.
- ``chat_id``/``sender_id`` luon la chuoi (tranh mat chinh xac so
  nguyen 64-bit).
- ``platform_date`` = ``message.date`` (epoch GIAY, giu nguyen don vi
  goc — khac Zalo dung mili-giay).
- Lenh: parse theo entity ``bot_command`` (ca text lan caption). Entity
  dung offset/length theo don vi UTF-16 — code cat chuoi theo UTF-16
  (khong cat theo code point Python) de dung ca khi co emoji truoc
  lenh. ``@suffix`` co va khac username bot nay thi BO QUA toan bo
  update (lenh danh cho bot khac). Chua biet username (chua co
  capability) thi KHONG bo qua (mo an toan, co canh bao o poller) —
  quyet cua vong code nay, ghi ro tai day.
- Tin dich vu (them/xoa thanh vien, doi ten nhom, ghim tin, ...) thi
  ``kind = "service"``; tin mang ``migrate_to_chat_id`` /
  ``migrate_from_chat_id`` thi ``kind = "migrate"``; con lai la
  ``"message"``.
- File dinh kem (``photo``/``document``/``voice``/``audio``/``video``)
  duoc ghi vao ``attachments[]`` voi du truong; anh lay size LON NHAT
  (phan tu cuoi mang ``photo``). Tai file ve dia do poller dam nhan
  qua ``download_fn``; parser chi goi hook va ghi ket qua
  (``local_path``/``pc_local_path``/``file_error``). CAM dua bytes hay
  base64 vao ban ghi.
- ``from_bot`` tu ``sender.is_bot``; dispatcher loai tin tu bot khac
  (ghi ledger) — parser van dua vao queue.
"""

from __future__ import annotations

from shared import ids
from shared.record import build_record, now_iso

# Cac khoa danh dau tin dich vu trong message (tai lieu Telegram).
SERVICE_KEYS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "pinned_message",
)

# Thu tu quet dinh kem trong 1 message (1 message thuong chi co 1
# loai media; quet het de phong truong hop co nhieu).
ATTACHMENT_ORDER = ("photo", "document", "voice", "audio", "video")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _entity_slice(text, offset, length):
    """Cat doan entity theo offset/length don vi UTF-16.

    Tra ve chuoi con, hoac None neu offset/length khong hop le.
    """
    if not isinstance(text, str):
        return None
    if not _is_int(offset) or not _is_int(length):
        return None
    if offset < 0 or length <= 0:
        return None
    try:
        encoded = text.encode("utf-16-le")
    except UnicodeError:
        return None
    start = offset * 2
    end = start + length * 2
    if end > len(encoded):
        return None
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeError:
        return None


def _command_suffix(command_text):
    """Tach (ten_lenh, suffix) tu chuoi lenh kieu "/start@BotTen".

    Tra ve (ten, suffix) voi suffix la None neu khong co @. Tra ve
    (None, None) neu khong phai lenh (khong bat dau bang "/").
    """
    if not isinstance(command_text, str) or not command_text.startswith("/"):
        return None, None
    token = command_text.split()[0] if command_text.split() else ""
    if "@" in token:
        name, suffix = token[1:].split("@", 1)
        return name, suffix
    return token[1:], None


def _iter_command_entities(message):
    """Yield (command_text, suffix) cho moi entity bot_command.

    Quet ca entities cua text va caption_entities cua caption.
    """
    for text_key, entities_key in (("text", "entities"),
                                   ("caption", "caption_entities")):
        text = message.get(text_key)
        entities = message.get(entities_key)
        if not isinstance(text, str) or not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if entity.get("type") != "bot_command":
                continue
            sliced = _entity_slice(
                text, entity.get("offset"), entity.get("length"))
            if sliced is None:
                continue
            name, suffix = _command_suffix(sliced)
            if name:
                yield name, suffix


def _display_name(first_name, last_name, username):
    """Ten hien thi: "Ten Ho", roi den username, roi None."""
    parts = [p for p in (first_name, last_name)
             if isinstance(p, str) and p]
    if parts:
        return " ".join(parts)
    if isinstance(username, str) and username:
        return username
    return None


def _coerce_file_size(value):
    """Ep file_size ve int hoac None (validate_record chi chap nhan 2 dang)."""
    if _is_int(value):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _attachment_entry(kind, media):
    """Dung 1 muc attachments tu dict media; None neu thieu file_id."""
    if not isinstance(media, dict):
        return None
    file_id = media.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_name = media.get("file_name")
    mime_type = media.get("mime_type")
    return {
        "kind": kind,
        "file_id": file_id,
        "file_name": file_name if isinstance(file_name, str) else None,
        "mime_type": mime_type if isinstance(mime_type, str) else None,
        "file_size": _coerce_file_size(media.get("file_size")),
        "local_path": None,
        "pc_local_path": None,
        "muse_local_path": None,
        "file_error": None,
    }


def _collect_attachments(message):
    """Thu thap attachments[] tu message (anh lay size lon nhat)."""
    attachments = []
    for kind in ATTACHMENT_ORDER:
        media = message.get(kind)
        if kind == "photo":
            if isinstance(media, list) and media:
                entry = _attachment_entry(kind, media[-1])
                if entry is not None:
                    attachments.append(entry)
            continue
        if isinstance(media, dict):
            entry = _attachment_entry(kind, media)
            if entry is not None:
                attachments.append(entry)
    return attachments


def _message_kind(message):
    """Xac dinh kind: migrate > service > message."""
    if "migrate_to_chat_id" in message or "migrate_from_chat_id" in message:
        return "migrate"
    for key in SERVICE_KEYS:
        if key in message:
            return "service"
    return "message"


def parse_update(update, bot_id, bot_username=None, received_at=None,
                 download_fn=None, log_fn=None):
    """Parse 1 update Telegram -> (record, skip_reason).

    ``record`` la ban ghi chuan v1, hoac None neu update bi bo qua;
    ``skip_reason`` la chuoi ly do khi bo qua, hoac None khi co record.
    ``download_fn`` (neu co) duoc goi theo dang
    ``download_fn(attachment, event_id)`` va tra dict
    ``{"local_path": ..., "file_error": ...}``; loi trong hook thi ghi
    ``file_error = "download_error"``, tin van vao queue.
    """
    log = log_fn or (lambda *args, **kwargs: None)
    if not isinstance(update, dict):
        return None, "not_object"
    update_id = update.get("update_id")
    if not _is_int(update_id):
        return None, "no_update_id"
    message = update.get("message")
    if not isinstance(message, dict):
        other_keys = [k for k in update.keys() if k != "update_id"]
        return None, "unsupported_update:%s" % (other_keys[0] if other_keys else "empty",)
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None, "no_chat_id"
    chat_id = chat.get("id")
    if chat_id is None or isinstance(chat_id, bool):
        return None, "no_chat_id"
    sender = message.get("from")
    if not isinstance(sender, dict):
        sender = {}
    sender_id = sender.get("id")
    if sender_id is None or isinstance(sender_id, bool):
        sender_id = ""

    # Luat @suffix: lenh danh cho bot khac thi bo qua toan bo update.
    # Chua biet username thi mo an toan (khong bo qua).
    if bot_username:
        wanted = str(bot_username).lower()
        for _name, suffix in _iter_command_entities(message):
            if suffix and str(suffix).lower() != wanted:
                return None, "command_for_other_bot"

    kind = _message_kind(message)
    chat_title = chat.get("title")
    if not isinstance(chat_title, str) or not chat_title:
        chat_title = _display_name(
            chat.get("first_name"), chat.get("last_name"),
            chat.get("username"))
    sender_name = _display_name(
        sender.get("first_name"), sender.get("last_name"),
        sender.get("username"))
    reply = message.get("reply_to_message")
    reply_to = None
    if isinstance(reply, dict):
        reply_to = reply.get("message_id")
        if isinstance(reply_to, bool):
            reply_to = None
    message_id = message.get("message_id")
    if isinstance(message_id, bool):
        message_id = None
    platform_date = message.get("date")
    if not _is_int(platform_date) and not isinstance(platform_date, float):
        platform_date = None
    entities = message.get("entities")
    if not isinstance(entities, list):
        entities = None
    caption_entities = message.get("caption_entities")
    if not isinstance(caption_entities, list):
        caption_entities = None
    text = message.get("text")
    if not isinstance(text, str):
        text = None
    caption = message.get("caption")
    if not isinstance(caption, str):
        caption = None

    attachments = _collect_attachments(message)
    event_id = ids.event_id(update)
    if download_fn is not None:
        for attachment in attachments:
            try:
                outcome = download_fn(attachment, event_id)
            except Exception as exc:  # noqa: BLE001 - tai loi khong chan tin
                log("parse bot=%s: download hook loi class=%s"
                    % (bot_id, type(exc).__name__))
                attachment["file_error"] = "download_error"
                continue
            if isinstance(outcome, dict):
                local_path = outcome.get("local_path")
                if isinstance(local_path, str) and local_path:
                    attachment["local_path"] = local_path
                    attachment["pc_local_path"] = local_path
                file_error = outcome.get("file_error")
                if isinstance(file_error, str) and file_error:
                    attachment["file_error"] = file_error

    record = build_record(
        bot_id=bot_id,
        platform="telegram",
        platform_event_id=str(update_id),
        event_id=event_id,
        kind=kind,
        chat_id=str(chat_id),
        chat_title=chat_title,
        sender_id=str(sender_id),
        sender_name=sender_name,
        message_id=message_id,
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        reply_to_message_id=reply_to,
        file_hint=None,
        attachments=attachments,
        from_bot=bool(sender.get("is_bot")),
        platform_date=platform_date,
        received_at=received_at or now_iso(),
        raw=update,
    )
    return record, None
