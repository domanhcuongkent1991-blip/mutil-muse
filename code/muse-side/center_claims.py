"""Claim nguyên tử + lease + heartbeat cho việc trung tâm — theo mẫu
``claims.py`` của bot (``01`` §4), nhưng lane riêng.

Khác claims bot:

- Khóa là ``task_id`` (không phải ``event_key``); thư mục riêng
  ``center_claims/`` để boot-scan của dispatcher bot không nhầm việc
  trung tâm thành tin bot (và ngược lại).
- File claim: ``center_claims/<account_id>__<task_id>.json`` +
  heartbeat cạnh bên ``.heartbeat``.
- Lease 15 phút: claim CHỈ hết hạn khi CẢ file claim VÀ file
  heartbeat đều cũ quá 15 phút (như claims bot).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
from muse_common import ensure_dir, now_epoch, now_iso
from shared import ids

LEASE_SECONDS = 900  # 15 phút (01 §4, theo mẫu claims bot)
HEARTBEAT_INTERVAL_SECONDS = 300  # ~5 phút cho việc dài (01 §4)

# -- Claim hai pha (cai tien 28/11/2026, NGHIEN-CUU-CAP-CLAIM) -----
# Pha "reserved": dispatcher da giu cho de danh thuc worker, worker
# CHUA xac nhan bat dau. Pha "running": worker da ack dung wake_id
# va dang chay that. Giu cho khong ack sau ACK_WAIT_SECONDS thi het
# han (giai phong + ghi danh thuc khong thanh o dispatcher), khong
# khoa suat cap du 15 phut nhu lease dang chay. Claim cu khong co
# truong phase: co heartbeat -> "running", chua co -> "reserved".
ACK_WAIT_SECONDS = 300  # han cho worker ack sau khi duoc danh thuc
PHASE_RESERVED = "reserved"
PHASE_RUNNING = "running"


def claims_dir(root):
    return ensure_dir(Path(root) / "center_claims")


def _claim_filename(account_id, task_id):
    ids.validate_bot_id(account_id)  # account_id cùng mẫu bot_id
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id phải là chuỗi không rỗng")
    return "%s__%s" % (
        account_id, ids.sanitize_filename(task_id))


def claim_path(root, account_id, task_id):
    return claims_dir(root) / (
        "%s.json" % _claim_filename(account_id, task_id))


def heartbeat_path(root, account_id, task_id):
    return claims_dir(root) / (
        "%s.heartbeat" % _claim_filename(account_id, task_id))


def _file_age_seconds(path, now):
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def claim_phase(root, account_id, task_id):
    """Pha hien tai cua claim: "reserved" | "running" | None.

    Claim cu khong co truong phase: co file heartbeat -> "running"
    (worker da bat dau), chua co -> "reserved" (moi giu cho)."""
    info = holder_of(root, account_id, task_id)
    if info is None:
        return None
    phase = info.get("phase")
    if phase in (PHASE_RESERVED, PHASE_RUNNING):
        return phase
    if heartbeat_path(root, account_id, task_id).exists():
        return PHASE_RUNNING
    return PHASE_RESERVED


def is_reserved_expired(root, account_id, task_id, now=None):
    """True khi claim o pha reserved qua han cho ack ma chua co
    heartbeat nao (worker chua xac nhan bat dau)."""
    now = now_epoch() if now is None else now
    if claim_phase(root, account_id, task_id) != PHASE_RESERVED:
        return False
    if heartbeat_path(root, account_id, task_id).exists():
        return False
    claim_age = _file_age_seconds(
        claim_path(root, account_id, task_id), now)
    return claim_age is not None and claim_age > ACK_WAIT_SECONDS


def is_expired(root, account_id, task_id, now=None):
    """True khi claim đã hết hạn.

    Pha reserved chua ack: het han sau ACK_WAIT_SECONDS (khong cho
    du lease 15 phut). Pha running: CẢ 2 file đều cũ quá lease."""
    now = now_epoch() if now is None else now
    cpath = claim_path(root, account_id, task_id)
    if not cpath.exists():
        return False
    if is_reserved_expired(root, account_id, task_id, now=now):
        return True
    claim_age = _file_age_seconds(cpath, now)
    if claim_age is None:
        return False
    hpath = heartbeat_path(root, account_id, task_id)
    hb_age = _file_age_seconds(hpath, now)
    if hb_age is None:
        hb_age = claim_age
    return claim_age > LEASE_SECONDS and hb_age > LEASE_SECONDS


def is_claimed(root, account_id, task_id, now=None):
    """True khi đang có claim CÒN HẠN (người khác đang giữ)."""
    cpath = claim_path(root, account_id, task_id)
    if not cpath.exists():
        return False
    return not is_expired(root, account_id, task_id, now=now)


def claim(root, account_id, task_id, holder, now=None, wake_id=None):
    """Giữ 1 việc nguyên tử. Thắng -> True; đã có người giữ -> False.

    Người thua đứng ngoài hoàn toàn: không làm, không báo xong.
    Claim hết hạn thì được tiếp quản (xóa file cũ rồi tạo lại bằng
    O_EXCL). ``holder`` là chuỗi định danh người giữ — không chứa
    secret, không chứa nội dung việc.

    Claim moi luon o pha "reserved" kem ``wake_id`` cua luot danh
    thuc; worker phai ack_start() dung wake_id do moi chuyen sang
    "running" (cai tien 28/11).
    """
    now = now_epoch() if now is None else now
    cpath = claim_path(root, account_id, task_id)
    payload = json.dumps(
        {
            "task_id": task_id,
            "account_id": account_id,
            "holder": str(holder),
            "claimed_at": now,
            "phase": PHASE_RESERVED,
            "wake_id": wake_id,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    for _attempt in range(2):
        try:
            fd = os.open(cpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if is_expired(root, account_id, task_id, now=now):
                _remove_claim_files(root, account_id, task_id)
                continue
            return False
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.utime(cpath, (now, now))
        except OSError:
            pass
        return True
    return False


def _remove_claim_files(root, account_id, task_id):
    # (Fix gộp 21/11, §4.2 mục 5 — audit log) Mọi lần xóa file claim
    # đều ghi 1 dòng ra stderr: holder + task + thời điểm + pid gọi
    # — để lần sau truy vết được tác nhân gỡ sớm (§2.2: 3 lần gỡ
    # sớm hơn lease chưa kiểm chứng được). Bao cả đường hook release
    # (release-task/release-room CLI đều đi qua release() dưới đây)
    # và ca tiếp quản khi hết hạn (claim() gọi hàm này khi takeover).
    # Chỉ ghi khi thật sự có file claim (xóa file heartbeat lẻ kèm
    # theo không ghi dòng riêng).
    cpath = claim_path(root, account_id, task_id)
    existed = cpath.exists()
    holder = None
    if existed:
        try:
            info = holder_of(root, account_id, task_id)
        except ValueError:
            info = None
        if isinstance(info, dict):
            holder = info.get("holder")
    for path in (cpath,
                 heartbeat_path(root, account_id, task_id)):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if existed:
        print("center_claims release: holder=%s task=%s account=%s "
              "at=%s pid=%d"
              % (holder, task_id, account_id, now_iso(), os.getpid()),
              file=sys.stderr, flush=True)


def ack_start(root, account_id, task_id, wake_id=None, now=None):
    """Worker xac nhan da bat dau dung luot danh thuc: chuyen claim
    tu "reserved" sang "running" + ghi heartbeat dau tien.

    ``wake_id`` truyen vao phai khop wake_id trong file claim (khi
    claim co wake_id); lech -> False, worker phai dung ngoai (luot
    cu da het han/bi thay). Claim cu khong co wake_id thi ack duoc
    ma khong can wake_id. Tra ve True khi ack thanh cong."""
    now = now_epoch() if now is None else now
    info = holder_of(root, account_id, task_id)
    if info is None:
        return False
    current_wake = info.get("wake_id")
    if current_wake is not None and wake_id != current_wake:
        return False
    if info.get("phase") == PHASE_RUNNING:
        return heartbeat(root, account_id, task_id, now=now)
    info = dict(info)
    info["phase"] = PHASE_RUNNING
    info["acked_at"] = now
    cpath = claim_path(root, account_id, task_id)
    tmp_path = cpath.with_suffix(".json.ack-tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        try:
            os.write(fd, json.dumps(
                info, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, cpath)
        try:
            os.utime(cpath, (now, now))
        except OSError:
            pass
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return heartbeat(root, account_id, task_id, now=now)


def heartbeat(root, account_id, task_id, now=None, wake_id=None):
    """Gia hạn lease: ghi/touch file heartbeat. Trả về False nếu
    không có file claim (không heartbeat cho việc không ai giữ).

    ``wake_id`` (neu truyen) phai khop wake_id cua claim; lech ->
    False (worker cua luot cu khong gia han duoc claim luot moi)."""
    now = now_epoch() if now is None else now
    if not claim_path(root, account_id, task_id).exists():
        return False
    if wake_id is not None:
        info = holder_of(root, account_id, task_id)
        if info is None or info.get("wake_id") != wake_id:
            return False
    hpath = heartbeat_path(root, account_id, task_id)
    try:
        fd = os.open(hpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"heartbeat_at": now}).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return False
    try:
        os.utime(hpath, (now, now))
    except OSError:
        pass
    return True


def release(root, account_id, task_id):
    """Bỏ giữ: xóa file claim + heartbeat. Trả về True nếu đã có claim.

    (Fix gộp 21/11, §4.2 mục 5) Mỗi lần xóa file claim thật sự đều
    ghi 1 dòng audit ra stderr (holder + task + thời điểm + pid)."""
    existed = claim_path(root, account_id, task_id).exists()
    _remove_claim_files(root, account_id, task_id)
    return existed


def ack(root, account_id, task_id):
    """Ack 1 việc: bỏ giữ (xóa claim + heartbeat). Sổ sách do caller
    ghi — ack ở đây chỉ là bỏ giữ."""
    return release(root, account_id, task_id)


def holder_of(root, account_id, task_id):
    """Đọc nội dung file claim (dict) hoặc None nếu không có/hỏng."""
    try:
        with open(claim_path(root, account_id, task_id), "r",
                   encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_claims(root, account_id=None, now=None):
    """Liệt kê mọi claim trung tâm: [{account_id, task_id, claim_age,
    heartbeat_age, expired}] — dùng cho boot-scan + đếm worker cap.

    ``account_id`` None = liệt kê mọi tài khoản (chỉ dùng khi đếm
    tổng; cap việc trung tâm KHÔNG cap xuyên tài khoản nên caller
    phải lọc theo tài khoản khi đếm cap).
    """
    now = now_epoch() if now is None else now
    out = []
    directory = Path(root) / "center_claims"
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        stem = path.name[: -len(".json")]
        if "__" not in stem:
            continue
        owner, task_id = stem.split("__", 1)
        if account_id is not None and owner != account_id:
            continue
        hb = directory / ("%s.heartbeat" % stem)
        claim_age = _file_age_seconds(path, now)
        hb_age = _file_age_seconds(hb, now)
        if hb_age is None:
            hb_age = claim_age
        info = holder_of(root, owner, task_id) or {}
        phase = info.get("phase")
        if phase not in (PHASE_RESERVED, PHASE_RUNNING):
            phase = PHASE_RUNNING if hb.exists() else PHASE_RESERVED
        reserved_expired = (
            phase == PHASE_RESERVED
            and not hb.exists()
            and claim_age is not None
            and claim_age > ACK_WAIT_SECONDS
        )
        out.append(
            {
                "account_id": owner,
                "task_id": task_id,
                "claim_age": claim_age,
                "heartbeat_age": hb_age,
                "phase": phase,
                "wake_id": info.get("wake_id"),
                "reserved_expired": reserved_expired,
                "expired": reserved_expired or (
                    claim_age is not None
                    and hb_age is not None
                    and claim_age > LEASE_SECONDS
                    and hb_age > LEASE_SECONDS
                ),
            }
        )
    return out


def count_running(root, account_id, now=None):
    """Số việc trung tâm đang được giữ (claim còn hạn) của 1 tài
    khoản — dùng cho worker cap riêng/tài khoản (TN2)."""
    return sum(
        1 for info in list_claims(root, account_id=account_id, now=now)
        if not info["expired"]
    )


def count_running_by_kind(root, account_id, now=None):
    """(so worker VIEC, so worker CHAT) dang giu claim con han cua
    1 tai khoan — phuc vu tach lan chat/viec (S2, thi diem
    muse-1992-a, Chu tich duyet 22/11/2026). Phan loai theo khoa
    claim: khoa phong chat bat dau bang CHAT_CLAIM_PREFIX, con lai
    la viec. count_running() cu giu nguyen cho cac caller khac."""
    task_running = 0
    chat_running = 0
    for info in list_claims(root, account_id=account_id, now=now):
        if info["expired"]:
            continue
        if is_chat_room_claim(info["task_id"]):
            chat_running += 1
        else:
            task_running += 1
    return task_running, chat_running


# -- Claim phong chat (v0.6.0, T3h — DESIGN §3.9 buoc 4, W8) -----------
# 1 luot tra loi chat giu 1 claim theo (tai khoan, phong), thue 15
# phut (chung lease voi claim viec). Khoa phong: "chat__<phong>" voi
# ":" doi thanh "-" (vd "dm:muse-chinh" -> "chat__dm-muse-chinh";
# "chung" -> "chat__chung"). 2 bat bien (NS-25): khong trung khoa
# viec (task_id luon dang "t-" + 12 hex, khong bao gio bat dau bang
# "chat__"); 2 phong khac nhau khong trung khoa.
# Claim phong dung CHUNG thu muc + co che lease/heartbeat voi claim
# viec (cung ham claim/is_claimed/release, chi khac task_id) nen
# count_running() dem ca worker chat vao cap 2 worker/tai khoan
# (W9 — DESIGN §3.9 buoc 3 trich dung count_running). Boot-scan cua
# dispatcher phai BO QUA claim phong (khong phai viec — khong ghi
# ledger "unknown" cho khoa "chat__...").

ROOM_RE = re.compile(r"^(chung|dm:[a-z0-9][a-z0-9-]{0,31})$")
CHAT_CLAIM_PREFIX = "chat__"


def chat_room_key(room):
    """Khoa claim cua 1 phong chat. Sai mau phong -> ValueError."""
    if not isinstance(room, str) or not ROOM_RE.match(room):
        raise ValueError("room phai la 'chung' hoac 'dm:<account_id>'")
    return CHAT_CLAIM_PREFIX + room.replace(":", "-")


def is_chat_room_claim(task_id):
    """True khi task_id thuc ra la khoa claim phong chat."""
    return isinstance(task_id, str) \
        and task_id.startswith(CHAT_CLAIM_PREFIX)


def claim_room(root, account_id, room, holder, now=None):
    """Giu 1 phong chat (W8). Thang -> True; da co nguoi giu -> False."""
    return claim(root, account_id, chat_room_key(room), holder, now=now)


def is_room_claimed(root, account_id, room, now=None):
    """True khi phong dang co claim chat CON HAN."""
    return is_claimed(root, account_id, chat_room_key(room), now=now)


def release_room(root, account_id, room):
    """Bo giu phong chat. Tra ve True neu da co claim."""
    return release(root, account_id, chat_room_key(room))


def _cli_release_room(args):
    """CLI ``release-room`` (D15, phuong an (A)): go claim phong
    chat cua 1 plan KHONG DUOC WAKE — chi khi holder truyen vao
    khop dung holder trong file claim VA holder do la cua
    dispatcher (``center_dispatcher:*``). Holder lech, khong co
    claim, phong sai mau, tai khoan sai mau -> chi canh bao ra
    stderr, exit 0 (khong bao gio lam hong chu ky hook). TUYET DOI
    khong go claim cua worker dang lam viec (holder khac
    ``center_dispatcher:*``)."""
    try:
        key = chat_room_key(args.room)
    except ValueError as exc:
        print("center_claims release-room: %s" % exc, file=sys.stderr)
        return 0
    try:
        claim = holder_of(args.root, args.account_id, key)
    except ValueError as exc:
        print("center_claims release-room: %s" % exc, file=sys.stderr)
        return 0
    if claim is None:
        print("center_claims release-room: khong co claim phong %s "
              "cua %s — bo qua" % (args.room, args.account_id),
              file=sys.stderr)
        return 0
    holder = claim.get("holder") if isinstance(claim, dict) else None
    if holder != args.holder:
        print("center_claims release-room: holder lech (claim dang "
              "do %s giu) — khong go claim phong %s cua %s"
              % (holder, args.room, args.account_id), file=sys.stderr)
        return 0
    if not str(holder).startswith("center_dispatcher:"):
        print("center_claims release-room: holder %s khong phai cua "
              "dispatcher — khong go claim phong %s cua %s (co the "
              "worker dang lam viec)" % (holder, args.room,
                                         args.account_id),
              file=sys.stderr)
        return 0
    released = release_room(args.root, args.account_id, args.room)
    print("center_claims release-room: %s claim phong %s cua %s"
          % ("da go" if released else "khong co",
             args.room, args.account_id))
    return 0


# -- CLI go claim viec (D17, phuong an (A), doi xung release-room) ------
# Mau task_id: "t-" + 12 hex (giong TASK_ID_RE trong center_store.py).
# Khoa claim phong chat ("chat__...") KHONG bao gio khop mau nay nen
# CLI nay khong the vo tinh go claim phong.
TASK_ID_RE = re.compile(r"^t-[0-9a-f]{12}$")


def _cli_release_task(args):
    """CLI ``release-task`` (D17, phuong an (A)): go claim viec cua
    1 plan KHONG DUOC WAKE — chi khi holder truyen vao khop dung
    holder trong file claim VA holder do la cua dispatcher
    (``center_dispatcher:*``). Holder lech, khong co claim, task
    sai mau, tai khoan sai mau -> chi canh bao ra stderr, exit 0
    (khong bao gio lam hong chu ky hook). TUYET DOI khong go claim
    cua worker dang lam viec (holder khac ``center_dispatcher:*``)."""
    if not isinstance(args.task_id, str) \
            or not TASK_ID_RE.match(args.task_id):
        print("center_claims release-task: task_id %r sai mau "
              "(phai la 't-' + 12 ky tu hex) — bo qua"
              % (args.task_id,), file=sys.stderr)
        return 0
    try:
        claim = holder_of(args.root, args.account_id, args.task_id)
    except ValueError as exc:
        print("center_claims release-task: %s" % exc, file=sys.stderr)
        return 0
    if claim is None:
        print("center_claims release-task: khong co claim viec %s "
              "cua %s — bo qua" % (args.task_id, args.account_id),
              file=sys.stderr)
        return 0
    holder = claim.get("holder") if isinstance(claim, dict) else None
    if holder != args.holder:
        print("center_claims release-task: holder lech (claim dang "
              "do %s giu) — khong go claim viec %s cua %s"
              % (holder, args.task_id, args.account_id), file=sys.stderr)
        return 0
    if not str(holder).startswith("center_dispatcher:"):
        print("center_claims release-task: holder %s khong phai cua "
              "dispatcher — khong go claim viec %s cua %s (co the "
              "worker dang lam viec)" % (holder, args.task_id,
                                         args.account_id),
              file=sys.stderr)
        return 0
    released = release(args.root, args.account_id, args.task_id)
    print("center_claims release-task: %s claim viec %s cua %s"
          % ("da go" if released else "khong co",
             args.task_id, args.account_id))
    return 0


def main(argv=None):
    """CLI: python3 center_claims.py release-room --root <root>
    --account-id <id> --room <phong> --holder <holder>.

    Hook da-bot goi CLI nay cho moi plan chat khong duoc wake trong
    chu ky (D15, phuong an (A)). Chi go khi holder khop dung va la
    holder cua dispatcher; moi truong hop khac deu exit 0 kem canh
    bao ra stderr — CLI khong bao gio lam hong chu ky hook.

    CLI: python3 center_claims.py release-task --root <root>
    --account-id <id> --task-id <task_id> --holder <holder>.

    Hook da-bot goi CLI nay cho moi plan viec khong duoc wake trong
    chu ky (D17, phuong an (A) — doi xung release-room, cung chot
    an toan: holder khop + tien to ``center_dispatcher:``).
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Claim trung tam (viec + phong chat).")
    sub = parser.add_subparsers(dest="command", required=True)
    release = sub.add_parser(
        "release-room",
        help="go claim phong chat cua plan khong duoc wake "
             "(chi khi holder khop va la cua dispatcher)")
    release.add_argument("--root", default=".",
                         help="thu muc goc phia Muse (mac dinh: .)")
    release.add_argument("--account-id", required=True,
                         help="tai khoan giu phong")
    release.add_argument("--room", required=True,
                         help="phong chat ('chung' hoac 'dm:<id>')")
    release.add_argument("--holder", required=True,
                         help="holder phai khop file claim")
    release_task = sub.add_parser(
        "release-task",
        help="go claim viec cua plan khong duoc wake "
             "(chi khi holder khop va la cua dispatcher)")
    release_task.add_argument(
        "--root", default=".",
        help="thu muc goc phia Muse (mac dinh: .)")
    release_task.add_argument("--account-id", required=True,
                              help="tai khoan giu viec")
    release_task.add_argument("--task-id", required=True,
                              help="viec ('t-' + 12 ky tu hex)")
    release_task.add_argument("--holder", required=True,
                              help="holder phai khop file claim")
    ack_start_cmd = sub.add_parser(
        "ack-start",
        help="worker xac nhan bat dau dung luot danh thuc "
             "(chuyen claim reserved -> running)")
    ack_start_cmd.add_argument(
        "--root", default=".",
        help="thu muc goc phia Muse (mac dinh: .)")
    ack_start_cmd.add_argument("--account-id", required=True,
                               help="tai khoan cua worker")
    ack_start_cmd.add_argument("--task-id", required=True,
                               help="viec ('t-' + 12 ky tu hex) "
                                    "hoac khoa phong chat__...")
    ack_start_cmd.add_argument("--wake-id", default=None,
                               help="ma luot danh thuc phai khop "
                                    "file claim")
    args = parser.parse_args(argv)
    if args.command == "release-room":
        return _cli_release_room(args)
    if args.command == "release-task":
        return _cli_release_task(args)
    if args.command == "ack-start":
        ok = ack_start(args.root, args.account_id, args.task_id,
                       wake_id=args.wake_id)
        print("center_claims ack-start: %s viec %s cua %s"
              % ("da xac nhan" if ok else "KHONG xac nhan duoc",
                 args.task_id, args.account_id))
        return 0 if ok else 1
    return 0


def select_oldest_unclaimed(candidates, is_claimed_fn=None):
    """Chọn việc cũ nhất chưa claim trong danh sách ĐÃ sắp xếp theo
    ``created_at`` tăng dần — hàm này KHÔNG tự sắp xếp, caller chịu
    trách nhiệm. ``candidates``: list các dict có khóa ``task_id``.
    Trả về dict ứng viên hoặc None.
    """
    if is_claimed_fn is None:
        raise ValueError("cần truyền is_claimed_fn")
    for item in candidates:
        task_id = item.get("task_id") if isinstance(item, dict) else None
        if task_id and not is_claimed_fn(task_id):
            return item
    return None


if __name__ == "__main__":
    raise SystemExit(main())
