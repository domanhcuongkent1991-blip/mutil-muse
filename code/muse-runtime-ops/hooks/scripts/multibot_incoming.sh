#!/usr/bin/env bash
# Hook: LAY TIN + DIEU PHOI da-bot (token-free, 10s).
# Moi chu ky:
#   1) fetcher.py: keo tin/config/health tu gateway PC ve queues/ (flock
#      trong fetcher: chu ky truoc chua xong thi bo qua, khong don).
#   1b) center_fetcher.py (trung tam T2): keo feed trung tam SAU buoc
#      fetcher bot — khong doi thu tu/logic buoc bot; 401 trung tam chi
#      dung keo trung tam, fetcher bot van chay.
#   2) deaf_watch.py: so do diec D8 — doc /health gateway, ghi cua so
#      diec vao deaf_log.jsonl (phan loai pc/muse/both/unknown). Chi
#      live mode; khong bao gio lam hong chu ky (|| true).
#   3) plan_cycle.py: dispatcher lap ke hoach (co tam dung, allowlist,
#      gom lane FIFO, claim, worker cap, cau dao, re-wake, boot-scan,
#      lenh he thong F23 + retry delivery + dong delegated qua Sender)
#      va in 1 dong JSON ke hoach. Runtime hook chi phat dung 1 quyet
#      dinh wake/silent moi lan chay, nen moi chu ky chi wake toi da
#      1 lane (lane con lai cho chu ky sau, 10s).
#   3b) center_plan_cycle.py (trung tam T2 + T3h): center dispatcher
#      lap ke hoach viec (chi viec assigned_to = minh, toi da 1
#      viec/chu ky, cap rieng 2 viec/tai khoan) + ke hoach chat
#      (center_chats, toi da 1 phong/chu ky, phong co tin cu nhat
#      len dau) + chat tac (chat_stuck, D4). Stderr cua buoc planner
#      duoc ghi ra ~/hooks/logs/center-plan.log (xoay vong, giu toi
#      da 5000 dong — Fix gop 21/11 §4.2 muc 12).
#   4) sync_replies.py: day outbox tra loi ve receiver PC (cursor rieng,
#      chi tien sau HTTP 200).
#   4b) center_outbox.py --flush (trung tam T2): day toi da 1 dong
#      outbox trung tam ve receiver PC (cho hub 200 moi tien con tro).
#   5) Co lane thi wake 1 worker cho dung lane do (1 wake = 1 bot, khong
#      bao gio wake chung 2 bot); khong co lane bot thi wake 1 worker
#      chat trung tam (1 wake = 1 phong, kind=center_chat); khong co
#      chat thi wake 1 worker viec trung tam (1 wake = 1 viec,
#      kind=center_task); khong co gi thi silent. Thu tu wake (T3h):
#      lane bot -> chat -> viec. Truoc khi phat quyet dinh wake/silent
#      (2 lenh nay exit ngay), hook giai phong claim phong cua moi
#      plan chat KHONG DUOC WAKE trong chu ky (D15, phuong an (A) —
#      planner giu claim ngay luc lap ke hoach, hook chi wake toi da
#      1 worker/chu ky; khong go thi plan ket claim toi 15 phut):
#      wake lane bot/viec/silent -> go het center_chats; wake chat
#      -> go het tru muc vua wake (khop account_id + room). Plan
#      duoc wake giu nguyen claim (worker tu go khi tra loi xong).
#      Tuong tu cho lane viec (D17, phuong an (A) — doi xung lane
#      chat): hook giai phong claim viec cua moi plan viec KHONG
#      DUOC WAKE trong chu ky (qua CLI release-task, doi chieu
#      holder trong truong "holder" cua muc center_tasks): wake
#      lane bot/chat/silent -> go het center_tasks; wake viec ->
#      go het tru muc vua wake (khop account_id + task_id). Plan
#      viec duoc wake giu nguyen claim (worker tu go khi xong viec).
#      Moi lenh release kem || true, khong bao gio lam hong chu ky.
# Dry-run (HATCH_HOOK_DRY_RUN=1): bo fetch + deaf_watch + sync (khong
# mang, khong ghi state that), planner
# chay tren ban sao tam voi sender=None (khong ghi root that, khong goi
# API) — quyet dinh wake/silent in ra van la quyet dinh that se dien ra.
# Center planner o dry-run khong co --account-id nen tra ke hoach rong
# (khong doc secret); quyet dinh wake viec trung tam o dry-run luon la
# khong co viec; buoc release claim chat (buoc 5) o dry-run khong co
# gi de go vi center_chats rong.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

