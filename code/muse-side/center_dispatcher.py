"""Center dispatcher phía Muse — SPEC-TRUNG-TAM TT10/TN2, doc 10 §6.3.

Chạy 1 chu kỳ (token-free, 10s): đọc việc local đã kéo về -> chỉ
nhận việc có ``assigned_to`` đúng ``account_id`` của mình -> claim
-> kiểm tra worker cap riêng việc trung tâm -> gọi worker việc.

Luật lệ (ghi rõ số nào là SUY LUẬN):

- Chỉ claim việc ``assigned_to`` = mình. Thấy việc của tài khoản
  khác (C5: thấy hết) thì CHỈ ĐỌC: không claim, không đánh thức
  worker, không ghi ledger cho việc đó.
- 1 lần đánh thức = 1 việc; tối đa 1 việc được đánh thức mỗi chu kỳ
  hook (SUY LUẬN — TN2).
- Worker cap RIÊNG việc trung tâm: tối đa 2 việc đang xử lý đồng
  thời/tài khoản (SUY LUẬN — TN2, per-account per-process; KHÔNG cap
  toàn hệ xuyên tài khoản — C-4). Vượt thì việc chờ, không bỏ việc.
- Worker việc KHÔNG nạp prompt bot, KHÔNG đọc trí nhớ bot (doc 10
  §6.1): worker chỉ nhận bản ghi việc + tin nhắn gắn ``task_id``.
  Nạp sai là lỗi (như quy tắc "1 lượt = 1 bot" của bot).
- Trạng thái cuối do hub báo qua feed: việc local đã
  ``da-xac-nhan`` -> ledger ``confirmed``; ``huy`` -> ``cancelled``
  (phía Muse không tự ghi ``confirmed`` — chỉ ghi lại trạng thái
  hub đã báo).
- Boot-scan: marker boot mới -> claim hết hạn (cả claim + heartbeat
  cũ quá 15 phút) thành ``unknown``, không chạy lại mù. Claim phong
  chat (khoa ``chat__...``) KHONG phai viec nen boot-scan BO QUA
  (v0.6.0, T3h).
- Thư độc: 1 việc worker lỗi giống nhau >=3 lần -> ``unknown`` +
  báo, không thử lại mãi (theo mẫu dispatcher bot, POISON=3).

(Fix gộp 21/11, PHUONG-AN-FIX-GOP-21-11.md §4.2 mục 1–3 — S1/S1c):
việc mà tài khoản đã báo xong (``account_id ∈ reported_by``) thì
KHÔNG còn là ứng viên "own", và claim đang giữ trên việc đó được
gỡ vô điều kiện trong bước đồng bộ (như ``_sync_terminal_states``);
``TERMINAL_STATUSES`` giữ nguyên (KHÔNG thêm ``da-bao-xong`` —
Q-FIX-01). Cap kẹt ≥3 chu kỳ liên tiếp (~55s) hoặc assignee chậm
>5 phút trên việc đang mở -> alert (file ``center/cap_alerts.jsonl``
+ DM kind="hoi" qua outbox trực tiếp, dedup 30 phút/đợt; hết kẹt
thì re-arm); trạng thái bền ``center/cap_stuck_state.json``.

Nhanh chat (v0.6.0, T3h — DESIGN §3.9, doc 10 §6.6): ``plan_chat_
for_account`` lap ke hoach tra loi tin ``hoi`` theo 10 dieu kien
W1–W10 (xem ``check_chat_wake``); toi da 1 phong/chu ky — phong co
tin cu nhat len dau. ``detect_stuck_chats`` + ``record_chat_stuck_
alerts`` san xuat dong "chat tac" (D4 — QUYET-DINH-QUA-DEM-20-11):
tin cho >3 phut HOAC >=3 tin/phong -> 1 dong muc "can xem" (ghi
dedup vao ``center/chat_stuck_alerts.jsonl``; hien thi tren tab
Canh bao thuoc T3i). Dispatcher CHI DOC file moc
``center_chat_state/<account_id>.json`` — KHONG BAO GIO tu ghi moc
(moc do worker chat ghi sau khi cau tra loi vao outbox — xem
``center_chat_brief.mark_chat_replied``).

Ngoai le duy nhat (v0.6.0, T3l — DESIGN §3.10): moi chu ky lap ke
hoach, dispatcher TINH LAI khoa ``discussion_rounds`` trong cung
file trang thai tu ``center_messages.jsonl``
(``center_discussion.refresh_discussion_rounds`` — idempotent, chi
ghi khi khac ban da luu, khong cham khoa ``rooms``). Vong thao luan
la bo dem suy ra tu tin nhan, khong phai moc "da tra loi" — tinh
lai khong danh dau tin nao la da tra loi, nen khong pha luat
"dispatcher khong tu ghi moc".

Hàm chạy 1 chu kỳ nhận thư mục gốc + các hàm tiêm vào (``deps``) để
test được không cần hook thật::

    deps = {
      "worker_fn": fn(account_id, task, ctx),
      "log_fn": ..., "alert_fn": ...,
      "boot_id_fn": fn() -> str|None,
      "running_counts_fn": fn(account_id) -> int,
      "worker_cap": 2,
    }

``task`` truyền cho worker là bản ghi việc (dict, như hub trả);
``ctx`` = {"root", "account_id", "task", "messages", "now"} —
``messages`` là các tin local gắn ``task_id`` này, theo seq tăng dần.
"""

from __future__ import annotations

from pathlib import Path

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
import center_claims
import center_discussion
import center_ledger
from muse_common import (
    append_jsonl,
    atomic_write_json,
    ensure_dir,
    epoch_to_iso,
    iso_to_epoch,
    now_epoch,
    now_iso,
    read_json,
    read_jsonl,
)
from shared import ids

# --- Hằng số (ghi rõ nguồn) ---
WORKER_CAP_PER_ACCOUNT = 2  # tối đa 2 việc/tài khoản (SUY LUẬN — TN2)
# (S2 — thi diem tren muse-1992-a, Chu tich duyet 22/11/2026) Tach
# lan: worker CHAT co cap rieng, khong tranh cho voi worker VIEC.
# Tran tong worker trung tam/tai khoan = cap viec + cap chat = 3.
CHAT_WORKER_CAP = 1  # toi da 1 worker chat dong thoi/tai khoan (S2)
MAX_WAKES_PER_CYCLE = 1  # tối đa 1 việc/chu kỳ hook (SUY LUẬN — TN2)
LEADER_ACCOUNT_ID = "muse-chinh"  # truong nhom (chu nhan chot 28/11)


