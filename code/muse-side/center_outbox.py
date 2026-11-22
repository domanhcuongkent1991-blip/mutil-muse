"""Center outbox phía Muse — mở rộng K2, SPEC-TRUNG-TAM TT8, doc 10 §6.4.

POST đổi trạng thái + gửi tin qua receiver trung tâm (cổng 8766),
chờ hub 200 mới tiến con trỏ (như reply outbox + syncer). Dedupe
theo ``client_key`` (đổi trạng thái/tạo việc) và ``msg_id`` (gửi tin)
— gửi lại cùng khóa thì hub trả ``duplicate: true``, không ghi thêm.

- File: ``center_outbox.jsonl`` (append-only, 600) tại thư mục gốc
  phía Muse (1 tài khoản/VM — như DESIGN §6).
- Con trỏ riêng: ``center_outbox_cursors/<account_id>.json``
  (số dòng vật lý, như ``sync_cursors``) — chỉ tiến sau khi hub
  trả 200 (kể cả ``duplicate: true``).
- Mỗi lần flush gửi TỐI ĐA 1 dòng (giới hạn receiver 1 request/2
  giây/tài khoản; chu kỳ hook 10s nên hàng chờ xả dần, không dồn).
- 401/403: chỉ dừng đẩy trung tâm của tài khoản đó; 3 chu kỳ liên
  tiếp -> báo. 429/5xx/lỗi mạng: không tiến con trỏ, thử lại chu kỳ
  sau. 400/404/409: lỗi vĩnh viễn theo thao tác — ghi log + tiến
  con trỏ qua dòng đó (bỏ dòng, không kẹt hàng chờ mãi).
- ``report_done()``: báo xong thật = POST ``da-bao-xong`` + 1 tin
  ``bao-cao`` gắn ``task_id``, ghi LIỀN NHAU trong cùng lượt outbox
  (trạng thái trước, tin ngay sau) — tiêu chí ghép nối "cùng lượt
  outbox" (V3-GC-01): tin ``bao-cao`` đầu tiên có ``seq`` lớn hơn
  ``seq`` sự kiện ``da-bao-xong`` mới tính là báo xong; tin
  ``bao-cao`` thứ 2 trở đi sau đó là báo tiến độ bổ sung.
- CẤM gửi tin ``giao-viec`` tay (chỉ hub tự sinh — NS-18): hàm
  enqueue_message từ chối kind này ngay phía Muse.
- Schema tin nhắn v4 (v0.6.0, T3l — D1, quyết qua đêm 20/11):
  ``to_account`` là danh sách tối đa 5 người (chuỗi cũ vẫn chấp
  nhận như danh sách 1 người); bên ghi mới LUÔN ghi danh sách.
  THỨ TỰ TRIỂN KHAI BẮT BUỘC — PC-trước/Muse-sau: store +
  receiver trên PC nâng TRƯỚC, chỉ sau đó mới nâng outbox phía
  Muse và cho gửi tin v4 (hai lần triển khai riêng, không có tính
  nguyên tử). ROLLBACK: phía Muse về bản cũ TRƯỚC (outbox cũ
  không gửi tin v4 nữa), rồi mới rollback PC; KHÔNG rollback PC
  trong khi outbox mới vẫn đang gửi tin v4. Nếu PC bắt buộc
  rollback gấp trong khi phía Muse còn mới thì outbox GIỮ dòng v4
  lại theo D1(f) (xem flush) cho tới khi PC xong.
- Tín hiệu "đang trả lời" (v0.6.0, T3h): worker chat ghi 1 dòng
  ``kind=typing`` vào outbox (``enqueue_typing`` / CLI ``--typing``)
  rồi flush — flush POST ``/v1/center/<account>/typing`` {room,
  started_at} qua receiver (chung gioi han 1 request/2s/tai khoan).
  Worker KHONG goi HTTP truc tiep — chi qua outbox nhu moi dong
  khac (cho hub 200 moi tien con tro).
- CẤM in secret, CẤM log nội dung việc/tin. Log mỗi lần flush: số
  dòng đã gửi, con trỏ trước->sau (chỉ task_id/msg_id/client_key
  dạng rút gọn trong log chi tiết — xem ``short_id``).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
import center_fetcher
import fetcher
from muse_common import (
    append_jsonl,
    atomic_write_bytes,
    atomic_write_json,
    ensure_dir,
    epoch_to_iso,
    iso_to_epoch,
    now_epoch,
    now_iso,
    read_json,
    read_jsonl,
    short_id,
)
from shared import ids

OUTBOX_FILENAME = "center_outbox.jsonl"
FLUSH_TIMEOUT_SECONDS = 15  # như fetcher/syncer
AUTH_FAILURE_ALERT_AFTER = 3  # SUY LUẬN, theo mẫu fetcher bot
# (Fix gộp 21/11, §4.2 mục 7 — N-S1) Tin cảnh báo: TTL 30 phút
# (dòng alert quá 30 phút chưa đẩy thì flush bỏ, không gửi muộn);
# cooldown dedup DM 30 phút/đợt/loại alert/tài khoản.
ALERT_TTL_SECONDS = 1800
ALERT_COOLDOWN_SECONDS = 1800
ALERT_KINDS = ("cap_stuck", "auth")
MAX_BODY_LEN = 5000  # tin nhắn ≤ 5.000 ký tự Unicode (GC-19)
MAX_NOTE_LEN = 5000  # note ≤ 5.000 ký tự Unicode (GC-19)
# (FIXL giai doan 1, 22/11 — Q-FIXL-06/Q-FIXL-13)
# Dead-letter: 1 file chung, giu 30 ngay, toi da 1000 dong.
DEAD_LETTER_FILENAME = "center_outbox_dead.jsonl"
DEAD_LETTER_MAX_LINES = 1000
DEAD_LETTER_KEEP_SECONDS = 30 * 24 * 3600
# (C-7, 22/11) Ma 403 vinh vien tren dong TIN NHAN: hub tra cac ma
# nay khi tin khong the gui duoc voi trang thai hien tai (phong cam,
# tat thao luan, nguoi nhan khong tham gia). Flush bo dong + ghi
# dead-letter thay vi giu lai thu vo han. KHONG gom
# "not_participating" (FIXL §6.1 giu lai vi tai khoan co the tham
# gia lai).
C7_DROP_MESSAGE_ERRORS = ("room_forbidden", "discussion_disabled",
                         "to_account_not_participating")
# Bo dem A2: N=3 dong cung viec bi bo trong X=60 phut thi bao 1 lan.
TRANSITION_DROP_ALERT_AFTER = 3
TRANSITION_DROP_WINDOW_SECONDS = 3600
FILE_ID_RE = re.compile(r"^f-[0-9a-f]{12}$")  # v0.2.5 (giong store hub)
# Phong (v0.3.0, T3c): "chung" hoac "dm:<account_id>" (mau
# account_id nhu store hub — chi kiem tra mau, quyen phong do hub
# kiem tra luc POST; bao-cao LUON o khung chung (P5)).
ROOM_RE = re.compile(r"^(chung|dm:[a-z0-9][a-z0-9-]{0,31})$")
ROOM_CHUNG = "chung"
# (v0.6.0, T3l — D1(j)) to_account danh sach toi da 5 nguoi
# (tran MAX_MENTIONS cua hub); chuoi cu chap nhan nhu danh sach
# 1 nguoi, van kiem tra mau account_id nhu hub.
TO_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_TO_ACCOUNT = 5

TASK_STATUSES = ("moi", "da-nhan", "dang-lam", "da-bao-xong",
                 "da-xac-nhan", "huy")
# Tin phía Muse được gửi tay: giao-viec CHỈ hub tự sinh (NS-18).
POSTABLE_MESSAGE_KINDS = ("hoi", "bao-cao")


def _default_log(message):
    print(message, flush=True)


def _default_alert(kind, account_id, detail):
    print("ALERT kind=%s account=%s detail=%s"
          % (kind, account_id, detail), flush=True)


def _new_client_key():
    return str(uuid.uuid4())


def _new_msg_id():
    return "m-" + secrets.token_hex(6)


def outbox_path(root):
    return ensure_dir(Path(root)) / OUTBOX_FILENAME


def validate_status_row(row):
    """Kiểm tra 1 dòng outbox đổi trạng thái; trả về list lỗi."""
    errors = []
    if not isinstance(row, dict):
        return ["dòng outbox phải là dict"]
    if row.get("kind") != "status":
        errors.append("kind phải là 'status'")
    if not isinstance(row.get("task_id"), str) or not row.get("task_id"):
        errors.append("task_id phải là chuỗi không rỗng")
    if row.get("to_status") not in TASK_STATUSES:
        errors.append("to_status phải là một trong %s" % (TASK_STATUSES,))
    if not isinstance(row.get("client_key"), str) or not row.get("client_key"):
        errors.append("client_key phải là chuỗi không rỗng")
    note = row.get("note")
    if note is not None and (
            not isinstance(note, str) or len(note) > MAX_NOTE_LEN):
        errors.append("note phải là chuỗi ≤ %d ký tự" % MAX_NOTE_LEN)
    return errors


def validate_message_row(row):
    """Kiểm tra 1 dòng outbox gửi tin; trả về list lỗi.

    (v0.2.5) Tin kem file: body được rỗng khi có file_id; tin vừa
    không body vừa không file -> lỗi. file_id (nếu có) phải đúng mẫu
    f- + 12 hex (mẫu của store hub).

    (NS-L3, AUDIT-V060 §2) Danh sách rỗng ``[]`` = gửi chung:
    chuẩn hóa về ``None`` ngay tại điểm vào (sửa trực tiếp trên
    ``row``) để dòng ghi ra outbox mang ``None``, không mang ``[]``.
    KHÔNG sửa trong hàm chuẩn hóa phía store — receiver dùng
    ``None`` làm tín hiệu lỗi ở đường kiểm tra ``to_account``."""
    errors = []
    if not isinstance(row, dict):
        return ["dòng outbox phải là dict"]
    if row.get("to_account") == []:
        row["to_account"] = None
    if row.get("kind") not in POSTABLE_MESSAGE_KINDS:
        errors.append("kind phải là một trong %s"
                      % (POSTABLE_MESSAGE_KINDS,))
    if not isinstance(row.get("msg_id"), str) or not row.get("msg_id"):
        errors.append("msg_id phải là chuỗi không rỗng")
    body = row.get("body")
    file_id = row.get("file_id")
    if not isinstance(body, str):
        errors.append("body phải là chuỗi")
    elif len(body) > MAX_BODY_LEN:
        errors.append("body phải ≤ %d ký tự" % MAX_BODY_LEN)
    elif not body and file_id is None:
        errors.append("body trống mà không có file_id")
    if file_id is not None and (
            not isinstance(file_id, str)
            or not FILE_ID_RE.match(file_id)):
        errors.append("file_id phải đúng mẫu f- + 12 ký tự hex")
    task_id = row.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        errors.append("task_id phải là chuỗi hoặc null")
    # (v0.6.0, T3l — D1(j)) to_account: None, chuoi cu (1 nguoi)
    # hoac danh sach v4 toi da 5 nguoi, tung phan tu khop mau
    # account_id. Qua 5 -> loi ngay luc ghi outbox (khong doi
    # toi khi day).
    to_account = row.get("to_account")
    if to_account is not None:
        if isinstance(to_account, str):
            if not TO_ACCOUNT_RE.match(to_account):
                errors.append("to_account phải khớp mẫu account_id")
        elif isinstance(to_account, list):
            if len(to_account) > MAX_TO_ACCOUNT:
                errors.append("to_account tối đa %d tài khoản"
                              % MAX_TO_ACCOUNT)
            for item in to_account:
                if not isinstance(item, str) \
                        or not TO_ACCOUNT_RE.match(item):
                    errors.append(
                        "to_account phải là danh sách account_id "
                        "hợp lệ")
                    break
        else:
            errors.append(
                "to_account phải là chuỗi, danh sách hoặc null")
    room = row.get("room")
    if room is not None and (
            not isinstance(room, str) or not ROOM_RE.match(room)):
        errors.append("room phải là 'chung' hoặc 'dm:<account_id>'")
    if row.get("kind") == "bao-cao" and room not in (None, ROOM_CHUNG):
        errors.append("bao-cao chỉ ở khung chung (P5)")
    # (Fix gộp 21/11, §4.2 mục 7) Dòng cảnh báo mang trường "alert"
    # ("cap_stuck"/"auth"); giá trị khác -> lỗi ngay lúc ghi.
    alert = row.get("alert")
    if alert is not None and alert not in ALERT_KINDS:
        errors.append("alert phải là một trong %s" % (ALERT_KINDS,))
    return errors


def enqueue_status(root, account_id, task_id, to_status, note=None,
                   client_key=None):
    """Ghi thêm 1 dòng đổi trạng thái vào outbox (append + fsync, 600).
    Raise ValueError nếu dòng không hợp lệ. Trả về dòng đã ghi."""
    ids.validate_bot_id(account_id)
    row = {
        "kind": "status",
        "account_id": account_id,
        "task_id": task_id,
        "to_status": to_status,
        "note": note,
        "client_key": client_key or _new_client_key(),
        "created_at": now_iso(),
    }
    errors = validate_status_row(row)
    if errors:
        raise ValueError("dòng outbox không hợp lệ: %s" % "; ".join(errors))
    append_jsonl(outbox_path(root), row, mode=0o600)
    return row


def enqueue_message(root, account_id, kind, body, task_id=None,
                    to_account=None, msg_id=None, file_id=None,
                    room=None, alert=None):
    """Ghi thêm 1 dòng gửi tin vào outbox. Raise ValueError nếu dòng
    không hợp lệ (kể cả kind=giao-viec — CẤM gửi tay). Trả về dòng.

    (Fix gộp 21/11, §4.2 mục 7) ``alert`` ("cap_stuck"/"auth"):
    dòng cảnh báo cho dispatcher/fetcher dùng — kind="hoi",
    room="dm:<account_id>" của tài khoản đang kẹt, không task_id,
    body không chứa "@" (store tự tách mentions từ body). Dòng đã
    có sẵn ``created_at`` ISO để flush tính tuổi (TTL 30 phút).

    (v0.2.5) ``file_id``: file da tai len hub truoc (qua
    center_files.upload_file) — tin kem file di qua outbox nhu tin
    thuong, dedupe theo ``msg_id``. Body duoc rong khi co file_id.

    (v0.3.0, T3c) ``room``: phong gui tin toi ("chung" mac dinh
    hoac "dm:<account_id>"); bao-cao LUON o khung chung (P5) nen
    khong truyen room cho bao-cao. Worker tra loi tin trong phong
    nao thi gui ``room`` cua phong do (lay tu ban ghi tin da keo
    ve). ``mentions`` KHONG gui trong body — may chu tu tach luc
    ghi (T3d).

    (v0.6.0, T3l) ``to_account``: danh sach v4 toi da 5 nguoi
    (ben ghi moi LUON ghi danh sach); chuoi cu van chap nhan nhu
    danh sach 1 nguoi. Qua 5 hoac phan tu sai mau -> ValueError
    ngay luc ghi outbox.

    (NS-L3, AUDIT-V060 §2) Danh sach rong ``[]`` = gui chung:
    chuan hoa ve ``None`` ngay tai diem vao (truoc khi lap dong
    va kiem tra) de dong ghi ra outbox mang ``None``."""
    ids.validate_bot_id(account_id)
    if to_account == []:
        to_account = None
    row = {
        "kind": kind,
        "account_id": account_id,
        "msg_id": msg_id or _new_msg_id(),
        "to_account": to_account,
        "task_id": task_id,
        "body": body,
        "file_id": file_id,
        "room": room,
        "created_at": now_iso(),
    }
    if alert is not None:
        row["alert"] = alert
    errors = validate_message_row(row)
    if errors:
        raise ValueError("dòng outbox không hợp lệ: %s" % "; ".join(errors))
    append_jsonl(outbox_path(root), row, mode=0o600)
    return row


def validate_typing_row(row):
    """Kiểm tra 1 dòng outbox tín hiệu typing; trả về list lỗi."""
    errors = []
    if not isinstance(row, dict):
        return ["dòng outbox phải là dict"]
    if row.get("kind") != "typing":
        errors.append("kind phải là 'typing'")
    if not isinstance(row.get("account_id"), str) \
            or not row.get("account_id"):
        errors.append("account_id phải là chuỗi không rỗng")
    room = row.get("room")
    if not isinstance(room, str) or not ROOM_RE.match(room):
        errors.append("room phải là 'chung' hoặc 'dm:<account_id>'")
    started_at = row.get("started_at")
    if isinstance(started_at, bool) \
            or not isinstance(started_at, (int, float)) \
            or started_at <= 0:
        errors.append("started_at phải là epoch giây > 0")
    return errors


def enqueue_typing(root, account_id, room, started_at=None):
    """Ghi thêm 1 dòng tín hiệu "đang trả lời" vào outbox (v0.6.0,
    T3h). 1 tín hiệu/lượt trả lời, sau khi worker chat giữ chỗ xong.
    Raise ValueError nếu dòng không hợp lệ. Trả về dòng đã ghi."""
    ids.validate_bot_id(account_id)
    row = {
        "kind": "typing",
        "account_id": account_id,
        "room": room,
        "started_at": started_at if started_at is not None
        else now_epoch(),
        "created_at": now_iso(),
    }
    errors = validate_typing_row(row)
    if errors:
        raise ValueError("dòng outbox không hợp lệ: %s" % "; ".join(errors))
    append_jsonl(outbox_path(root), row, mode=0o600)
    return row


def report_done(root, account_id, task_id, report_body, note=None,
                to_account=None, file_id=None):
    """Báo xong thật (doc 10 §6.4 + V3-GC-01): ghi LIỀN NHAU trong
    cùng lượt outbox — dòng đổi trạng thái ``da-bao-xong`` trước,
    dòng tin ``bao-cao`` gắn ``task_id`` ngay sau. Trả về
    (status_row, message_row).

    (v0.2.5) ``file_id``: dinh kem file vao tin bao-cao (file phai
    tai len hub truoc)."""
    status_row = enqueue_status(
        root, account_id, task_id, "da-bao-xong", note=note)
    message_row = enqueue_message(
        root, account_id, "bao-cao", report_body, task_id=task_id,
        to_account=to_account, file_id=file_id)
    return status_row, message_row


def read_outbox(root):
    """Đọc outbox: trả về (rows, total_lines, skipped).

    ``rows`` là list ``(line_no, row)`` với ``line_no`` đếm từ 1 theo
    DÒNG VẬT LÝ trong file (kể cả dòng hỏng) — flush dùng số dòng
    này làm cursor nên phải ổn định.
    """
    path = Path(root) / OUTBOX_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], 0, 0
    rows = []
    skipped = 0
    lines = text.splitlines()
    total = len(lines)
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        rows.append((line_no, row))
    return rows, total, skipped


def _cursor_path(root, account_id):
    return Path(root) / "center_outbox_cursors" / ("%s.json" % account_id)


def load_cursor(path):
    """Đọc cursor outbox: {"line": N}. File thiếu/hỏng -> line 0."""
    data = read_json(path)
    if isinstance(data, dict):
        line = data.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line >= 0:
            return {"line": line}
    return {"line": 0}


def save_cursor(path, line):
    atomic_write_json(path, {"line": line})


def _outbox_state_path(root, account_id):
    return Path(root) / "center_outbox_state" / ("%s.json" % account_id)


def _load_failure_state(root, account_id):
    data = read_json(_outbox_state_path(root, account_id))
    if isinstance(data, dict):
        return data
    return {"consec_auth_failures": 0, "alerted": False}


def _save_failure_state(root, account_id, state):
    atomic_write_json(_outbox_state_path(root, account_id), state)


def _auth_hint(status, body):
    """Hint chan doan theo ma + body (FIXL §6.1).

    401 -> kiem tra ma bi mat; 403 not_participating -> kiem tra co
    tham gia; 403 khac (fail-open, van dem) -> kiem tra ma bi mat
    hoac phan quyen tai khoan. Hint cu sai lech (do ma bi mat) da
    bi xoa khoi moi duong.
    """
    if status == 401:
        return "kiểm tra mã bí mật"
    if isinstance(body, dict) and body.get("error") == "not_participating":
        return "kiểm tra cờ tham gia"
    return "kiểm tra mã bí mật hoặc phân quyền tài khoản"


def _note_auth_failure(root, account_id, log_fn, alert_fn,
                       status=None, body=None, count=True):
    """Dem loi xac thuc theo chu ky flush (FIXL §6.1).

    ``count=True`` (mac dinh, tuong thich nguoc): tang
    ``consec_auth_failures``, du 3 chu ky thi bao 1 lan qua
    ``alert_fn`` roi im do co ``alerted``. ``count=False`` (chi
    dung cho 403 ``not_participating``): chi ghi 1 dong log kem
    hint, khong tang bo dem, khong cham co ``alerted``, khong goi
    ``alert_fn``.
    """
    hint = _auth_hint(status, body)
    if not count:
        log_fn("center_outbox account=%s: 403 not_participating, "
               "khong dem loi ma (hint: %s)" % (account_id, hint))
        return
    state = _load_failure_state(root, account_id)
    failures = int(state.get("consec_auth_failures") or 0) + 1
    state["consec_auth_failures"] = failures
    if failures >= AUTH_FAILURE_ALERT_AFTER and not state.get("alerted"):
        state["alerted"] = True
        alert_fn(
            "center_outbox_auth_failure",
            account_id,
            {"consec_cycles": failures, "hint": hint},
        )
    _save_failure_state(root, account_id, state)
    log_fn("center_outbox account=%s: 401/403 chu ky lien tiep=%d "
           "(hint: %s)" % (account_id, failures, hint))


def _reset_auth_state(root, account_id):
    """Reset 2 truong loi ma; GIU NGUYEN ``dropped_by_task`` (FIXL).

    (Truoc day ham ghi de toan bo file bang 2 truong, xoa moc dem
    A2 ngay sau lan day 200 dau tien.)
    """
    state = _load_failure_state(root, account_id)
    if state.get("consec_auth_failures") or state.get("alerted"):
        state["consec_auth_failures"] = 0
        state["alerted"] = False
        _save_failure_state(root, account_id, state)


def _dead_letter_path(root):
    return Path(root) / DEAD_LETTER_FILENAME


def _trim_dead_letter_locked(path):
    """Cat file dead-letter trong cung lan giu khoa (G4, FIXL §6.2).

    Giu dong trong 30 ngay, toi da 1000 dong moi nhat. Chi goi khi
    da giu khoa flock cua file dead-letter. Dong khong doc duoc
    thoi gian thi giu lai (khong xoa du lieu khong xac dinh duoc).
    """
    entries, _skipped = read_jsonl(path)
    now = now_epoch()
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        dropped = entry.get("dropped_at")
        epoch = iso_to_epoch(dropped) if isinstance(dropped, str) else None
        if epoch is not None and now - epoch > DEAD_LETTER_KEEP_SECONDS:
            continue
        kept.append(entry)
    kept = kept[-DEAD_LETTER_MAX_LINES:]
    if len(kept) != len(entries):
        data = "".join(
            json.dumps(item, ensure_ascii=False) + "\n" for item in kept
        ).encode("utf-8")
        atomic_write_bytes(path, data, mode=0o600)


def write_dead_letter(root, account_id, line_no, row, http_status,
                      reason, hub_error=None, hub_body=None):
    """Ghi 1 dong vao file dead-letter chung (FIXL §6.2, Q-FIXL-06).

    1 helper duy nhat ghi file ``<root>/center_outbox_dead.jsonl``
    (ghi ro ``account_id`` tung dong, quyen 600 = chi chu nhan
    xem). Giu 1 khoa flock tren file ``.lock`` trong suot append +
    cat (G4: duong cat chung khoa voi duong ghi). Khong co bo nho
    dem trong bo nho nen khong can dong bo lai cache sau khi cat.
    """
    entry = {
        "line_no": line_no,
        "task_id": row.get("task_id") if isinstance(row, dict) else None,
        "to_status": row.get("to_status")
        if isinstance(row, dict) else None,
        "kind": row.get("kind") if isinstance(row, dict) else None,
        "http_status": http_status,
        "reason": reason,
        "hub_error": hub_error,
        "hub_body": hub_body if isinstance(hub_body, dict) else None,
        "dropped_at": now_iso(),
        "account_id": account_id,
    }
    path = _dead_letter_path(root)
    ensure_dir(path.parent)
    lock_path = Path(str(path) + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            append_jsonl(path, entry)
            _trim_dead_letter_locked(path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return entry


def _record_transition_drop(root, account_id, task_id, to_status,
                            alert_fn, now=None):
    """Ghi moc bo dong A2 + bao khi du N=3 moc cung viec trong
    X=60 phut (FIXL §2 A2, Q-FIXL-13).

    Moc luu trong ``center_outbox_state/<account_id>.json`` truong
    ``dropped_by_task`` (tai dung ``_load/_save_failure_state``).
    Bao qua ``alert_fn`` loai ``center_outbox_transition_dropped``
    (khong dem vao ``consec_auth_failures``, khong goi
    ``_note_auth_failure``). Re-arm: bao xong thi XOA moc cua viec
    do va dem lai tu dau.
    """
    if now is None:
        now = now_epoch()
    # (FIXL, sua sau audit §4 #1) Chuan hoa key truoc khi dung:
    # key None ghi JSON thanh "null", lan load sau
    # dropped.get(None) khong tim thay "null" nen moc dem lai
    # tu dau moi chu ky, khong bao gio du N=3. Dong thieu
    # task_id dem chung duoi key "unknown" (alert cung ghi
    # task_id la "unknown").
    task_key = str(task_id) if task_id is not None else "unknown"
    state = _load_failure_state(root, account_id)
    dropped = state.get("dropped_by_task")
    if not isinstance(dropped, dict):
        dropped = {}
    marks = [mark for mark in dropped.get(task_key, [])
             if isinstance(mark, (int, float))
             and not isinstance(mark, bool)
             and now - mark <= TRANSITION_DROP_WINDOW_SECONDS]
    marks.append(now)
    dropped[task_key] = marks
    state["dropped_by_task"] = dropped
    _save_failure_state(root, account_id, state)
    if len(marks) >= TRANSITION_DROP_ALERT_AFTER:
        alert_fn("center_outbox_transition_dropped", account_id, {
            "task_id": task_key,
            "to_status": to_status,
            "dropped": len(marks),
            "hint": "kiểm tra dòng trạng thái lùi",
        })
        del dropped[task_key]
        state["dropped_by_task"] = dropped
        _save_failure_state(root, account_id, state)


# --- Đường cảnh báo chung (Fix gộp 21/11, §4.2 mục 3 + 7) -----------
# File alert là kênh chính (UI tab Cảnh báo đọc, như D4); DM qua
# outbox trực tiếp là kênh thứ hai (không cần worker — vì cap kẹt
# thì không đánh thức được worker). Dedup 30 phút/đợt/loại/tài
# khoản; hết kẹt thì caller xóa entry dedup để đợt mới lại báo
# (luật re-arm — xem clear_alert_reported). DM lỗi mã chỉ là
# best-effort (push xoay cặp với fetch: fetch 401 thì push cũng
# 401, dòng alert kẹt trong outbox và bị flush bỏ khi quá TTL).

def _alert_paths(root, alert_kind):
    """(file .jsonl, file dedup) cho 1 loại alert. Loại lạ -> lỗi."""
    directory = Path(root) / "center"
    if alert_kind == "cap_stuck":
        return (directory / "cap_alerts.jsonl",
                directory / "cap_alerts_reported.json")
    if alert_kind == "auth":
        return (directory / "auth_alerts.jsonl",
                directory / "auth_alerts_reported.json")
    raise ValueError("loại alert không hợp lệ: %r" % (alert_kind,))


def record_alert(root, account_id, alert_kind, dedup_key, text,
                 detail=None, now=None):
    """Ghi 1 alert: 1 dòng vào file alert (kênh chính) + 1 DM
    kind="hoi" vào outbox (room="dm:<account_id>" của chính tài
    khoản này — 1 VM = 1 tài khoản; body không chứa "@").

    Dedup theo ``dedup_key`` trong 30 phút: trong cooldown thì
    không ghi gì thêm, trả về False. Hết cooldown (hoặc entry đã
    bị xóa khi re-arm) thì ghi lại, trả về True. Enqueue DM lỗi
    (vd outbox hỏng) thì file alert vẫn có — file là kênh chính.
    """
    ids.validate_bot_id(account_id)
    if now is None:
        now = now_epoch()
    alerts_path, reported_path = _alert_paths(root, alert_kind)
    reported = read_json(reported_path)
    if not isinstance(reported, dict):
        reported = {}
    entry = reported.get(dedup_key)
    if isinstance(entry, dict):
        epoch = entry.get("epoch")
        if isinstance(epoch, (int, float)) \
                and not isinstance(epoch, bool) \
                and now - epoch < ALERT_COOLDOWN_SECONDS:
            return False
    alert = {
        "at": epoch_to_iso(now),
        "account_id": account_id,
        "alert": alert_kind,
        "key": dedup_key,
        "text": text,
    }
    if isinstance(detail, dict) and detail:
        alert["detail"] = detail
    append_jsonl(alerts_path, alert)
    reported[dedup_key] = {"at": epoch_to_iso(now), "epoch": now}
    atomic_write_json(reported_path, reported)
    try:
        enqueue_message(root, account_id, "hoi", text,
                        room="dm:%s" % account_id, alert=alert_kind)
    except Exception as exc:  # noqa: BLE001 - DM best-effort, file đã ghi
        print("center_outbox record_alert: khong enqueue duoc DM "
              "alert=%s (%s: %s)"
              % (alert_kind, type(exc).__name__, exc), file=sys.stderr,
              flush=True)
    return True


def clear_alert_reported(root, alert_kind, dedup_key):
    """Xóa 1 entry dedup (luật re-arm): hết kẹt / hết lỗi mã thì
    đợt kẹt/lỗi mới sau đó lại báo (không bị cooldown đợt cũ chặn).
    Không có entry thì không làm gì."""
    _alerts_path, reported_path = _alert_paths(root, alert_kind)
    reported = read_json(reported_path)
    if isinstance(reported, dict) and dedup_key in reported:
        del reported[dedup_key]
        atomic_write_json(reported_path, reported)


def _http_post(opener, url, secret, payload, timeout, account_label=None):
    """POST JSON, trả về (status, body_dict_or_None, error_text).

    status=0 nghĩa là lỗi transport. Không bao giờ đưa secret hay
    nội dung payload vào thông báo lỗi. Header nhãn theo fail-open
    MA9 (TS5) như mọi request trung tâm khác.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + secret,
        "Content-Type": "application/json",
    }
    label = fetcher._valid_account_label(account_label)
    if label is not None:
        headers["X-Muse-Account"] = label
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - body đọc lỗi thì coi như rỗng
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - mọi lỗi transport về 1 kênh
        return 0, None, "%s: %s" % (type(exc).__name__, exc)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body = None
    return status, body if isinstance(body, dict) else None, None