ROOT="$HOME/workspace/multibot-runtime"
CODE="$HOME/workspace/multibot-code"
DRY="${HATCH_HOOK_DRY_RUN:-0}"

# (Fix gộp 21/11, §4.2 mục 12) Ghi stderr của bước planner trung tâm
# ra file log xoay vòng ~/hooks/logs/center-plan.log (giữ tối đa
# 5000 dòng) để có log có cấu trúc, grep được (hook log
# multibot-incoming.jsonl đã có stderr từng chu kỳ nhưng phải moi
# từ JSON). Không đổi thứ tự wake (bot -> chat -> việc), không đổi
# 1 wake/chu kỳ. Mọi bước ghi log kèm || true: ghi log không bao
# giờ được làm hỏng chu kỳ hook.
CENTER_PLAN_LOG="$HOME/hooks/logs/center-plan.log"
CENTER_PLAN_LOG_MAX_LINES=5000

run_center_planner() {
  local err_file out tmp_log
  err_file=$(mktemp) || {
    python3 "$ROOT/ops/center_plan_cycle.py" --root "$ROOT"
    return 0
  }
  out=$(python3 "$ROOT/ops/center_plan_cycle.py" --root "$ROOT" \
    2>"$err_file") \
    || out='{"center_tasks":[],"center_chats":[]}'
  {
    mkdir -p "$(dirname "$CENTER_PLAN_LOG")" 2>/dev/null || true
    printf '[%s] center_plan_cycle stderr:\n' \
      "$(date '+%Y-%m-%d %H:%M:%S %Z')" >>"$CENTER_PLAN_LOG" \
      2>/dev/null || true
    cat "$err_file" >>"$CENTER_PLAN_LOG" 2>/dev/null || true
    tmp_log=$(mktemp) 2>/dev/null || tmp_log=""
    if [ -n "$tmp_log" ]; then
      tail -n "$CENTER_PLAN_LOG_MAX_LINES" "$CENTER_PLAN_LOG" \
        >"$tmp_log" 2>/dev/null \
        && mv "$tmp_log" "$CENTER_PLAN_LOG" 2>/dev/null \
        || rm -f "$tmp_log"
    fi
  } || true
  rm -f "$err_file" || true
  printf '%s' "$out"
}

if [ "$DRY" = "1" ]; then
  plan=$(python3 "$ROOT/ops/plan_cycle.py" --root "$ROOT" --dry-run) || plan='{"lanes":[]}'
  center_plan='{"center_tasks":[],"center_chats":[]}'
else
  python3 "$CODE/muse-side/fetcher.py" --root "$ROOT" || true
  python3 "$CODE/muse-side/center_fetcher.py" --root "$ROOT" || true
  python3 "$ROOT/ops/deaf_watch.py" --root "$ROOT" || true
  plan=$(python3 "$ROOT/ops/plan_cycle.py" --root "$ROOT") || plan='{"lanes":[]}'
  center_plan=$(run_center_planner) || center_plan='{"center_tasks":[],"center_chats":[]}'
  python3 "$CODE/muse-side/sync_replies.py" --root "$ROOT" || true
  python3 "$CODE/muse-side/center_outbox.py" --root "$ROOT" --flush || true
fi

# Dam bao plan luon la JSON hop le dang {"lanes": [...]}; neu khong thi
# coi nhu khong co gi de dispatch.
plan=$(printf '%s' "$plan" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d) if isinstance(d,dict) and isinstance(d.get('lanes'),list) else '{\"lanes\":[]}')" 2>/dev/null || printf '{"lanes":[]}')