def sort_wake_candidates(candidates, wake_fail_counts, account_id):
    """Sap ung vien danh thuc: it that bai truoc; cung muc that bai
    thi viec truong nhom giao truoc viec nhieu; cu nhat truoc trong
    cung nhom (goi giam tac 28/11)."""
    def key(item):
        record = item.get("record") or {}
        leader = 0 if (account_id != LEADER_ACCOUNT_ID
                       and record.get("assigned_from")
                       == LEADER_ACCOUNT_ID) else 1
        return (wake_fail_counts.get(item["task_id"], 0), leader,
                record.get("created_at") or "")

    return sorted(candidates, key=key)
POISON_MAX_FAILURES = 3  # worker lỗi giống nhau >=3 lần -> unknown
TERMINAL_STATUSES = ("da-xac-nhan", "huy")


def _default_log(message):
    print(message, flush=True)


def _default_alert(kind, account_id, detail):
    print("ALERT kind=%s account=%s detail=%s"
          % (kind, account_id, detail), flush=True)


def _state_dir(root, name):
    return ensure_dir(Path(root) / "center_dispatcher_state" / name)


def _read_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", "r",
                   encoding="utf-8") as handle:
            value = handle.read().strip()
            return value or None
    except OSError:
        return None


# --- Đọc việc + tin local ---

