#!/usr/bin/env python3
"""Ban tom tat cho worker chat trung tam (v0.6.0, T3h — DESIGN §3.9,
doc 10 §6.6).

Worker chat chay helper nay dau luot (payload kind=center_chat):

    python3 center_chat_brief.py --root <root> --account-id <id>
        --room <phong> --seqs <seq1,seq2,...>

Ban in ra la boi canh chinh: tin can tra loi (snapshot seqs — CHI
tra loi dung cac seq nay, tin moi ve trong luc tra loi cho chu ky
sau), 20 tin gan nhat trong phong, tin gan viec kem 1 dong tieu
de/trang thai, file dinh kem + luat mo/gui file, luat tra loi chep
nguyen van, cau thua nhan cham + cach tinh X, lenh mau (tin hieu
" dang tra loi", gui tra loi qua outbox, ghi moc).

Ghi moc sau khi tra loi:

    python3 center_chat_brief.py --root <root> --account-id <id>
        --room <phong> --seqs <seq1,seq2,...> --mark-replied

Chi ghi moc replied_up_to_seq = max(seqs) SAU KHI cau tra loi da
vao outbox thanh cong (kiem chung: trong outbox phai co it nhat 1
dong tin kind=hoi cua dung tai khoan nay vao dung phong nay; chua
co thi tu choi ghi moc). Ghi bang tmp+rename (mau atomic_write_json);
thu muc center_chat_state/ do lan ghi dau tien tao (GC-13).
Dispatcher CHI DOC moc, khong bao gio tu ghi.

Vong thao luan (v0.6.0, T3l — DESIGN §3.10, D9/D10 ĐÃ CHỐT):
brief them muc "VONG THAO LUAN" — so luot da dung/tong 5, trang
thai vong, ai la nguoi mo vong — de worker biet minh dang o luot
may va tra loi the nao (muon hoi tiep ai thi ghi ro ``to_account``;
khong ghi ``to_account`` thi khong danh thuc ai).

Chi doc file, khong goi mang, khong in secret. Stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
import center_claims
import center_discussion
import center_dispatcher
import center_outbox
import center_state_lock
from muse_common import (
    atomic_write_json,
    ensure_dir,
    iso_to_epoch,
    now_epoch,
    now_iso,
    read_json,
)
from shared import ids

DEFAULT_ROOT = Path.home() / "workspace" / "multibot-runtime"

RECENT_LIMIT = 20  # worker doc 20 tin gan nhat (doc 10 §3.7)
MAX_REPLY_CHARS = 5000  # tran tin gop (Q-V060-02)
SNAPSHOT_BODY_CHARS = 2000  # cat than tin ngoai snapshot (boi canh)
TASK_TITLE_CHARS = 80  # cat tieu de viec trong dong tom tat
# Tin cho >= 60 giay moi can cau thua nhan cham (SUY LUAN — chu ky
# hook 10 giay: tin cho qua vai chu ky moi goi la "tin ton").
STALE_SECONDS = 60

# Cau thua nhan cham — TAM THOI (D7, QUYET-DINH-QUA-DEM-20-11):
# dung tam thay cho cau chinh thuc chua duoc duyet (Q-V060-01).
# Khi chu nhan duyet cau chinh thuc thi thay the ngay cau nay (KHONG
# ghi cau tam nay vao prompts/delay_ack_approved.txt — file do chi
# danh cho cau chinh thuc da duyet). PHAM VI: chi duong chat trung
# tam (center_chat_brief) — KHONG sua duong D1 cua lane bot.
# X = so phut thuc te tin ton cu nhat da cho (tinh tu created_at cua
# tin do toi luc tra loi, lam tron xuong, toi thieu 1).
DELAY_ACK_TEMPLATE = "Xin lỗi vì trả lời chậm, tin này đã chờ {minutes} phút."

# Cau ranh gioi tag — NGUYEN VAN SPEC TT18 (v0.4.0). KHONG sua 1 chu
# khi chep vao brief.
BOUNDARY_SENTENCE = (
    "Tin tag bạn CÓ THỂ là việc mới: tag trong khung chung tạo 1 "
    "việc chung cho mọi tài khoản được tag — việc đó nằm trong mục "
    "việc của bạn, báo xong như việc thường; tag trong phòng riêng "
    "chỉ là nhắc tên, không tạo việc.")

REPLY_RULES = (
    "1. Khung chung không tag thì KHÔNG trả lời (chỉ trả lời khi "
    "được tag hoặc khi đang làm việc liên quan).\n"
    "2. Gộp hết tin chưa trả lời trong phòng thành 1 câu trả lời "
    "(1 phòng = 1 câu trả lời, không giới hạn số tin gộp); tin gộp "
    "vượt 5.000 ký tự thì tách thành nhiều tin theo thứ tự thời "
    "gian, mỗi tin ≤ 5.000 ký tự — không tóm tắt, không bỏ tin nào "
    "(Q-V060-02).\n"
    "3. CHAT TRƯỚC, việc sau.\n"
    "4. Tin vừa hỏi vừa sinh việc → WORKER VIỆC GIỮ TOÀN BỘ "
    "(worker chat không trả lời phần hỏi đó).\n"
    "5. " + BOUNDARY_SENTENCE + "\n"
    "6. File chỉ mở khi câu hỏi liên quan; file quá lớn/không tải "
    "được thì trả lời bằng chữ và nói rõ lý do.\n"
    "7. Chỉ trả lời đúng các seq trong snapshot lượt này; tin mới "
    "về trong lúc trả lời chờ chu kỳ sau, không nhét thêm, không "
    "trả lời 2 lần.\n"
    "8. Cau tra loi can file thi tu tao file duoi ~/workspace roi "
    "tai len + dinh kem; file da tai len ma gui tin that bai thi "
    "gui lai bang tin moi, khong vut file (SPEC TT16).")


def _default_log(message):
    print(message, flush=True)


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + " […]"


def split_grouped_reply(text, limit=MAX_REPLY_CHARS):
    """Tach cau tra loi gop thanh nhieu tin theo thu tu thoi gian,
    moi tin <= limit ky tu; khong tom tat, khong bo tin nao
    (Q-V060-02). Tach uu tien theo dong (giu nguyen xuong dong),
    dong dai hon limit thi cat cung theo ky tu. Noi cac phan lai
    phai ra dung ban goc."""
    if not isinstance(text, str):
        raise ValueError("text phai la chuoi")
    if limit < 1:
        raise ValueError("limit phai >= 1")
    if len(text) <= limit:
        return [text] if text else []
    parts = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            room = limit - len(current)
            if room <= 0:
                parts.append(current)
                current = ""
                room = limit
            take = min(room, len(line))
            current += line[:take]
            line = line[take:]
            if len(current) >= limit:
                parts.append(current)
                current = ""
        if len(current) + len(line) > limit:
            if current:
                parts.append(current)
                current = ""
        current += line
    if current:
        parts.append(current)
    return [part for part in parts if part]


def _task_titles(root):
    """{task_id: (title, status)} tu viec local (chi tieu de +
    trang thai — 1 dong tom tat/tin gan viec)."""
    out = {}
    for item in center_dispatcher.read_local_tasks(root):
        record = item["record"]
        out[item["task_id"]] = (
            record.get("title") or "",
            record.get("status") or "",
        )
    return out


def _format_message(entry, task_titles, full_body=False, mark=""):
    record = entry["record"]
    body = record.get("body") or ""
    if not full_body:
        body = _truncate(body, SNAPSHOT_BODY_CHARS)
    task_id = record.get("task_id")
    task_line = ""
    if task_id and task_id in task_titles:
        title, status = task_titles[task_id]
        task_line = " [việc: %s (%s)]" % (
            _truncate(str(title), TASK_TITLE_CHARS), status)
    file_meta = record.get("file") or {}
    file_line = ""
    if isinstance(file_meta, dict) and file_meta.get("file_id"):
        file_line = " [file: %s (%s bytes, sha256 %s)]" % (
            file_meta.get("file_name") or file_meta.get("file_id"),
            file_meta.get("file_size") or "?",
            (file_meta.get("sha256") or "?")[:12])
    return ("- seq %(seq)s%(mark)s [%(kind)s] %(sender)s lúc "
            "%(at)s%(task)s%(file)s: %(body)s" % {
                "seq": entry["seq"], "mark": mark,
                "kind": record.get("kind") or "?",
                "sender": record.get("from_account") or "?",
                "at": record.get("created_at") or entry.get("at") or "?",
                "task": task_line, "file": file_line, "body": body,
            })


def _delay_ack_line(snapshot, now):
    """Dong cau thua nhan cham (TAM THOI — D7) + X.

    Chi dung khi tin ton cu nhat da cho >= STALE_SECONDS: X = so
    phut thuc te da cho (lam tron xuong, toi thieu 1). Tin moi
    (cho < 60 giay) thi tra ve None — tra loi thang, khong can cau
    nay. Tra ve (dong_cau, so_phut) hoac (None, 0)."""
    if not snapshot:
        return None, 0
    oldest = snapshot[0]
    record = oldest["record"]
    created = record.get("created_at") or oldest.get("at")
    epoch = iso_to_epoch(created) if isinstance(created, str) else None
    if epoch is None:
        return None, 0
    waited = now - epoch
    if waited < STALE_SECONDS:
        return None, 0
    minutes = max(1, int(waited // 60))
    return DELAY_ACK_TEMPLATE.format(minutes=minutes), minutes


def _discussion_lines(root, room, seqs):
    """Muc vong thao luan cho brief (T3l — DESIGN §3.10, D9/D10
    ĐÃ CHỐT): so luot da dung/tong 5, trang thai vong, ai mo vong
    — tinh lai truc tiep tu tin nhan trong phong (khong doc file
    trang thai, de luon moi nhat luc worker soan tra loi). Them
    huong dan worker tra loi tiep: muon hoi tiep ai thi ghi ro
    ``to_account``; khong ghi thi khong danh thuc ai (lop chong
    vong lap 1)."""
    messages = [entry for entry in center_dispatcher.read_local_messages(root)
                if (entry["record"].get("room") or "chung") == room]
    rounds = center_discussion.compute_discussion_rounds(messages)
    room_rounds = center_discussion.summarize_rounds_for_room(rounds, room)
    snapshot_keys = center_discussion.round_keys_for_seqs(
        messages, rounds, seqs)
    lines = []
    lines.append("VÒNG THẢO LUẬN (5 lượt/vòng — D9/D10 đã chốt):")
    if not room_rounds:
        lines.append("- Không có vòng thảo luận nào đang mở trong "
                     "phòng này.")
    for info in room_rounds:
        marker = (" (tin trong snapshot thuộc vòng này)"
                  if info["key"] in snapshot_keys else "")
        people = ", ".join(info["participants"])
        if info["state"] == center_discussion.ROUND_STATE_CLOSED:
            lines.append(
                "- Vòng %s: đã dùng %d/5 lượt, trạng thái: ĐÃ DỪNG%s; "
                "người mở vòng: %s; người tham gia: %s. Vòng đã dừng "
                "— không hỏi tiếp tài khoản khác, chờ chủ nhân nhắn "
                "tin gọi tên tài khoản để mở vòng mới."
                % (info["key"], info["count"], marker,
                   info["opener"], people))
        else:
            lines.append(
                "- Vòng %s: đã dùng %d/5 lượt, còn %d lượt, trạng "
                "thái: đang mở%s; người mở vòng: %s; người tham gia: "
                "%s."
                % (info["key"], info["count"], info["remaining"],
                   marker, info["opener"], people))
    lines.append(
        "Muốn hỏi tiếp ai thì ghi rõ gửi cho ai (to_account); câu trả "
        "lời không ghi to_account thì không đánh thức ai — vòng thảo "
        "luận dừng tại đó.")
    lines.append(
        "Tin đã ghi rõ gửi cho ai (to_account) thì @tên trong tin chỉ "
        "là nhắc tên, không tạo việc — cứ nhắc tên thoải mái trong "
        "lúc thảo luận.")
    lines.append(
        "Tin dài tách ra thì mỗi tin tách tính 1 lượt — viết gọn, "
        "đừng tách quá 2 tin.")
    lines.append(
        "Gửi câu trả lời thảo luận kèm --to-account <id> (1 người) "
        "hoặc ghi danh sách to_account (tối đa 5 người) khi muốn hỏi "
        "tiếp nhiều người; không ghi to_account thì không ai bị "
        "đánh thức.")
    return lines


def build_brief(root, account_id, room, seqs, now=None):
    """Soan ban tom tat cho worker chat. Tra ve chuoi brief.

    ``seqs`` la snapshot luc lap ke hoach (chi tra loi dung cac seq
    nay). Tin trong phong lay tu center_messages.jsonl local."""
    ids.validate_bot_id(account_id)
    root = Path(root)
    if now is None:
        now = now_epoch()
    seq_set = set(seqs)
    messages = [entry for entry in center_dispatcher.read_local_messages(root)
                if (entry["record"].get("room") or "chung") == room]
    snapshot = [entry for entry in messages if entry["seq"] in seq_set]
    recent = messages[-RECENT_LIMIT:]
    titles = _task_titles(root)
    ack_line, ack_minutes = _delay_ack_line(snapshot, now)

    lines = []
    lines.append("=== BAN TOM TAT CHAT TRUNG TAM (T3h) ===")
    lines.append("Tài khoản: %s | Phòng: %s | Snapshot seqs: %s"
                 % (account_id, room,
                    ", ".join(str(seq) for seq in seqs) or "(rỗng)"))
    lines.append("")
    lines.append("TIN CẦN TRẢ LỜI (snapshot — chỉ trả lời đúng các seq "
                 "này, tin mới về trong lúc trả lời chờ chu kỳ sau):")
    if snapshot:
        for entry in snapshot:
            lines.append(_format_message(entry, titles, full_body=True,
                                         mark=" *"))
    else:
        lines.append("(không còn tin nào trong snapshot — có thể tin đã "
                     "được trả lời ở lượt khác; kết thúc im lặng, "
                     "không gửi gì)")
    lines.append("")
    lines.append("20 TIN GẦN NHẤT TRONG PHÒNG (bối cảnh; * = tin trong "
                 "snapshot):")
    if recent:
        for entry in recent:
            lines.append(_format_message(
                entry, titles,
                full_body=entry["seq"] in seq_set,
                mark=" *" if entry["seq"] in seq_set else ""))
    else:
        lines.append("(phòng chưa có tin nào)")
    lines.append("")
    lines.append("TIN GẮN VIỆC (1 dòng tiêu đề/trạng thái — xem mục "
                 "trên, dòng có [việc: ...]):")
    linked = [entry for entry in recent if entry["record"].get("task_id")]
    if linked:
        for entry in linked:
            task_id = entry["record"].get("task_id")
            title, status = titles.get(task_id, ("", ""))
            lines.append("- seq %s — việc %s: %s (%s)"
                         % (entry["seq"], task_id,
                            _truncate(str(title), TASK_TITLE_CHARS),
                            status))
    else:
        lines.append("(không có tin gắn việc trong 20 tin gần nhất)")
    lines.append("")
    lines.extend(_discussion_lines(root, room, seqs))
    lines.append("")
    lines.append("FILE ĐÍNH KÈM (chỉ mở khi câu hỏi liên quan):")
    files = []
    for entry in snapshot:
        meta = entry["record"].get("file")
        if isinstance(meta, dict) and meta.get("file_id"):
            files.append(meta)
    if files:
        for meta in files:
            lines.append(
                "- %s (%s bytes) — tải về khi cần: python3 "
                "~/workspace/multibot-code/muse-side/center_files.py "
                "--root <root> --account-id %s download %s %s"
                % (meta.get("file_name") or meta.get("file_id"),
                   meta.get("file_size") or "?",
                   account_id, meta.get("file_id"),
                   meta.get("sha256") or ""))
    else:
        lines.append("(không có file đính kèm trong snapshot)")
    lines.append("File quá lớn/không tải được thì trả lời bằng chữ và "
                 "nói rõ lý do, không im lặng bỏ qua.")
    lines.append("")
    lines.append("GỬI FILE (tối đa 1 file/lượt trả lời):")
    lines.append(
        "1) Tải file lên hub trước để lấy file_id (in ra meta JSON "
        "gồm file_id/file_name/file_size/sha256):\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_files.py "
        "--root <root> --account-id <id> upload <đường dẫn file>")
    lines.append(
        "2) Gửi tin kèm file (tin chỉ có file thì bỏ --body-file):\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_outbox.py "
        "--root <root> --account-id <id> "
        "--message-kind hoi --body-file <file nội dung> --file-id "
        "<f-...> --room <phòng>")
    lines.append(
        "Giới hạn: tối đa 50 MB/file. Tin kèm file chỉ chấp nhận kind "
        "hoi hoặc bao-cao; câu trả lời kèm file trong phòng riêng phải "
        "là tin hoi (vì bao-cao bắt buộc ở khung chung).")
    lines.append(
        "1 lượt upload và 1 lượt gửi tin là 2 request riêng, phải đi "
        "liền trong cùng lượt worker: tải lên xong gửi tin ngay; file "
        "chưa có tin tham chiếu bị dọn sau 7 ngày.")
    lines.append(
        "Tải lên thất bại thì trả lời bằng chữ và nói rõ không gửi "
        "được file (fallback text-only), không im lặng bỏ qua.")
    lines.append(
        "File lấy từ cả 2 nguồn: tự tạo dưới ~/workspace rồi tải lên, "
        "hoặc gửi lại file đã có.")
    lines.append("")
    lines.append("LUẬT TRẢ LỜI (chép nguyên văn — không tự đổi):")
    lines.append(REPLY_RULES)
    lines.append("")
    if ack_line is not None:
        lines.append("CÂU THỪA NHẬN CHẬM — TẠM THỜI (D7): tin tồn cũ "
                     "nhất đã chờ %d phút. Mở đầu câu trả lời gộp bằng "
                     "đúng câu này:" % ack_minutes)
        lines.append(ack_line)
        lines.append("(TẠM THỜI — chưa phải câu chính thức đã duyệt; "
                     "khi chủ nhân duyệt câu chính thức thì thay thế "
                     "ngay. KHÔNG ghi câu này vào "
                     "prompts/delay_ack_approved.txt.)")
    else:
        lines.append("CÂU THỪA NHẬN CHẬM: không cần — tin trong "
                     "snapshot còn mới (chờ < 60 giây); trả lời thẳng, "
                     "không mở đầu bằng câu thừa nhận.")
    lines.append("")
    lines.append("LỆNH MẪU (điền sẵn id — chạy đúng thứ tự):")
    seqs_arg = ",".join(str(seq) for seq in seqs)
    chat_key = center_claims.chat_room_key(room)
    claim_info = center_claims.holder_of(root, account_id, chat_key)
    wake_arg = ""
    if claim_info and claim_info.get("wake_id"):
        wake_arg = " --wake-id %s" % claim_info["wake_id"]
    lines.append(
        "0) Xác nhận bắt đầu lượt (claim hai pha 28/11 — chạy ngay, "
        "trước mọi bước khác; lệnh báo KHÔNG xác nhận được thì "
        "dừng, không trả lời lượt này):\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_claims.py "
        "ack-start --root <root> --account-id %s --task-id %s%s"
        % (account_id, chat_key, wake_arg))
    lines.append(
        "1) Tín hiệu \"đang trả lời\" (1 tín hiệu/lượt, sau khi giữ "
        "chỗ xong — qua outbox, không gọi HTTP trực tiếp):\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_outbox.py "
        "--root <root> --account-id %s --typing --room %s\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_outbox.py "
        "--root <root> --flush"
        % (account_id, room))
    lines.append(
        "2) Gửi câu trả lời gộp (soạn nội dung vào file dưới "
        "~/workspace/multibot-runtime/drafts/ rồi gửi — KHÔNG gắn "
        "--task-id khi không có việc kèm theo; có việc kèm theo thì "
        "gắn --task-id của việc đó; CẤM bịa task_id giả):\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_outbox.py "
        "--root <root> --account-id %s --message-kind hoi "
        "--body-file <file-noi-dung> --room %s"
        % (account_id, room))
    lines.append(
        "   Câu gộp > 5.000 ký tự thì tách thành nhiều tin theo thứ "
        "tự thời gian (mỗi tin ≤ 5.000 ký tự), gửi lần lượt từng "
        "tin bằng lệnh trên — không tóm tắt, không bỏ tin nào.")
    lines.append(
        "3) Ghi mốc đã trả lời (CHỈ SAU KHI câu trả lời đã vào "
        "outbox thành công — lệnh này tự kiểm chứng outbox, chưa có "
        "câu trả lời thì từ chối ghi):\n"
        "   python3 ~/workspace/multibot-code/muse-side/center_chat_brief.py "
        "--root <root> --account-id %s --room %s --seqs %s --mark-replied"
        % (account_id, room, seqs_arg))
    return "\n".join(lines) + "\n"


def mark_chat_replied(root, account_id, room, seqs, now=None):
    """Ghi moc replied_up_to_seq = max(seqs) sau khi cau tra loi da
    vao outbox thanh cong. Kiem chung: outbox phai co it nhat 1 dong
    tin kind=hoi cua dung tai khoan nay vao dung phong nay; chua co
    thi raise ValueError (khong ghi moc). Ghi tmp+rename duoi khoa
    chung ``center_state_lock`` (NS-L1); bo giu claim phong
    (best-effort). Tra ve moc moi."""
    ids.validate_bot_id(account_id)
    if room not in (None, "chung") and not (
            isinstance(room, str) and room.startswith("dm:")):
        raise ValueError("room phai la 'chung' hoac 'dm:<account_id>'")
    room = room or "chung"
    seqs = [int(seq) for seq in seqs]
    if not seqs:
        raise ValueError("seqs rong — khong ghi moc")
    root = Path(root)
    rows, _total, _skipped = center_outbox.read_outbox(root)
    found = any(
        isinstance(row, dict) and row.get("kind") == "hoi"
        and row.get("account_id") == account_id
        and (row.get("room") or "chung") == room
        for _line_no, row in rows
    )
    if not found:
        raise ValueError(
            "chua thay cau tra loi kind=hoi cua %s vao phong %s trong "
            "outbox — khong ghi moc (tra loi truoc roi ghi moc sau)"
            % (account_id, room))
    # Kiem chung outbox TRUOC khi giu khoa (chi doc, khong ghi
    # trang thai) — giu nguyen thu tu cu. Chu trinh doc-sua-ghi
    # trang thai ben duoi giu khoa chung (NS-L1) de khong ghi de
    # mat khoa ``discussion_rounds`` ma ``refresh_discussion_rounds``
    # vua tinh lai.
    with center_state_lock.chat_state_lock(root, account_id):
        state = center_dispatcher.read_chat_state(root, account_id)
        rooms = dict(state.get("rooms") or {})
        current = center_dispatcher.replied_up_to_seq(state, room)
        new_marker = max(current, max(seqs))
        entry = dict(rooms.get(room) or {})
        entry["replied_up_to_seq"] = new_marker
        entry["updated_at"] = now_iso()
        rooms[room] = entry
        data = {
            "account_id": account_id,
            "rooms": rooms,
            "discussion_rounds": state.get("discussion_rounds") or {},
        }
        ensure_dir(Path(root) / center_dispatcher.CHAT_STATE_DIRNAME)
        atomic_write_json(
            center_dispatcher.chat_state_path(root, account_id), data)
    try:
        center_claims.release_room(root, account_id, room)
    except (ValueError, OSError):
        pass
    return new_marker


def _parse_seqs(text):
    seqs = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            seqs.append(int(part))
        except ValueError:
            raise ValueError("seqs phai la danh sach so, phan cach "
                             "bang dau phay: %r" % part)
    if not seqs:
        raise ValueError("--seqs rong")
    return sorted(set(seqs))


def _log_worker_start(root, account_id, room, seqs, now):
    """Ghi 1 dong JSON vao
    ``center_chat_state/<account_id>_worker_start.jsonl`` moi lan
    worker xin brief (moc log worker chat bat dau chay, gop 22/11):
    de do khoang trong giua luc dispatcher danh thuc worker (log o
    hook multibot-incoming.jsonl) va luc worker that su bat dau chay.
    Moi tai khoan 1 file rieng; chi 1 worker chat/tai khoan chay tai
    1 thoi diem (cap chat=1, S2) nen append thang khong can khoa.
    Khong bao gio lam hong viec xin brief: loi ghi log chi in ra
    stderr, khong raise."""
    try:
        log_dir = Path(root) / "center_chat_state"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
             "epoch": now, "account_id": account_id,
             "room": room, "seqs": list(seqs)},
            ensure_ascii=False)
        with open(log_dir / ("%s_worker_start.jsonl" % account_id),
                  "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        print("center_chat_brief: khong ghi duoc moc worker start: %s"
              % exc, file=sys.stderr)


def main(argv=None):
    """CLI: brief hoac --mark-replied (xem docstring module)."""
    parser = argparse.ArgumentParser(
        description="Ban tom tat + ghi moc cho worker chat trung tam "
                    "(T3h).")
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help="thư mục gốc phía Muse (mặc định: "
                             "%s)" % DEFAULT_ROOT)
    parser.add_argument("--account-id", default=None,
                        help="tài khoản trả lời chat")
    parser.add_argument("--room", default=None,
                        help="phòng ('chung' hoặc 'dm:<account_id>')")
    parser.add_argument("--seqs", default=None,
                        help="snapshot seqs, phân cách bằng dấu phẩy")
    parser.add_argument("--mark-replied", action="store_true",
                        help="ghi mốc đã trả lời (sau khi câu trả lời "
                             "đã vào outbox thành công)")
    args = parser.parse_args(argv)
    if args.account_id is None or args.room is None \
            or args.seqs is None:
        print("center_chat_brief: --account-id, --room, --seqs la bat "
              "buoc", file=sys.stderr)
        return 2
    try:
        seqs = _parse_seqs(args.seqs)
        if args.mark_replied:
            marker = mark_chat_replied(
                args.root, args.account_id, args.room, seqs)
            print("center_chat_brief: da ghi moc %s=%d"
                  % (args.room, marker))
            return 0
        sys.stdout.write(build_brief(
            args.root, args.account_id, args.room, seqs))
        _log_worker_start(args.root, args.account_id, args.room, seqs,
                          now_epoch())
        return 0
    except ValueError as exc:
        print("center_chat_brief: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
