#!/usr/bin/env python3
"""Vòng thảo luận tài khoản phía Muse (v0.6.0, T3l — DESIGN §3.10,
doc 10 §3.8/§6.6, frag-05 §1.6/§2/§6.4; quyết qua đêm 20/11 D9/D10).

Helper thuần + tính lại trạng thái vòng từ tin nhắn local. Stdlib-only.

Khóa vòng (D9 — Q-V060-31 ĐÃ CHỐT): ``(phòng, người mở vòng, tập
người tham gia)`` — tập người tham gia = người gửi tin mở vòng +
danh sách ``to_account`` của tin đó::

    discussion_key(room, opener, participants)
        -> "room|opener|p1,p2,..."  (participants đã sắp xếp)

Đếm lượt: 1 lượt = 1 tin ``hoi`` thảo luận (``kind == "hoi"``,
``to_account`` khác rỗng, người gửi không phải chủ nhân) trong 1
vòng; tin mở vòng là lượt 1, chạm 5 thì dừng (``state == "dong"``).
Tin của chủ nhân (``from_account == "owner"``) vào phòng → ``count``
về 0, ``state`` về ``"mo"`` cho mọi vòng trong phòng đó (chủ nhân là
người duy nhất mở vòng mới — tin thảo luận của tài khoản sau khi
vòng đã dừng KHÔNG mở vòng mới, KHÔNG đánh thức ai).

Trạng thái vòng lưu cùng file mốc "đã trả lời" của T3h
(``center_chat_state/<account_id>.json``, khóa
``discussion_rounds`` — ``{count: 0–5, state: "mo"|"dong",
opened_seq, last_seq}``). :func:`refresh_discussion_rounds` tính
lại TOÀN BỘ từ ``center_messages.jsonl`` mỗi chu kỳ lập kế hoạch
(idempotent — không trôi, không cộng dồn): ghi file CHỈ khi bản
tính lại khác bản đã lưu, và KHÔNG BAO GIỜ chạm khóa ``rooms``
(mốc đã trả lời vẫn chỉ worker chat ghi sau khi trả lời xong —
xem ``center_chat_brief.mark_chat_replied``).

Chống vòng lặp (frag-05 §2, NS-28): lớp 1 — chỉ trả lời khi
``to_account`` là mình (câu trả lời không ghi ``to_account`` thì
không đánh thức ai); lớp 2 — đếm lượt, hết 5 thì dừng; lớp 3 —
CHỈ CÒN các phanh tốc độ có sẵn (claim 15 phút, cap 2 worker/tài
khoản, 1 lượt đánh thức/chu kỳ, outbox 1 dòng/10 giây, receiver 1
req/2 giây) — các phanh này chỉ làm chậm vòng lặp, KHÔNG dừng
được vòng lặp (lớp 3 cũ "tin do chính mình vừa gửi thì không tự
đánh thức lại chính mình" đã bị W2 bao phủ nên BỎ).

Ngữ nghĩa chặn W7 (khớp frag-05 §1.9 + test T3h): vòng chạm 5
lượt thì KHÔNG đánh thức ai nữa cho vòng đó — kể cả người nhận
của chính tin thứ 5 (tin mở vòng + tối đa 4 tin trả lời qua lại;
tin thứ 5 là câu trả lời cuối, không ai trả lời tiếp).
"""

from __future__ import annotations

from pathlib import Path

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
from muse_common import (
    atomic_write_json,
    ensure_dir,
    read_json,
    read_jsonl,
)
from shared import ids

import center_state_lock

DISCUSSION_ROUND_LIMIT = 5  # W7: vòng chạm 5 lượt thì dừng
ROUND_STATE_OPEN = "mo"
ROUND_STATE_CLOSED = "dong"
CHAT_STATE_DIRNAME = "center_chat_state"


# --- Chuẩn hóa ---