def _assignee_list(value):
    """Chuan hoa assigned_to (chuoi v1 / danh sach v2) -> danh sach."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, str) and item]
    return []


def _reported_by_list(value):
    """Chuan hoa reported_by (danh sach cac tai khoan da bao xong)
    -> danh sach. Thieu/khong phai danh sach -> []."""
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, str) and item]
    return []


def read_local_tasks(root):
    """Đọc mọi việc local trong center_tasks/, sắp xếp theo
    (created_at, task_id) tăng dần. Trả về list dict::

        {"task_id", "seq", "record", "path"}
    """
    directory = Path(root) / "center_tasks"
    items = []
    if not directory.is_dir():
        return items
    for path in sorted(directory.glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        record = data.get("record")
        if not isinstance(record, dict):
            continue
        task_id = record.get("task_id") or data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        items.append({
            "path": path,
            "seq": data.get("seq"),
            "task_id": task_id,
            "record": record,
        })
    items.sort(key=lambda it: (
        str(it["record"].get("created_at") or ""), it["task_id"]))
    return items


def read_messages_for_task(root, task_id):
    """Tin local gắn 1 việc, theo seq tăng dần (chỉ metadata + bản
    ghi tin; caller — worker — mới đọc nội dung để làm việc)."""
    path = Path(root) / "center_messages.jsonl"
    entries, _skipped = read_jsonl(path)
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record = entry.get("record")
        if not isinstance(record, dict):
            continue
        if record.get("task_id") != task_id:
            continue
        out.append(entry)
    out.sort(key=lambda e: (
        e.get("seq") if isinstance(e.get("seq"), int) else 0))
    return out


# --- Đồng bộ trạng thái cuối từ hub ---

def _sync_terminal_states(root, account_id, tasks, log_fn):
    """Việc local đã ở trạng thái cuối theo hub -> ghi ledger
    confirmed/cancelled (chỉ ghi lại trạng thái hub đã báo) + bỏ
    giữ claim nếu còn. Trả về số việc đã đồng bộ."""
    synced = 0
    for item in tasks:
        record = item["record"]
        if account_id not in _assignee_list(record.get("assigned_to")):
            continue
        status = record.get("status")
        if status == "da-xac-nhan":
            stage = "confirmed"
        elif status == "huy":
            stage = "cancelled"
        else:
            continue
        if center_ledger.record_once(
                root, account_id, item["task_id"], stage,
                detail={"status": status}):
            synced += 1
        center_claims.release(root, account_id, item["task_id"])
    if synced:
        log_fn("center_dispatcher account=%s: dong bo %d viec ve "
               "trang thai cuoi" % (account_id, synced))
    return synced


def _sync_reported_done(root, account_id, tasks, log_fn):
    """(Fix gộp 21/11, §4.2 mục 2 — S1-A) Việc mà tài khoản đã báo
    xong (``account_id ∈ reported_by``) -> gỡ claim đang giữ trên
    việc đó, VÔ ĐIỀU KIỆN (không kiểm tiền tố holder — tiền lệ
    ``_sync_terminal_states`` gỡ vô điều kiện; holder không phân
    biệt được claim dispatcher với claim worker vì worker kế thừa
    holder không đổi; an toàn vì claim đó, theo định nghĩa, thuộc
    về worker của chính tài khoản đã báo xong). Trả về số claim đã
    gỡ. Không ghi ledger (trạng thái hub ``da-bao-xong`` chưa phải
    trạng thái cuối toàn cục — Q-FIX-01, TERMINAL_STATUSES giữ
    nguyên)."""
    released = 0
    for item in tasks:
        record = item["record"]
        if account_id not in _assignee_list(record.get("assigned_to")):
            continue
        if account_id not in _reported_by_list(record.get("reported_by")):
            continue
        if center_claims.release(root, account_id, item["task_id"]):
            released += 1
    if released:
        log_fn("center_dispatcher account=%s: go %d claim tren viec "
               "tai khoan da bao xong" % (account_id, released))
    return released


# --- Boot-scan ---

def _boot_scan(root, account_id, now, log_fn, alert_fn):
    """Quét sau thay máy: claim hết hạn -> unknown (không chạy lại mù)."""
    unknown_keys = []
    for info in center_claims.list_claims(
            root, account_id=account_id, now=now):
        if not info["expired"]:
            continue
        task_id = info["task_id"]
        if info.get("reserved_expired"):
            # (Cai tien 28/11) Giu cho chua ack: worker chua tung
            # bat dau nen chi giai phong, khong ghi unknown (viec
            # se duoc danh thuc lai binh thuong).
            center_claims.release(root, account_id, task_id)
            continue
        if center_claims.is_chat_room_claim(task_id):
            # Claim phong chat (T3h) khong phai viec: het han thi tu
            # het hieu luc, khong ghi ledger "unknown" cho khoa
            # "chat__..." (chi bo giu de chu ky sau dung lai duoc).
            center_claims.release(root, account_id, task_id)
            continue
        center_ledger.record(
            root, account_id, task_id, "unknown",
            detail={"reason": "boot_scan_stale_claim"},
        )
        center_claims.release(root, account_id, task_id)
        unknown_keys.append(task_id)
    if unknown_keys:
        alert_fn("center_boot_scan_unknown", account_id,
                 {"count": len(unknown_keys)})
        log_fn("center_dispatcher account=%s: boot-scan danh dau %d "
               "viec unknown" % (account_id, len(unknown_keys)))
    return unknown_keys


def _check_boot(root, account_id, deps, now, log_fn, alert_fn):
    boot_id_fn = deps.get("boot_id_fn") or _read_boot_id
    try:
        boot_id = boot_id_fn()
    except Exception:  # noqa: BLE001 - không đọc được boot_id thì bỏ qua
        boot_id = None
    if not boot_id:
        return []
    marker_path = ensure_dir(
        Path(root) / "center_dispatcher_state") / "boot_marker.json"
    marker = read_json(marker_path)
    if isinstance(marker, dict) and marker.get("boot_id") == boot_id:
        return []
    is_first = not isinstance(marker, dict)
    atomic_write_json(marker_path, {"boot_id": boot_id, "at": now_iso()})
    if is_first:
        return []
    log_fn("center_dispatcher: phat hien boot moi, chay boot-scan")
    return _boot_scan(root, account_id, now, log_fn, alert_fn)


# --- Đếm lỗi worker (thư độc) ---

def _fail_counts_path(root, account_id):
    ids.validate_bot_id(account_id)
    return ensure_dir(
        Path(root) / "center_dispatcher_state" / "fail_counts"
    ) / ("%s.json" % account_id)


def _bump_failure(root, account_id, task_id):
    path = _fail_counts_path(root, account_id)
    data = read_json(path)
    if not isinstance(data, dict):
        data = {}
    entry = data.get(task_id)
    count = (int(entry.get("count") or 0) + 1) if isinstance(entry, dict) else 1
    data[task_id] = {"count": count, "last_at": now_iso()}
    atomic_write_json(path, data)
    return count


def _clear_failure(root, account_id, task_id):
    path = _fail_counts_path(root, account_id)
    data = read_json(path)
    if isinstance(data, dict) and task_id in data:
        del data[task_id]
        atomic_write_json(path, data)


# --- Danh thuc khong ack + tam lui viec (cai tien 28/11) -----------
# Claim "reserved" qua han cho ack (worker khong xac nhan bat dau)
# bi giai phong trong buoc don truoc khi tinh cap; moi lan nhu vay
# ghi 1 "wake_no_ack" cho viec. Sau WAKE_NO_ACK_MAX lan lien tiep,
# viec tam lui DEFER_SECONDS de viec khac di tiep (khong doi trang
# thai tren hub, khong unknown). Worker ack thanh cong (claim
# running hoac viec da qua trang thai Moi) thi xoa bo dem.
WAKE_NO_ACK_MAX = 3
WAKE_DEFER_SECONDS = 900  # tam lui 15 phut sau 3 lan khong ack


def _wake_state_path(root, account_id):
    ids.validate_bot_id(account_id)
    return ensure_dir(
        Path(root) / "center_dispatcher_state" / "wake_no_ack"
    ) / ("%s.json" % account_id)


def _load_wake_state(root, account_id):
    data = read_json(_wake_state_path(root, account_id))
    return data if isinstance(data, dict) else {}


def _save_wake_state(root, account_id, state):
    atomic_write_json(_wake_state_path(root, account_id), state)


def _sweep_reserved_claims(root, account_id, tasks, now, log_fn):
    """Don claim reserved qua han ack + cap nhat bo dem wake_no_ack.

    Tra ve (so claim da giai phong, tap task_id dang tam lui)."""
    state = _load_wake_state(root, account_id)
    status_by_task = {}
    for item in tasks:
        record = item.get("record") or {}
        status_by_task[item.get("task_id")] = record.get("status")
    released = 0
    changed = False
    for info in center_claims.list_claims(
            root, account_id=account_id, now=now):
        task_id = info["task_id"]
        if info.get("phase") == center_claims.PHASE_RUNNING:
            if task_id in state:
                del state[task_id]
                changed = True
            continue
        if not info.get("reserved_expired"):
            continue
        # Viec da qua trang thai Moi nghia la worker da bat dau
        # that (ack bang trang thai) — khong tinh wake_no_ack.
        if status_by_task.get(task_id) not in (None, "moi"):
            center_claims.release(root, account_id, task_id)
            if task_id in state:
                del state[task_id]
                changed = True
            continue
        center_claims.release(root, account_id, task_id)
        released += 1
        entry = state.get(task_id)
        count = (int(entry.get("count") or 0) + 1) \
            if isinstance(entry, dict) else 1
        entry = {"count": count, "last_at": now}
        if count >= WAKE_NO_ACK_MAX:
            entry["deferred_until"] = now + WAKE_DEFER_SECONDS
        state[task_id] = entry
        changed = True
        log_fn("center_dispatcher account=%s viec=%s: danh thuc "
               "khong ack lan %d, da giai phong giu cho"
               % (account_id, task_id, count))
    deferred = set()
    fail_counts = {}
    for task_id, entry in list(state.items()):
        if not isinstance(entry, dict):
            continue
        fail_counts[task_id] = int(entry.get("count") or 0)
        until = entry.get("deferred_until")
        if isinstance(until, (int, float)) and until > now:
            deferred.add(task_id)
        elif status_by_task.get(task_id) not in (None, "moi"):
            del state[task_id]
            fail_counts.pop(task_id, None)
            changed = True
    if changed:
        _save_wake_state(root, account_id, state)
    return released, deferred, fail_counts


# --- Nhanh chat (v0.6.0, T3h — DESIGN §3.9, doc 10 §6.6) -----------

CHAT_STATE_DIRNAME = "center_chat_state"
CHAT_STUCK_WAIT_SECONDS = 180  # D4: tin cho >3 phut
CHAT_STUCK_MIN_COUNT = 3  # D4: >=3 tin/phong
CHAT_STUCK_LEVEL = "cần xem"  # D4: muc "can xem" trong tab Canh bao
CHAT_STUCK_CLAIM_GRACE_SECONDS = 60  # G-H5: claim phong moi hon
# thi coi nhu vua lap ke hoach trong chu ky nay (worker chua kip
# chay) — van bao tac; claim da giu qua grace thi coi nhu worker
# dang lam (vd doc file lau) — khong bao tac oan.
DISCUSSION_ROUND_LIMIT = 5  # W7: vong thao luan cham 5 luot thi dung


def chat_state_path(root, account_id):
    ids.validate_bot_id(account_id)
    return Path(root) / CHAT_STATE_DIRNAME / ("%s.json" % account_id)


def read_chat_state(root, account_id):
    """Doc file moc da tra loi theo phong (CHI DOC — dispatcher
    khong bao gio tu ghi moc, khong tao thu muc; GC-13). File
    thieu/hong -> {"rooms": {}, "discussion_rounds": {}}."""
    data = read_json(chat_state_path(root, account_id))
    if not isinstance(data, dict):
        return {"rooms": {}, "discussion_rounds": {}}
    rooms = data.get("rooms")
    rounds = data.get("discussion_rounds")
    return {
        "rooms": rooms if isinstance(rooms, dict) else {},
        "discussion_rounds": rounds if isinstance(rounds, dict) else {},
    }


def replied_up_to_seq(state, room):
    """Moc da tra loi cua 1 phong (mac dinh 0). Tin co seq <= moc
    thi coi la da tra loi (W2)."""
    rooms = state.get("rooms") or {}
    entry = rooms.get(room)
    if isinstance(entry, dict):
        value = entry.get("replied_up_to_seq")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def read_local_messages(root):
    """Moi tin local trong center_messages.jsonl, theo seq tang dan.
    Tra ve list {"seq", "at", "msg_id", "record"} (chi entry hop le)."""
    path = Path(root) / "center_messages.jsonl"
    entries, _skipped = read_jsonl(path)
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record = entry.get("record")
        if not isinstance(record, dict):
            continue
        seq = entry.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        out.append({"seq": seq, "at": entry.get("at"),
                    "msg_id": entry.get("msg_id"), "record": record})
    out.sort(key=lambda e: e["seq"])
    return out


def read_task_source_msg_ids(root):
    """Tap msg_id da sinh viec (W6) — lay tu source_msg_id cua moi
    viec local."""
    out = set()
    for item in read_local_tasks(root):
        value = item["record"].get("source_msg_id")
        if isinstance(value, str) and value:
            out.add(value)
    return out


def _as_account_list(value):
    """Chuan hoa to_account/mentions (chuoi v3 / danh sach v4) -> list."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, str) and item]
    return []