lane=$(printf '%s' "$plan" | python3 -c "import json,sys; d=json.load(sys.stdin); ls=d.get('lanes') or []; print(json.dumps(ls[0]) if ls else '')" 2>/dev/null || printf '')

# Dam bao center_plan luon la JSON hop le dang {"center_tasks": [...],
# "center_chats": [...]}.
center_plan=$(printf '%s' "$center_plan" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d) if isinstance(d,dict) and isinstance(d.get('center_tasks'),list) and isinstance(d.get('center_chats',[]),list) else '{\"center_tasks\":[],\"center_chats\":[]}')" 2>/dev/null || printf '{"center_tasks":[],"center_chats":[]}')

center_task=$(printf '%s' "$center_plan" | python3 -c "import json,sys; d=json.load(sys.stdin); ts=d.get('center_tasks') or []; print(json.dumps(ts[0]) if ts else '')" 2>/dev/null || printf '')

center_chat=$(printf '%s' "$center_plan" | python3 -c "import json,sys; d=json.load(sys.stdin); cs=d.get('center_chats') or []; print(json.dumps(cs[0]) if cs else '')" 2>/dev/null || printf '')

# D15 (phuong an (A)): giai phong claim phong cua cac plan chat KHONG
# DUOC WAKE trong chu ky. Planner giu claim ngay luc lap ke hoach
# (center_dispatcher.py); hook chi wake toi da 1 worker/chu ky nen
# plan khong duoc wake se ket claim toi 15 phut neu khong ai go.
# Phai go TRUOC lenh wake/silent (2 lenh nay exit ngay khi phat quyet
# dinh). $1 = JSON muc center_chats vua duoc wake (rong = khong wake
# chat nao); chi giu lai muc khop dung (account_id, room) do, go het
# cac muc con lai qua CLI center_claims.py release-room (CLI tu doi
# chieu holder, chi go khi holder khop va la cua dispatcher — khong
# bao gio go claim cua worker dang lam viec). Moi lenh release kem
# || true: release khong bao gio duoc lam hong chu ky hook.
release_unwoken_center_chats() {
  local woken_json="${1:-}"
  printf '%s' "$center_plan" | python3 -c '
import json, os, subprocess, sys
woken_raw = sys.argv[1]
code_dir, root = sys.argv[2], sys.argv[3]
woken = json.loads(woken_raw) if woken_raw else None
plan = json.load(sys.stdin)
for chat in plan.get("center_chats") or []:
    account_id = chat.get("account_id")
    room = chat.get("room")
    holder = chat.get("holder")
    if not account_id or not room or not holder:
        continue
    if woken and account_id == woken.get("account_id") \
            and room == woken.get("room"):
        continue
    try:
        subprocess.run(
            [sys.executable,
             os.path.join(code_dir, "muse-side", "center_claims.py"),
             "release-room", "--root", root,
             "--account-id", account_id, "--room", room,
             "--holder", holder],
            check=False)
    except OSError as exc:
        print("release_unwoken_center_chats: %s" % exc, file=sys.stderr)
' "$woken_json" "$CODE" "$ROOT" || true
}