def as_account_list(value):
    """Chuẩn hóa to_account/mentions (chuỗi v3 / danh sách v4) -> list."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, str) and item]
    return []


def is_discussion_message(record):
    """Tin thảo luận: kind=hoi, to_account khác rỗng, người gửi
    không phải chủ nhân và không phải tin hub tự sinh. Tin của chủ
    nhân KHÔNG tính vào lượt (frag-05 §1.6); tin không ghi
    to_account cũng không tính; tin hub tự sinh (source="hub")
    không tính dù có to_account."""
    if not isinstance(record, dict):
        return False
    if record.get("kind") != "hoi":
        return False
    if record.get("source") == "hub":
        return False
    sender = record.get("from_account")
    if not isinstance(sender, str) or not sender or sender == "owner":
        return False
    return bool(as_account_list(record.get("to_account")))


def discussion_key(room, opener, participants):
    """Khóa vòng "room|opener|p1,p2,..." (D9): participants đã sắp
    xếp, gồm người mở vòng + những người trong to_account của tin
    mở vòng. Raise ValueError nếu đầu vào không hợp lệ."""
    if not isinstance(room, str) or not room:
        raise ValueError("room phải là chuỗi khác rỗng")
    if not isinstance(opener, str) or not opener:
        raise ValueError("opener phải là chuỗi khác rỗng")
    ordered = sorted({item for item in participants
                      if isinstance(item, str) and item})
    if not ordered:
        raise ValueError("participants rỗng — vòng cần ít nhất 1 người")
    if opener not in ordered:
        raise ValueError("participants phải gồm người mở vòng")
    return "%s|%s|%s" % (room, opener, ",".join(ordered))


def parse_discussion_key(key):
    """Tách khóa vòng -> (room, opener, participants tuple) hoặc
    None nếu khóa không đúng dạng. Tập tài khoản của vòng =
    {opener} ∪ participants (khóa T3h cũ chỉ ghi người còn lại ở
    phần thứ ba nên phải hợp với opener mới đủ tập)."""
    if not isinstance(key, str):
        return None
    parts = key.split("|")
    if len(parts) != 3:
        return None
    room, opener, raw = parts
    if not room or not opener or not raw:
        return None
    participants = tuple(item for item in raw.split(",") if item)
    if not participants:
        return None
    return room, opener, participants


def round_accounts(key):
    """Tập tài khoản của 1 vòng (theo khóa) hoặc None nếu khóa hỏng."""
    parsed = parse_discussion_key(key)
    if parsed is None:
        return None
    _room, opener, participants = parsed
    return {opener} | set(participants)


def round_for_message(rounds, room, from_account, to_account):
    """Tìm vòng mà 1 tin thảo luận thuộc về: cùng phòng và tập
    người tham gia của tin ({người gửi} ∪ to_account) là tập con
    của tập tài khoản vòng đó. Ưu tiên: khớp chính xác tập trước,
    rồi vòng có tập nhỏ nhất, rồi vòng hoạt động gần nhất
    (last_seq lớn nhất). Trả về (key, round_dict) hoặc None."""
    if not isinstance(rounds, dict) or not rounds:
        return None
    if not isinstance(from_account, str) or not from_account:
        return None
    message_set = {from_account} | set(as_account_list(to_account))
    if not message_set:
        return None
    best = None
    best_rank = None
    for key, value in rounds.items():
        accounts = round_accounts(key)
        if accounts is None:
            continue
        parsed = parse_discussion_key(key)
        if parsed is None or parsed[0] != room:
            continue
        if not message_set <= accounts:
            continue
        if not isinstance(value, dict):
            continue
        last_seq = value.get("last_seq")
        last_seq = last_seq if isinstance(last_seq, int) \
            and not isinstance(last_seq, bool) else 0
        # Khớp chính xác (0) trước tập con (1); tập càng nhỏ càng
        # trước; last_seq càng lớn càng trước.
        rank = (0 if message_set == accounts else 1,
                len(accounts), -last_seq, key)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = (key, value)
    return best


def is_round_closed_for_message(rounds, room, from_account, to_account):
    """W7: vòng của chính tin này đã đóng (state "dong", count >= 5)
    thì chặn đánh thức. Tin chưa thuộc vòng nào (vòng mới) thì
    không chặn."""
    found = round_for_message(rounds, room, from_account, to_account)
    if found is None:
        return False
    _key, value = found
    count = value.get("count")
    return value.get("state") == ROUND_STATE_CLOSED \
        and isinstance(count, int) and not isinstance(count, bool) \
        and count >= DISCUSSION_ROUND_LIMIT


# --- Tính lại vòng từ tin nhắn ---

def compute_discussion_rounds(messages):
    """Tính lại toàn bộ vòng thảo luận từ danh sách tin nhắn local
    (mỗi phần tử {"seq": int, "record": dict}, theo seq tăng dần).

    - Tin thảo luận (xem is_discussion_message) thuộc vòng khớp
      nhất (cùng phòng, tập người tham gia là tập con của vòng):
      count += 1 (trần 5); chạm 5 -> state "dong"; last_seq = seq
      tin mới nhất thuộc vòng (kể cả tin sau khi vòng đã dừng —
      tin đó vẫn thuộc vòng, chỉ không đánh thức ai).
    - Không vòng nào khớp -> mở vòng mới, khóa theo (phòng, người
      gửi tin này, tập người tham gia của tin này), count=1.
    - Tin của chủ nhân vào phòng -> count về 0, state về "mo" cho
      mọi vòng trong phòng đó; tin thảo luận tiếp theo khớp vòng đó
      là lượt 1 của lần mở mới (opened_seq cập nhật theo tin đó).
      Vòng count==0 được GIỮ LẠI (không lược bỏ) — đúng chữ mô
      tả reset trong nhiệm vụ (count 0–5, state "mo").
    - Tin thảo luận sau khi vòng đã dừng vẫn thuộc vòng đó (count
      giữ 5, last_seq cập nhật) — chỉ không đánh thức ai.

    Trả về dict khóa vòng -> {count, state, opened_seq, last_seq}."""
    rounds = {}
    ordered = sorted(
        (entry for entry in messages or []
         if isinstance(entry, dict)
         and isinstance(entry.get("seq"), int)
         and not isinstance(entry.get("seq"), bool)
         and isinstance(entry.get("record"), dict)),
        key=lambda entry: entry["seq"])
    for entry in ordered:
        seq = entry["seq"]
        record = entry["record"]
        room = record.get("room") or "chung"
        if not isinstance(room, str):
            continue
        sender = record.get("from_account")
        if sender == "owner":
            # Chủ nhân xen tin: mở lại mọi vòng trong phòng này.
            for key, value in rounds.items():
                parsed = parse_discussion_key(key)
                if parsed is None or parsed[0] != room:
                    continue
                if not isinstance(value, dict):
                    continue
                value["count"] = 0
                value["state"] = ROUND_STATE_OPEN
            continue
        if not is_discussion_message(record):
            continue
        to_accounts = as_account_list(record.get("to_account"))
        found = round_for_message(rounds, room, sender, to_accounts)
        if found is None:
            participants = sorted({sender} | set(to_accounts))
            key = discussion_key(room, sender, participants)
            rounds[key] = {
                "count": 1,
                "state": ROUND_STATE_OPEN,
                "opened_seq": seq,
                "last_seq": seq,
            }
            continue
        key, value = found
        parsed = parse_discussion_key(key)
        if value.get("state") == ROUND_STATE_CLOSED \
                and parsed is not None and parsed[1] == sender \
                and len(to_accounts) >= 2:
            # Sua 28/11: nguoi mo vong gui tin moi sau khi vong
            # dong thi mo vong moi (luot 1) — de tai moi cua
            # truong nhom khong bi vong cu chan.
            value["count"] = 1
            value["state"] = ROUND_STATE_OPEN
            value["opened_seq"] = seq
        elif value.get("count") == 0:
            # Vòng vừa được chủ nhân mở lại: tin này là lượt 1.
            value["opened_seq"] = seq
            value["count"] = 1
            value["state"] = ROUND_STATE_OPEN
        elif isinstance(value.get("count"), int) \
                and value["count"] < DISCUSSION_ROUND_LIMIT:
            value["count"] += 1
            if value["count"] >= DISCUSSION_ROUND_LIMIT:
                value["state"] = ROUND_STATE_CLOSED
        # count đã chạm 5: giữ nguyên 5/"dong", chỉ cập nhật last_seq.
        value["last_seq"] = seq
    return rounds


# --- Đọc/ghi trạng thái vòng ---

def _read_local_messages(root):
    """Mọi tin local trong center_messages.jsonl, theo seq tăng dần
    (bản sao gọn của center_dispatcher.read_local_messages — tránh
    import vòng)."""
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


def chat_state_path(root, account_id):
    ids.validate_bot_id(account_id)
    return Path(root) / CHAT_STATE_DIRNAME / ("%s.json" % account_id)


def refresh_discussion_rounds(root, account_id):
    """Tính lại vòng thảo luận từ tin nhắn local và cập nhật khóa
    ``discussion_rounds`` trong
    ``center_chat_state/<account_id>.json``. Idempotent: chỉ ghi
    file khi bản tính lại khác bản đã lưu (không có vòng nào và
    chưa có file thì không tạo file); khóa ``rooms`` giữ nguyên.
    Chỉ khi sắp ghi mới giữ khóa chung
    ``center_state_lock.chat_state_lock`` (NS-L1): đọc lại trong
    khóa rồi mới ghi (double-check) để không ghi đè mất mốc
    ``replied_up_to_seq`` / vòng mà ``mark_chat_replied`` vừa ghi.
    Đường không-ghi (không có vòng) không chạm khóa, không tạo thư
    mục trạng thái — giữ đúng cam kết "dispatcher không tự ghi mốc"
    (GC-13). Trả về dict vòng vừa tính."""
    root = Path(root)
    computed = compute_discussion_rounds(_read_local_messages(root))
    data = read_json(chat_state_path(root, account_id))
    stored = {}
    rooms = {}
    if isinstance(data, dict):
        if isinstance(data.get("discussion_rounds"), dict):
            stored = data["discussion_rounds"]
        if isinstance(data.get("rooms"), dict):
            rooms = data["rooms"]
    if computed == stored:
        return computed
    with center_state_lock.chat_state_lock(root, account_id):
        data = read_json(chat_state_path(root, account_id))
        stored = {}
        rooms = {}
        if isinstance(data, dict):
            if isinstance(data.get("discussion_rounds"), dict):
                stored = data["discussion_rounds"]
            if isinstance(data.get("rooms"), dict):
                rooms = data["rooms"]
        if computed == stored:
            return computed
        ensure_dir(Path(root) / CHAT_STATE_DIRNAME)
        atomic_write_json(chat_state_path(root, account_id), {
            "account_id": account_id,
            "rooms": rooms,
            "discussion_rounds": computed,
        })
    return computed


# --- Tóm tắt vòng cho brief / output lập kế hoạch ---

def summarize_rounds_for_room(rounds, room):
    """Danh sách vòng trong 1 phòng, mỗi vòng 1 dict đầy đủ
    {key, opener, participants, count, state, remaining,
    opened_seq, last_seq}, xếp theo opened_seq tăng dần."""
    out = []
    if not isinstance(rounds, dict):
        return out
    for key, value in rounds.items():
        parsed = parse_discussion_key(key)
        if parsed is None or parsed[0] != room:
            continue
        if not isinstance(value, dict):
            continue
        count = value.get("count")
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        _room, opener, participants = parsed
        out.append({
            "key": key,
            "opener": opener,
            "participants": sorted({opener} | set(participants)),
            "count": count,
            "state": value.get("state") or ROUND_STATE_OPEN,
            "remaining": max(
                0, DISCUSSION_ROUND_LIMIT - count),
            "opened_seq": value.get("opened_seq"),
            "last_seq": value.get("last_seq"),
        })
    out.sort(key=lambda item: (
        item["opened_seq"] if isinstance(item["opened_seq"], int)
        else 0, item["key"]))
    return out


def round_keys_for_seqs(messages, rounds, seqs):
    """Tập khóa vòng mà các seq cho trước thuộc về (chỉ tin thảo
    luận mới thuộc vòng; tin chủ nhân/tin thường không thuộc vòng
    nào)."""
    keys = set()
    by_seq = {entry["seq"]: entry for entry in messages or []
              if isinstance(entry, dict)
              and isinstance(entry.get("seq"), int)}
    for seq in seqs or []:
        entry = by_seq.get(seq)
        if entry is None:
            continue
        record = entry.get("record")
        if not is_discussion_message(record):
            continue
        room = record.get("room") or "chung"
        if not isinstance(room, str):
            continue
        found = round_for_message(
            rounds, room, record.get("from_account"),
            as_account_list(record.get("to_account")))
        if found is not None:
            keys.add(found[0])
    return keys