def _is_discussion_message(record, account_id):
    """Tin thao luan (T3l — DESIGN §3.10): tai khoan khac gui, ghi ro
    to_account chua minh. Tin cua chu nhan (from_account="owner")
    khong phai tin thao luan."""
    sender = record.get("from_account")
    if not isinstance(sender, str) or sender in (account_id, "owner"):
        return False
    return account_id in _as_account_list(record.get("to_account"))


def _discussion_round_blocked(rounds, room, record):
    """W7 (T3l — D9, Q-V060-31 ĐÃ CHỐT): vòng của CHÍNH tin này đã
    đóng ở 5 lượt thì chặn. Chỉ xét vòng mà tin thuộc về (khóa theo
    (phòng, người mở vòng, tập người tham gia)); vòng khác trong
    cùng phòng — dù đã đóng — không chặn tin này. Tin chưa thuộc
    vòng nào (vòng mới, lượt 1) thì không chặn."""
    if not isinstance(rounds, dict) or not rounds:
        return False
    sender = record.get("from_account")
    to_accounts = _as_account_list(record.get("to_account"))
    if not isinstance(sender, str) or not sender or not to_accounts:
        return False
    return center_discussion.is_round_closed_for_message(
        rounds, room, sender, to_accounts)


def check_chat_wake(record, seq, account_id, ctx):
    """Kiem tra W1–W7 + W10 cho 1 tin. W8 (claim phong) + W9 (cap
    worker) KHONG kiem o day — 2 dieu kien nay phu thuoc phong/tai
    khoan, kiem o tang chon phong (plan_chat_for_account).

    ctx = {"replied_up_to_seq": int, "source_msg_ids": set,
           "discussion_rounds": dict, "participating": bool,
           "discussion_enabled": bool}
    Tra ve list cac ma W khong dat (rong = dat het).

    - W4: phong rieng cua minh (room == "dm:<minh>") thi dat; khung
      chung thi phai tag ro minh — tag = to_account chua minh, HOAC
      @mention trong body chua minh NHUNG CHI khi tin do chu nhan
      gui (from_account == "owner") VA to_account rong/khong co
      (D16, AUDIT-V060-LAN2): tin cua chu nhan qua UI mang
      to_account=None nen chi co mentions moi goi duoc ten — xem
      ui_app._handle_center_message; chu nhan da ghi ro to_account
      thi chi nguoi trong to_account duoc wake (quyet §5b, y do D8).
      Tin tu tai khoan Muse thi CHI to_account moi tinh (NS-H1,
      AUDIT-V060: mentions cua tai khoan khac khong duoc danh thuc
      them tai khoan bi @).
    - W5 (D3 — QUYET-DINH-QUA-DEM-20-11): tin hub tu sinh nhan bang
      truong record["source"] == "hub"; THIEU truong source = KHONG
      phai tin hub.
    - W7/W10 (thao luan): chi ap cho tin thao luan (xem
      _is_discussion_message); tin thuong luon dat 2 dieu kien nay.
    """
    failed = []
    if record.get("kind") != "hoi":
        failed.append("W1")
    if not (isinstance(seq, int) and not isinstance(seq, bool)
            and seq > int(ctx.get("replied_up_to_seq") or 0)):
        failed.append("W2")
    if record.get("from_account") == account_id:
        failed.append("W3")
    room = record.get("room")
    if room == "dm:%s" % account_id:
        pass
    elif room in (None, "chung"):
        # NS-H1 + D16: mentions chi tinh khi tin do chu nhan gui
        # (from_account == "owner") VA to_account rong/khong co;
        # chu nhan da ghi ro to_account thi chi nguoi trong do duoc
        # wake (quyet §5b, y do D8). Tin tu tai khoan Muse thi chi
        # to_account moi tinh — tranh 1 tin danh thuc them tai khoan
        # chi bi @ trong body.
        tagged = account_id in _as_account_list(
            record.get("to_account"))
        if (not tagged and record.get("from_account") == "owner"
                and not _as_account_list(record.get("to_account"))):
            tagged = account_id in _as_account_list(
                record.get("mentions"))
        if not tagged:
            failed.append("W4")
    else:
        failed.append("W4")
    if record.get("source") == "hub":
        failed.append("W5")
    msg_id = record.get("msg_id")
    if isinstance(msg_id, str) and msg_id \
            and msg_id in (ctx.get("source_msg_ids") or set()):
        failed.append("W6")
    is_discussion = _is_discussion_message(record, account_id)
    if is_discussion and _discussion_round_blocked(
            ctx.get("discussion_rounds") or {}, room, record):
        failed.append("W7")
    if not ctx.get("participating", True):
        failed.append("W10")
    elif is_discussion and not ctx.get("discussion_enabled", True):
        failed.append("W10")
    return failed


