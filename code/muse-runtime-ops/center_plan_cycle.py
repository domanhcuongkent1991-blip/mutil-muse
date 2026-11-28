#!/usr/bin/env python3
"""Lập kế hoạch 1 chu kỳ center dispatcher cho hook đa-bot (token-free).

Chạy :func:`center_dispatcher.run_cycle` với ``worker_fn`` GHI LẠI
việc thay vì gọi worker thật; in ra stdout 1 dòng JSON duy nhất::

    {"center_tasks": [{"account_id": ..., "task_id": ...,
                       "kind": "center_task",
                       "holder": "center_dispatcher:<epoch>"}],
     "center_chats": [{"account_id": ..., "room": ...,
                       "seqs": [...], "kind": "center_chat",
                       "holder": "center_dispatcher:<epoch>",
                       "discussion": {"rounds": [...]}}],
     "chat_stuck": [{"account_id": ..., "room": ...,
                     "waiting_count": ..., "oldest_seq": ...,
                     "oldest_wait_seconds": ...,
                     "level": "cần xem"}],
     "summary": {...}}

(v0.6.0, T3h — DESIGN §3.9, doc 10 §6.6): ``center_chats`` tối đa
1 phần tử/chu kỳ — phòng có tin cũ nhất lên đầu; ``chat_stuck``
là các phòng chat tắc (D4: tin chờ >3 phút hoặc ≥3 tin/phòng,
mức "cần xem"). Script bash của hook đọc JSON này: nếu không có
lane bot nào thì wake 1 worker chat cho phòng đầu tiên (nếu có),
rồi mới tới 1 worker việc cho việc đầu tiên (thứ tự wake: lane
bot → chat → việc). MỌI log đều ra stderr (không lẫn vào JSON).

(v0.6.0, T3l — DESIGN §3.10, D9 ĐÃ CHỐT): NGOẠI LỆ duy nhất của
"1 chat/chu kỳ" — tin nhiều người nhận (``to_account`` 2+ tài
khoản): TẤT CẢ tài khoản được tag đều có plan riêng chứa tin đó
trong CÙNG 1 chu kỳ (mỗi tài khoản 1 worker riêng, trả lời song
song; bộ đếm lượt DÙNG CHUNG — 1 tin = 1 lượt, không đếm 2 lần).
Planner phát mọi plan chứa seq cũ nhất (cùng 1 tin, cùng phòng);
plan phòng khác vẫn chỉ lấy 1 (phòng có tin cũ nhất). Mỗi mục
``center_chats`` kèm thêm trường ``discussion`` tối thiểu
(``{"rounds": [...]}`` — vòng thảo luận trong phòng đó: khóa,
người mở, người tham gia, số lượt đã dùng/tổng 5, trạng thái) để
hook/worker thấy trạng thái vòng mà không phải đọc lại file trạng
thái; brief worker chat vẫn là nơi mang đầy đủ thông tin vòng.
LƯU Ý (D15, phuong an (A)): hook chi wake ``center_chats[0]`` —
moi muc ``center_chats`` khac trong cung chu ky KHONG duoc wake
thi hook giai phong claim phong cua no ngay trong chu ky (qua CLI
``center_claims.py release-room``, doi chieu holder trong truong
``holder`` cua muc do); chu ky sau lap lai ke hoach moi, chi cham
~10 giay/nguoi nhan. Phuong an "hook wake 1 worker cho MOI muc
``center_chats``" KHONG lam — runtime chi cho 1 quyet dinh
wake/silent moi lan chay hook.

LƯU Ý (D17, phuong an (A) cho lane viec — doi xung lane chat):
moi muc ``center_tasks`` mang ``holder`` claim viec ma dispatcher
vua giu (``run_cycle_for_account`` tra ``woken_holder`` trong
summary chu ky); hook wake chat/lane bot/silent thi giai phong
claim viec cua plan khong duoc wake ngay trong chu ky (qua CLI
``center_claims.py release-task``, doi chieu holder); chu ky sau
lap lai ke hoach viec moi, chi cham ~10 giay thay vi ~900 giay.
Plan viec duoc wake giu nguyen claim (worker tu go khi xong viec).

Chế độ dry-run (``--dry-run``): sao chép các thư mục trạng thái
trung tâm sang thư mục tạm, chạy dispatcher trên bản sao với
``--account-id`` cho trước (không đọc secret — planner chỉ cần
account_id, không cần file cấu hình), in kế hoạch rồi xóa bản sao —
KHÔNG ghi gì vào root thật, KHÔNG gọi mạng.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
MUSE_SIDE = Path("/home/hatch/workspace/multibot-code/muse-side")
DEFAULT_ROOT = Path("/home/hatch/workspace/multibot-runtime")

for _candidate in (str(OPS_DIR), str(MUSE_SIDE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
import center_claims  # noqa: E402
import center_discussion  # noqa: E402
import center_dispatcher  # noqa: E402

# Thư mục trạng thái sao chép sang sandbox khi dry-run. Không sao
# chép center-config (chứa secret) — planner chỉ cần account_id
# truyền qua --account-id.
_SANDBOX_DIRS = (
    "center_tasks",
    "center_ledger.jsonl",
    "center_claims",
    "center_dispatcher_state",
    "center_messages.jsonl",
    # v0.6.0, T3h (doc 10 §6.6): file moc da tra loi theo phong +
    # hang cho canh bao chat tac (khong co secret trong 2 thu muc nay).
    "center_chat_state",
    "center",
)


def _log(message):
    print("[center-plan] %s" % message, file=sys.stderr, flush=True)


def _alert(kind, account_id, detail):
    print("[center-plan] ALERT kind=%s account=%s detail=%s"
          % (kind, account_id, detail), file=sys.stderr, flush=True)


def _build_sandbox(real_root):
    """Sao chép trạng thái trung tâm sang thư mục tạm; trả về đường
    dẫn sandbox (caller tự xóa)."""
    sandbox = Path(tempfile.mkdtemp(prefix="multibot-center-plan-"))
    for name in _SANDBOX_DIRS:
        source = Path(real_root) / name
        if source.is_dir():
            shutil.copytree(source, sandbox / name, symlinks=False)
        elif source.is_file():
            shutil.copy2(source, sandbox / name)
    return sandbox


def _discussion_summary(root, account_id, room):
    """Trang thai vong thao luan toi thieu cho 1 muc center_chats
    (T3l): {"rounds": [...]} — cac vong trong phong do (khoa, nguoi
    mo, nguoi tham gia, so luot da dung/tong 5, trang thai). Tinh
    lai truc tiep tu tin nhan (khong doc file trang thai); phong
    khong co vong nao thi {"rounds": []}."""
    messages = center_dispatcher.read_local_messages(root)
    room_messages = [entry for entry in messages
                     if (entry["record"].get("room") or "chung") == room]
    rounds = center_discussion.compute_discussion_rounds(room_messages)
    return {"rounds": center_discussion.summarize_rounds_for_room(
        rounds, room)}


def run_planning(root, account_id=None):
    """Chạy 1 chu kỳ center dispatcher, trả về
    (plan_tasks, plan_chats, stuck_chats, summary).

    ``account_id`` None = chạy cho mọi tài khoản có cấu hình trung
    tâm (đọc tên file center-config, không đọc secret).
    Chat lập kế hoạch TRƯỚC việc (thứ tự wake lane bot → chat →
    việc — chat giữ slot cap worker trước).
    """
    import center_fetcher  # noqa: E402  (chi doc ten file cau hinh)

    recorded = []

    def _worker_fn(acc, task, ctx):
        recorded.append({
            "account_id": acc,
            "task_id": task["task_id"],
            "kind": "center_task",
            "_task": task,
        })
        return {}

    deps = {"worker_fn": _worker_fn, "log_fn": _log, "alert_fn": _alert}
    if account_id is not None:
        accounts = [account_id]
    else:
        accounts = center_fetcher.discover_center_accounts(root)
    # Nhanh chat (v0.6.0, T3h): toi da 1 phong/chu ky — giu phong co
    # tin cu nhat trong moi tai khoan, roi chon 1 phong cu nhat
    # chung (bo giu claim cac phong thua).
    # (v0.6.0, T3l — D9): NGOAI LE — tin nhieu nguoi nhan: moi plan
    # chua seq cu nhat (cung 1 tin, cung phong) deu duoc phat trong
    # cung chu ky (moi tai khoan 1 worker rieng, song song); plan
    # phong khac van chi lay 1.
    chat_candidates = []
    stuck = []
    for acc in accounts:
        chat = center_dispatcher.plan_chat_for_account(
            root, acc, deps={"log_fn": _log, "alert_fn": _alert})
        if chat is not None:
            chat_candidates.append(chat)
        stuck.extend(center_dispatcher.detect_stuck_chats(root, acc))
    if chat_candidates:
        chat_candidates.sort(key=lambda c: min(c["seqs"]))
        oldest_seq = min(chat_candidates[0]["seqs"])
        winners = []
        for cand in chat_candidates:
            if oldest_seq in cand["seqs"]:
                winners.append(cand)
            else:
                center_claims.release_room(
                    Path(root), cand["account_id"], cand["room"])
        chats = [{
            "account_id": cand["account_id"],
            "room": cand["room"],
            "seqs": cand["seqs"],
            "kind": "center_chat",
            "holder": cand["holder"],
            "wake_id": cand.get("wake_id"),
            "rewake": bool(cand.get("rewake")),
            "ack_task_id": center_claims.chat_room_key(cand["room"]),
            "discussion": _discussion_summary(
                root, cand["account_id"], cand["room"]),
        } for cand in winners]
    else:
        chats = []
    # Chat tac (D4): ghi dedup vao hang cho canh bao local (khong
    # spam — 1 dot tac chi ghi 1 dong).
    if stuck:
        center_dispatcher.record_chat_stuck_alerts(root, stuck)
    if account_id is not None:
        summaries = {
            account_id: center_dispatcher.run_cycle_for_account(
                root, account_id, deps=deps)
        }
    else:
        summaries = {}
        for acc in accounts:
            summaries[acc] = center_dispatcher.run_cycle_for_account(
                root, acc, deps=deps)
    tasks = recorded
    if len(tasks) > 1:
        # Không bao giờ nên xảy ra (dispatcher chỉ wake tối đa 1
        # việc/chu kỳ). Phòng thủ: bỏ giữ claim của việc thừa để
        # chu kỳ sau xử lý lại, và ghi log lỗi rõ ràng.
        _log(
            "LOI: center dispatcher ghi %d viec trong 1 chu ky (chi "
            "wake viec dau); bo giu claim %d viec thua"
            % (len(tasks), len(tasks) - 1)
        )
        for extra in tasks[1:]:
            center_claims.release(
                Path(root), extra["account_id"], extra["task_id"])
        tasks = tasks[:1]
    plan = [
        {
            "account_id": task["account_id"],
            "task_id": task["task_id"],
            "kind": "center_task",
            # D17: holder claim viec — lay tu summary chu ky (duong
            # chinh: dispatcher tra woken_holder); du phong doc lai
            # file claim neu summary thieu (claim nay dispatcher vua
            # giu trong chinh chu ky nay).
            "holder": _summary_holder(summaries, task)
            or _plan_task_holder(
                root, task["account_id"], task["task_id"]),
            "wake_id": _summary_wake_id(summaries, task)
            or _plan_task_wake_id(
                root, task["account_id"], task["task_id"]),
            "ack_task_id": task["task_id"],
        }
        for task in tasks
    ]
    return plan, chats, stuck, summaries


def _summary_wake_id(summaries, task):
    summary = (summaries or {}).get(task["account_id"])
    if not isinstance(summary, dict):
        return None
    if summary.get("woken_task_id") != task["task_id"]:
        return None
    wake_id = summary.get("woken_wake_id")
    return wake_id if isinstance(wake_id, str) and wake_id else None


def _plan_task_wake_id(root, account_id, task_id):
    info = center_claims.holder_of(Path(root), account_id, task_id)
    if isinstance(info, dict):
        wake_id = info.get("wake_id")
        return wake_id if isinstance(wake_id, str) and wake_id else None
    return None


def _summary_holder(summaries, task):
    """Holder claim viec trong summary chu ky cua dung tai khoan +
    dung task (D17 — dispatcher tra ``woken_holder``). Tra ve None
    neu summary khong co (vd worker loi giua chung)."""
    summary = (summaries or {}).get(task["account_id"])
    if not isinstance(summary, dict):
        return None
    if summary.get("woken_task_id") != task["task_id"]:
        return None
    holder = summary.get("woken_holder")
    return holder if isinstance(holder, str) and holder else None


def _plan_task_holder(root, account_id, task_id):
    """Holder claim viec hien co trong file claim (du phong khi ctx
    worker khong mang holder). Tra ve None neu khong co claim."""
    try:
        info = center_claims.holder_of(root, account_id, task_id)
    except ValueError:
        return None
    if isinstance(info, dict):
        holder = info.get("holder")
        return holder if isinstance(holder, str) and holder else None
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Lap ke hoach center dispatcher cho hook da-bot."
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--account-id", default=None,
                        help="chỉ lập kế hoạch cho tài khoản này "
                             "(mặc định: mọi tài khoản có cấu hình)")
    parser.add_argument("--dry-run", action="store_true",
                        help="chạy trên bản sao tạm (cần --account-id), "
                             "không ghi root thật, không gọi mạng")
    args = parser.parse_args(argv)

    plan, chats, stuck, summary = [], [], [], {}
    try:
        if args.dry_run:
            if not args.account_id:
                _log("DRY-RUN: can --account-id (khong doc secret)")
                plan, chats, stuck, summary = [], [], [], {}
            else:
                _log("DRY-RUN: sao chep trang thai sang sandbox")
                sandbox = _build_sandbox(args.root)
                try:
                    plan, chats, stuck, summary = run_planning(
                        sandbox, account_id=args.account_id)
                finally:
                    shutil.rmtree(sandbox, ignore_errors=True)
                _log("DRY-RUN: ke hoach=%s"
                     % json.dumps(plan, ensure_ascii=False))
        else:
            plan, chats, stuck, summary = run_planning(
                args.root, account_id=args.account_id)
    except Exception:  # noqa: BLE001 - hook không được crash: ghi log, trả rỗng
        print("[center-plan] LOI NGOAI CUNG:\n%s" % traceback.format_exc(),
              file=sys.stderr, flush=True)
        plan, chats, stuck, summary = [], [], [], {}
    print(json.dumps({"center_tasks": plan, "center_chats": chats,
                      "chat_stuck": stuck, "summary": summary},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