# D17 (phuong an (A), doi xung release_unwoken_center_chats): giai
# phong claim viec cua cac plan viec KHONG DUOC WAKE trong chu ky.
# $1 = JSON muc center_tasks vua duoc wake (rong = khong wake viec
# nao); chi giu lai muc khop dung (account_id, task_id) do, go het
# cac muc con lai qua CLI center_claims.py release-task (CLI tu doi
# chieu holder, chi go khi holder khop va la cua dispatcher — khong
# bao gio go claim cua worker dang lam viec). Moi lenh release kem
# || true: release khong bao gio duoc lam hong chu ky hook.
release_unwoken_center_tasks() {
  local woken_json="${1:-}"
  printf '%s' "$center_plan" | python3 -c '
import json, os, subprocess, sys
woken_raw = sys.argv[1]
code_dir, root = sys.argv[2], sys.argv[3]
woken = json.loads(woken_raw) if woken_raw else None
plan = json.load(sys.stdin)
for task in plan.get("center_tasks") or []:
    account_id = task.get("account_id")
    task_id = task.get("task_id")
    holder = task.get("holder")
    if not account_id or not task_id or not holder:
        continue
    if woken and account_id == woken.get("account_id") \
            and task_id == woken.get("task_id"):
        continue
    try:
        subprocess.run(
            [sys.executable,
             os.path.join(code_dir, "muse-side", "center_claims.py"),
             "release-task", "--root", root,
             "--account-id", account_id, "--task-id", task_id,
             "--holder", holder],
            check=False)
    except OSError as exc:
        print("release_unwoken_center_tasks: %s" % exc, file=sys.stderr)
' "$woken_json" "$CODE" "$ROOT" || true
}

if [ -n "$lane" ]; then
  payload=$(printf '%s' "$lane" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({'bot_id':d['bot_id'],'chat_id':d['chat_id'],'event_keys':d['event_keys'],'merged':bool(d.get('merged',False))}))")
  release_unwoken_center_chats ""
  release_unwoken_center_tasks ""
  wake "multibot-incoming" "$payload"
elif [ -n "$center_chat" ]; then
  # Khong co lane bot: wake 1 worker chat trung tam (1 wake = 1 phong).
  # Payload mang kind rieng de worker phan biet voi lane bot/nhanh viec;
  # prompt hook (multibot-incoming.json) co NHANH CHAT TRUNG TAM — worker
  # nhan payload kind=center_chat thi lam theo nhanh chat, bo qua toan
  # bo nhanh worker bot + nhanh viec.
  payload=$(printf '%s' "$center_chat" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({'kind':'center_chat','account_id':d['account_id'],'room':d['room'],'seqs':d['seqs'],'wake_id':d.get('wake_id'),'ack_task_id':d.get('ack_task_id')}))")
  release_unwoken_center_chats "$center_chat"
  release_unwoken_center_tasks ""
  printf '%s' "$payload" | python3 -c "import json,subprocess,sys; d=json.load(sys.stdin); subprocess.run(['python3','/home/hatch/workspace/multibot-code/muse-side/center_claims.py','mark-wake-emitted','--root','/home/hatch/workspace/multibot-runtime','--account-id',d['account_id'],'--task-id',d.get('ack_task_id') or '', '--wake-id', d.get('wake_id') or ''],check=False)" >/dev/null 2>&1 || true
  wake "multibot-incoming" "$payload"
elif [ -n "$center_task" ]; then
  # Khong co lane bot: wake 1 worker viec trung tam (1 wake = 1 viec).
  # Payload mang kind rieng de worker phan biet voi lane bot; prompt
  # hook (multibot-incoming.json v2) da co NHANH VIEC TRUNG TAM o dau
  # prompt — worker nhan payload kind=center_task thi lam theo nhanh
  # viec, bo qua toan bo nhanh worker bot.
  payload=$(printf '%s' "$center_task" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({'kind':'center_task','account_id':d['account_id'],'center_task_id':d['task_id'],'wake_id':d.get('wake_id'),'ack_task_id':d.get('ack_task_id') or d['task_id']}))")
  release_unwoken_center_chats ""
  release_unwoken_center_tasks "$center_task"
  printf '%s' "$payload" | python3 -c "import json,subprocess,sys; d=json.load(sys.stdin); subprocess.run(['python3','/home/hatch/workspace/multibot-code/muse-side/center_claims.py','mark-wake-emitted','--root','/home/hatch/workspace/multibot-runtime','--account-id',d['account_id'],'--task-id',d.get('ack_task_id') or '', '--wake-id', d.get('wake_id') or ''],check=False)" >/dev/null 2>&1 || true
  wake "multibot-incoming" "$payload"
else
  release_unwoken_center_chats ""
  release_unwoken_center_tasks ""
  silent "nothing to dispatch" '{}'
fi