def _wake_eligible_by_room(root, account_id, deps, now):
    """Tin du dieu kien wake (W1–W7 + W10), gom theo phong.
    Tra ve {room: [entry, ...]} (moi list theo seq tang dan).

    T3l: tinh lai vong thao luan tu tin nhan truoc khi xet W7
    (``center_discussion.refresh_discussion_rounds`` — idempotent;
    chi ghi khoa ``discussion_rounds``, khong cham moc ``rooms``).
    Tin nhieu nguoi nhan (``to_account`` 2+ tai khoan) thi moi tai
    khoan duoc tag deu thay tin nay trong ke hoach cua rieng minh
    (moi tai khoan 1 plan rieng, chung 1 bo dem — 1 tin = 1 luot)."""
    state = read_chat_state(root, account_id)
    refreshed = center_discussion.refresh_discussion_rounds(
        root, account_id)
    state = {"rooms": state.get("rooms") or {},
             "discussion_rounds": refreshed}
    source_ids = read_task_source_msg_ids(root)
    participating_fn = (deps or {}).get("participating_fn")
    discussion_fn = (deps or {}).get("discussion_enabled_fn")
    participating = True if participating_fn is None \
        else bool(participating_fn(account_id))
    discussion_enabled = True if discussion_fn is None \
        else bool(discussion_fn(account_id))
    by_room = {}
    for entry in read_local_messages(root):
        record = entry["record"]
        room = record.get("room") or "chung"
        if not isinstance(room, str):
            continue
        ctx = {
            "replied_up_to_seq": replied_up_to_seq(state, room),
            "source_msg_ids": source_ids,
            "discussion_rounds": state.get("discussion_rounds") or {},
            "participating": participating,
            "discussion_enabled": discussion_enabled,
        }
        if check_chat_wake(record, entry["seq"], account_id, ctx):
            continue
        by_room.setdefault(room, []).append(entry)
    return by_room


def plan_chat_for_account(root, account_id, deps=None, now=None):
    """Lap ke hoach tra loi chat cho 1 tai khoan: toi da 1 phong —
    phong co tin cu nhat len dau. Giu claim phong (W8, thue 15
    phut) khi chon; hook giai phong claim cua plan khong duoc wake
    ngay trong chu ky (D15, phuong an (A) — chu ky sau lap lai ke
    hoach moi, chi cham ~10 giay); plan duoc wake thi worker giu
    claim va tu go khi tra loi xong. Tra ve None khi khong co phong
    nao du dieu kien, hoac dict {"account_id", "room", "seqs",
    "kind": "center_chat", "holder": <holder claim phong>}. CHI DOC
    file moc — khong ghi moc, khong tao thu muc trang thai."""
    deps = deps or {}
    log_fn = deps.get("log_fn") or _default_log
    if now is None:
        now = now_epoch()
    cap = deps.get("worker_cap", WORKER_CAP_PER_ACCOUNT)
    running_fn = deps.get("running_counts_fn") or (
        lambda acc: center_claims.count_running(root, acc, now=now))
    # W9 (S2 — tach lan chat): chat chi cho khi LAN CHAT da co
    # worker dang chay (cap chat rieng) hoac TONG worker trung tam
    # da cham tran tong (cap viec + cap chat). Worker viec dang
    # chay KHONG con chan chat nhu luat gop chung truoc S2.
    total_cap = cap + CHAT_WORKER_CAP
    total_running = running_fn(account_id)
    _task_running, chat_running = \
        center_claims.count_running_by_kind(root, account_id, now=now)
    if chat_running >= CHAT_WORKER_CAP or total_running >= total_cap:
        log_fn(
            "center_dispatcher account=%s: worker cap lan chat "
            "(chat dang chay=%d, cap chat=%d, tong dang chay=%d, "
            "tran tong=%d), chat cho"
            % (account_id, chat_running, CHAT_WORKER_CAP,
               total_running, total_cap))
        return None
    by_room = _wake_eligible_by_room(root, account_id, deps, now)
    if not by_room:
        return None
    rooms = sorted(by_room, key=lambda r: by_room[r][0]["seq"])
    for room in rooms:
        # W8: phong dang co worker chat giu thi bo qua, thu phong sau.
        if center_claims.is_room_claimed(root, account_id, room, now=now):
            continue
        holder = "center_dispatcher:%d" % int(now)
        if not center_claims.claim_room(
                root, account_id, room, holder, now=now):
            continue
        seqs = [entry["seq"] for entry in by_room[room]]
        return {"account_id": account_id, "room": room,
                "seqs": seqs, "kind": "center_chat",
                "holder": holder}
    return None


def _message_age_seconds(entry, now):
    """So giay tin da cho (tu created_at), khong parse duoc -> None."""
    record = entry["record"]
    created = record.get("created_at") or entry.get("at")
    epoch = iso_to_epoch(created) if isinstance(created, str) else None
    if epoch is None:
        return None
    return now - epoch


def _room_held_by_working_worker(root, account_id, room, now):
    """True khi phong dang co claim CON HAN va da giu qua grace
    (G-H5): worker presumably dang tra loi (vd doc file lau) nen
    khong bao "tac". Claim moi hon grace la claim vua lap ke hoach
    trong chu ky nay — worker chua chay (hook wake sau ~10 giay),
    van bao tac; neu khong thi ``run_planning`` (lap ke hoach roi
    moi kiem tra tac trong cung chu ky) se khong bao gio bao tac
    duoc, va trieu chung [CHẶN] (plan giu claim nhung hook khong
    wake) cung bi che mat. Moc giu lay tu ``claimed_at`` trong file
    claim; khong doc duoc moc thi ap dung luat G-H5 goc (co claim
    la loai)."""
    if not center_claims.is_room_claimed(root, account_id, room, now=now):
        return False
    info = center_claims.holder_of(
        root, account_id, center_claims.chat_room_key(room))
    claimed_at = info.get("claimed_at") if isinstance(info, dict) \
        else None
    if not isinstance(claimed_at, (int, float)) \
            or isinstance(claimed_at, bool):
        return True
    return now - claimed_at >= CHAT_STUCK_CLAIM_GRACE_SECONDS