def _post_row(opener, config, row, http_post_fn):
    """POST 1 dòng outbox lên receiver. Trả về (status, body, error).

    Dòng status -> POST .../tasks/<id>/status {to_status, note,
    client_key}; dòng message -> POST .../messages {msg_id, kind,
    to_account, task_id, body}; dòng typing -> POST .../typing
    {room, started_at} (v0.6.0, T3h).
    """
    account_id = config["account_id"]
    base = config["receiver_endpoint"]
    if row.get("kind") == "status":
        url = "%s/v1/center/%s/tasks/%s/status" % (
            base, account_id, row["task_id"])
        payload = {
            "to_status": row["to_status"],
            "note": row.get("note"),
            "client_key": row["client_key"],
        }
    elif row.get("kind") == "typing":
        url = "%s/v1/center/%s/typing" % (base, account_id)
        payload = {
            "room": row["room"],
            "started_at": row["started_at"],
        }
    else:
        url = "%s/v1/center/%s/messages" % (base, account_id)
        payload = {
            "msg_id": row["msg_id"],
            "kind": row["kind"],
            "to_account": row.get("to_account"),
            "task_id": row.get("task_id"),
            "body": row["body"],
        }
        # (v0.2.5) Tin kem file: gui file_id kem — hub kiem tra file
        # ton tai + dung nguoi tai len (400 unknown_file_id neu sai).
        if row.get("file_id"):
            payload["file_id"] = row["file_id"]
        # (v0.3.0, T3c) Gui room theo phong (khong gui khi None —
        # hub mac dinh "chung"); mentions KHONG gui — may chu tu
        # tach luc ghi (T3d).
        if row.get("room"):
            payload["room"] = row["room"]
    if http_post_fn is not None:
        return http_post_fn(url, config["push_secret"], payload,
                            FLUSH_TIMEOUT_SECONDS,
                            config.get("account_id"))
    return _http_post(opener, url, config["push_secret"], payload,
                      FLUSH_TIMEOUT_SECONDS, config.get("account_id"))


