"""Luồng chuyển bot — trạng thái, nhật ký, kiểm chứng bước 7 (doc 09 §4/§6).

Module này giữ mọi logic thuần của wizard chuyển bot để tầng app
(``ui_app``) chỉ còn routing + kiểm tra CSRF/khóa. Không đọc thư mục
secrets/ dưới mọi hình thức (§4.6). Không gọi endpoint gateway (§4.3
bước 1): chỉ đọc file fetch-state + trạng thái unit do tầng app đo.

Quy ước ghi (B4/N19): sự kiện nhật ký ghi TRƯỚC khi ghi trạng thái —
lỗi ghi nhật ký raise và chặn thao tác, không để trạng thái tiến mà
không có nhật ký. Thứ tự hoàn tất bước 9: ghi ``completed`` (fsync) →
xóa khỏi ``transfers.json`` → audit (best-effort, ở tầng app).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from pc.ui import ui_store

# Hằng số doc 09 §7 (trỏ §7 ở mọi chỗ dùng — N29). Một số hằng dùng
# chung đã nằm ở ui_store; ở đây chỉ thêm hằng riêng của wizard.
TRANSFER_VERSION = 1
MARKER_LENGTH = 6  # mã test 6 ký tự (doc 09 §7).
# Bảng chữ Crockford 32 ký tự: chữ số 2-9 + chữ cái trừ L/O (bỏ
# 0/O/1/l để không nhầm khi đọc/gõ); 6 ký tự = 30 bit entropy (R2).
MARKER_ALPHABET = "23456789ABCDEFGHIJKMNPQRSTUVWXYZ"
MARKER_UNIQUENESS_WINDOW_SECONDS = 24 * 3600  # kiểm tra 24h qua (R2).
REASON_PRESETS = (
    "Kiểm chứng bước 7 thất bại",
    "Tài khoản mới chưa sẵn sàng",
    "Đổi ý — chưa chuyển nữa",
    "Lý do khác (ghi ở ô ghi chú)",
)

# Thứ tự khóa cố định, không đảo (B5): khóa bot (tầng app) → khóa
# transfers → khóa registry (bên trong modify_registry_checked).


class TransferError(ValueError):
    """Lỗi nghiệp vụ của luồng chuyển: hiện thẳng cho chủ nhân."""


class RevisionMismatch(TransferError):
    """Ghi với revision lệch — báo tải lại trang (S21)."""


def now_iso(now=None):
    """Epoch -> ISO-8601 UTC."""
    stamp = now if now is not None else ui_store.now_epoch()
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def new_transfer_id(bot_id, now):
    """transfer_id duy nhất: tr-<bot>-<mốc>-<hậu tố ngẫu nhiên> (S21)."""
    suffix = secrets.token_hex(2)
    return "tr-%s-%d-%s" % (bot_id, int(now), suffix)


def new_transfer_record(bot_id, from_account_id, from_label_at_start,
                        to_account_id, now):
    """Bản ghi luồng mới, đủ schema §6.3 (CẤM bớt trường).

    Bước 0 done ngay khi tạo (không trạng thái nháp — N18);
    current_step = 1. revision bắt đầu ở 1, tăng mỗi lần ghi.
    """
    steps = {}
    for step_no in range(10):
        if step_no == 0:
            status, at = "done", now_iso(now)
        elif step_no == 1:
            status, at = "current", None
        else:
            status, at = "locked", None
        steps[str(step_no)] = {"status": status, "at": at, "note": ""}
    return {
        "transfer_id": new_transfer_id(bot_id, now),
        "bot_id": bot_id,
        "from_account_id": from_account_id,
        "from_label_at_start": from_label_at_start,
        "to_account_id": to_account_id,
        "status": "active",
        "revision": 1,
        "current_step": 1,
        "created_at": now_iso(now),
        "last_progress_at": now_iso(now),
        "expires_at": now_iso(now + ui_store.TRANSFER_EXPIRY_SECONDS),
        "history_choice": None,
        "cursor_step2": None,
        "cursor_step5": None,
        "cursor_start_choice": None,
        "pending_reply_choice": None,
        "pending_count": None,
        "history_keep_decision": None,
        "step7_started_at": None,
        "step7_fetch_count_at_start": None,
        "step7_marker": None,
        "step7_attempt": 1,
        "step7_marker_found_at": None,
        # Trường cộng thêm (được phép, §6.3): bằng chứng bước 7 để
        # bước 9 tóm tắt cả 2 tầng (S2).
        "step7_reply_tier": None,
        "step7_label_tier": None,
        "step7_dm_path": None,
        "step7_labels_seen": [],
        "step7_marker_event_key": None,
        "step7_adjudication": None,
        "steps": steps,
        "rollback_reason": None,
        "cancel_reason": None,
    }


def refresh_progress(record, now):
    """Làm mới last_progress_at/expires_at + tăng revision.

    Chỉ hoàn tất bước / quay lại / hủy làm mới (C6); đọc/kiểm tra
    lại không gọi hàm này.
    """
    record["last_progress_at"] = now_iso(now)
    record["expires_at"] = now_iso(now + ui_store.TRANSFER_EXPIRY_SECONDS)
    record["revision"] = int(record.get("revision") or 0) + 1


# ---------------------------------------------------------------------------
# Lưu trữ: transfers.json (khóa riêng) + transfer_log.jsonl (append + fsync)
# ---------------------------------------------------------------------------

def _transfers_path(root):
    return Path(root) / "ui-config" / "transfers.json"


def _transfers_lock_path(root):
    return Path(root) / "ui-config" / "transfers.lock"


def _transfer_log_path(root):
    return Path(root) / "ui-config" / "transfer_log.jsonl"


@contextlib.contextmanager
def transfers_lock(root):
    """Khóa flock RIÊNG cho transfers.json — CẤM dùng chung khóa
    registry (§6.1)."""
    lock_path = _transfers_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_transfers_data(root, data):
    """Ghi transfers.json nguyên tử (tmp + fsync + rename), 600.
    Caller phải giữ transfers_lock."""
    path = _transfers_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def modify_transfers(root, mutate_fn):
    """Đọc + sửa + ghi transfers.json trong cùng 1 khóa riêng.

    ``mutate_fn`` nhận dict {"version": 1, "transfers": {...}}, sửa
    tại chỗ, trả giá trị tùy ý. Raise trong mutate_fn -> không ghi.
    """
    with transfers_lock(root):
        data = ui_store.read_transfers(root)
        result = mutate_fn(data)
        write_transfers_data(root, data)
    return result, data


def make_event(kind, record, step, detail, now):
    """1 sự kiện nhật ký chuyển theo schema §6.2.

    ``detail`` chỉ mang trường đã liệt kê + khóa "note" cho các câu
    bắt buộc phải ghi nguyên văn theo thiết kế (vd "Y vẫn giữ secret
    — nợ dọn"). CẤM nội dung tin / mã test / secret trong detail.
    """
    return {
        "ts": int(now),
        "kind": kind,
        "transfer_id": record["transfer_id"],
        "bot_id": record["bot_id"],
        "from_account_id": record.get("from_account_id"),
        "to_account_id": record.get("to_account_id"),
        "step": step,
        "detail": dict(detail or {}),
    }


def append_transfer_event(root, event):
    """Ghi 1 sự kiện vào transfer_log.jsonl (append + fsync, 600).

    Lỗi ghi PHẢI RAISE và chặn thao tác (B4) — hàm này không nuốt lỗi.
    """
    path = _transfer_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def transfer_log_has_kind(root, transfer_id, kind):
    """True khi nhật ký đã có sự kiện ``kind`` cho luồng này (dùng
    cho vào lại lũy đẳng + chống ghi trùng completed — N19/B5)."""
    for event in ui_store.read_transfer_log(root):
        if isinstance(event, dict) \
                and event.get("transfer_id") == transfer_id \
                and event.get("kind") == kind:
            return True
    return False


def expire_stale_transfers(root, now=None):
    """Ghi nhận hết hạn cho luồng quá 72h không tiến triển: sự kiện
    ``expired`` (bắt buộc cleanup_owed) + xóa khỏi transfers.json.

    Đánh giá lười: hết hạn có hiệu lực ngay khi đọc (C6); hàm này chỉ
    ghi nhận. Lỗi ghi nhật ký raise để caller quyết (khởi động UI thì
    nuốt, POST đổi trạng thái thì báo lỗi). Trả về số luồng đã hết hạn.
    """
    if now is None:
        now = ui_store.now_epoch()
    expired = []
    with transfers_lock(root):
        data = ui_store.read_transfers(root)
        transfers = data.get("transfers") or {}
        for transfer_id, record in list(transfers.items()):
            if record.get("status") != "active":
                continue
            if ui_store.is_transfer_active(record, now):
                continue
            step6_done = ui_store.transfer_step_done(record, 6)
            step8_done = ui_store.transfer_step_done(record, 8)
            event = make_event("expired", record,
                               int(record.get("current_step") or 0), {
                                   "cleanup_owed": bool(
                                       step6_done and not step8_done),
                               }, now)
            append_transfer_event(root, event)
            del transfers[transfer_id]
            expired.append(transfer_id)
        if expired:
            write_transfers_data(root, data)
    return len(expired)


# ---------------------------------------------------------------------------
# Mã test bước 7 (R1/R2) + tìm tin + kiểm tra DM + gom nhóm trả lời
# ---------------------------------------------------------------------------

def generate_marker(existing_texts):
    """Mã test 6 ký tự Crockford, sinh bằng module secrets.

    ``existing_texts``: nội dung tin trong 24h qua — mã trùng với bất
    kỳ tin nào thì sinh lại (R2). Tối đa 100 lần thử.
    """
    for _attempt in range(100):
        marker = "".join(
            secrets.choice(MARKER_ALPHABET) for _ in range(MARKER_LENGTH))
        if all(marker not in (text or "") for text in existing_texts):
            return marker
    raise TransferError("Không sinh được mã test không trùng — thử lại sau.")


def generate_step7_marker(root, bot_id, now):
    """Sinh mã test, kiểm tra trùng với tin trong 24h qua (R2).

    Chỉ đọc trường ``text`` của bản ghi (không caption/file_hint)."""
    cutoff = now - MARKER_UNIQUENESS_WINDOW_SECONDS
    texts = []
    for row in ui_store.read_jsonl_rows(
            Path(root) / "data" / bot_id / "queue.jsonl"):
        received = ui_store.parse_iso_to_epoch(row.get("received_at"))
        if received is None or received < cutoff:
            continue
        record = row.get("record")
        if isinstance(record, dict) and isinstance(record.get("text"), str):
            texts.append(record["text"])
    return generate_marker(texts)


def find_marker_message(root, bot_id, marker, since_epoch):
    """Tin khớp ĐẦU TIÊN sau ``since_epoch`` chứa mã test.

    CHỈ tìm trong trường ``text`` phía server (R2). Trả về dict
    {"row", "record", "received_at", "event_key"} hoặc None. Thứ tự
    file queue là thứ tự nhận tin (seq tăng dần).
    """
    if not marker:
        return None
    for row in ui_store.read_jsonl_rows(
            Path(root) / "data" / bot_id / "queue.jsonl"):
        received = ui_store.parse_iso_to_epoch(row.get("received_at"))
        if received is None or received <= since_epoch:
            continue
        record = row.get("record")
        if not isinstance(record, dict):
            continue
        text = record.get("text")
        if not isinstance(text, str) or marker not in text:
            continue
        event_id = row.get("event_id") or record.get("event_id") or ""
        return {
            "row": row,
            "record": record,
            "received_at": received,
            "event_key": "%s__%s" % (bot_id, event_id) if event_id else None,
        }
    return None


def dm_check(record, platform):
    """Kiểm tra loại chat từ ``raw`` (đọc-không-hiện, C5/UC-46).

    Chỉ đọc 2 trường loại chat/id, CẤM hiện nội dung raw. Trả về:
    "dm" (chắc chắn là DM), "not_dm" (chắc chắn không phải DM),
    "unknown" (trường vắng/không rõ -> đường dự phòng tick xác nhận).
    """
    if not isinstance(record, dict):
        return "unknown"
    raw = record.get("raw")
    if not isinstance(raw, dict):
        return "unknown"
    message = raw.get("message")
    if not isinstance(message, dict):
        return "unknown"
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return "unknown"
    if platform == "zalo":
        chat_type = chat.get("chat_type")
        if chat_type is None:
            return "unknown"
        if chat_type != "PRIVATE":
            return "not_dm"
        sender = message.get("from")
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        if sender_id is None or chat.get("id") is None:
            return "unknown"
        return "dm" if str(chat.get("id")) == str(sender_id) else "not_dm"
    if platform == "telegram":
        chat_type = chat.get("type")
        if chat_type is None:
            return "unknown"
        return "dm" if chat_type == "private" else "not_dm"
    return "unknown"


def reply_groups(root, bot_id, event_key):
    """Nhóm trả lời ``sent`` ghép cặp với 1 tin (C1).

    Đếm theo nhóm qua ``group_id``: các phần của cùng 1 lần trả lời
    có cùng group_id; mục thiếu group_id tính là 1 nhóm riêng. Chỉ
    tính status "sent" (failed/dead không phải trả lời đã tới).
    Trả về (số nhóm, tổng số dòng sent).
    """
    groups = {}
    total = 0
    for index, row in enumerate(ui_store.read_jsonl_rows(
            Path(root) / "data" / bot_id / "replies.jsonl")):
        if row.get("in_reply_to_event_key") != event_key:
            continue
        if row.get("status") != "sent":
            continue
        total += 1
        group_id = row.get("group_id")
        if isinstance(group_id, str) and group_id:
            key = ("group", group_id)
        else:
            key = ("single", index)
        groups.setdefault(key, []).append(row)
    return len(groups), total


# ---------------------------------------------------------------------------
# Đánh giá bước 7 (thuần đọc — GET làm tươi không đổi trạng thái, C7)
# ---------------------------------------------------------------------------

def evaluate_step7(root, bot_id, transfer, now, fetch_state=None,
                   registry_entry=None, allowlist=None, dm_tick=False,
                   attested_tick=False, adjudication=None):
    """Chạy lại đủ 4 kiểm tra bước 7 phía server tại thời điểm gọi.

    Trả về dict với từng kiểm tra (pass/message) + 2 tầng
    (reply_tier/label_tier) + cờ timed_out/needs_adjudication.
    Hàm này KHÔNG ghi gì — tầng app quyết ghi nhật ký/trạng thái.
    """
    if fetch_state is None:
        fetch_state = ui_store.read_fetch_state(root)
    if registry_entry is None:
        registry_entry = {}
    entry = ui_store.fetch_state_entry(fetch_state, bot_id)
    fetch_labels = entry.get("fetch_labels")
    if not isinstance(fetch_labels, dict):
        fetch_labels = {}
    started_at = ui_store.parse_iso_to_epoch(
        transfer.get("step7_started_at"))
    if started_at is None:
        started_at = now
    dest = transfer.get("to_account_id")
    old_ids = {value for value in (
        transfer.get("from_account_id"),
        transfer.get("from_label_at_start")) if value}

    # Kiểm tra (1): bằng chứng fetch + ma trận nhãn (B3, quyết §1.5).
    baseline = transfer.get("step7_fetch_count_at_start")
    count_now = entry.get("fetch_count")
    last_fetch = ui_store.parse_iso_to_epoch(entry.get("last_fetch_at"))
    fetch_evidence = False
    if isinstance(count_now, int) and isinstance(baseline, int) \
            and count_now > baseline:
        fetch_evidence = True
    if last_fetch is not None and last_fetch > started_at:
        fetch_evidence = True
    seen = ui_store.labels_seen_since(fetch_labels, started_at)
    label_tier = "not-checked"
    case = None
    check1_pass = False
    if not fetch_evidence:
        check1_message = (
            "Chưa thấy lần lấy tin nào sau khi bắt đầu bước 7 — kiểm tra "
            "fetcher ở tài khoản mới đã bật chưa.")
    elif not seen:
        # Trường hợp (b): CHẶN theo quyết chủ nhân 16/11/2026 — không
        # có tick chấp nhận riêng để hoàn tất khi thiếu nhãn.
        case = "b"
        label_tier = "degraded-no-label"
        check1_message = (
            "Chưa thấy nhãn của tài khoản mới — chưa kiểm chứng được tài "
            "khoản mới đang lấy. Kiểm tra account_id trong fetcher-config "
            "của tài khoản mới, gateway đã nâng cấp ghi nhãn chưa, và "
            "UC-47 đã đạt chưa. Nếu chưa khắc phục được, chuyển thủ công "
            "theo quy trình §6 (doc 08).")
    elif dest not in seen:
        case = "c"
        check1_message = (
            "Nhãn thấy được không khớp tài khoản đích — kiểm tra "
            "account_id trong fetcher-config của tài khoản mới.")
    elif seen == {dest}:
        case = "a"
        label_tier = "verified"
        check1_pass = True
        check1_message = "Thấy nhãn đích %s đang lấy tin." % dest
    else:
        case = "d"
        check1_message = (
            "Thấy cả nhãn đích và nhãn khác (%s) — tài khoản cũ vẫn đang "
            "lấy. Quay lại bước 1/8 để dừng lấy tin ở tài khoản cũ."
            % ", ".join(sorted(seen)))
    check1 = {"pass": check1_pass, "message": check1_message,
              "case": case, "labels_seen": sorted(seen),
              "fetch_evidence": fetch_evidence}

    # Kiểm tra (2): không còn nhãn cũ trong 5 phút (dùng last_seen_at).
    check2_pass = True
    check2_message = "Không còn nhãn cũ trong 5 phút gần nhất."
    if old_ids:
        for label in sorted(old_ids):
            info = fetch_labels.get(label)
            if not isinstance(info, dict):
                continue
            last_seen = ui_store.parse_iso_to_epoch(info.get("last_seen_at"))
            if last_seen is not None \
                    and now - last_seen <= ui_store.DUAL_FETCH_WINDOW_SECONDS:
                check2_pass = False
                check2_message = (
                    "Tài khoản cũ vẫn đang lấy — quay lại bước 1/8.")
                break
    else:
        check2_message = "Bot chưa gắn tài khoản cũ — bỏ qua kiểm tra này."
    check2 = {"pass": check2_pass, "message": check2_message}

    # Kiểm tra (3): tin test DM + ghép cặp trả lời.
    marker = transfer.get("step7_marker")
    found = find_marker_message(root, bot_id, marker, started_at)
    check3_pass = False
    reply_tier = None
    dm_path = None
    marker_event_key = None
    needs_adjudication = False
    attested_open = False
    groups = 0
    if found is None:
        check3_message = (
            "Chưa thấy tin test trong hàng đợi — gửi 1 tin nhắn chữ "
            "(DM) cho bot chứa đúng mã test.")
    else:
        marker_event_key = found["event_key"]
        record = found["record"]
        received_at = found["received_at"]
        # Người gửi thuộc owner_sender_ids + chat thuộc allowlist mới
        # nhất; không đọc được allowlist thì bỏ qua, ghi rõ (S8).
        allowlist_note = ""
        allowlist_ok = True
        if allowlist is None:
            allowlist_note = " (không đọc được allowlist — bỏ qua kiểm tra người gửi/chat)"
        else:
            owners = allowlist.get("owner_sender_ids") or []
            chats = allowlist.get("allowed_chats") or []
            if str(record.get("sender_id") or "") not in \
                    [str(value) for value in owners]:
                allowlist_ok = False
            if str(record.get("chat_id") or "") not in \
                    [str(value) for value in chats]:
                allowlist_ok = False
        platform = (registry_entry.get("platform")
                    if isinstance(registry_entry, dict) else None) or ""
        if not platform:
            platform = ui_store.read_bot_config(root, bot_id).get(
                "platform") or ""
        dm_result = dm_check(record, platform)
        if not allowlist_ok:
            check3_message = (
                "Tin test không thuộc người chủ/chat được phép — kiểm tra "
                "người gửi và chat, rồi gửi lại tin test từ DM của chủ.")
        elif dm_result == "not_dm":
            check3_message = (
                "Tin test không phải tin nhắn riêng (DM) — gửi lại từ "
                "chat riêng với bot.")
        elif dm_result == "unknown" and not dm_tick:
            check3_message = (
                "Không xác định được tin test có phải DM không từ dữ liệu "
                "trên máy — tick xác nhận \"tôi đã gửi từ DM\" nếu đúng.")
        else:
            dm_path = "DM theo raw" if dm_result == "dm" \
                else "tin test từ chủ nhân"
            groups, _total = reply_groups(root, bot_id, marker_event_key)
            in_window = (now - received_at
                         <= ui_store.TRANSFER_REPLY_WINDOW_SECONDS)
            # Tầng xác nhận chỉ mở 5 phút sau lúc tìm thấy tin test
            # (tính từ lúc tin xuất hiện trong queue — mốc quan sát
            # được phía PC) và chỉ khi đã có nhãn (label_tier verified).
            attested_open = (
                label_tier == "verified"
                and now - received_at
                >= ui_store.TRANSFER_ATTEST_OPEN_SECONDS)
            if groups >= 2 and in_window:
                if adjudication == "single_multi_part":
                    reply_tier = "sync"
                    check3_pass = True
                    check3_message = (
                        "Thấy %d nhóm trả lời — chủ nhân đã phân xử là 1 "
                        "trả lời nhiều phần, không trùng.%s"
                        % (groups, allowlist_note))
                elif adjudication == "duplicate_rollback":
                    check3_message = (
                        "Chủ nhân phân xử là trả lời trùng — dùng Quay lại "
                        "để về tài khoản cũ, không hoàn tất bước 7.")
                else:
                    needs_adjudication = True
                    check3_message = (
                        "Thấy %d nhóm trả lời cho 1 tin test — xem số trả "
                        "lời và chọn phân xử: 1 trả lời nhiều phần hay có "
                        "trùng.%s" % (groups, allowlist_note))
            elif groups >= 1 and in_window:
                reply_tier = "sync"
                check3_pass = True
                check3_message = (
                    "Tin test đã đi đủ tuyến, trả lời đã đồng bộ về.%s"
                    % allowlist_note)
            elif attested_tick and attested_open:
                reply_tier = "attested"
                check3_pass = True
                check3_message = (
                    "Chủ nhân xác nhận đã thấy bot trả lời trong cuộc trò "
                    "chuyện DM (tầng xác nhận).%s" % allowlist_note)
            elif attested_tick and not attested_open:
                check3_message = (
                    "Tầng xác nhận chưa mở: chỉ mở 5 phút sau khi tìm thấy "
                    "tin test và khi đã có nhãn đích được kiểm chứng.")
            else:
                check3_message = (
                    "Chưa thấy trả lời đồng bộ cho tin test. Đợi thêm, "
                    "hoặc sau 5 phút dùng tầng xác nhận (chỉ khi đã có "
                    "nhãn).%s" % allowlist_note)
    check3 = {"pass": check3_pass, "message": check3_message,
              "marker_found": found is not None,
              "marker_event_key": marker_event_key,
              "dm_path": dm_path, "reply_tier": reply_tier,
              "reply_groups": groups,
              "needs_adjudication": needs_adjudication,
              "attested_open": attested_open}

    # Kiểm tra (4): không tạm dừng + đang bật.
    paused = bool(registry_entry.get("paused")) \
        if isinstance(registry_entry, dict) else False
    enabled = bool(registry_entry.get("enabled")) \
        if isinstance(registry_entry, dict) else False
    if paused:
        check4 = {"pass": False,
                  "message": "Bot đang tạm dừng — gỡ tạm dừng trước khi "
                             "hoàn tất bước 7."}
    elif not enabled:
        check4 = {"pass": False,
                  "message": "Bot đang tắt — bật bot trước khi hoàn tất "
                             "bước 7."}
    else:
        check4 = {"pass": True,
                  "message": "Bot đang bật và không tạm dừng."}

    timed_out = (now - started_at
                 > ui_store.TRANSFER_STEP7_TIMEOUT_SECONDS)
    all_pass = check1["pass"] and check2["pass"] and check3["pass"] \
        and check4["pass"]
    return {
        "check1": check1, "check2": check2, "check3": check3,
        "check4": check4, "all_pass": all_pass,
        "label_tier": label_tier,
        "reply_tier": reply_tier,
        "timed_out": timed_out,
        "labels_seen": sorted(seen),
    }


# ---------------------------------------------------------------------------
# Kiểm tra cổng bước 1 (lặng-fetch) + lý do quay lại/hủy
# ---------------------------------------------------------------------------

def fetch_quiet_info(fetch_state, bot_id, now):
    """(quiet, last_fetch_epoch): lặng khi chưa từng fetch hoặc lần
    fetch cuối cách đây ≥90 giây (doc 09 §7).

    Chi dung khi khong co nhan cu de xet; cong moc 1 dung
    `fetch_quiet_info_by_label` (2.11, 21/11)."""
    entry = ui_store.fetch_state_entry(fetch_state, bot_id)
    last_fetch = ui_store.parse_iso_to_epoch(entry.get("last_fetch_at"))
    if last_fetch is None:
        return True, None
    return (now - last_fetch >= ui_store.TRANSFER_QUIET_SECONDS), last_fetch


def fetch_quiet_info_by_label(fetch_state, bot_id, old_labels, now):
    """(quiet, detail): lặng khi KHÔNG CÒN nhãn cũ nào lấy trong 5 phút
    (DUAL_FETCH_WINDOW_SECONDS) và không còn lượt không-nhãn trong 5 phút.
    old_labels: tập nhãn cũ = {from_account_id, from_label_at_start} (bỏ rỗng).
    detail: {"last_old_seen": epoch|None, "blocking_labels": [...],
             "unlabelled_recent": bool, "no_data": bool} — để UI viết
    thông báo chẩn đoán được.

    Mau check2 moc 7 (transfers.py:478–490): doc `last_seen_at` tung
    nhan cu + `unlabelled.last_seen_at`, chan khi bat ky moc nao
    ≤300 s. KHONG dung `active_fetch_labels` cho cong nay — ham do
    doi ≥2 moc trong cua so moi tinh la "dang hoat dong", nhan cu
    chi lay 1 lan trong 5 phut qua se khong chan -> cong mo oan.
    `no_data` (hoan toan khong co truong nhan nao — gateway cu) thi
    lang de UI cho qua bang xac nhan + ghi chu noi bat."""
    entry = ui_store.fetch_state_entry(fetch_state, bot_id)
    fetch_labels = entry.get("fetch_labels")
    if not isinstance(fetch_labels, dict):
        fetch_labels = {}
    no_data = "fetch_labels" not in entry and "unlabelled" not in entry
    window = ui_store.DUAL_FETCH_WINDOW_SECONDS
    blocking = []
    last_old_seen = None
    for label in sorted(old_labels or ()):
        if not label:
            continue
        info = fetch_labels.get(label)
        if not isinstance(info, dict):
            continue
        last_seen = ui_store.parse_iso_to_epoch(info.get("last_seen_at"))
        if last_seen is not None and now - last_seen <= window:
            blocking.append(label)
            if last_old_seen is None or last_seen > last_old_seen:
                last_old_seen = last_seen
    unlabelled_recent = False
    raw_unlabelled = entry.get("unlabelled")
    if isinstance(raw_unlabelled, dict):
        last_un = ui_store.parse_iso_to_epoch(
            raw_unlabelled.get("last_seen_at"))
        if last_un is not None and now - last_un <= window:
            unlabelled_recent = True
    if no_data:
        return True, {"last_old_seen": None, "blocking_labels": [],
                      "unlabelled_recent": False, "no_data": True}
    quiet = not blocking and not unlabelled_recent
    return quiet, {"last_old_seen": last_old_seen,
                   "blocking_labels": blocking,
                   "unlabelled_recent": unlabelled_recent,
                   "no_data": False}


def format_step1_block_message(detail, now):
    """Thong bao chan moc 1 chuan doan duoc (2.11, D-2.11-5 — giu
    nguyen van cau chu thiet ke)."""
    blocking = detail.get("blocking_labels") or []
    parts = []
    if blocking:
        labels_text = ", ".join(blocking)
        last_old = detail.get("last_old_seen")
        if last_old is not None:
            when = time.strftime("%H:%M:%S",
                                 time.localtime(last_old))
            ago = max(0, int(now - last_old))
            parts.append(
                "Tài khoản cũ (%s) vẫn đang lấy tin — lần lấy cuối lúc "
                "%s (cách đây %d giây). Dừng fetcher ở tài khoản %s "
                "(tắt service hoặc xóa fetcher-config) rồi thử lại."
                % (labels_text, when, ago, labels_text))
        else:
            parts.append(
                "Tài khoản cũ (%s) vẫn đang lấy tin. Dừng fetcher ở "
                "tài khoản %s (tắt service hoặc xóa fetcher-config) "
                "rồi thử lại." % (labels_text, labels_text))
    if detail.get("unlabelled_recent"):
        if blocking:
            parts.append(
                "Ngoài ra còn thấy lượt lấy tin không ghi nhãn trong "
                "5 phút gần nhất — kiểm tra fetcher chưa nâng cấp "
                "(chưa ghi nhãn) rồi dừng nó.")
        else:
            parts.append(
                "Thấy lượt lấy tin không ghi nhãn trong 5 phút gần "
                "nhất — kiểm tra fetcher chưa nâng cấp (chưa ghi nhãn) "
                "rồi dừng nó.")
    return " ".join(parts)


def combine_reason(choice, note):
    """Lý do quay lại/hủy: 1 lý do chọn sẵn + ghi chú tự do, ≤~200 ký
    tự (doc 09 §7). Trả về chuỗi lý do, hoặc raise TransferError."""
    choice = (choice or "").strip()
    note = (note or "").strip()
    if choice not in REASON_PRESETS:
        raise TransferError("Chọn 1 lý do trong danh sách.")
    reason = choice
    if note:
        reason = "%s — %s" % (choice, note)
    if len(reason) > ui_store.TRANSFER_REASON_MAX_CHARS:
        raise TransferError(
            "Lý do tối đa %d ký tự (hiện %d ký tự)."
            % (ui_store.TRANSFER_REASON_MAX_CHARS, len(reason)))
    return reason


def validate_step_fields(step, form, transfer, context):
    """Kiểm tra trường của bước 1–5, 8 (bước 6/7/9 có đường riêng).

    ``form``: dict key -> str (đã strip ở tầng app). ``context``:
    {"latest_seq": int, "capability_id": str|None, "fetch_state": ...,
    "now": epoch}. Trả về (detail_dict, updates_dict) để ghi nhật ký
    + cập nhật bản ghi. Raise TransferError khi không đạt.
    """
    now = context.get("now")
    if step == 1:
        if form.get("confirm") != "yes":
            raise TransferError("Tick xác nhận đã dừng lấy tin ở tài "
                                "khoản cũ.")
        return {"note": "Xác nhận dừng lấy tin ở tài khoản cũ"}, {}
    if step == 2:
        raw_cursor = form.get("cursor", "")
        try:
            cursor = int(raw_cursor)
        except (TypeError, ValueError):
            raise TransferError("Cursor phải là số nguyên ≥ 0.")
        if cursor < 0:
            raise TransferError("Cursor phải là số nguyên ≥ 0.")
        choice = form.get("pending_choice")
        if choice not in ("all_done", "to_new_account"):
            raise TransferError("Chọn 1 trong 2 trường hợp tin tồn.")
        pending_count = 0
        if choice == "to_new_account":
            raw_pending = form.get("pending_count", "")
            try:
                pending_count = int(raw_pending)
            except (TypeError, ValueError):
                raise TransferError(
                    "Nhập số tin tồn chưa trả lời (số nguyên ≥ 0).")
            if pending_count < 0:
                raise TransferError(
                    "Số tin tồn phải là số nguyên ≥ 0.")
        detail = {"cursor_step2": cursor,
                  "pending_reply_choice": choice,
                  "pending_count": pending_count}
        latest_seq = context.get("latest_seq")
        if isinstance(latest_seq, int) and cursor > latest_seq:
            detail["note"] = (
                "Cursor phía Muse mới hơn seq trên PC — đã cảnh báo, "
                "chủ nhân vẫn xác nhận.")
        return detail, {"cursor_step2": cursor,
                        "pending_reply_choice": choice,
                        "pending_count": pending_count}
    if step == 3:
        code = (form.get("bot_code") or "").strip()
        if not code:
            raise TransferError("Nhập mã số bot mà getMe của tài khoản "
                                "mới trả về.")
        capability_id = context.get("capability_id")
        if capability_id is None:
            detail = {"note": "không đối chiếu được getMe (thiếu ảnh chụp)"}
        else:
            if code.strip() != str(capability_id).strip():
                raise TransferError(
                    "Mã số không khớp bot này — kiểm tra lại token ở "
                    "tài khoản mới.")
            detail = {"note": "Đối chiếu getMe khớp ảnh chụp phía PC."}
        return detail, {}
    if step == 4:
        if form.get("confirm") != "yes":
            raise TransferError(
                "Tick xác nhận secret đã giao qua kênh an toàn.")
        return {"note": "Xác nhận secret đã giao, cấu hình kèm account_id"}, {}
    if step == 5:
        choice = form.get("history_choice")
        if choice not in ("with_history", "without_history"):
            raise TransferError("Chọn có chuyển hay không chuyển lịch sử.")
        if choice == "without_history" \
                and transfer.get("pending_reply_choice") == "to_new_account":
            raise TransferError(
                "Bước 2 đã chọn \"để tài khoản mới trả lời\" nên bước 5 "
                "không được chọn \"không chuyển lịch sử\" — tin dở phải "
                "nằm trong phần lịch sử chuyển.")
        if choice == "with_history":
            for key, label in (
                    ("copy_cursor", "cursor"),
                    ("copy_ledger", "sổ vòng đời + sổ giao hàng + journal"),
                    ("copy_history", "lịch sử chat"),
                    ("copy_memory", "trí nhớ 3 lớp"),
                    ("copy_allowlist", "bản sao allowlist cuối"),
                    ("copy_prompt", "thẻ prompt của bot")):
                if form.get(key) != "yes":
                    raise TransferError(
                        "Tick đủ checklist chép, gồm: %s." % label)
            accounting = form.get("accounting_choice")
            if accounting not in ("copied", "remeasure_d2"):
                raise TransferError(
                    "Chọn đã chép kế toán của bot, hoặc ghi rõ D2 đo lại "
                    "từ đầu.")
            raw_cursor = form.get("cursor_step5", "")
            try:
                cursor5 = int(raw_cursor)
            except (TypeError, ValueError):
                raise TransferError(
                    "Nhập cursor của tài khoản mới sau khi chép "
                    "(số nguyên ≥ 0).")
            if cursor5 < 0:
                raise TransferError("Cursor phải là số nguyên ≥ 0.")
            cursor2 = transfer.get("cursor_step2")
            note = ""
            if isinstance(cursor2, int):
                if cursor5 > cursor2:
                    message = (
                        "Cursor của tài khoản mới mới hơn cursor bước 2 — "
                        "bị chặn. Sửa file cursor ở tài khoản mới rồi gõ "
                        "lại số.")
                    if _dest_not_fetching_yet(transfer, context):
                        message += (" Tài khoản mới chưa bật fetcher mà "
                                    "cursor đã mới hơn — nghi \"Y đã lấy "
                                    "sớm\".")
                    raise TransferError(message)
                if cursor5 < cursor2:
                    note = ("Fetcher sẽ lấy lại từ chỗ cũ, chống lặp "
                            "chặn trùng.")
            detail = {"history_choice": "with_history",
                      "cursor_step5": cursor5,
                      "accounting": accounting}
            if note:
                detail["note"] = note
            return detail, {"history_choice": "with_history",
                            "cursor_step5": cursor5,
                            "cursor_start_choice": None}
        # Nhánh không chuyển lịch sử.
        if form.get("consequence") != "yes":
            raise TransferError("Tick xác nhận đã hiểu hậu quả quên "
                                "chuyện cũ.")
        if form.get("new_prompt") != "yes":
            raise TransferError(
                "Xác nhận tài khoản mới đã tạo thẻ prompt mới (bắt buộc "
                "trước bước 7).")
        start_choice = form.get("cursor_start_choice")
        if start_choice not in ("latest", "zero"):
            raise TransferError("Chọn điểm bắt đầu cursor của tài khoản "
                                "mới.")
        detail = {"history_choice": "without_history",
                  "cursor_start_choice": start_choice}
        return detail, {"history_choice": "without_history",
                        "cursor_step5": None,
                        "cursor_start_choice": start_choice}
    if step == 8:
        for key in ("cleanup_fetcher", "cleanup_syncer", "cleanup_vault"):
            if form.get(key) != "yes":
                raise TransferError(
                    "Tick đủ từng mục dọn dẹp ở tài khoản cũ (kể cả gỡ "
                    "credential khỏi Vault — cấm bỏ qua).")
        keep = form.get("history_keep_decision")
        if keep not in ("kept_at_X", "deleted_at_X", "archived_at"):
            raise TransferError("Chọn nơi giữ lịch sử cũ ở tài khoản cũ.")
        detail = {"history_keep_decision": keep}
        return detail, {"history_keep_decision": keep}
    raise TransferError("Bước không hỗ trợ ở đường này.")


def _dest_not_fetching_yet(transfer, context):
    """Nghi "Y đã lấy sớm": nhãn đích chưa từng thấy, hoặc lần thấy
    cuối trước lúc bắt đầu luồng."""
    fetch_state = context.get("fetch_state") or {}
    entry = ui_store.fetch_state_entry(fetch_state, transfer.get("bot_id"))
    labels = entry.get("fetch_labels")
    if not isinstance(labels, dict):
        return True
    info = labels.get(transfer.get("to_account_id"))
    if not isinstance(info, dict):
        return True
    last_seen = ui_store.parse_iso_to_epoch(info.get("last_seen_at"))
    created = ui_store.parse_iso_to_epoch(transfer.get("created_at"))
    if last_seen is None:
        return True
    if created is not None and last_seen < created:
        return True
    return False