def detect_stuck_chats(root, account_id, deps=None, now=None):
    """Chat tac (D4 — QUYET-DINH-QUA-DEM-20-11): phong co tin cho
    tra loi (du W1–W7 + W10, chua tinh W9 — cap chi lam cham, khong
    phai tac) ma tin cu nhat cho >3 phut HOAC >=3 tin/phong. Phong
    dang co claim da giu qua grace (G-H5 — worker dang tra loi)
    thi LOAI KHOI danh sach tac. Tra ve list {"account_id", "room",
    "waiting_count", "oldest_seq", "oldest_wait_seconds", "level":
    "cần xem"} — chi metadata, khong mang noi dung tin. Phong xep
    theo tin cu nhat."""
    if now is None:
        now = now_epoch()
    by_room = _wake_eligible_by_room(root, account_id, deps or {}, now)
    out = []
    for room, entries in by_room.items():
        # G-H5: phong dang co claim cu (worker dang tra loi) thi
        # khong phai tac — tin cho trong luc worker dang lam (vd
        # doc file lau) khong sinh canh bao "tac" oan.
        if _room_held_by_working_worker(root, account_id, room, now):
            continue
        oldest = entries[0]
        age = _message_age_seconds(oldest, now)
        waiting = len(entries)
        if not ((age is not None and age > CHAT_STUCK_WAIT_SECONDS)
                or waiting >= CHAT_STUCK_MIN_COUNT):
            continue
        out.append({
            "account_id": account_id,
            "room": room,
            "waiting_count": waiting,
            "oldest_seq": oldest["seq"],
            "oldest_wait_seconds": int(age) if age is not None else None,
            "level": CHAT_STUCK_LEVEL,
        })
    out.sort(key=lambda item: item["oldest_seq"])
    return out


def _chat_stuck_reported_path(root):
    return Path(root) / "center" / "chat_stuck_reported.json"


def _chat_stuck_alerts_path(root):
    return Path(root) / "center" / "chat_stuck_alerts.jsonl"


def _wait_text(wait_seconds):
    if wait_seconds is None:
        return "không rõ bao lâu"
    minutes = int(wait_seconds) // 60
    if minutes < 1:
        return "%d giây" % int(wait_seconds)
    return "%d phút" % minutes


def record_chat_stuck_alerts(root, stuck, now=None, notify_fn=None):
    """Ghi dong canh bao chat tac (dedup theo (tai khoan, phong) +
    seq cu nhat — 1 dot tac chi ghi 1 dong; dot moi (seq cu nhat
    doi) thi ghi tiep). Mac dinh ghi vao
    center/chat_stuck_alerts.jsonl (hang cho K4/UI doc — cung mau
    center_overdue). Tra ve so dong vua ghi."""
    if now is None:
        now = now_epoch()
    reported = read_json(_chat_stuck_reported_path(root))
    if not isinstance(reported, dict):
        reported = {}
    written = 0
    changed = False
    for item in stuck or []:
        key = "%s|%s" % (item["account_id"], item["room"])
        if reported.get(key) == item["oldest_seq"]:
            continue
        alert = {
            "at": epoch_to_iso(now),
            "account_id": item["account_id"],
            "room": item["room"],
            "level": CHAT_STUCK_LEVEL,
            "waiting_count": item["waiting_count"],
            "oldest_seq": item["oldest_seq"],
            "oldest_wait_seconds": item.get("oldest_wait_seconds"),
            "text": ("Chat tắc: phòng %s có %d tin chờ trả lời, tin cũ "
                     "nhất chờ %s (mức cần xem)"
                     % (item["room"], item["waiting_count"],
                        _wait_text(item.get("oldest_wait_seconds")))),
        }
        if notify_fn is not None:
            notify_fn(alert)
        else:
            append_jsonl(_chat_stuck_alerts_path(root), alert)
        reported[key] = item["oldest_seq"]
        changed = True
        written += 1
    if changed:
        atomic_write_json(_chat_stuck_reported_path(root), reported)
    return written


# --- Cảnh báo cap kẹt + assignee chậm (Fix gộp 21/11, §4.2 mục 3)
# Ngưỡng đã chốt (§4.3, giữ đúng số): cap kẹt bắn alert khi ≥3 chu
# kỳ liên tiếp (~55s; chu kỳ hook thực đo trung vị 18,4s); assignee
# chậm >5 phút chưa báo; cooldown DM 30 phút/đợt/loại/tài khoản;
# file alert là kênh chính, DM qua outbox trực tiếp (không cần
# worker). Trạng thái bền theo mẫu record_chat_stuck_alerts:
# center/cap_stuck_state.json (đếm chu kỳ kẹt liên tiếp + mốc
# first-seen assignee chậm) + center/cap_alerts_reported.json
# (dedup DM) — auth dùng file dedup riêng
# center/auth_alerts_reported.json (xem center_outbox.record_alert
# + center_fetcher._note_auth_failure). Luật re-arm: hết kẹt thì
# xóa entry dedup để đợt mới lại báo.

CAP_STUCK_ALERT_AFTER_CYCLES = 3  # ≥3 chu kỳ liên tiếp thì bắn alert
SLOW_ASSIGNEE_SECONDS = 300  # assignee >5 phút chưa báo thì chậm


def _cap_state_path(root):
    return Path(root) / "center" / "cap_stuck_state.json"


def _load_cap_state(root):
    """{"cap_consec": int, "slow_first_seen": {"task|assignee": epoch}}.
    File thiếu/hỏng -> trạng thái rỗng."""
    data = read_json(_cap_state_path(root))
    if not isinstance(data, dict):
        return {"cap_consec": 0, "slow_first_seen": {}}
    consec = data.get("cap_consec")
    first_seen = data.get("slow_first_seen")
    return {
        "cap_consec": consec if isinstance(consec, int)
        and not isinstance(consec, bool) else 0,
        "slow_first_seen": first_seen if isinstance(first_seen, dict)
        else {},
    }


def _save_cap_state(root, state):
    atomic_write_json(_cap_state_path(root), state)


def _fire_alert(root, account_id, alert_kind, dedup_key, text,
                detail=None):
    """Ghi alert qua đường cảnh báo chung (file + DM dedup 30 phút).
    Lỗi đường cảnh báo không được làm hỏng chu kỳ dispatcher."""
    try:
        import center_outbox  # noqa: PLC0415 - tránh import vòng
        return center_outbox.record_alert(
            root, account_id, alert_kind, dedup_key, text, detail=detail)
    except Exception:  # noqa: BLE001 - alert là phụ, chu kỳ là chính
        return False


def _clear_alert_dedup(root, alert_kind, dedup_key):
    """Xóa 1 entry dedup (luật re-arm)."""
    try:
        import center_outbox  # noqa: PLC0415 - tránh import vòng
        center_outbox.clear_alert_reported(root, alert_kind, dedup_key)
    except Exception:  # noqa: BLE001 - dedup chỉ là chống lặp
        pass


def _note_cap_blocked(root, account_id):
    """Tăng bộ đếm chu kỳ kẹt cap liên tiếp, trả về số mới."""
    state = _load_cap_state(root)
    consec = int(state.get("cap_consec") or 0) + 1
    state["cap_consec"] = consec
    _save_cap_state(root, state)
    return consec