def flush(root, account_id=None, config=None, log_fn=None, alert_fn=None,
          http_post_fn=None):
    """Đẩy tối đa 1 dòng outbox chưa đồng bộ về hub. Trả về dict thống
    kê (không nội dung, không secret).

    Chỉ tiến con trỏ SAU KHI hub trả 200 (kể cả duplicate:true).
    401/403: chỉ dừng đẩy trung tâm của tài khoản đó. 429/5xx/lỗi
    mạng: không tiến, thử lại chu kỳ sau. 400/404/409: lỗi vĩnh viễn
    — ghi log + tiến con trỏ qua dòng đó (bỏ dòng, không kẹt hàng).
    (FIXL A2, 22/11) 403 ``transition_forbidden`` trên dòng STATUS:
    bo dong nhu 4xx (khong dem loi ma); 403 ``not_participating``:
    giu dong, chi ghi log kem hint, khong dem; 403 khong co ma moi
    thi fail-open nhu cu. Moi lan bo dong (A2/TTL/4xx) deu ghi
    dead-letter + log.
    (C-7, 22/11) 403 vinh vien tren dong TIN NHAN (``room_forbidden``,
    ``discussion_disabled``, ``to_account_not_participating``): bo
    dong nhu 4xx (khong dem loi ma). 403 ``not_assignee`` (ma moi cua
    hub) tren dong STATUS: bo dong nhu A2. 401 khong bao gio bo dong.
    NGOẠI LỆ DUY NHẤT (v0.6.0, T3l — D1(f)): 400 ``bad_to_account``
    trên dòng tin v4 (``to_account`` là danh sách) thì GIỮ dòng lại,
    không tiến con trỏ, thử lại chu kỳ sau.
    (Fix gộp 21/11, §4.2 mục 7 — N-S1): dòng có trường ``alert``
    mà ``created_at`` quá 30 phút chưa đẩy được thì BỎ (tiến cursor,
    đếm vào ``dropped``) — CHỈ dòng alert, không chạm tin thường.
    """
    log_fn = log_fn or _default_log
    alert_fn = alert_fn or _default_alert
    if config is None:
        if account_id is None:
            accounts = center_fetcher.discover_center_accounts(root)
            if not accounts:
                log_fn("center_outbox: khong co tai khoan nao co cau hinh")
                return {"sent": 0, "error": "no_config"}
            account_id = sorted(accounts)[0]
        configs = center_fetcher.load_center_configs(root, log_fn=log_fn)
        config = configs.get(account_id)
        if config is None:
            log_fn("center_outbox account=%s: thieu cau hinh" % account_id)
            return {"sent": 0, "error": "no_config"}
    account_id = config["account_id"]
    stats = {
        "account_id": account_id, "sent": 0, "duplicates": 0,
        "dropped": 0, "cursor_before": 0, "cursor_after": 0,
        "error": None,
    }
    if not config.get("receiver_endpoint") or not config.get("push_secret"):
        log_fn("center_outbox account=%s: thieu receiver_endpoint hoac "
               "push_secret" % account_id)
        stats["error"] = "no_receiver_config"
        return stats
    cursor_file = _cursor_path(root, account_id)
    cursor = load_cursor(cursor_file)
    stats["cursor_before"] = cursor["line"]
    stats["cursor_after"] = cursor["line"]
    rows, total, skipped = read_outbox(root)
    if skipped:
        log_fn("center_outbox account=%s: bo %d dong outbox hong"
               % (account_id, skipped))
    pending = [(n, r) for (n, r) in rows if n > cursor["line"]]
    if not pending:
        if total > cursor["line"]:
            save_cursor(cursor_file, total)
            stats["cursor_after"] = total
        _reset_auth_state(root, account_id)
        log_fn("center_outbox account=%s: khong co dong moi, outbox=%d, "
               "cursor=%d" % (account_id, total, stats["cursor_after"]))
        return stats
    line_no, row = pending[0]
    # (Fix gộp 21/11, §4.2 mục 7 — N-S1) TTL dòng alert: dòng alert
    # quá 30 phút chưa đẩy được thì BỎ (tiến cursor, đếm vào
    # dropped) — không gửi muộn sai thời điểm. CHỈ dòng alert;
    # tin thường không chạm (tin thường kẹt thì thử lại chu kỳ sau
    # như cũ).
    if row.get("alert") in ALERT_KINDS:
        created_epoch = iso_to_epoch(row.get("created_at")) \
            if isinstance(row.get("created_at"), str) else None
        if created_epoch is not None \
                and now_epoch() - created_epoch > ALERT_TTL_SECONDS:
            log_fn(
                "center_outbox account=%s: bo dong alert %d qua TTL "
                "30 phut (alert=%s)" % (account_id, line_no,
                                        row.get("alert")))
            write_dead_letter(root, account_id, line_no, row, None,
                              "alert_ttl_expired")
            save_cursor(cursor_file, line_no)
            stats["cursor_after"] = line_no
            stats["dropped"] = 1
            stats["error"] = "alert_ttl_expired"
            return stats
    try:
        opener = fetcher.build_opener(
            config["receiver_endpoint"], config.get("via_proxy", False),
            log_fn)
    except RuntimeError as exc:
        stats["error"] = str(exc)
        log_fn("center_outbox account=%s: %s" % (account_id, exc))
        return stats
    status, body, error = _post_row(opener, config, row, http_post_fn)
    if status == 0:
        log_fn("center_outbox account=%s: transport loi (%s), thu lai "
               "chu ky sau" % (account_id, error))
        stats["error"] = "transport"
        return stats
    if status in (401, 403):
        if status == 403 and isinstance(body, dict) \
                and body.get("error") == "not_participating":
            # (FIXL §6.1) Co tham gia bi tat: giu dong thu lai chu ky
            # sau, khong dem vao bo dem loi ma, chi ghi log kem hint.
            _note_auth_failure(root, account_id, log_fn, alert_fn,
                               status=403, body=body, count=False)
            stats["error"] = "auth_403"
            return stats
        if status == 403 and row.get("kind") == "status" \
                and isinstance(body, dict) \
                and body.get("code") == "transition_forbidden":
            # (FIXL A2) Dong status lui vinh vien theo hub: bo dong,
            # tien con tro nhu nhanh 4xx; khong dem loi ma, khong
            # goi _note_auth_failure. Chi dong STATUS; 401 khong
            # bao gio bo dong; body None/khong co ma moi thi
            # fail-open o nhanh duoi (giu nguyen hanh vi cu).
            write_dead_letter(root, account_id, line_no, row, 403,
                              "transition_forbidden",
                              hub_error=body.get("error"),
                              hub_body=body)
            log_fn(
                "center_outbox account=%s: bo dong %d (403 "
                "transition_forbidden, kind=%s, task=%s)"
                % (account_id, line_no, row.get("kind"),
                   short_id(row.get("task_id"))))
            save_cursor(cursor_file, line_no)
            stats["cursor_after"] = line_no
            stats["dropped"] = 1
            stats["error"] = "transition_forbidden"
            _record_transition_drop(root, account_id,
                                    row.get("task_id"),
                                    row.get("to_status"), alert_fn)
            return stats
        if status == 403 and row.get("kind") != "status" \
                and isinstance(body, dict) \
                and body.get("error") in C7_DROP_MESSAGE_ERRORS:
            # (C-7, Q-C7-01) Dong TIN NHAN bi 403 vinh vien theo hub:
            # bo dong, tien con tro nhu nhanh 4xx; khong dem loi ma,
            # khong goi _note_auth_failure. Chi dong tin (kind !=
            # "status"); "not_participating" da duoc xu ly o nhanh
            # tren (giu dong theo FIXL §6.1); 401 khong bao gio bo.
            reason = body.get("error")
            write_dead_letter(root, account_id, line_no, row, 403,
                              reason,
                              hub_error=body.get("error"),
                              hub_body=body)
            log_fn(
                "center_outbox account=%s: bo dong %d (403 %s, "
                "kind=%s, task=%s)"
                % (account_id, line_no, reason, row.get("kind"),
                   short_id(row.get("task_id"))))
            save_cursor(cursor_file, line_no)
            stats["cursor_after"] = line_no
            stats["dropped"] = 1
            stats["error"] = reason
            return stats
        if status == 403 and row.get("kind") == "status" \
                and isinstance(body, dict) \
                and body.get("code") == "not_assignee":
            # (C-7, Q-C7-03) Dong STATUS cua tai khoan khong con la
            # assignee/assigner: hub tra 403 code "not_assignee"
            # (them o receiver theo mau C3). Bo dong nhu A2; khong
            # dem loi ma. Hub cu (chua co ma) tra 403 "forbidden"
            # khong ma -> fail-open o nhanh duoi (giu dong nhu cu).
            write_dead_letter(root, account_id, line_no, row, 403,
                              "not_assignee",
                              hub_error=body.get("error"),
                              hub_body=body)
            log_fn(
                "center_outbox account=%s: bo dong %d (403 "
                "not_assignee, kind=%s, task=%s)"
                % (account_id, line_no, row.get("kind"),
                   short_id(row.get("task_id"))))
            save_cursor(cursor_file, line_no)
            stats["cursor_after"] = line_no
            stats["dropped"] = 1
            stats["error"] = "not_assignee"
            return stats
        _note_auth_failure(root, account_id, log_fn, alert_fn,
                           status=status, body=body)
        stats["error"] = "auth_%d" % status
        return stats
    if status == 429 or status >= 500:
        log_fn("center_outbox account=%s: hub status=%s, thu lai chu ky "
               "sau" % (account_id, status))
        stats["error"] = "http_%d" % status
        return stats
    if status == 400 and isinstance(body, dict) \
            and body.get("error") == "bad_to_account" \
            and isinstance(row.get("to_account"), list):
        # (v0.6.0, T3l — D1(f)) NGOAI LE DUY NHAT cho 400: dong
        # tin v4 (``to_account`` la danh sach) bi receiver CU tra
        # 400 ``bad_to_account`` — KHONG phai loi vinh vien: GIU
        # dong lai (khong tien con tro), ghi log, thu lai chu ky
        # sau. Chi rieng truong hop nay; cac loi 400 khac van vinh
        # vien nhu cu. Thu tu trieu khai PC-truoc/Muse-sau phai
        # chan truong hop nay xay ra; day la luoi an toan khi PC
        # rollback trong khi phia Muse con moi (rollback: Muse ve
        # ban cu truoc, roi moi rollback PC — xem docstring module).
        log_fn("center_outbox account=%s: giu dong %d (tin v4, "
               "receiver tra 400 bad_to_account), thu lai chu ky "
               "sau" % (account_id, line_no))
        stats["error"] = "http_400_bad_to_account_v4_retry"
        return stats
    if status != 200 or not isinstance(body, dict):
        # 400/404/409 (và mọi 4xx khác): lỗi vĩnh viễn theo thao tác
        # — bỏ dòng để không kẹt hàng chờ (ghi log + dead-letter,
        # không lặng lẽ).
        write_dead_letter(root, account_id, line_no, row, status,
                          "http_%d" % status,
                          hub_error=body.get("error")
                          if isinstance(body, dict) else None,
                          hub_body=body)
        log_fn(
            "center_outbox account=%s: bo dong %d (status=%s, kind=%s, "
            "task=%s)" % (account_id, line_no, status, row.get("kind"),
                          short_id(row.get("task_id"))))
        save_cursor(cursor_file, line_no)
        stats["cursor_after"] = line_no
        stats["dropped"] = 1
        stats["error"] = "http_%d" % status
        return stats
    # 200: tiến con trỏ (kể cả duplicate:true — hub đã có dòng này).
    save_cursor(cursor_file, line_no)
    stats["cursor_after"] = line_no
    stats["sent"] = 1
    if body.get("duplicate"):
        stats["duplicates"] = 1
    _reset_auth_state(root, account_id)
    log_fn(
        "center_outbox account=%s: dong %d ok (duplicate=%s), cursor "
        "%d -> %d" % (account_id, line_no, bool(body.get("duplicate")),
                      stats["cursor_before"], stats["cursor_after"]))
    return stats