def _reset_cap_stuck(root, account_id):
    """Hết kẹt: bộ đếm về 0 + xóa entry dedup cap (re-arm) để đợt
    kẹt mới sau đó lại báo."""
    state = _load_cap_state(root)
    if int(state.get("cap_consec") or 0):
        state["cap_consec"] = 0
        _save_cap_state(root, state)
    _clear_alert_dedup(root, "cap_stuck", "cap_stuck|%s" % account_id)


def _check_slow_assignees(root, account_id, own, now, log_fn):
    """Assignee chậm (S2-e phía Muse): trên mỗi việc đang mở của
    mình, assignee nào chưa báo xong quá 5 phút (tính từ lần đầu
    thấy việc đó mở + người đó chưa báo trong chu kỳ này — mốc
    first-seen lưu theo (việc, tài khoản) trong cap_stuck_state)
    thì bắn 1 alert (dedup 30 phút/đợt). Người đã báo / việc không
    còn mở thì dọn mốc + xóa dedup (re-arm). Không đổi luật P2:
    việc KHÔNG tự chốt khi thiếu người, chỉ cảnh báo."""
    state = _load_cap_state(root)
    first_seen = state.get("slow_first_seen") or {}
    if not isinstance(first_seen, dict):
        first_seen = {}
    changed = False
    live_keys = set()
    for item in own:
        record = item["record"]
        reported = set(_reported_by_list(record.get("reported_by")))
        for assignee in _assignee_list(record.get("assigned_to")):
            if assignee in reported:
                continue
            key = "%s|%s" % (item["task_id"], assignee)
            live_keys.add(key)
            seen = first_seen.get(key)
            if not isinstance(seen, (int, float)) \
                    or isinstance(seen, bool):
                first_seen[key] = now
                changed = True
                continue
            if now - seen > SLOW_ASSIGNEE_SECONDS:
                if _fire_alert(
                        root, account_id, "cap_stuck",
                        "slow|%s" % key,
                        "Cảnh báo chậm: tài khoản %s chưa báo xong "
                        "việc %s sau hơn 5 phút (việc đang mở, còn "
                        "chờ người này)." % (assignee, item["task_id"]),
                        detail={"task_id": item["task_id"],
                                "assignee": assignee,
                                "wait_seconds": int(now - seen)}):
                    log_fn("center_dispatcher account=%s: assignee "
                           "%s cham tren viec %s (>5 phut)"
                           % (account_id, assignee, item["task_id"]))
    for key in list(first_seen):
        if key not in live_keys:
            del first_seen[key]
            changed = True
            _clear_alert_dedup(root, "cap_stuck", "slow|%s" % key)
    if changed:
        state["slow_first_seen"] = first_seen
        _save_cap_state(root, state)


# --- Chu kỳ chính (1 tài khoản) ---