def _read_body_file(path):
    """Đọc nội dung tin từ file (như --text-file của Sender bot).

    Trả về chuỗi đã bỏ ký tự xuống dòng ở cuối; rỗng thì ValueError.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("không đọc được file nội dung %s: %s"
                         % (path, exc))
    body = text.rstrip("\n")
    if not body:
        raise ValueError("file nội dung %s rỗng" % path)
    return body


def main(argv=None):
    """CLI: python3 center_outbox.py --root <root> --flush.

    Worker việc trung tâm báo cáo qua CLI này (không gọi API trực
    tiếp, không tự ghi outbox tay):

    - Đổi trạng thái: --account-id <id> --task-id <id>
      --status <to_status> [--note "<ghi chú>"]
    - Gửi tin: --account-id <id> [--task-id <id>]
      --message-kind hoi|bao-cao --body-file <file>
      [--to-account <id>] [--file-id <f-...>] [--room <phong>]
      (giao-viec bị từ chối — chỉ hub tự sinh; tin chỉ có file thì
      --body-file được bỏ, body rỗng; --room chỉ cho tin hoi,
      bao-cao luôn ở khung chung; --task-id được bỏ trống khi
      --message-kind hoi và không có việc gắn kèm — ghi task_id:
      null, CẤM bịa task_id giả — NS-32 (a), v0.6.0 T3h)
    - Tín hiệu "đang trả lời": --account-id <id> --typing
      --room <phong> (1 tín hiệu/lượt trả lời chat, v0.6.0 T3h)
    - Báo xong thật: --account-id <id> --task-id <id>
      --report-done --body-file <file> [--note ...] [--to-account ...]
      [--file-id <f-...>]
      (= da-bao-xong + 1 tin bao-cao liền nhau, cùng lượt outbox)
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Center outbox phía Muse (token-free).")
    parser.add_argument("--root", default=".",
                        help="thư mục gốc phía Muse (mặc định: .)")
    parser.add_argument("--account-id", default=None,
                        help="tài khoản cần flush (mặc định: tài khoản "
                             "đầu tiên có cấu hình)")
    parser.add_argument("--flush", action="store_true",
                        help="đẩy tối đa 1 dòng outbox về hub")
    parser.add_argument("--task-id", default=None,
                        help="việc cần báo cáo (dùng với --status, "
                             "--message-kind, --report-done)")
    parser.add_argument("--status", default=None,
                        help="đổi trạng thái việc tới trạng thái này")
    parser.add_argument("--message-kind", default=None,
                        choices=list(POSTABLE_MESSAGE_KINDS),
                        help="gửi tin kind này (hoi|bao-cao)")
    parser.add_argument("--report-done", action="store_true",
                        help="báo xong thật: da-bao-xong + 1 tin bao-cao")
    parser.add_argument("--body-file", default=None,
                        help="file chứa nội dung tin (UTF-8)")
    parser.add_argument("--note", default=None,
                        help="ghi chú kèm đổi trạng thái")
    parser.add_argument("--to-account", default=None,
                        help="tài khoản nhận tin (mặc định: không chỉ "
                             "định)")
    parser.add_argument("--file-id", default=None,
                        help="file đã tải lên hub (f- + 12 ký tự hex), "
                             "đính kèm vào tin (v0.2.5)")
    parser.add_argument("--room", default=None,
                        help="phòng gửi tin tới ('chung' hoặc "
                             "'dm:<account_id>', v0.3.0, T3c; chỉ dùng "
                             "với --message-kind hoi, bao-cao luôn ở "
                             "khung chung)")
    parser.add_argument("--typing", action="store_true",
                        help="tín hiệu 'đang trả lời' (v0.6.0, T3h): "
                             "ghi 1 dòng typing vào outbox (cần "
                             "--account-id + --room)")
    args = parser.parse_args(argv)
    if args.flush:
        result = flush(args.root, account_id=args.account_id)
        # Chưa có cấu hình trung tâm = chưa có gì để đẩy (trạng thái
        # bình thường khi trung tâm chưa setup) -> thoát 0 như syncer bot.
        if result.get("error") == "no_config":
            return 0
        return 0 if not result.get("error") else 1
    if args.typing:
        if args.account_id is None or args.room is None:
            print("center_outbox: --typing cần --account-id và --room",
                  file=sys.stderr)
            return 2
        try:
            enqueue_typing(args.root, args.account_id, args.room)
        except ValueError as exc:
            print("center_outbox: %s" % exc, file=sys.stderr)
            return 1
        return 0
    if not (args.status or args.message_kind or args.report_done):
        parser.print_help()
        return 2
    if args.account_id is None:
        print("center_outbox: --account-id là bắt buộc",
              file=sys.stderr)
        return 2
    # NS-32 (a), v0.6.0 T3h: --task-id được bỏ trống khi --message-kind
    # hoi và không có việc gắn kèm (ghi task_id: null); --status và
    # --report-done vẫn bắt buộc --task-id. CẤM bịa task_id giả.
    if args.task_id is None and args.message_kind != "hoi":
        print("center_outbox: --task-id là bắt buộc (trừ --message-kind "
              "hoi không gắn việc)", file=sys.stderr)
        return 2
    try:
        if args.report_done:
            if args.body_file is None and args.file_id is None:
                print("center_outbox: --report-done cần --body-file "
                      "(hoặc --file-id cho tin chỉ có file)",
                      file=sys.stderr)
                return 2
            body = _read_body_file(args.body_file) \
                if args.body_file is not None else ""
            report_done(args.root, args.account_id, args.task_id, body,
                        note=args.note, to_account=args.to_account,
                        file_id=args.file_id)
        elif args.message_kind:
            if args.body_file is None and args.file_id is None:
                print("center_outbox: --message-kind cần --body-file "
                      "(hoặc --file-id cho tin chỉ có file)",
                      file=sys.stderr)
                return 2
            body = _read_body_file(args.body_file) \
                if args.body_file is not None else ""
            enqueue_message(args.root, args.account_id, args.message_kind,
                            body, task_id=args.task_id,
                            to_account=args.to_account,
                            file_id=args.file_id, room=args.room)
        else:
            enqueue_status(args.root, args.account_id, args.task_id,
                           args.status, note=args.note)
    except ValueError as exc:
        print("center_outbox: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