def run_cycle_for_account(root, account_id, deps=None, now=None):
    """Chạy 1 chu kỳ dispatcher cho 1 tài khoản. Trả về dict thống kê
    (chỉ số đếm + task_id đã đánh thức + holder claim việc vừa giữ,
    không nội dung việc). ``woken_holder`` (D17, phuong an (A)) để
    planner mang holder vào plan ``center_tasks`` cho hook đối chiếu
    khi giải phóng claim việc của plan không được wake — đối xứng
    với ``holder`` trong plan ``center_chats`` (D15)."""
    deps = deps or {}
    root = Path(root)
    log_fn = deps.get("log_fn") or _default_log
    alert_fn = deps.get("alert_fn") or _default_alert
    worker_fn = deps.get("worker_fn")
    if now is None:
        now_fn = deps.get("now_fn")
        now = now_fn() if callable(now_fn) else now_epoch()
    cap = deps.get("worker_cap", WORKER_CAP_PER_ACCOUNT)
    running_counts_fn = deps.get("running_counts_fn") or (
        lambda acc: center_claims.count_running(root, acc, now=now)
    )

    summary = {
        "account_id": account_id,
        "tasks_seen": 0,
        "own_tasks": 0,
        "other_tasks_read_only": 0,
        "woken": 0,
        "woken_task_id": None,
        "woken_holder": None,  # D17: holder claim viec vua giu
        "cap_blocked": False,
        "cap_consec": 0,  # chu kỳ kẹt cap liên tiếp (Fix 21/11)
        "terminal_synced": 0,
        "reported_synced": 0,  # claim đã gỡ trên việc đã báo xong
        "boot_unknown": 0,
        "poison_unknown": 0,
    }

    boot_unknown = _check_boot(root, account_id, deps, now, log_fn, alert_fn)
    summary["boot_unknown"] = len(boot_unknown)

    tasks = read_local_tasks(root)
    summary["tasks_seen"] = len(tasks)
    summary["terminal_synced"] = _sync_terminal_states(
        root, account_id, tasks, log_fn)
    # (Fix gộp 21/11, §4.2 mục 2) Gỡ claim trên việc tài khoản đã
    # báo xong — TRƯỚC khi tính danh sách ứng viên, để claim đó
    # không chiếm cap trong chính chu kỳ này.
    summary["reported_synced"] = _sync_reported_done(
        root, account_id, tasks, log_fn)
    # (Cai tien 28/11) Don giu cho reserved qua han ack TRUOC khi
    # tinh cap: giu cho khong ack khong con khoa suat cap 15 phut.
    swept, deferred_tasks, wake_fail_counts = _sweep_reserved_claims(
        root, account_id, tasks, now, log_fn)
    summary["reserved_swept"] = swept
    summary["deferred_tasks"] = sorted(deferred_tasks)

    own = []
    for item in tasks:
        record = item["record"]
        if account_id not in _assignee_list(record.get("assigned_to")):
            # Việc của tài khoản khác: CHỈ ĐỌC — không claim, không
            # đánh thức worker, không ghi ledger cho việc đó (TT10).
            summary["other_tasks_read_only"] += 1
            continue
        if record.get("status") in TERMINAL_STATUSES:
            continue
        # (Fix gộp 21/11, §4.2 mục 1 — S1-A) Việc mà tài khoản đã
        # báo xong thì KHÔNG còn là ứng viên "own" của tài khoản
        # đó nữa (dù trạng thái chung vẫn "da-bao-xong" vì còn
        # người khác chưa báo — Q-FIX-01: KHÔNG thêm da-bao-xong
        # vào TERMINAL_STATUSES toàn cục).
        if account_id in _reported_by_list(record.get("reported_by")):
            continue
        if center_ledger.is_terminal(root, account_id, item["task_id"]):
            continue
        if center_ledger.is_held(root, account_id, item["task_id"]):
            continue
        own.append(item)
        # Ghi received 1 lần khi thấy việc của mình lần đầu.
        center_ledger.record_once(
            root, account_id, item["task_id"], "received",
            detail={"status": record.get("status")})
    summary["own_tasks"] = len(own)
    if not own:
        # Không còn việc mở nào của mình: không thể kẹt cap vì
        # việc nữa — reset bộ đếm + re-arm dedup (đợt mới lại báo).
        _reset_cap_stuck(root, account_id)
        return summary

    # (Fix gộp 21/11, §4.2 mục 3) Assignee chậm >5 phút trên việc
    # đang mở -> alert (không đổi luật P2).
    _check_slow_assignees(root, account_id, own, now, log_fn)

    # Worker cap riêng việc trung tâm (per-account, KHÔNG cap xuyên
    # tài khoản): vượt thì việc chờ, không đánh thức thêm.
    # (S2 — tach lan): viec bi chan khi LAN VIEC da day
    # (task_running >= cap) HOAC tong worker trung tam da cham
    # tran tong (cap + CHAT_WORKER_CAP). Worker chat dang chay
    # khong chiem cho cua viec, va nguoc lai.
    running = running_counts_fn(account_id)
    task_running, _chat_running = \
        center_claims.count_running_by_kind(root, account_id, now=now)
    total_cap = cap + CHAT_WORKER_CAP
    if task_running >= cap or running >= total_cap:
        log_fn(
            "center_dispatcher account=%s: worker cap viec trung tam "
            "(viec dang chay=%d, cap viec=%d, tong dang chay=%d, "
            "tran tong=%d), viec cho"
            % (account_id, task_running, cap, running, total_cap)
        )
        summary["cap_blocked"] = True
        # (Fix gộp 21/11, §4.2 mục 3) Cap kẹt ≥3 chu kỳ liên tiếp
        # -> alert file + DM (dedup 30 phút/đợt).
        consec = _note_cap_blocked(root, account_id)
        summary["cap_consec"] = consec
        if consec >= CAP_STUCK_ALERT_AFTER_CYCLES:
            if _fire_alert(
                    root, account_id, "cap_stuck",
                    "cap_stuck|%s" % account_id,
                    "Cảnh báo cap kẹt: tài khoản %s đang kẹt cap "
                    "worker (%d/%d) %d chu kỳ liên tiếp, không đánh "
                    "thức được worker mới. Việc đã báo xong có thể "
                    "đang chiếm chỗ; kiểm tra tab Cảnh báo."
                    % (account_id, running, total_cap, consec),
                    detail={"running": running, "cap": total_cap,
                            "consec_cycles": consec}):
                log_fn("center_dispatcher account=%s: cap ket %d chu "
                       "ky lien tiep, da ban alert"
                       % (account_id, consec))
        return summary
    # Không kẹt cap: reset bộ đếm + re-arm dedup.
    _reset_cap_stuck(root, account_id)

    # Chỉ claim việc cũ nhất chưa ai giữ; tối đa 1 việc/chu kỳ.
    # Viec dang tam lui vi danh thuc khong ack 3 lan lien tiep thi
    # bo qua trong vong nay de viec khac di tiep (cai tien 28/11).
    # Viec tung danh thuc khong ack xep sau viec chua tung that
    # bai (giu thu tu cu nhat trong cung so lan that bai) de mot
    # viec loi khong khoa hang doi.
    candidates = sort_wake_candidates(
        [item for item in own
         if item["task_id"] not in deferred_tasks],
        wake_fail_counts, account_id)
    candidate = center_claims.select_oldest_unclaimed(
        candidates,
        lambda tid: center_claims.is_claimed(root, account_id, tid, now=now),
    )
    if candidate is None:
        return summary
    task_id = candidate["task_id"]
    holder = "center_dispatcher:%d" % int(now)
    wake_id = "w-%s-%d" % (task_id, int(now))
    if not center_claims.claim(root, account_id, task_id, holder,
                               now=now, wake_id=wake_id):
        return summary
    if worker_fn is None:
        # Không có worker để giao: bỏ giữ ngay để chu kỳ sau xử lý
        # lại (không claim treo).
        center_claims.release(root, account_id, task_id)
        log_fn("center_dispatcher account=%s: thieu worker_fn, bo giu "
               "viec %s" % (account_id, task_id))
        return summary
    ctx = {
        "root": root,
        "account_id": account_id,
        "task": candidate["record"],
        "messages": read_messages_for_task(root, task_id),
        "now": now,
    }
    try:
        worker_fn(account_id, candidate, ctx)
        summary["woken"] = 1
        summary["woken_task_id"] = task_id
        summary["woken_holder"] = holder  # D17: de planner mang vao plan
        summary["woken_wake_id"] = wake_id  # worker ack dung luot nay
        _clear_failure(root, account_id, task_id)
    except Exception as exc:  # noqa: BLE001 - worker lỗi: đếm thư độc, không crash chu kỳ
        log_fn(
            "center_dispatcher account=%s viec=%s: worker loi class=%s"
            % (account_id, task_id, type(exc).__name__)
        )
        failures = _bump_failure(root, account_id, task_id)
        center_claims.release(root, account_id, task_id)
        if failures >= POISON_MAX_FAILURES:
            center_ledger.record(
                root, account_id, task_id, "unknown",
                detail={"reason": "poison_worker_failures",
                        "failures": failures},
            )
            alert_fn("center_poison_task", account_id,
                     {"task_id": task_id, "failures": failures})
            summary["poison_unknown"] = 1
    return summary


def run_cycle(root, deps=None, now=None):
    """Chạy 1 chu kỳ dispatcher cho mọi tài khoản có cấu hình trung
    tâm trên máy này (thường chỉ 1 tài khoản/VM). Tài khoản phát
    hiện CHỈ từ tên file trong ``center-config/`` — không đoán tài
    khoản từ việc local (tránh claim nhầm việc của tài khoản khác).

    Trả về {"accounts": {account_id: summary}}.
    """
    import center_fetcher

    deps = deps or {}
    log_fn = deps.get("log_fn") or _default_log
    account_ids = center_fetcher.discover_center_accounts(root)
    if not account_ids:
        log_fn("center_dispatcher: khong co tai khoan nao co cau hinh")
        return {"accounts": {}}
    results = {}
    for account_id in account_ids:
        results[account_id] = run_cycle_for_account(
            root, account_id, deps=deps, now=now)
    return {"accounts": results}
