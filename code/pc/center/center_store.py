#!/usr/bin/env python3
"""Module store chung cua hub trung tam (module trung tam v0.2.2, chặng T1).

NHA GHI DUY NHAT cho kho trung tam tren PC (doc 10 §5.4): moi ghi vao
kho — receiver POST, UI (T3), gateway khi tra feed (CHI ghi ``seen_at``,
xem ben duoi) — deu phai qua module nay. Module giu ``store.lock``
(flock) trong suot moi chu trinh doc-sua-ghi + ghi snapshot kieu
tmp+rename + append ``events.jsonl`` kem flush/fsync.

Kho nam duoi goc du lieu (mac dinh ``~/multibot/``)::

  data/center/events.jsonl      append-only: 1 dong/su kien
  data/center/tasks_state.json  snapshot trang thai hien tai tung viec
  data/center/seq.json          {"center_seq": N, "floor_seq": M}
  data/center/seen_state.json   bang chung fetch dau tien
                                per-(account_id, task_id)
  data/center/store.lock        flock
  data/center/archive/          ban sao su kien/viec da cat tia
  data/center/files/            file dinh kem (v0.2.5): <file_id>
                                (bytes, 600) + <file_id>.meta.json
                                (sidecar, 600); thu muc 700
  data/center/drafts.json       kho nhap theo phong (v0.3.0, T3c):
                                {"version": 1, "owner": {room: text}}
  data/center/read_state.json   moc seq da doc/phong cua chu nhan
                                (v0.3.0, T3c): {"version": 1,
                                "owner": {room: seq}} — chi chu nhan
  ui-config/center.json         cau hinh (ngay tao + overdue_severe_hours)
  ui-config/accounts.json       danh sach tai khoan + co
                                ``center_participating`` (chi DOC co)

Thu muc ``data/center/`` 700, file kho 600. Khong secret nao o day.

Quy tac quan trong (thiet ke v0.2.2):

- 1 so thu tu chung ``center_seq`` cho ca viec + tin. Tao viec ghi 2
  dong lien tiep trong cung 1 lan giu khoa: viec seq N, tin
  ``giao-viec`` tu sinh seq N+1 (C-3). Neu chet giua chung, viec thanh
  "viec mo coi" (co dong tao viec, chua co tin): viec VAN duoc coi la
  da tao; store tu bu tin khi phat hien (retry cung ``client_key``
  hoac rebuild) — V2-NS-03.
- ``seen_at``: CHI module nay ghi, moc = thoi diem store commit xong
  ghi (gio hub, ISO-8601 kem offset), CHI cho lan fetch dau tien ma
  response feed thuc su chua viec do, do chinh ``assigned_to`` thuc
  hien. Lay le qua ``GET /tasks/<id>`` KHONG tinh ``seen_at`` (C-2).
- Chuyen tay (R5): reset ve ``moi``, ``seen_at`` ve ``null``; store
  xoa muc ``seen_state.json`` cua viec do; rebuild chi lay moc sau
  lan chuyen tay gan nhat (nguon moc: ``history.at`` — 2B-GC-04).
- Tin ``bao-cao`` thu 2 tro di sau ``da-bao-xong`` (V3-GC-01): chi
  luu + hien, khong doi trang thai, khong tinh lai la bao xong.
  Tieu chi ghep noi "cung luot outbox": chi tin ``bao-cao`` DAU TIEN
  cho viec do co ``seq`` lon hon ``seq`` cua su kien chuyen trang
  thai ``da-bao-xong`` gan nhat moi tinh la bao xong; tin sau do la
  bao tien do bo sung (``report_role`` = "progress").
- Prune (SUY LUAN — nhu queue prune 5000/30 ngay): chi cat su kien
  co ``seq <= watermark`` (watermark = seq nho nhat cac tai khoan
  da keo; tai khoan im lang qua 30 ngay bi loai khoi min — NS-10);
  chua co cursor thi chi cat theo tuoi. Truoc khi cat, chep sang
  ``archive/`` (ca viec + tin). Snapshot ``tasks_state.json`` duoc
  giu nguyen khi prune (viec van tra duoc qua GET /tasks/<id>).
- Chan mau toi thieu R6: ``find_secret_like`` — 5 lop mau (center
  secret / queue secret / sync secret / bot token / mat khau UI)
  + 1 mau chung bat dang "secret/token/... = gia tri". Chi chan
  mau da biet; khong bat duoc moi dang lo (TS2 — rui ro con lai
  chu nhan da chap nhan). Test dung MAU GIA DANG, khong secret that.

Chi dung thu vien chuan Python. Khong doc token bot, khong doc
hang doi bot, khong ghi file cua tai. Log (neu co) chi metadata.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets as _secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

CENTER_VERSION = 1
SCHEMA_VERSION = 1  # schema tin nhan (1/2/3/4 theo
# message_schema_version). GIU NGUYEN = 1 (D1(h), quyet qua dem
# 20/11): tin hub tu sinh (_assign_message_record) khong bao gio
# co to_account la danh sach (chi null hoac chuoi 1 nguoi) nen
# khong can version 4; neu sau nay hub tu sinh tin dang danh
# sach thi moi doi hang nay.
TASK_SCHEMA_VERSION = 2  # schema việc v2 (v0.4.0): assigned_to là danh sách

TASK_KINDS = ("app", "nghien-cuu", "viet", "vanhanh", "khac")
TASK_STATUSES = ("moi", "da-nhan", "dang-lam", "da-bao-xong",
                 "da-xac-nhan", "huy")
TERMINAL_STATUSES = ("da-xac-nhan", "huy")
MESSAGE_KINDS = ("giao-viec", "hoi", "bao-cao")
# Tin POST /messages duoc phep gui tay: giao-viec CHI hub tu sinh.
POSTABLE_MESSAGE_KINDS = ("hoi", "bao-cao")

MAX_TITLE_LEN = 200          # ky tu Unicode (GC-18)
MAX_TASK_BODY_LEN = 10000    # ky tu Unicode (GC-19)
MAX_MESSAGE_BODY_LEN = 5000  # ky tu Unicode (GC-19)
MAX_NOTE_LEN = 5000          # ky tu Unicode (GC-19)
PRUNE_MAX_EVENTS = 5000      # SUY LUAN
PRUNE_MAX_AGE_DAYS = 30      # SUY LUAN
TYPING_TTL_SECONDS = 90  # v0.6.0, T3h — tin hieu "dang tra loi" tu
# het sau 90 giay (DA CHOT, §2d); GIAO DIEN kiem tra khi doc.
HUB_SOURCE = "hub"  # v0.6.0, T3h — D3: truong source cua tin hub tu
# sinh; thieu truong source = KHONG phai tin hub.
DEFAULT_OVERDUE_SEVERE_HOURS = 24  # NS-8, doi duoc qua center.json
OVERDUE_SEVERE_HOURS_MIN = 1    # SPEC TT20 (v0.5.0)
OVERDUE_SEVERE_HOURS_MAX = 168  # SPEC TT20 (v0.5.0)

# --- Don dep tu dong (v0.5.0, T3f — SPEC TT19, DESIGN §3.8/§6) -------
PRUNE_HOUR = 21  # gio don co dinh, gio Asia/Saigon (Q2 — KHONG co o
# dat gio; hen gio he thong chay theo gio dia phuong cua PC).
PRUNE_DAY_VALUES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PRUNE_RETENTION_DAYS_VALUES = (7, 15, 30, 60, 120)
DEFAULT_PRUNE_DAY = "sun"  # Chu nhat (Q2)
DEFAULT_PRUNE_RETENTION_DAYS = 30  # Q3
DEFAULT_PRUNE_NEAR_FULL_FRACTION = 0.8  # 80% kho 2 GB (Q2)
PRUNE_NEAR_FULL_MIN = 0.5  # SPEC TT19
PRUNE_NEAR_FULL_MAX = 0.95  # SPEC TT19
# Cac khoa duoc phep trong center.json (khoa la o POST -> tu choi
# toan bo — SPEC TT19/DESIGN §11; khoa la DA CO trong file thi giu
# nguyen khi ghi lai — GC-02).
CENTER_CONFIG_KEYS = ("prune_day", "prune_retention_days",
                      "prune_near_full_fraction", "overdue_severe_hours",
                      "shared_links_open_to_all")
# (Link chung, D-LC-16 — 21/11) Co "mo Link chung cho moi tai
# khoan", bool, mac dinh False. DIEN GIAI THEO DAP AN DA DUYET
# 17:31 21/11 (nghia (c) + luu san co): co DUOC LUU SAN de sau
# nay gan hanh vi ma khong phai doi schema, nhung CHUA GAN hanh
# vi mo rong nao — route them/xoa + render form/nut xoa lam viec
# nhu cu bat ke co true/false (D-LC-15/D-LC-17: cho cam san —
# placeholder, chua phai cong quyen that vi session UI khong mang
# account_id, xem §5.1 thiet ke). Doi sau khoi tuong co da co tac
# dung.
DEFAULT_SHARED_LINKS_OPEN_TO_ALL = False
PRUNE_TRIGGER_VALUES = ("schedule", "near_full", "manual")

# --- Kho file dinh kem (v0.2.5, Q-C7 — SPEC TT16, doc 10 §5.7) --------
MAX_FILE_BYTES = 50 * 1024 * 1024        # 50 MB/file — DOI 20/11 (sua Q-C7 cu 19/11)
MAX_FILE_STORE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB tong kho — DOI 20/11 (sua Q-C7 cu 19/11)
MAX_FILE_NAME_LEN = 200  # ky tu Unicode (cung cap voi tieu de viec)
# File khong co tin tham chieu xoa sau 7 ngay; lich don chay theo
# SPEC TT19 (v0.5.0): lich theo ngay 21:00 + tu don khi kho gan day
# + nut "Don ngay" — file co the nam lai toi da ~14 ngay neu chi
# cho lich tuan (7 ngay cho du tuoi + toi da 7 ngay toi ky don).
FILE_UNREFERENCED_MAX_AGE_DAYS = 7
FILE_ID_RE = re.compile(r"^f-[0-9a-f]{12}$")

ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TASK_ID_RE = re.compile(r"^t-[0-9a-f]{12}$")
MSG_ID_RE = re.compile(r"^m-[0-9a-f]{12}$")

# --- Phong chat rieng (v0.3.0, T3c — SPEC TT6/TT17, DESIGN §6) -------
ROOM_CHUNG = "chung"  # khung chung (mac dinh khi mo tab)
MAX_MENTIONS = 5  # account_id/tin (SUY LUAN — doc 10 §13; tach o T3d)
ROOM_RE = re.compile(r"^(chung|dm:[a-z0-9][a-z0-9-]{0,31})$")
# Tag ten giai doan 1 (v0.3.0, T3d — SPEC TT18, NS-02): chi khop
# "@<account_id>" go tay. Lookbehind chan khop giua tu/email
# (vd "a@muse-chinh" khong phai tag); token khong gioi han do dai
# o regex de khong khop dut 1 phan cua token dai (chi toan bo
# token duoc kiem tra voi danh sach tai khoan).
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([a-z0-9][a-z0-9-]*)")


def _as_assignee_list(value):
    """Chuan hoa ``assigned_to`` (chuoi v1 hoac danh sach v2) ->
    danh sach (rong neu khong hop le). Dung o moi noi doc ban ghi
    viec — ban ghi v1 cu (chuoi) doc tuong thich nhu danh sach
    1 nguoi (doc 10 §2.1)."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value
                if isinstance(item, str) and item]
    return []


def _as_to_account_list(value):
    """Chuan hoa ``to_account`` (v0.6.0, T3l — D1(i), SPEC TT6):
    None -> None; chuoi v1-v3 -> [chuoi] (danh sach 1 nguoi);
    danh sach v4 -> giu nguyen cac phan tu; kieu khac ->
    ValueError. Theo mau ``_as_assignee_list()`` cua
    ``assigned_to`` (v1 -> v2). Dung truoc khi bam dedupe
    (``_fingerprint_message``) de chuoi cu va danh sach 1 nguoi
    co cung dau van tay, va de kiem tra Luat 2 khong phu thuoc
    dang luu (chuoi hay danh sach)."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    raise ValueError("to_account phai la None, chuoi hoac danh sach")


def _tag_task_suppressed_by_law2(from_account, to_account):
    """Luat 2 (v0.6.0, T3l — SPEC TT6, PLAN NS-26; mo rong theo
    D14, QUYET-DINH-QUA-DEM-20-11.md): tin da ghi ro ``to_account``
    (chuoi hoac danh sach khong rong) thi tag trong tin chi nhac
    ten, KHONG sinh viec — ap cho CA tin tu tai khoan Muse LAN tin
    cua chu nhan gui tay qua o "Gui cho:" (receiver/UI). Ly do D14:
    o "Gui cho:" sinh ra de chu nhan goi ten tai khoan ma khong
    sinh viec; chi go ``@ten`` ma KHONG dung "Gui cho:"
    (``to_account`` rong/None = gui chung) thi van sinh viec theo
    P2. Kieu ``to_account`` la thi tra False (an toan: P2 nhu cu).
    ``from_account`` chi con de ghi chu, khong doi hanh vi."""
    try:
        targets = _as_to_account_list(to_account)
    except ValueError:
        return False
    return bool(targets)


def _normalize_assignees(assigned_to):
    """Kiem tra + chuan hoa danh sach nguoi nhan luc tao/chuyen tay:
    chuoi -> [chuoi]; danh sach -> bo trung (giu thu tu); 1..5 nguoi
    (MAX_MENTIONS); moi muc khop mau account_id. Sai -> CenterError
    400."""
    if isinstance(assigned_to, str):
        assigned_to = [assigned_to]
    if not isinstance(assigned_to, list) or not assigned_to:
        raise CenterError(400, "bad_assigned_to")
    seen = set()
    out = []
    for item in assigned_to:
        if not isinstance(item, str) or not ACCOUNT_ID_RE.match(item):
            raise CenterError(400, "bad_assigned_to")
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    if len(out) > MAX_MENTIONS:
        raise CenterError(400, "too_many_assignees")
    return out


def _normalize_task(task):
    """Chuan hoa ban ghi viec ve dang v2 (chi trong bo nho): assigned_to
    thanh danh sach; them reported_by/refused_by/source_msg_id mac
    dinh khi thieu (ban ghi v1 cu)."""
    task = dict(task)
    task["assigned_to"] = _as_assignee_list(task.get("assigned_to"))
    if not isinstance(task.get("reported_by"), list):
        task["reported_by"] = []
    else:
        task["reported_by"] = [a for a in task["reported_by"]
                               if isinstance(a, str)]
    if not isinstance(task.get("refused_by"), list):
        task["refused_by"] = []
    else:
        task["refused_by"] = [a for a in task["refused_by"]
                              if isinstance(a, str)]
    if "source_msg_id" not in task:
        task["source_msg_id"] = None
    return task


def parse_mentions(body, known_accounts):
    """Tach mention tu ``body`` (1 cho duy nhat — NS-02, T3d).

    Tra ve danh sach account_id + "owner" theo thu tu xuat hien
    dau tien (trung lap lay 1 lan), hoac None neu khong tag ai.
    Mention khong ton tai (khong trong ``known_accounts`` va khac
    "owner") thi BO khoi danh sach, giu nguyen chu "@..." trong
    body lam chu thuong, tin van ghi (Q-V030-1). Qua MAX_MENTIONS
    (tinh tren danh sach hop le, sau khi bo trung) -> CenterError
    400 "too_many_mentions", khong ghi gi (nguoi gui tach thanh
    nhieu tin roi gui lai — khong tu bo tag thua lang le).
    """
    found = []
    seen = set()
    for match in MENTION_RE.finditer(body or ""):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        if name == "owner" or name in known_accounts:
            found.append(name)
    if len(found) > MAX_MENTIONS:
        raise CenterError(400, "too_many_mentions")
    return found or None


def parse_room(value):
    """Chuan hoa phong: None/rong -> "chung"; hop le -> gia tri;
    sai mau -> CenterError 400 "bad_room"."""
    if value is None or value == "":
        return ROOM_CHUNG
    if isinstance(value, str) and ROOM_RE.match(value):
        return value
    raise CenterError(400, "bad_room")


def message_schema_version(room, mentions, has_file, to_account=None):
    """Quy tac v4 (v0.6.0, T3l — D1(h), SPEC TT6): = 4 khi
    ``to_account`` la danh sach (ke ca danh sach 1 nguoi);
    = 3 khi room khac "chung" hoac mentions khong rong;
    = 2 khi CHI co file; = 1 khi chi text. Quy tac 4 thang quy
    tac 3; quy tac 3 thang quy tac 2; mentions rong/None khong
    tang version. Chuoi cu (v1-v3) khong phai danh sach nen giu
    luat cu — tuong thich nguoc."""
    if isinstance(to_account, list):
        return 4
    if room != ROOM_CHUNG or (mentions is not None and len(mentions) > 0):
        return 3
    if has_file:
        return 2
    return 1

# --- Bang chuyen doi trang thai (doc 10 §2.2) ---------------------------
# Moi luat: tap vai tro duoc phep + note co bat buoc khong.
# Vai tro: "assigned_to" (tai khoan nhan), "assigned_from" (nguoi giao),
# "owner" (chu nhan — chi qua UI o T3, khong qua receiver).
# Rieng moi -> huy co 2 luat: assigned_to tu choi viec (note bat buoc,
# quyet R4) va assigned_from/owner huy (khong can note).
TRANSITIONS = {
    ("moi", "da-nhan"): [{"by": ("assigned_to",), "note_required": False}],
    ("moi", "huy"): [
        {"by": ("assigned_to",), "note_required": True},
        {"by": ("assigned_from", "owner"), "note_required": False},
    ],
    ("da-nhan", "dang-lam"): [{"by": ("assigned_to",),
                                "note_required": False}],
    ("da-nhan", "huy"): [{"by": ("assigned_from", "owner"),
                           "note_required": False}],
    ("dang-lam", "da-bao-xong"): [{"by": ("assigned_to",),
                                   "note_required": False}],
    ("dang-lam", "huy"): [{"by": ("assigned_from", "owner"),
                           "note_required": False}],
    ("da-bao-xong", "da-xac-nhan"): [{"by": ("assigned_from", "owner"),
                                       "note_required": False}],
    ("da-bao-xong", "dang-lam"): [{"by": ("assigned_from", "owner"),
                                    "note_required": True}],
    ("da-bao-xong", "huy"): [{"by": ("assigned_from", "owner"),
                               "note_required": False}],
}

# --- Chan mau toi thieu R6 (quyet R6, 19/11) ---------------------------
# Thu tu quan trong: lop cu the truoc, mau chung sau. Moi mau la
# (nhan, regex, mo ta). Regex chi khop DANG "tu khoa + gia tri" hoac
# dang token dac thu (Telegram ``<so>:<chuoi>``, ``sk-...``) — khong
# khop chuoi ngau nhien dai de tranh chan nham van ban thuong.
# GIA TRI TRONG TEST CHI LA MAU GIA DANG ("TEST-FAKE-..."), khong bao
# gio la secret that hay gia dang that.
SECRET_LIKE_PATTERNS = (
    ("center_secret",
     re.compile(r"center[-_ ]?(fetch|push)?[-_ ]?secret\s*[:=]\s*\S+",
                re.IGNORECASE),
     "mau center secret"),
    ("queue_secret",
     re.compile(r"queue[-_ ]?secret\s*[:=]\s*\S+", re.IGNORECASE),
     "mau queue secret"),
    ("sync_secret",
     re.compile(r"sync[-_ ]?secret\s*[:=]\s*\S+", re.IGNORECASE),
     "mau sync secret"),
    ("bot_token",
     re.compile(r"(bot[-_ ]?token|telegram[-_ ]?token|zalo[-_ ]?token)"
                r"\s*[:=]\s*\S+|\b\d{5,}:[A-Za-z0-9_\-]{20,}\b"
                r"|\bsk-[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE),
     "mau bot token"),
    ("ui_password",
     re.compile(r"(mat[-_ ]?khau[-_ ]?(ui|dang[-_ ]?nhap)"
                r"|ui[-_ ]?pass(word)?|ui[-_ ]?mat[-_ ]?khau)"
                r"\s*[:=]\s*\S+", re.IGNORECASE),
     "mau mat khau UI"),
    ("secret_generic",
     re.compile(r"\b(secret|token|password|mat[-_ ]?khau)\b"
                r"\s*[:=]\s*\S{8,}", re.IGNORECASE),
     "mau chung secret/token/password"),
)


def find_secret_like(text):
    """Tra ve nhan lop mau R6 khop trong ``text``, hoac None.

    Chi nhan dien mau da biet (TS2). Khong log/ghi gia tri khop.
    """
    if not isinstance(text, str) or not text:
        return None
    for label, pattern, _desc in SECRET_LIKE_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _disposition_filename(file_name):
    """Ten file an toan cho header Content-Disposition: ban ASCII
    (khong dau ngoac/keo) + ban RFC 5987. Dung chung cho ca
    attachment va inline (chi khac tu dau)."""
    from urllib.parse import quote

    safe = "".join(ch for ch in (file_name or "")
                   if ch.isascii() and ch not in '"\\')
    safe = safe.strip() or "file"
    return safe, quote(file_name or "file", safe="")


def attachment_disposition(file_name):
    """Gia tri header Content-Disposition an toan cho tai file ve.

    Luon ``attachment`` (trinh duyet tai ve, khong mo inline) +
    ``filename*`` RFC 5987 (ten da kiem tra CR/LF luc tai len, quote
    them de chac). Ten fallback ASCII cho trinh duyet cu.
    """
    safe, quoted = _disposition_filename(file_name)
    return ('attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (safe, quoted))


def inline_disposition(file_name):
    """Gia tri header Content-Disposition de trinh duyet MO inline
    (chi dung cho anh da biet — caller kiem tra duoi truoc bang
    ``ui_pages.image_content_type``). Ten xu ly giong
    ``attachment_disposition``, chi doi ``attachment`` thanh
    ``inline`` (xem truoc anh phuong an A, 21/11 — SPEC TT16)."""
    safe, quoted = _disposition_filename(file_name)
    return ('inline; filename="%s"; filename*=UTF-8\'\'%s'
            % (safe, quoted))


class CenterError(Exception):
    """Loi nghiep vu trung tam, mang theo ma HTTP + ma loi JSON."""

    def __init__(self, http_status, error, detail=None):
        super().__init__(error)
        self.http_status = http_status
        self.error = error
        self.detail = detail


def now_iso():
    """Gio hub hien tai, ISO-8601 kem offset (gio PC, dong bo NTP)."""
    return datetime.now().astimezone().isoformat()


def parse_iso_timestamp(text):
    """Parse ISO-8601 thanh datetime co mui gio; khong parse duoc -> None.

    Chuoi khong kem mui gio bi tu choi (tra None): do THIEU offset
    chu khong doan gio dia phuong — due_at/created_at/seen_at bat
    buoc kem offset (NS-24).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _write_json_atomic(path, data, mode=0o600):
    """Ghi JSON kieu tmp+rename + fsync (ghi hong thi file cu con)."""
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, mode)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _fingerprint_create(title, body, kind, assigned_to, due_at):
    """Dau van tay noi dung tao viec (dedupe client_key — V2-NS-09).
    assigned_to chuan hoa ve danh sach truoc khi bam: chuoi v1 va
    danh sach v2 1 nguoi co cung dau van tay."""
    blob = json.dumps([title, body, kind,
                       _as_assignee_list(assigned_to), due_at],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _fingerprint_message(message):
    """Dau van tay noi dung tin (dedupe client_key — V2-NS-09):
    kind + body + room + task_id + to_account + file_id.
    (v0.6.0, T3l — D1(i)): ``to_account`` chuan hoa ve danh sach
    TRUOC KHI bam (dang ``_as_to_account_list()``) de chuoi v1
    va danh sach v4 1 nguoi co cung dau van tay — gui lai cung
    ``client_key`` sau nang cap khong 409 oan. Kieu la -> giu
    nguyen gia tri (khong crash duong dedupe)."""
    file_id = None
    file_meta = message.get("file")
    if isinstance(file_meta, dict):
        file_id = file_meta.get("file_id")
    elif isinstance(message.get("file_id"), str):
        file_id = message.get("file_id")
    try:
        to_account = _as_to_account_list(message.get("to_account"))
    except ValueError:
        to_account = message.get("to_account")
    blob = json.dumps([message.get("kind"), message.get("body"),
                       message.get("room"), message.get("task_id"),
                       to_account, file_id],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def typing_entry_is_live(entry, now=None):
    """True khi muc typing con han (started_at + 90 giay > now).
    Dau moi TTL duy nhat (NS-23): GIAO DIEN kiem tra khi doc;
    receiver khong tu xoa theo gio (chi xoa khi tin hoi that vao
    dung phong — xem append_message)."""
    if not isinstance(entry, dict):
        return False
    started_at = entry.get("started_at")
    if isinstance(started_at, bool) \
            or not isinstance(started_at, (int, float)):
        return False
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    return started_at + TYPING_TTL_SECONDS > now


def read_typing(root):
    """Doc data/center/typing.json -> {account_id: {room: entry}}.
    CHI DOC. File thieu/hong/khong phai dict -> {}."""
    data = _read_json(Path(root) / "data" / "center" / "typing.json")
    return data if isinstance(data, dict) else {}


# --- Phong dang chat do (v0.6.0, T3i — DESIGN §8.5) -------------------
# File data/center/chat_ui_state.json (600):
# {"version": 1, "owner": {"last_room": "chung", "updated_at": null}}
# Chi chua ma phong + gio ghi; khong secret, khong noi dung tin.
CHAT_UI_STATE_VERSION = 1


def _chat_ui_state_path(root):
    return Path(root) / "data" / "center" / "chat_ui_state.json"


def get_last_chat_room(root):
    """Phong chu nhan dang chat do (luu phia may chu).

    File thieu/hong/gia tri khong hop le -> "chung"; file hong thi
    ve mac dinh, KHONG ghi de file hong (cung luat fallback nhu
    ui_prefs.json). Phong luu khong con ton tai thi lop goi (UI)
    rot lang le ve "chung" (Q-V060-11)."""
    data = _read_json(_chat_ui_state_path(root))
    if isinstance(data, dict) and isinstance(data.get("owner"), dict):
        room = data["owner"].get("last_room")
        if isinstance(room, str) and ROOM_RE.match(room):
            return room
    return ROOM_CHUNG


def set_last_chat_room(root, room):
    """Luu phong dang chat do. Phong khong hop le (sai mau
    ``parse_room``) -> khong ghi gi. Tra ve True neu da ghi."""
    try:
        room = parse_room(room)
    except CenterError:
        return False
    path = _chat_ui_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        path,
        {"version": CHAT_UI_STATE_VERSION,
         "owner": {"last_room": room,
                   "updated_at": now_iso()}},
        mode=0o600)
    return True


def read_accounts(root):
    """Doc ui-config/accounts.json -> {account_id: record}.

    CHI DOC. Co ``center_participating`` mac dinh false khi vang
    (TT9). Muc khong khop mau account_id hoac thieu display_name
    bi bo (khong crash, khong doan). File thieu/hong -> {}.
    """
    root = Path(root)
    data = _read_json(root / "ui-config" / "accounts.json")
    accounts = {}
    if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
        for account_id, record in data["accounts"].items():
            if not isinstance(account_id, str) \
                    or not ACCOUNT_ID_RE.match(account_id):
                continue
            if not isinstance(record, dict):
                continue
            name = record.get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            accounts[account_id] = {
                "display_name": name,
                "notes": record.get("notes") if isinstance(
                    record.get("notes"), str) else "",
                "created_at": record.get("created_at") if isinstance(
                    record.get("created_at"), str) else "",
                "center_participating": bool(
                    record.get("center_participating", False)),
                # (v0.6.0, T3l — SPEC TT25) Cong tac tat thao luan
                # tung tai khoan: khoa "center_discussion_enabled"
                # trong accounts.json, mac dinh True (mo). Vang
                # hoac gia tri la -> True, khong doan, khong crash.
                # Chi chu nhan bat/tat tren trang Tai khoan (ghi
                # audit); receiver doc co nay de tu choi 403
                # discussion_disabled.
                "center_discussion_enabled": (
                    record.get("center_discussion_enabled", True)
                    if isinstance(
                        record.get("center_discussion_enabled", True),
                        bool)
                    else True),
            }
    return accounts


def load_center_config(root):
    """Doc ui-config/center.json -> cau hinh (khong secret, khong
    danh sach tai khoan). Thieu/hong -> mac dinh (NS-8). Thieu tung
    khoa -> mac dinh tung khoa; gia tri ngoai khoang cho phep -> coi
    nhu thieu khoa do (mac dinh), khong crash (v0.5.0, DESIGN §6)."""
    data = _read_json(Path(root) / "ui-config" / "center.json")
    config = {"version": 1, "created_at": None,
              "overdue_severe_hours": DEFAULT_OVERDUE_SEVERE_HOURS,
              "prune_day": DEFAULT_PRUNE_DAY,
              "prune_retention_days": DEFAULT_PRUNE_RETENTION_DAYS,
              "prune_near_full_fraction": DEFAULT_PRUNE_NEAR_FULL_FRACTION,
              "shared_links_open_to_all":
                  DEFAULT_SHARED_LINKS_OPEN_TO_ALL}
    if isinstance(data, dict):
        severe = data.get("overdue_severe_hours")
        if isinstance(severe, (int, float)) and not isinstance(severe, bool) \
                and OVERDUE_SEVERE_HOURS_MIN <= severe \
                <= OVERDUE_SEVERE_HOURS_MAX:
            config["overdue_severe_hours"] = severe
        if isinstance(data.get("created_at"), str):
            config["created_at"] = data["created_at"]
        day = data.get("prune_day")
        if isinstance(day, str) and day in PRUNE_DAY_VALUES:
            config["prune_day"] = day
        retention = data.get("prune_retention_days")
        if isinstance(retention, int) and not isinstance(retention, bool) \
                and retention in PRUNE_RETENTION_DAYS_VALUES:
            config["prune_retention_days"] = retention
        fraction = data.get("prune_near_full_fraction")
        if isinstance(fraction, (int, float)) \
                and not isinstance(fraction, bool) \
                and PRUNE_NEAR_FULL_MIN <= fraction <= PRUNE_NEAR_FULL_MAX:
            config["prune_near_full_fraction"] = float(fraction)
        flag = data.get("shared_links_open_to_all")
        if isinstance(flag, bool):
            config["shared_links_open_to_all"] = flag
    return config


def validate_center_config_updates(updates):
    """Kiem tra dict cap nhat center.json (tu POST /api/settings/
    center). Tra ve (clean, loi): clean la dict gia tri da chuan hoa
    (chi cac khoa hop le); loi la chuoi tieng Viet hoac None.

    Khoa la -> loi (tu choi TOAN BO, khong ghi gi — SPEC TT19);
    gia tri ngoai khoang -> loi (400, khong ghi gi)."""
    if not isinstance(updates, dict):
        return None, "dữ liệu không hợp lệ"
    clean = {}
    for key, raw in updates.items():
        if key not in CENTER_CONFIG_KEYS:
            return None, "khóa không hợp lệ: %s" % key
        if key == "prune_day":
            if not isinstance(raw, str) or raw not in PRUNE_DAY_VALUES:
                return None, "ngày chạy dọn không hợp lệ"
            clean[key] = raw
        elif key == "prune_retention_days":
            if isinstance(raw, bool) or not isinstance(raw, int) \
                    or raw not in PRUNE_RETENTION_DAYS_VALUES:
                return None, ("thời gian giữ lại phải là một trong "
                              "7/15/30/60/120 ngày")
            clean[key] = raw
        elif key == "prune_near_full_fraction":
            if isinstance(raw, bool) \
                    or not isinstance(raw, (int, float)) \
                    or not PRUNE_NEAR_FULL_MIN <= raw <= PRUNE_NEAR_FULL_MAX:
                return None, "ngưỡng tự dọn phải trong khoảng 50–95%"
            clean[key] = float(raw)
        elif key == "overdue_severe_hours":
            if isinstance(raw, bool) or not isinstance(raw, int) \
                    or not OVERDUE_SEVERE_HOURS_MIN <= raw \
                    <= OVERDUE_SEVERE_HOURS_MAX:
                return None, "số giờ quá hạn phải trong khoảng 1–168"
            clean[key] = raw
        elif key == "shared_links_open_to_all":
            # (Link chung, D-LC-16) Nhanh bool: chi nhan True/False
            # that, gia tri khac -> 400. PHAI kiem isinstance(raw,
            # bool) o nhanh nay — isinstance(True, int) la True nen
            # neu de lọt vao nhanh so thi True bi nhan nhu so 1.
            if not isinstance(raw, bool):
                return None, "giá trị không hợp lệ"
            clean[key] = raw
    return clean, None


@contextmanager
def _center_config_lock(root):
    """Khoa flock rieng cho chu trinh doc-sua-ghi center.json (cung
    mau _ui_prefs_lock — khong dung chung lock voi ui_prefs de doi
    cai dat khong chan toggle node)."""
    lock_path = Path(root) / "ui-config" / "center.json.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save_center_config(root, updates):
    """Ghi cac khoa da kiem tra (validate truoc) vao
    ui-config/center.json: flock + tmp+rename, quyen 600.

    Khoa la DA CO trong file duoc giu nguyen (GC-02 — chi tu choi
    khoa la o POST moi, tranh mat du lieu khi rollback phien ban).
    Tra ve dict cau hinh day du sau khi ghi (da chuan hoa)."""
    root = Path(root)
    path = root / "ui-config" / "center.json"
    with _center_config_lock(root):
        data = _read_json(path)
        if not isinstance(data, dict):
            data = {}
        # GC-G02 (T3m): giu nguyen version hien co neu la so nguyen
        # >= 1 (nhu save_alert_settings); chi dat version = 1 khi
        # thieu/khong hop le. Khong tu tang version.
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) \
                or version < 1:
            data["version"] = 1
        for key, value in updates.items():
            data[key] = value
        _write_json_atomic(path, data, mode=0o600)
    return load_center_config(root)


def load_center_cursors(root, state_path=None):
    """Doc cursor keo feed trung tam cua tung tai khoan tu muc
    ``center`` trong ``gateway-fetch-state.json`` (gateway ghi
    ``last_fetch_seq`` moi lan feed GET 200 — xem
    ``pc/gateway/gateway_main.py``). Tra ve dict
    {account_id: seq} cho cac tai khoan DANG THAM GIA, hoac None
    khi CHUA DU du kien de cat theo so luong:

    - 1 tai khoan dang tham gia chua co cursor (chua keo lan nao)
      -> None (watermark khong xac dinh -> chi cat theo tuoi,
      an toan hon mat tin — GC-F02, DESIGN §3.8);
    - khong co tai khoan nao dang tham gia -> {} (rong, cung chi
      cat theo tuoi).

    File trang thai thieu/hong -> coi nhu chua co cursor nao.
    """
    root = Path(root)
    path = Path(state_path) if state_path is not None \
        else root / "gateway-fetch-state.json"
    data = _read_json(path)
    center = data.get("center") if isinstance(data, dict) else None
    if not isinstance(center, dict):
        center = {}
    participating = [account_id for account_id, record
                     in read_accounts(root).items()
                     if record.get("center_participating")]
    if not participating:
        return {}
    cursors = {}
    for account_id in participating:
        entry = center.get(account_id)
        seq = entry.get("last_fetch_seq") \
            if isinstance(entry, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) \
                or seq < 0:
            return None
        cursors[account_id] = seq
    return cursors


def read_prune_state(root):
    """Doc data/center/prune_state.json -> dict trang thai lan don
    cuoi hoac None (chua don lan nao / file thieu-hong). CHI DOC —
    chi lan don moi ghi (DESIGN §3.8/§6).

    Tuong thich file cu (GC-F01, T3m): file do ban T3f/T3g ghi chi
    co ``events_archived`` (thuc chat dem snapshot viec) thi coi
    gia tri do la ``tasks_archived``; file moi chi ghi
    ``tasks_archived``. Neu ca 2 khoa cung co thi ten moi thang.
    """
    data = _read_json(Path(root) / "data" / "center" / "prune_state.json")
    if not isinstance(data, dict):
        return None
    if data.get("last_prune_trigger") not in PRUNE_TRIGGER_VALUES:
        return None
    result = data.get("last_prune_result")
    if isinstance(result, dict) and "events_archived" in result:
        if "tasks_archived" not in result:
            result["tasks_archived"] = result.pop("events_archived")
        else:
            result.pop("events_archived")
    return data


def read_center_secret(root, account_id, kind):
    """Doc 1 secret trung tam (fetch/push) tu file 600.

    Doc lai moi lan goi de xoay secret khong can restart. Thieu/rong
    -> None (khong bao gio chap nhan secret rong). CAM dung lai
    secret bot (queue/sync) cho trung tam (TS1) — ham nay chi doc
    file ``center-<account_id>.<kind>_secret``.
    """
    if kind not in ("fetch", "push"):
        raise ValueError("kind secret trung tam phai la fetch/push")
    path = Path(root) / "secrets" / (
        "center-%s.%s_secret" % (account_id, kind))
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first:
        return None
    return first.encode("utf-8")


def has_center_secrets(root, account_id):
    """True neu ca 2 file secret trung tam deu ton tai va khong rong."""
    return read_center_secret(root, account_id, "fetch") is not None \
        and read_center_secret(root, account_id, "push") is not None


class CenterStore:
    """Kho trung tam: single-writer qua flock + tmp+rename.

    Moi phuong thuc ghi giu ``store.lock`` trong suot chu trinh
    doc-sua-ghi; moi phuong thuc doc chi doc file (chiu dong do
    cuoi file nhu gateway doc queue).
    """

    def __init__(self, root):
        self.root = Path(root)
        self.center_dir = self.root / "data" / "center"
        self.events_path = self.center_dir / "events.jsonl"
        self.tasks_path = self.center_dir / "tasks_state.json"
        self.seq_path = self.center_dir / "seq.json"
        self.seen_path = self.center_dir / "seen_state.json"
        self.lock_path = self.center_dir / "store.lock"
        self.archive_dir = self.center_dir / "archive"
        self.files_dir = self.center_dir / "files"
        # Kho nhap theo phong + moc da doc (v0.3.0, T3c — DESIGN §8.2):
        # drafts.json {"version": 1, "owner": {room: text}} (chi chu);
        # read_state.json {"version": 1, "owner": {room: seq}} (moc da
        # doc cua chu nhan; khoa theo (owner, room) — chi chu nhan co
        # huy hieu). Ca 2 file 600, ghi qua store.lock nhu moi ghi kho.
        self.drafts_path = self.center_dir / "drafts.json"
        self.read_state_path = self.center_dir / "read_state.json"
        # Tin hieu "dang tra loi" (v0.6.0, T3h): typing.json
        # {account_id: {room: {"started_at": epoch}}} — ghi qua
        # store.lock nhu moi ghi kho; TTL 90 giay do giao dien kiem
        # tra khi doc.
        self.typing_path = self.center_dir / "typing.json"

    # -- khoa + doc/ghi thap -------------------------------------------
    @contextmanager
    def _locked(self):
        self.center_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.center_dir, 0o700)
        except OSError:
            pass
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- tin hieu "dang tra loi" (v0.6.0, T3h) -------------------------
    def _read_typing_locked(self):
        data = _read_json(self.typing_path)
        return data if isinstance(data, dict) else {}

    def set_typing(self, account_id, room, started_at):
        """Ghi tin hieu typing cua 1 tai khoan vao 1 phong (ghi de —
        1 tin hieu/luot tra loi). Tu giu store.lock (khong goi khi
        dang giu khoa)."""
        if not isinstance(account_id, str) \
                or not ACCOUNT_ID_RE.match(account_id):
            raise CenterError(400, "bad_account_id")
        room = parse_room(room)
        if isinstance(started_at, bool) \
                or not isinstance(started_at, (int, float)) \
                or started_at <= 0:
            raise CenterError(400, "bad_body")
        with self._locked():
            data = self._read_typing_locked()
            rooms = data.get(account_id)
            if not isinstance(rooms, dict):
                rooms = {}
            rooms[room] = {"started_at": started_at}
            data[account_id] = rooms
            _write_json_atomic(self.typing_path, data)

    def clear_typing(self, account_id, room):
        """Xoa tin hieu typing cua 1 tai khoan o 1 phong (neu co)."""
        with self._locked():
            self._clear_typing_locked(account_id, room)

    def _clear_typing_locked(self, account_id, room):
        """Xoa muc typing. CHI goi khi dang giu khoa."""
        data = self._read_typing_locked()
        rooms = data.get(account_id)
        if not isinstance(rooms, dict) or room not in rooms:
            return
        del rooms[room]
        if rooms:
            data[account_id] = rooms
        else:
            del data[account_id]
        _write_json_atomic(self.typing_path, data)

    def _read_events_locked(self):
        """Doc events.jsonl (da giu khoa): list dict hop le theo seq."""
        try:
            data = self.events_path.read_bytes()
        except OSError:
            return []
        events = []
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict) and isinstance(row.get("seq"), int) \
                    and not isinstance(row.get("seq"), bool):
                events.append(row)
        events.sort(key=lambda row: row["seq"])
        return events

    def _read_tasks_locked(self):
        data = _read_json(self.tasks_path)
        if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
            tasks = {}
            for task_id, task in data["tasks"].items():
                if isinstance(task, dict):
                    tasks[task_id] = _normalize_task(task)
            return tasks
        return {}

    def _read_seq_locked(self, events=None):
        data = _read_json(self.seq_path)
        seq = None
        floor = 0
        if isinstance(data, dict):
            value = data.get("center_seq")
            if isinstance(value, int) and not isinstance(value, bool) \
                    and value >= 0:
                seq = value
            floor_value = data.get("floor_seq")
            if isinstance(floor_value, int) \
                    and not isinstance(floor_value, bool) \
                    and floor_value >= 0:
                floor = floor_value
        if seq is None:
            if events is None:
                events = self._read_events_locked()
            seq = max((row["seq"] for row in events), default=0)
        return seq, floor

    def _read_seen_locked(self):
        data = _read_json(self.seen_path)
        seen = {}
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            for account, tasks in data["seen"].items():
                if isinstance(tasks, dict):
                    kept = {tid: stamp for tid, stamp in tasks.items()
                            if isinstance(tid, str)
                            and isinstance(stamp, str)}
                    if kept:
                        seen[account] = kept
        return seen

    def _write_tasks_locked(self, tasks):
        _write_json_atomic(self.tasks_path,
                           {"version": 1, "tasks": tasks})

    def _write_seq_locked(self, seq, floor=0):
        _write_json_atomic(self.seq_path,
                           {"center_seq": seq, "floor_seq": floor})

    def _write_seen_locked(self, seen):
        _write_json_atomic(self.seen_path, {"version": 1, "seen": seen})

    def _append_event_locked(self, events, seq, event):
        """Ghi 1 dong event (seq da gan) + fsync; cap nhat seq.json.

        ``events`` la list dang giu trong bo nho (de goi lien tiep
        trong cung 1 lan giu khoa khong can doc lai file).
        """
        event = dict(event)
        event["seq"] = seq
        existed = self.events_path.exists()
        self.center_dir.mkdir(parents=True, exist_ok=True)
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            try:
                os.chmod(self.events_path, 0o600)
            except OSError:
                pass
        events.append(event)
        _floor = self._read_seq_locked(events)[1]
        self._write_seq_locked(seq, _floor)
        return event

    # -- doc cong khai (khong khoa ghi) ---------------------------------
    def read_events(self):
        """Doc events.jsonl chiu dong do (khong giu khoa ghi)."""
        try:
            data = self.events_path.read_bytes()
        except OSError:
            return []
        events = []
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict) and isinstance(row.get("seq"), int):
                events.append(row)
        events.sort(key=lambda row: row["seq"])
        return events

    def get_task(self, task_id):
        """Lay ban ghi viec hien tai, hoac None."""
        with self._locked():
            return self._read_tasks_locked().get(task_id)

    def get_task_by_source(self, source_msg_id):
        """Lay viec tao tu tin tag co ``source_msg_id`` cho truoc,
        hoac None."""
        with self._locked():
            for task in self._read_tasks_locked().values():
                if task.get("source_msg_id") == source_msg_id:
                    return task
        return None

    def list_tasks(self):
        """Toan bo viec hien tai (dict task_id -> task)."""
        with self._locked():
            return self._read_tasks_locked()

    def latest_seq(self):
        with self._locked():
            events = self._read_events_locked()
            seq, _floor = self._read_seq_locked(events)
            return seq

    def get_feed(self, since, limit=100, room=None,
                 visible_rooms=None):
        """Su kien feed dang {seq, at, kind, task_id|msg_id, record}.

        ``kind`` feed la "task"/"message" (DESIGN §6); su kien doi
        trang thai / chuyen tay deu mang ban ghi viec sau thay doi.

        Loc theo phong (v0.3.0, T3c — SPEC TT7): tin chi tra khi
        phong cua tin nam trong ``visible_rooms`` (None = thay het
        — chu nhan tren UI); ``room`` chi dinh thi chi tra tin trong
        phong do. Viec (kind ``task``) KHONG theo phong — tra du nhu
        cu. Tin cu khong ghi ``room`` thi mac dinh ``"chung"``.
        ``latest_seq``/``floor_seq`` tinh tren TOAN BO log (khong
        loc) de cursor tien dung qua ca tin khong duoc thay; gioi
        han ``limit`` ap cho danh sach DA LOC (quet het log neu can
        de du 100 su kien thay duoc — tranh ket cung khi 100 su kien
        dau deu nam ngoai tam thay).
        """
        if room is not None:
            room = parse_room(room)
        events = self.read_events()
        latest = max((row["seq"] for row in events), default=0)
        floor = events[0]["seq"] if events else 0
        out = []
        for row in events:
            if row["seq"] <= since:
                continue
            kind = row.get("kind")
            if kind in ("task_created", "task_status", "task_reassigned"):
                item = {"seq": row["seq"], "at": row.get("at"),
                        "kind": "task", "task_id": row.get("task_id"),
                        "record": row.get("record")}
            elif kind == "message":
                record = row.get("record") or {}
                msg_room = record.get("room") or ROOM_CHUNG
                if visible_rooms is not None \
                        and msg_room not in visible_rooms:
                    continue
                if room is not None and msg_room != room:
                    continue
                # Dong dau phong len ban ghi tra ve (tin cu v1/v2
                # thieu truong -> "chung") de moi ben doc thay cung
                # 1 dang.
                record["room"] = msg_room
                item = {"seq": row["seq"], "at": row.get("at"),
                        "kind": "message", "msg_id": row.get("msg_id"),
                        "record": record}
            else:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out, latest, floor

    def center_health(self):
        """So dem cho GET /health (khong noi dung nhay cam)."""
        accounts = read_accounts(self.root)
        participating = sum(1 for rec in accounts.values()
                            if rec.get("center_participating"))
        tasks = self.list_tasks()
        now = datetime.now(timezone.utc)
        open_tasks = 0
        overdue = 0
        for task in tasks.values():
            if task.get("status") in TERMINAL_STATUSES:
                continue
            open_tasks += 1
            due = parse_iso_timestamp(task.get("due_at"))
            if due is not None and now > due:
                overdue += 1
        events = self.read_events()
        last_write_at = events[-1].get("at") if events else None
        latest = max((row["seq"] for row in events), default=0)
        return {"accounts": participating, "open_tasks": open_tasks,
                "overdue_tasks": overdue, "latest_seq": latest,
                "last_write_at": last_write_at}

    # -- tao viec --------------------------------------------------------
    def create_task(self, caller, title, body, kind, assigned_to, due_at,
                    client_key):
        """Tao viec + tu sinh tin giao-viec trong cung 1 lan giu khoa.

        Tra ve (task, duplicate). ``caller`` la assigned_from.
        ``assigned_to`` nhan chuoi (v1) hoac danh sach 1..5 (v2) —
        luu luon dang danh sach (schema v2). Kiem tra dinh dang o day;
        kiem tra tai khoan (ton tai / tham gia / co secret) do lop goi
        thuc hien truoc (can accounts.json + secret tren PC) — store
        chi nhan ``assigned_to`` da duoc kiem tra.
        """
        self._validate_task_fields(title, body, kind, due_at, client_key)
        assignees = _normalize_assignees(assigned_to)
        if caller in assignees:
            # Cam tu giao (R3, SUY LUAN).
            raise CenterError(400, "self_assign_forbidden")
        fingerprint = _fingerprint_create(title, body, kind, assignees,
                                          due_at)
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            seq, _floor = self._read_seq_locked(events)
            # Dedupe theo client_key (V2-NS-09).
            for row in events:
                if row.get("kind") != "task_created":
                    continue
                if row.get("client_key") != client_key:
                    continue
                old = row.get("record") or {}
                old_fp = _fingerprint_create(
                    old.get("title"), old.get("body"), old.get("kind"),
                    old.get("assigned_to"), old.get("due_at"))
                if old_fp != fingerprint:
                    raise CenterError(409, "client_key_conflict")
                task_id = row.get("task_id")
                task = tasks.get(task_id) if task_id else None
                if task is None:
                    task = _normalize_task(old)
                # Viec da co nhung tin giao-viec chua co -> bu tin truoc
                # roi moi tra (V2-NS-03).
                repaired = self._ensure_assign_message_locked(
                    events, seq, task)
                if repaired is not None:
                    seq = repaired
                return task, True
            now = now_iso()
            task_id = "t-" + _secrets.token_hex(6)
            task = {
                "schema_version": TASK_SCHEMA_VERSION,
                "task_id": task_id,
                "title": title,
                "body": body,
                "kind": kind,
                "created_by": caller,
                "assigned_from": caller,
                "assigned_to": assignees,
                "reported_by": [],
                "refused_by": [],
                "source_msg_id": None,
                "status": "moi",
                "due_at": due_at,
                "created_at": now,
                "updated_at": now,
                "seen_at": None,
                "history": [],
                "client_key": client_key,
            }
            # (1) dong tao viec — seq N, gan dung luc append.
            seq += 1
            self._append_event_locked(events, seq, {
                "at": now, "kind": "task_created", "task_id": task_id,
                "client_key": client_key, "record": dict(task)})
            # (2) tin giao-viec tu sinh — seq N+1.
            msg = self._assign_message_record(
                caller,
                assignees[0] if len(assignees) == 1 else None,
                task_id, title, now)
            seq += 1
            self._append_event_locked(events, seq, {
                "at": now, "kind": "message", "task_id": task_id,
                "msg_id": msg["msg_id"], "record": msg})
            tasks[task_id] = task
            self._write_tasks_locked(tasks)
            return task, False

    @staticmethod
    def _filter_tag_assignees(mentions, from_account, accounts):
        """Loc nguoi lam viec tu danh sach mention da tach (doc 10
        §3.6, ap theo thu tu): (a) "owner" chi nhac; (b) mention khong
        ton tai da bi bo luc tach (phong thu o day); (c) tai khoan da
        go/tam ngung (center_participating=false) giu trong mentions
        nhung KHONG lam viec; (d) tag chinh minh chi nhac (giu cam tu
        giao R3); (e) tag trung gop thanh 1; (f) con lai -> nguoi lam
        viec. Tra ve danh sach (co the rong)."""
        out = []
        for name in mentions or []:
            if name == "owner":
                continue
            if name == from_account:
                continue
            record = accounts.get(name) if isinstance(accounts, dict) \
                else None
            if not isinstance(record, dict):
                continue
            if not record.get("center_participating"):
                continue
            if name not in out:
                out.append(name)
        return out

    @staticmethod
    def _tag_task_title(body):
        """Tieu de viec tao tu tag: dong dau tien khong rong, cat ngan
        200 ky tu kem "…" khi cat (toan van van con trong body)."""
        first = ""
        for line in (body or "").splitlines():
            if line.strip():
                first = line.strip()
                break
        if len(first) > MAX_TITLE_LEN:
            return first[:MAX_TITLE_LEN - 1] + "…"
        return first

    def _create_tag_task_locked(self, events, tasks, seq, from_account,
                                body, source_msg_id, assignees,
                                orphan_repair=False):
        """Tao viec tu tag + tin giao-viec tu sinh (CHI goi khi dang
        giu khoa). Tra ve (task, seq_moi). ``orphan_repair=True`` thi
        tin giao-viec mang co ``orphan_repair`` (duong bu CHAN-1).

        Goi ham nay khi da chac chua co viec nao voi source_msg_id
        nay (kiem tra o lop goi — ham khong tu kiem tra lai)."""
        now = now_iso()
        task_id = "t-" + _secrets.token_hex(6)
        client_key = "tag:" + source_msg_id
        task = {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "title": self._tag_task_title(body),
            "body": body,
            "kind": "khac",
            "created_by": from_account,
            "assigned_from": from_account,
            "assigned_to": list(assignees),
            "reported_by": [],
            "refused_by": [],
            "source_msg_id": source_msg_id,
            "status": "moi",
            "due_at": None,
            "created_at": now,
            "updated_at": now,
            "seen_at": None,
            "history": [],
            "client_key": client_key,
        }
        seq += 1
        self._append_event_locked(events, seq, {
            "at": now, "kind": "task_created", "task_id": task_id,
            "client_key": client_key, "record": dict(task)})
        msg = self._assign_message_record(from_account, None, task_id,
                                          task["title"], now)
        seq += 1
        event = {"at": now, "kind": "message", "task_id": task_id,
                 "msg_id": msg["msg_id"], "record": msg}
        if orphan_repair:
            event["orphan_repair"] = True
        self._append_event_locked(events, seq, event)
        tasks[task_id] = task
        self._write_tasks_locked(tasks)
        return task, seq

    def create_task_from_tag(self, from_account, body, source_msg_id,
                             assignees):
        """Tao viec tu 1 tin tag (duong cong khai cho test/rebuild
        goi truc tiep). Dedupe theo ``source_msg_id``: da co viec
        thi tra viec do + True, khong tao viec thu 2."""
        if not isinstance(body, str) or not body.strip():
            raise CenterError(400, "bad_body")
        if not isinstance(source_msg_id, str) \
                or not MSG_ID_RE.match(source_msg_id):
            raise CenterError(400, "bad_msg_id")
        assignees = _normalize_assignees(assignees)
        if from_account in assignees:
            raise CenterError(400, "self_assign_forbidden")
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            seq, _floor = self._read_seq_locked(events)
            for task in tasks.values():
                if task.get("source_msg_id") == source_msg_id:
                    return task, True
            task, _seq = self._create_tag_task_locked(
                events, tasks, seq, from_account, body, source_msg_id,
                assignees)
            return task, False

    def _repair_tag_task_locked(self, events, tasks, seq, message):
        """Bu viec cho tin tag da luu nhung chua co viec (duong bu
        CHAN-1: gui lai cung msg_id, hoac rebuild §5.4). Chay lai loc
        tai thoi diem goi — tai khoan da ngung sau luc gui tin goc thi
        khong duoc giao bu. Tra ve seq moi, hoac None neu khong can
        bu (khong phai tin tag sinh viec / da co viec / het nguoi
        lam viec)."""
        if message.get("kind") != "hoi":
            return None
        if (message.get("room") or ROOM_CHUNG) != ROOM_CHUNG:
            return None
        mentions = message.get("mentions") or []
        if not mentions:
            return None
        body = message.get("body") or ""
        if not body.strip():
            return None
        # (v0.6.0, T3l — Luat 2, mo rong theo D14) Tin da ghi ro
        # to_account (ke ca tin chu nhan qua o "Gui cho:") thi tag
        # chi nhac ten — khong bu viec cho tin da luu (ke ca khi
        # gui lai/rebuild).
        if _tag_task_suppressed_by_law2(
                message.get("from_account"), message.get("to_account")):
            return None
        msg_id = message.get("msg_id")
        if not isinstance(msg_id, str) or not MSG_ID_RE.match(msg_id):
            return None
        for task in tasks.values():
            if task.get("source_msg_id") == msg_id:
                return None
        assignees = self._filter_tag_assignees(
            mentions, message.get("from_account"),
            read_accounts(self.root))
        if not assignees:
            return None
        _task, seq = self._create_tag_task_locked(
            events, tasks, seq, message.get("from_account"), body,
            msg_id, assignees, orphan_repair=True)
        return seq

    def _assign_message_record(self, from_account, to_account, task_id,
                               title, now):
        # Tin giao-viec (hub tu sinh) LUON o khung chung (P5).
        # (v0.6.0, T3h — D3) Tin hub tu sinh mang source="hub".
        return {"schema_version": SCHEMA_VERSION,
                "msg_id": "m-" + _secrets.token_hex(6),
                "from_account": from_account,
                "to_account": to_account,
                "kind": "giao-viec",
                "task_id": task_id,
                "body": title,
                "created_at": now,
                "report_role": None,
                "room": ROOM_CHUNG,
                "mentions": None,
                "source": HUB_SOURCE}

    def _ensure_assign_message_locked(self, events, seq, task):
        """Bu tin giao-viec cho viec mo coi. Tra ve seq moi, hoac None
        neu tin da co. CHI goi khi dang giu khoa."""
        task_id = task.get("task_id")
        for row in events:
            if row.get("kind") == "message" \
                    and row.get("task_id") == task_id:
                record = row.get("record") or {}
                if record.get("kind") == "giao-viec":
                    return None
        now = now_iso()
        assignees = _as_assignee_list(task.get("assigned_to"))
        msg = self._assign_message_record(
            task.get("assigned_from"),
            assignees[0] if len(assignees) == 1 else None,
            task_id, task.get("title", ""), now)
        seq += 1
        event = {"at": now, "kind": "message", "task_id": task_id,
                 "msg_id": msg["msg_id"], "record": msg,
                 "orphan_repair": True}
        self._append_event_locked(events, seq, event)
        return seq

    @staticmethod
    def _validate_task_fields(title, body, kind, due_at, client_key):
        if not isinstance(client_key, str) or not client_key.strip():
            raise CenterError(400, "missing_client_key")
        if not isinstance(title, str) or not title.strip():
            raise CenterError(400, "missing_title")
        if len(title) > MAX_TITLE_LEN:
            raise CenterError(400, "title_too_long")
        if not isinstance(body, str):
            raise CenterError(400, "bad_body")
        if len(body) > MAX_TASK_BODY_LEN:
            raise CenterError(400, "body_too_long")
        if kind not in TASK_KINDS:
            raise CenterError(400, "bad_kind")
        if not isinstance(due_at, str) or not due_at.strip():
            raise CenterError(400, "missing_due_at")
        if parse_iso_timestamp(due_at) is None:
            raise CenterError(400, "bad_due_at")

    # -- doi trang thai --------------------------------------------------
    def change_status(self, task_id, to_status, caller, caller_role,
                      note, client_key):
        """Doi trang thai theo bang chuyen doi + quyen (TT2/TT3).

        ``caller_role`` la "assigned_to" / "assigned_from" / "owner"
        (lop goi tinh tu caller so voi ban ghi viec). Tra ve
        (task, duplicate). Replay cung client_key + cung to_status
        -> 200 + duplicate:true, khong ghi them; khac to_status
        -> 409 (V2-NS-09). Dedupe theo "the he" (GC-E01): chi xet
        su kien tu lan tra lai gan nhat tro di; yeu cau cu cung
        client_key tu truoc lan tra lai thi xu ly nhu yeu cau moi.

        Viec chung (v0.4.0, CHAN-2 / Q-V040-1 / Q-V040-4):
        - ``dang-lam -> da-bao-xong`` cua 1 nguoi chi ghi nguoi do vao
          ``reported_by`` + 1 su kien bao cao rieng (kind ``task``,
          co ``seq``, truong ``report_by``); trang thai chung chi
          chuyen khi tat ca da bao. Viec 1 nguoi: su kien nay CHINH LA
          su kien chuyen trang thai — khong append 2 su kien.
        - ``moi -> huy`` cua 1 nguoi lam viec (tu choi, note bat
          buoc): chi go nguoi do khoi ``assigned_to`` + ghi
          ``refused_by`` + 1 tin ``hoi`` thong bao trong khung chung;
          chi huy ca viec khi nguoi cuoi cung tu choi. Lich su ghi
          action ``tu-choi`` (lech T3 (d)); chu nhan/nguoi giao huy
          thi lich su ghi action ``huy`` — 2 duong tach biet.
        - Tra lai (``da-bao-xong -> dang-lam``): reset ``reported_by``
          ve rong — tat ca bao lai.
        """
        if not isinstance(client_key, str) or not client_key.strip():
            raise CenterError(400, "missing_client_key")
        if to_status not in TASK_STATUSES:
            raise CenterError(400, "bad_status")
        if note is not None and not isinstance(note, str):
            raise CenterError(400, "bad_note")
        if isinstance(note, str) and len(note) > MAX_NOTE_LEN:
            raise CenterError(400, "note_too_long")
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            task = tasks.get(task_id)
            if task is None:
                raise CenterError(404, "task_not_found")
            seq, _floor = self._read_seq_locked(events)
            # Replay idempotent (chi xet su kien doi trang thai cua
            # dung viec nay). Su kien luu ca to_status da yeu cau
            # (khac record.status o su kien bao cao giua chung, noi
            # trang thai chung chua doi).
            #
            # GC-E01 (T3m): dedupe theo "the he" — chi xet su kien
            # co seq TU the he hien tai tro di (seq cua su kien tra
            # lai gan nhat); yeu cau cu cung client_key tu the he
            # truoc (seq < the he) khong con la trung nua, xu ly nhu
            # yeu cau moi. Chua tung tra lai thi the he = 0.
            # Su kien tra lai chinh no (seq == the he) VAN duoc xet
            # de gui lai chinh yeu cau tra lai do van dedupe dung
            # (neu loai no thi retry mang cua lan tra lai se 400 thay
            # vi duplicate:true).
            generation = self._latest_return_seq(events, task_id) or 0
            for row in events:
                if row.get("kind") != "task_status":
                    continue
                if row.get("task_id") != task_id:
                    continue
                if row.get("client_key") != client_key:
                    continue
                if not isinstance(row.get("seq"), int) \
                        or row["seq"] < generation:
                    continue
                old_to = row.get("to_status")
                if old_to is None:
                    old_to = (row.get("record") or {}).get("status")
                if old_to == to_status:
                    return task, True
                raise CenterError(409, "client_key_conflict")
            from_status = task.get("status")
            rules = TRANSITIONS.get((from_status, to_status))
            if not rules:
                raise CenterError(400, "bad_transition")
            rule = None
            for candidate in rules:
                if caller_role in candidate["by"]:
                    rule = candidate
                    break
            if rule is None:
                raise CenterError(403, "forbidden")
            if rule["note_required"] and not (
                    isinstance(note, str) and note.strip()):
                raise CenterError(400, "note_required")
            assignees = _as_assignee_list(task.get("assigned_to"))
            if caller_role == "assigned_to" \
                    and caller not in assignees:
                raise CenterError(403, "forbidden")
            now = now_iso()
            reported_by = list(task.get("reported_by") or [])
            refused_by = list(task.get("refused_by") or [])
            new_status = to_status
            report_by = None
            notify_refusal = False
            if (from_status, to_status) == ("dang-lam", "da-bao-xong") \
                    and caller_role == "assigned_to":
                # Bao xong theo tung nguoi (CHAN-2): bao lai cung
                # client_key moi thi tra duplicate, khong ghi them.
                if caller in reported_by:
                    return task, True
                reported_by = reported_by + [caller]
                report_by = caller
                if set(reported_by) < set(assignees):
                    new_status = "dang-lam"
                history_note = ("%s đã báo xong (%d/%d)"
                                % (caller, len(reported_by),
                                   len(assignees)))
                if isinstance(note, str) and note.strip():
                    history_note += " — %s" % note.strip()
            elif (from_status, to_status) == ("moi", "huy") \
                    and caller_role == "assigned_to":
                # Tu choi viec cua 1 nguoi (Q-V040-1).
                if caller in refused_by:
                    return task, True
                refused_by = refused_by + [caller]
                new_assignees = [a for a in assignees if a != caller]
                history_note = "%s từ chối: %s" % (caller, note.strip())
                if not new_assignees:
                    history_note += " — tất cả người làm việc đã từ chối"
                    new_status = "huy"
                else:
                    new_status = "moi"
                # Moi lan tu choi deu co 1 tin thong bao (ke ca nguoi
                # cuoi cung — viec vua huy van can tin bao).
                notify_refusal = True
                entry = {"at": now, "by": caller,
                         "from_status": from_status,
                         "to_status": new_status,
                         "note": history_note,
                         "from_assigned_to": list(assignees),
                         "to_assigned_to": list(new_assignees),
                         # Lech T3 (d), T3m: tu choi co action rieng
                         # "tu-choi", phan biet voi "huy" cua chu
                         # nhan (ca khi nguoi cuoi tu choi lam viec
                         # huy han — lich su van ghi tu-choi, khong
                         # ghi huy).
                         "action": "tu-choi"}
                history = list(task.get("history") or [])
                history.append(entry)
                task = dict(task)
                task["schema_version"] = TASK_SCHEMA_VERSION
                task["status"] = new_status
                task["assigned_to"] = new_assignees
                task["refused_by"] = refused_by
                task["updated_at"] = now
                task["history"] = history
                seq += 1
                self._append_event_locked(events, seq, {
                    "at": now, "kind": "task_status", "task_id": task_id,
                    "client_key": client_key, "to_status": to_status,
                    "record": dict(task), "history_entry": entry})
                tasks[task_id] = task
                self._write_tasks_locked(tasks)
                if notify_refusal:
                    # 1 tin hoi do hub tu sinh trong khung chung, gan
                    # task_id, chi ghi ten viec (khong ghi noi dung
                    # note tu choi — Q-V040-1).
                    msg = {"schema_version": SCHEMA_VERSION,
                           "msg_id": "m-" + _secrets.token_hex(6),
                           "from_account": caller,
                           "to_account": None,
                           "kind": "hoi",
                           "task_id": task_id,
                           "body": "%s đã từ chối việc %s"
                                   % (caller, task.get("title") or ""),
                           "created_at": now,
                           "report_role": None,
                           "room": ROOM_CHUNG,
                           "mentions": None,
                           # (v0.6.0, T3h — D3) Tin hoi do hub tu
                           # sinh (thong bao tu choi) mang
                           # source="hub".
                           "source": HUB_SOURCE}
                    seq += 1
                    self._append_event_locked(events, seq, {
                        "at": now, "kind": "message", "task_id": task_id,
                        "msg_id": msg["msg_id"], "record": msg})
                return task, False
            else:
                history_note = note if isinstance(note, str) else None
            entry = {"at": now, "by": caller,
                     "from_status": from_status, "to_status": new_status,
                     "note": history_note,
                     "from_assigned_to": list(assignees),
                     "to_assigned_to": list(assignees)}
            # Lech T3 (d), T3m: chu nhan/nguoi giao huy viec co
            # action rieng "huy" — khong dung chung voi "tu-choi"
            # cua nguoi lam viec (nhanh tu choi o tren).
            if to_status == "huy" \
                    and caller_role in ("assigned_from", "owner"):
                entry["action"] = "huy"
            history = list(task.get("history") or [])
            history.append(entry)
            task = dict(task)
            task["schema_version"] = TASK_SCHEMA_VERSION
            task["status"] = new_status
            task["reported_by"] = reported_by
            task["refused_by"] = refused_by
            task["updated_at"] = now
            task["history"] = history
            if (from_status, to_status) == ("da-bao-xong", "dang-lam"):
                # Tra lai viec chung (Q-V040-4): tat ca bao xong lai.
                task["reported_by"] = []
            seq += 1
            event = {"at": now, "kind": "task_status",
                     "task_id": task_id, "client_key": client_key,
                     "to_status": to_status, "record": dict(task),
                     "history_entry": entry}
            if report_by is not None:
                event["report_by"] = report_by
            self._append_event_locked(events, seq, event)
            tasks[task_id] = task
            self._write_tasks_locked(tasks)
            return task, False

    # -- chuyen tay (R5 — dung o T3, store san sang tu T1) ---------------
    def reassign_task(self, task_id, new_assigned_to, by):
        """Chuyen tay o moi trang thai chua xong: thay TOAN BO danh
        sach nguoi lam viec (v0.4.0: form chon nhieu, 1..5 nguoi),
        reset ve ``moi``, ``seen_at`` ve ``null``, ``reported_by`` /
        ``refused_by`` ve rong, xoa muc seen cua viec do.
        ``new_assigned_to`` nhan chuoi (v1) hoac danh sach (v2)."""
        new_assignees = _normalize_assignees(new_assigned_to)
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            task = tasks.get(task_id)
            if task is None:
                raise CenterError(404, "task_not_found")
            if task.get("status") in TERMINAL_STATUSES:
                raise CenterError(400, "bad_transition")
            seq, _floor = self._read_seq_locked(events)
            now = now_iso()
            assignees = _as_assignee_list(task.get("assigned_to"))
            entry = {"at": now, "by": by,
                     "from_status": task.get("status"),
                     "to_status": "moi",
                     "note": None,
                     "from_assigned_to": list(assignees),
                     "to_assigned_to": list(new_assignees)}
            history = list(task.get("history") or [])
            history.append(entry)
            task = dict(task)
            task["schema_version"] = TASK_SCHEMA_VERSION
            task["assigned_to"] = new_assignees
            task["reported_by"] = []
            task["refused_by"] = []
            task["status"] = "moi"
            task["updated_at"] = now
            task["seen_at"] = None
            task["history"] = history
            seq += 1
            self._append_event_locked(events, seq, {
                "at": now, "kind": "task_reassigned", "task_id": task_id,
                "record": dict(task), "history_entry": entry})
            tasks[task_id] = task
            self._write_tasks_locked(tasks)
            seen = self._read_seen_locked()
            changed = False
            for account in list(seen):
                if task_id in seen[account]:
                    del seen[account][task_id]
                    changed = True
                if not seen[account]:
                    del seen[account]
            if changed:
                self._write_seen_locked(seen)
            return task

    def remove_assignee(self, account_id, by="owner"):
        """Go 1 tai khoan khoi danh sach nguoi lam viec cua moi viec
        chua xong (Q-V040-2 — tai khoan bi tam ngung/go giua chung):
        giu ``history`` + bao cao da lam (``reported_by`` cua ho go
        theo); trang thai viec KHONG doi, khong tu chuyen cho ai
        khac; viec khong con ai thi thanh viec mo coi "can chuyen
        tay" (Q-AM-06). Tra ve {"removed_from": [...task_id...],
        "orphan": [...task_id...]}. Moi viec bi go ghi 1 su kien
        ``task_status`` (from==to==trang thai hien tai) de feed keo
        ve ban ghi moi."""
        if not isinstance(account_id, str) or not account_id:
            raise CenterError(400, "bad_assigned_to")
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            seq, _floor = self._read_seq_locked(events)
            now = now_iso()
            removed_from = []
            orphan = []
            seen = self._read_seen_locked()
            seen_changed = False
            for task_id, task in list(tasks.items()):
                if task.get("status") in TERMINAL_STATUSES:
                    continue
                assignees = _as_assignee_list(task.get("assigned_to"))
                if account_id not in assignees:
                    continue
                new_assignees = [a for a in assignees
                                 if a != account_id]
                new_reported = [a for a in (task.get("reported_by") or [])
                                if a != account_id]
                entry = {"at": now, "by": by,
                         "from_status": task.get("status"),
                         "to_status": task.get("status"),
                         "note": ("%s đã ngưng tham gia — gỡ khỏi danh "
                                  "sách người làm việc (Q-V040-2)"
                                  % account_id),
                         "from_assigned_to": list(assignees),
                         "to_assigned_to": list(new_assignees)}
                history = list(task.get("history") or [])
                history.append(entry)
                task = dict(task)
                task["schema_version"] = TASK_SCHEMA_VERSION
                task["assigned_to"] = new_assignees
                task["reported_by"] = new_reported
                task["updated_at"] = now
                task["history"] = history
                seq += 1
                self._append_event_locked(events, seq, {
                    "at": now, "kind": "task_status", "task_id": task_id,
                    "to_status": task.get("status"),
                    "record": dict(task), "history_entry": entry})
                tasks[task_id] = task
                removed_from.append(task_id)
                if not new_assignees:
                    orphan.append(task_id)
                if account_id in seen and task_id in seen[account_id]:
                    del seen[account_id][task_id]
                    if not seen[account_id]:
                        del seen[account_id]
                    seen_changed = True
            if removed_from:
                self._write_tasks_locked(tasks)
            if seen_changed:
                self._write_seen_locked(seen)
            return {"removed_from": removed_from, "orphan": orphan}

    def set_task_due(self, task_id, due_at, by="owner", allow_change=False):
        """Dat / doi han cho viec (Q-V040-3 + GC-E05).

        ``allow_change=False`` (mac dinh): chi dat moi khi
        ``due_at`` = null; viec da co han thi tu choi 400
        ``due_already_set`` — ma loi nay GIU cho duong receiver
        sau nay (receiver chua co endpoint dat/doi han).
        ``allow_change=True`` (chi duong UI chu nhan): da co han
        thi DOI han, khong cho xoa han ve null (Q-V060-37).

        Han moi phai o tuong lai (400 ``due_in_past``) o ca 2 che
        do. Moi lan doi han append 1 muc ``history`` action
        ``doi-han`` {old_due_at, new_due_at, by, at}; trang thai
        khong doi. Viec da xong/huy thi tu choi (bad_transition)."""
        parsed = parse_iso_timestamp(due_at)
        if parsed is None:
            raise CenterError(400, "bad_due_at")
        if parsed <= datetime.now(timezone.utc):
            raise CenterError(400, "due_in_past")
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            task = tasks.get(task_id)
            if task is None:
                raise CenterError(404, "task_not_found")
            if task.get("status") in TERMINAL_STATUSES:
                raise CenterError(400, "bad_transition")
            old_due = task.get("due_at")
            changing = old_due is not None
            if changing and not allow_change:
                raise CenterError(400, "due_already_set")
            seq, _floor = self._read_seq_locked(events)
            now = now_iso()
            assignees = _as_assignee_list(task.get("assigned_to"))
            if changing:
                entry = {"at": now, "by": by,
                         "from_status": task.get("status"),
                         "to_status": task.get("status"),
                         "note": "Đổi hạn: %s → %s" % (old_due, due_at),
                         "from_assigned_to": list(assignees),
                         "to_assigned_to": list(assignees),
                         "action": "doi-han",
                         "old_due_at": old_due,
                         "new_due_at": due_at}
            else:
                entry = {"at": now, "by": by,
                         "from_status": task.get("status"),
                         "to_status": task.get("status"),
                         "note": "Đặt hạn: %s" % due_at,
                         "from_assigned_to": list(assignees),
                         "to_assigned_to": list(assignees)}
            history = list(task.get("history") or [])
            history.append(entry)
            task = dict(task)
            task["schema_version"] = TASK_SCHEMA_VERSION
            task["due_at"] = due_at
            task["updated_at"] = now
            task["history"] = history
            seq += 1
            self._append_event_locked(events, seq, {
                "at": now, "kind": "task_status", "task_id": task_id,
                "to_status": task.get("status"),
                "record": dict(task), "history_entry": entry})
            tasks[task_id] = task
            self._write_tasks_locked(tasks)
            return task

    # -- tin nhan ---------------------------------------------------------
    def append_message(self, from_account, kind, to_account, task_id,
                       body, msg_id, file_id=None, room=None,
                       mentions=None, client_key=None, source=None):
        """Ghi 1 tin (dedupe theo msg_id). Tra ve (message, duplicate).

        Tin hub tu sinh (v0.6.0, T3h — D3): chi duong noi bo cua hub
        truyen ``source="hub"`` (ghi truong source vao ban ghi);
        duong receiver/UI khong truyen -> ban ghi khong co truong
        source (= khong phai tin hub). Tin ``hoi`` that moi ghi xoa
        muc typing cua dung tai khoan do o dung phong do (DESIGN
        §3.9 buoc 8).

        Phong (v0.3.0, T3c): ``room`` mac dinh ``"chung"``; sai mau
        -> 400 ``bad_room``; kiem tra quyen phong (403) do lop goi
        (receiver/UI) thuc hien truoc — store chi ghi dung phong da
        duoc kiem tra. ``mentions`` (v0.3.0, T3d — NS-02): may chu
        TU TACH tu ``body`` tai day (1 cho duy nhat, ca duong UI
        lan receiver dung chung); tham so ``mentions`` cua lop goi
        (neu truyen) KHONG duoc tin — chi kiem tra kieu (sai kieu
        -> 400 ``bad_mentions``) roi bo qua, ghi theo ket qua tach
        tu body. Qua MAX_MENTIONS -> 400 ``too_many_mentions``;
        mention khong ton tai thi bo khoi danh sach, giu nguyen chu
        "@..." trong body (Q-V030-1). ``schema_version`` theo quy
        tac v4 (D1(h): = 4 khi ``to_account`` la danh sach, ke ca
        danh sach 1 nguoi; chuoi cu giu luat cu; hang
        SCHEMA_VERSION = 1 van danh cho tin hub tu sinh, khong
        doi). ``to_account`` (v0.6.0, T3l — D1(j)): chuoi cu hoac
        danh sach toi da 5; phan tu sai mau hoac qua 5 -> 400
        ``bad_to_account``. Danh sach rong ``[]`` chuan hoa ve
        ``None`` ngay tai diem vao (NS-L3, AUDIT-V060 §2: gui
        chung, khong phai tin v4 — schema ve 1, fingerprint
        khop ``None``). Kiem tra ton tai/tham gia/thao luan
        cua tung phan tu do lop goi (receiver/UI) thuc hien truoc
        — store chi kiem tra mau + tran.

        Tin kem file (v0.2.5, schema v2): ``file_id`` phai ton tai va
        do chinh nguoi gui tai len (sidecar ``uploaded_by`` khop) —
        sai thi 400 ``unknown_file_id`` (NS-06: lan dau bi 400 thi
        duoc gui lai cung ``msg_id`` voi ``file_id`` da sua, vi tin
        chua tung duoc ghi; lan dau da thanh cong thi dedupe tra ban
        da luu, khong duoc dung lai ``msg_id``). Tin co file thi
        ``body`` duoc rong (tin chi co file); tin vua khong ``body``
        vua khong ``file`` -> 400. Chi ``hoi``/``bao-cao`` duoc kem
        file (NS-07 — ``giao-viec`` da bi chan o POSTABLE_MESSAGE_KINDS).

        Tag sinh viec (v0.4.0, P2 moi — doc 10 §3.6) + Luat 2
        (v0.6.0, T3l — SPEC TT6, PLAN NS-26; mo rong theo D14):
        tin ``hoi`` o khung chung co mentions hop le + co chu thi
        hub tao 1 viec chung trong CUNG 1 lan giu khoa (tin seq N
        -> viec N+1 -> tin ``giao-viec`` N+2) — TRU khi Luat 2 ap
        dung: tin da ghi ro ``to_account`` (tai khoan Muse hay chu
        nhan qua o "Gui cho:") thi tag chi nhac ten, KHONG sinh
        viec; chi go ``@ten`` ma khong ghi ``to_account`` thi van
        sinh viec theo P2. Gui lai cung ``msg_id`` ma viec chua
        tao (chet giua chung) -> tao bu viec cho tin da luu
        (CHAN-1).
        Tin UI khong co ``msg_id`` do nguoi gui sinh: form mang truong
        an ``client_key`` de dedupe gui lai form sau loi (cung
        ``client_key`` + cung noi dung -> tra ban da co +
        ``duplicate: true``; khac noi dung -> 409 — nhu dedupe
        ``client_key`` tao viec, V2-NS-09).

        V3-GC-01/2B-NS-03: tin ``bao-cao`` chi tinh la bao xong khi la
        tin DAU TIEN cua chinh nguoi gui do co seq lon hon seq cua su
        kien bao cao gan nhat cua chinh nguoi do (theo tung nguoi);
        tin sau la bao tien do bo sung (``report_role`` = "progress"),
        khong doi trang thai.
        """
        # (NS-L3, AUDIT-V060 §2) Danh sach rong [] = gui chung:
        # chuan hoa ve None NGAY tai diem vao (truoc moi kiem tra,
        # bam dedupe va luu) de schema ve 1 va fingerprint khop
        # None. KHONG sua trong _as_to_account_list() — receiver
        # dung None lam tin hieu loi o duong kiem tra to_account.
        if to_account == []:
            to_account = None
        if not isinstance(msg_id, str) or not MSG_ID_RE.match(msg_id):
            raise CenterError(400, "bad_msg_id")
        if kind not in POSTABLE_MESSAGE_KINDS:
            raise CenterError(400, "kind_not_allowed")
        if not isinstance(body, str):
            raise CenterError(400, "bad_body")
        if len(body) > MAX_MESSAGE_BODY_LEN:
            raise CenterError(400, "body_too_long")
        if file_id is not None and not isinstance(file_id, str):
            raise CenterError(400, "unknown_file_id")
        # (v0.6.0, T3l — D1(j), SPEC TT6) ``to_account``: chuoi cu
        # (v1-v3) van chap nhan nhu danh sach 1 nguoi; danh sach v4
        # toi da 5 nguoi (tran MAX_MENTIONS) — qua 5 hoac phan tu
        # sai mau -> 400 ``bad_to_account``. Gia tri goc duoc giu
        # nguyen khi luu (chuoi van la chuoi, danh sach van la danh
        # sach — test T3i ghim hanh vi nay); ben ghi MOI (outbox)
        # luon gui danh sach.
        try:
            _to_account_list = _as_to_account_list(to_account)
        except ValueError:
            raise CenterError(400, "bad_to_account")
        if _to_account_list is not None:
            if len(_to_account_list) > MAX_MENTIONS:
                raise CenterError(400, "bad_to_account")
            for _target in _to_account_list:
                if not isinstance(_target, str) \
                        or not ACCOUNT_ID_RE.match(_target):
                    raise CenterError(400, "bad_to_account")
        if task_id is not None and not isinstance(task_id, str):
            raise CenterError(400, "bad_task_id")
        if client_key is not None and (
                not isinstance(client_key, str)
                or not client_key.strip()):
            raise CenterError(400, "bad_client_key")
        if source is not None and (
                not isinstance(source, str) or not source):
            raise CenterError(400, "bad_source")
        room = parse_room(room)
        if mentions is not None and (
                not isinstance(mentions, list)
                or any(not isinstance(item, str) for item in mentions)):
            raise CenterError(400, "bad_mentions")
        # T3d (NS-02): mentions do may chu tu tach tu body — gia tri
        # lop goi truyen vao (neu co) chi duoc kiem tra kieu o tren,
        # khong duoc tin. Danh sach tai khoan lay tu accounts.json
        # tren hub (thieu file -> rong: chi "owner" con hop le).
        mentions = parse_mentions(body, read_accounts(self.root))
        with self._locked():
            events = self._read_events_locked()
            tasks = self._read_tasks_locked()
            seq, _floor = self._read_seq_locked(events)
            for row in events:
                if row.get("kind") != "message":
                    continue
                # Dedupe gui lai form UI theo client_key (v0.4.0).
                if client_key is not None \
                        and row.get("client_key") == client_key:
                    old = row.get("record") or {}
                    old_fp = _fingerprint_message(old)
                    new_fp = _fingerprint_message({
                        "kind": kind, "body": body, "room": room,
                        "task_id": task_id, "to_account": to_account,
                        "file_id": file_id})
                    if old_fp != new_fp:
                        raise CenterError(409, "client_key_conflict")
                    repaired = self._repair_tag_task_locked(
                        events, tasks, seq, old)
                    if repaired is not None:
                        seq = repaired
                    return old, True
                if row.get("msg_id") == msg_id:
                    stored = row.get("record") or {}
                    # Gui lai cung msg_id: tin da co — kiem tra duong
                    # bu viec tu tag (CHAN-1) roi moi tra.
                    repaired = self._repair_tag_task_locked(
                        events, tasks, seq, stored)
                    if repaired is not None:
                        seq = repaired
                    return stored, True
            if task_id is not None and task_id not in tasks:
                raise CenterError(404, "task_not_found")
            file_meta = None
            if file_id is not None:
                file_meta = self._read_file_meta_locked(file_id)
                if file_meta is None \
                        or file_meta.get("uploaded_by") != from_account:
                    raise CenterError(400, "unknown_file_id")
            if not body and file_meta is None:
                raise CenterError(400, "bad_body")
            now = now_iso()
            report_role = None
            if kind == "bao-cao" and task_id is not None:
                report_role = self._classify_bao_cao_locked(
                    events, task_id, seq + 1, from_account)
            elif kind == "bao-cao":
                report_role = "progress"
            message = {"schema_version": message_schema_version(
                           room, mentions, file_meta is not None,
                           to_account),
                       "msg_id": msg_id,
                       "from_account": from_account,
                       "to_account": to_account,
                       "kind": kind,
                       "task_id": task_id,
                       "body": body,
                       "file": file_meta,
                       "created_at": now,
                       "report_role": report_role,
                       "room": room,
                       "mentions": mentions}
            if source is not None:
                # (v0.6.0, T3h — D3) Chi ghi truong source khi duong
                # noi bo cua hub truyen vao; tin thuong khong co
                # truong nay (= khong phai tin hub).
                message["source"] = source
            seq += 1
            self._append_event_locked(events, seq, {
                "at": now, "kind": "message", "task_id": task_id,
                "msg_id": msg_id, "record": dict(message),
                "client_key": client_key})
            # Tin hoi that moi ghi -> xoa tin hieu "dang tra loi"
            # cua dung tai khoan do o dung phong do (DESIGN §3.9
            # buoc 8). Tin cua chu nhan (from_account="owner")
            # khong co muc typing nen xoa la no-op.
            if kind == "hoi":
                self._clear_typing_locked(from_account, room)
            message = dict(message)
            message["seq"] = seq
            # Tag sinh viec (v0.4.0, §3.6): cung 1 lan giu khoa.
            # (v0.6.0, T3l — Luat 2, mo rong theo D14) Tin da ghi
            # ro to_account (ke ca tin chu nhan qua o "Gui cho:")
            # thi tag chi nhac ten, khong sinh viec.
            if kind == "hoi" and room == ROOM_CHUNG and mentions \
                    and body.strip() \
                    and not _tag_task_suppressed_by_law2(
                        from_account, to_account):
                assignees = self._filter_tag_assignees(
                    mentions, from_account, read_accounts(self.root))
                if assignees:
                    _task, seq = self._create_tag_task_locked(
                        events, tasks, seq, from_account, body, msg_id,
                        assignees)
            return message, False

    @staticmethod
    def _is_report_event(row, account_id):
        """Su kien bao cao cua rieng ``account_id`` (CHAN-2): su kien
        ``task_status`` ma nguoi bao la ``account_id`` va muc dich
        yeu cau la ``da-bao-xong``. Su kien moi co truong
        ``report_by`` + ``to_status``; su kien cu (truoc T3e) suy tu
        history (by + to_status ``da-bao-xong``)."""
        if row.get("kind") != "task_status":
            return False
        history = row.get("history_entry") or {}
        reporter = row.get("report_by") or history.get("by")
        if reporter != account_id:
            return False
        target = row.get("to_status")
        if target is None:
            target = (row.get("record") or {}).get("status")
        return target == "da-bao-xong"

    @staticmethod
    def _latest_return_seq(events, task_id):
        """Seq cua lan tra lai (``da-bao-xong -> dang-lam``) gan nhat
        cua viec, hoac None. Ghe p noi chi dung su kien bao cao MOI
        sau lan tra lai nay (GC-02)."""
        latest = None
        for row in events:
            if row.get("kind") != "task_status":
                continue
            if row.get("task_id") != task_id:
                continue
            history = row.get("history_entry") or {}
            if history.get("from_status") == "da-bao-xong" \
                    and history.get("to_status") == "dang-lam":
                latest = row["seq"]
        return latest

    @staticmethod
    def _classify_bao_cao_locked(events, task_id, next_seq, from_account):
        """2B-NS-03 (CHAN-2): "completion" neu la tin bao-cao DAU
        TIEN cua chinh ``from_account`` co seq lon hon seq cua su
        kien bao cao gan nhat cua chinh nguoi do (chi xet su kien
        sau lan tra lai gan nhat); nguoc lai "progress"."""
        return_seq = CenterStore._latest_return_seq(events, task_id)
        report_seq = None
        for row in events:
            if return_seq is not None and row["seq"] <= return_seq:
                continue
            if row.get("task_id") != task_id:
                continue
            if CenterStore._is_report_event(row, from_account):
                report_seq = row["seq"]
        if report_seq is None:
            return "progress"
        if next_seq <= report_seq:
            # Tin bao-cao toi TRUOC su kien bao cao (gui lai lech thu
            # tu): tinh la bao tien do, phai gui lai tin khac sau moi
            # tinh la bao xong (2B-NS-03).
            return "progress"
        for row in events:
            if row.get("kind") != "message":
                continue
            if row.get("task_id") != task_id:
                continue
            record = row.get("record") or {}
            if record.get("kind") == "bao-cao" \
                    and record.get("from_account") == from_account \
                    and report_seq < row["seq"] < next_seq:
                return "progress"
        return "completion"

    def classify_bao_cao(self, task_id, msg_seq, from_account):
        """Phan loai 1 tin bao-cao da ghi (doc, khong ghi) theo tung
        nguoi gui (2B-NS-03)."""
        events = self.read_events()
        # Gia lap: tin dang xet chua co trong events — dung seq truyen
        # vao de so sanh voi su kien bao cao gan nhat cua chinh nguoi
        # gui do.
        return self._classify_bao_cao_locked(
            events, task_id, msg_seq, from_account)

    def task_report_match(self, task_id):
        """Trang thai bao cao theo tung nguoi lam viec (de UI gan co
        "bao cao chua khop trang thai" — doc 10 §6.4: UI tinh tu
        events.jsonl, khong luu co rieng). Tra ve {account_id:
        {"reported": bool, "matched": bool, "report_seq": int|None}}
        cho moi nguoi trong danh sach assigned_to hien tai."""
        task = self.get_task(task_id)
        if task is None:
            return {}
        events = self.read_events()
        return_seq = self._latest_return_seq(events, task_id)
        reported_by = task.get("reported_by") or []
        out = {}
        for account in _as_assignee_list(task.get("assigned_to")):
            report_seq = None
            for row in events:
                if return_seq is not None and row["seq"] <= return_seq:
                    continue
                if row.get("task_id") != task_id:
                    continue
                if self._is_report_event(row, account):
                    report_seq = row["seq"]
            matched = False
            if report_seq is not None:
                for row in events:
                    if row.get("kind") != "message":
                        continue
                    if row.get("task_id") != task_id:
                        continue
                    record = row.get("record") or {}
                    if record.get("kind") == "bao-cao" \
                            and record.get("from_account") == account \
                            and row["seq"] > report_seq:
                        matched = True
                        break
            out[account] = {"reported": account in reported_by,
                            "matched": matched,
                            "report_seq": report_seq}
        return out

    # -- phong rieng (v0.3.0, T3c — SPEC TT17, DESIGN §8.2) ------------
    def list_rooms(self, accounts):
        """Danh sach phong cho chu nhan tren UI: khung chung + 1 phong
        rieng/tai khoan. Phong xuat hien khi tai khoan tham gia xong
        (P8); chua tham gia thi KHONG co phong; tai khoan da tam
        ngung/go nhung con tin trong phong thi phong thanh LUU TRU
        (chi chu nhan xem lai, khong gui them). ``accounts`` la dict
        tu ``read_accounts`` (account_id -> record)."""
        rooms = [{"room": ROOM_CHUNG, "account_id": None,
                  "display_name": "Khung chung", "archived": False}]
        rooms_with_messages = set()
        for row in self.read_events():
            if row.get("kind") != "message":
                continue
            record = row.get("record") or {}
            msg_room = record.get("room") or ROOM_CHUNG
            if msg_room.startswith("dm:"):
                rooms_with_messages.add(msg_room)
        ordered = sorted(
            accounts,
            key=lambda aid: (accounts[aid].get("display_name") or aid)
            .lower())
        for account_id in ordered:
            record = accounts[account_id] or {}
            dm_room = "dm:" + account_id
            participating = bool(record.get("center_participating"))
            if participating or dm_room in rooms_with_messages:
                rooms.append({
                    "room": dm_room,
                    "account_id": account_id,
                    "display_name":
                        record.get("display_name") or account_id,
                    "archived": not participating,
                })
        return rooms

    def file_visible_rooms(self, file_id):
        """Tap phong cua cac tin tham chieu file (theo quyen tin).
        Tra ve None neu khong co tin nao tham chieu (file chua co tin
        — NS-14: chi nguoi tai len + chu nhan tai duoc)."""
        rooms = set()
        for row in self.read_events():
            if row.get("kind") != "message":
                continue
            record = row.get("record") or {}
            file_meta = record.get("file")
            if isinstance(file_meta, dict) \
                    and file_meta.get("file_id") == file_id:
                rooms.add(record.get("room") or ROOM_CHUNG)
        return rooms or None

    # -- kho nhap theo phong (P9 — drafts.json) ------------------------
    def _read_drafts(self):
        data = _read_json(self.drafts_path)
        drafts = {}
        if isinstance(data, dict) and isinstance(data.get("owner"), dict):
            for room, text in data["owner"].items():
                if isinstance(room, str) and isinstance(text, str):
                    drafts[room] = text
        return drafts

    def _write_drafts(self, drafts):
        _write_json_atomic(self.drafts_path,
                           {"version": 1, "owner": drafts})

    def get_draft(self, room):
        """Ban nhap dang go do cua phong (chi chu — file da chon
        khong giu duoc qua chuyen phong, P9). Phong chua co nhap -> "".
        """
        return self._read_drafts().get(parse_room(room), "")

    def save_draft(self, room, body):
        """Luu nhap phong (chi chu); nhap rong/trang -> xoa muc."""
        room = parse_room(room)
        if not isinstance(body, str):
            raise CenterError(400, "bad_draft")
        with self._locked():
            drafts = self._read_drafts()
            if body.strip():
                drafts[room] = body
            else:
                drafts.pop(room, None)
            self._write_drafts(drafts)

    def delete_draft(self, room):
        """Xoa nhap phong (goi sau khi gui tin xong trong phong do)."""
        room = parse_room(room)
        with self._locked():
            drafts = self._read_drafts()
            if room in drafts:
                del drafts[room]
                self._write_drafts(drafts)

    # -- moc da doc + huy hieu chua doc (P7 — read_state.json, NS-08) --
    def _read_state(self):
        data = _read_json(self.read_state_path)
        state = {}
        if isinstance(data, dict) and isinstance(data.get("owner"), dict):
            for room, seq in data["owner"].items():
                if isinstance(room, str) and isinstance(seq, int) \
                        and not isinstance(seq, bool) and seq >= 0:
                    state[room] = seq
        return state

    def _write_read_state(self, state):
        _write_json_atomic(self.read_state_path,
                           {"version": 1, "owner": state})

    def get_read_watermark(self, room):
        """Moc seq da doc cua chu nhan trong phong (mac dinh 0)."""
        return self._read_state().get(parse_room(room), 0)

    def mark_room_read(self, room, seq):
        """Cap nhat moc da doc cua chu nhan cho phong — CHI TANG,
        khong giam. Tra ve moc moi.

        LUAT DA DOC MOI (v0.6.0, T3i — thay NS-08 cu): phuc vu
        fragment chat KHONG tu danh dau da doc — chi goi ham nay
        khi chu nhan bam vien "N tin moi" (POST /center/mark-read).
        """
        room = parse_room(room)
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return self.get_read_watermark(room)
        with self._locked():
            state = self._read_state()
            if seq > state.get(room, 0):
                state[room] = seq
                self._write_read_state(state)
            return state.get(room, 0)

    def room_unread_counts(self):
        """{room: so tin chua doc cua chu nhan} — tin co seq > moc
        da doc cua phong VA from_account != "owner" (khong dem tin
        chinh chu nhan gui). Chi chu nhan co huy hieu (tai khoan
        khong dung UI, khong co moc doc)."""
        state = self._read_state()
        counts = {}
        for row in self.read_events():
            if row.get("kind") != "message":
                continue
            record = row.get("record") or {}
            room = record.get("room") or ROOM_CHUNG
            counts.setdefault(room, 0)
            if record.get("from_account") == "owner":
                continue
            if row["seq"] > state.get(room, 0):
                counts[room] += 1
        return counts

    def rooms_with_unread_owner_mention(self):
        """{room} co tin mentions chua "owner" ma seq con sau moc
        da doc cua phong — de UI hien huy hieu "@" rieng ben canh
        huy hieu so (T3d, P7). Tin chinh chu nhan gui khong tinh
        (chu nhan da biet minh viet gi); chi chu nhan co huy hieu
        (tai khoan khong dung UI)."""
        state = self._read_state()
        rooms = set()
        for row in self.read_events():
            if row.get("kind") != "message":
                continue
            record = row.get("record") or {}
            if "owner" not in (record.get("mentions") or []):
                continue
            if record.get("from_account") == "owner":
                continue
            room = record.get("room") or ROOM_CHUNG
            if row["seq"] > state.get(room, 0):
                rooms.add(room)
        return rooms

    # -- file dinh kem (v0.2.5, Q-C7 — doc 10 §5.7, DESIGN §10) ---------
    def file_bytes_path(self, file_id):
        """Duong dan bytes cua file (ten CHI la file_id — ten goc
        KHONG bao gio thanh duong dan, chong path traversal)."""
        return self.files_dir / file_id

    def _file_meta_path(self, file_id):
        return self.files_dir / ("%s.meta.json" % file_id)

    @staticmethod
    def _validate_file_name(file_name):
        """Kiem tra ten file goc: rong / qua 200 ky tu / chua ky tu
        dieu khien (CR/LF) / khop mau secret R6 -> CenterError 400.
        Ten khong bao gio dung lam duong dan (chi de hien thi)."""
        if not isinstance(file_name, str) or not file_name:
            raise CenterError(400, "bad_file_name")
        if len(file_name) > MAX_FILE_NAME_LEN:
            raise CenterError(400, "bad_file_name")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in file_name):
            raise CenterError(400, "bad_file_name")
        label = find_secret_like(file_name)
        if label is not None:
            raise CenterError(400, "blocked_secret_pattern")

    def _read_file_meta_locked(self, file_id):
        """Doc sidecar + kiem tra bytes ton tai (da giu khoa hoac
        doc don). Tra ve dict metadata, hoac None khi khong ton tai
        / sai mau / sidecar hong / thieu bytes (file do coi nhu
        khong ton tai — 404 chung)."""
        if not isinstance(file_id, str) or not FILE_ID_RE.match(file_id):
            return None
        meta = _read_json(self._file_meta_path(file_id))
        if not isinstance(meta, dict):
            return None
        if meta.get("file_id") != file_id:
            return None
        try:
            info = os.lstat(self.file_bytes_path(file_id))
        except OSError:
            return None
        import stat as _stat
        if not _stat.S_ISREG(info.st_mode):
            return None
        return meta

    def get_file_meta(self, file_id):
        """Metadata file (sidecar), hoac None. Doc khong can khoa
        (sidecar ghi tmp+rename — doc duoc ban hoan chinh)."""
        return self._read_file_meta_locked(file_id)

    def _store_total_bytes_locked(self):
        """Tong dung luong bytes file trong kho (chi tinh file bytes
        dung mau file_id; bo qua sidecar + file tmp). Kiem tra nhanh
        moi lan tai len (UCT-09)."""
        total = 0
        try:
            entries = list(self.files_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if not FILE_ID_RE.match(entry.name):
                continue
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total

    def save_file(self, uploaded_by, file_name, data):
        """Luu 1 file dinh kem vao kho. Tra ve metadata dict.

        thu tu kiem tra (khong luu gi khi loi): rong -> 400
        ``empty_file``; qua 50 MB/file -> 413 ``file_too_large``
        (PERMANENT); ten file sai/mau secret -> 400; tong kho day
        2 GB -> 413 ``store_full`` (RETRYABLE — kho day la tam
        thoi, don kho xong gui lai duoc). Tu v0.5.0 (SPEC TT19):
        truoc khi quyet 413, neu tong kho >= nguong tu don
        (mac dinh 80%) thi don ngay trong luong nay roi tinh lai
        tong — chi 413 khi tong + file moi > 2 GB. Ghi bytes
        tmp+rename (600) roi sidecar tmp+rename (600); chi khi ca 2
        xong moi tra ``file_id``. Moi ghi giu ``store.lock`` (tranh
        2 lan tai len cung luc lam lech tong dung luong kho).
        """
        if not isinstance(data, (bytes, bytearray)):
            raise CenterError(400, "bad_file")
        data = bytes(data)
        if len(data) == 0:
            raise CenterError(400, "empty_file")
        if len(data) > MAX_FILE_BYTES:
            raise CenterError(413, "file_too_large")
        self._validate_file_name(file_name)
        if not isinstance(uploaded_by, str) or not uploaded_by:
            raise CenterError(400, "bad_uploaded_by")
        file_id = "f-" + _secrets.token_hex(6)
        sha256 = hashlib.sha256(data).hexdigest()
        meta = {"file_id": file_id,
                "file_name": file_name,
                "file_size": len(data),
                "sha256": sha256,
                "uploaded_by": uploaded_by,
                "uploaded_at": now_iso()}
        # Tu don khi kho gan day (v0.5.0, SPEC TT19 — CHAN-1 da sua):
        # neu tong kho >= nguong (mac dinh 80% kho 2 GB) thi don ngay
        # trong luong tai len, TRUOC KHI quyet 413; don xong tinh lai
        # tong — chi tu choi 413 khi tong + file moi > 2 GB (khong
        # xoa file dang dung). Don goi ban NOI BO _prune_locked() /
        # _prune_files_locked() (da giu khoa); CAM goi thang
        # prune()/prune_files() public tu trong nay — 2 ham public tu
        # giu store.lock ben trong, goi tu trong save_file se treo
        # vinh vien (flock lay lan 2 tren fd khac — NS-01).
        config = load_center_config(self.root)
        with self._locked():
            self.files_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.files_dir, 0o700)
            except OSError:
                pass
            total = self._store_total_bytes_locked()
            threshold = config["prune_near_full_fraction"] \
                * MAX_FILE_STORE_BYTES
            if total >= threshold:
                # GC-F02: tu don cung truyen cursor (doc 1 lan trong
                # lan don nay, goi ban NOI BO _prune_locked — da giu
                # khoa, CAM goi prune()/prune_all() public o day).
                pruned = self._prune_locked(
                    max_age_days=config["prune_retention_days"],
                    cursors=load_center_cursors(self.root))
                files_pruned = self._prune_files_locked()
                result = self._merge_prune_results(pruned, files_pruned)
                self._write_prune_state_locked(result, "near_full")
                total = self._store_total_bytes_locked()
            if total + len(data) > MAX_FILE_STORE_BYTES:
                raise CenterError(413, "store_full")
            target = self.file_bytes_path(file_id)
            tmp_path = self.files_dir / (file_id + ".tmp")
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, target)
            _write_json_atomic(self._file_meta_path(file_id), meta)
        return dict(meta)

    def delete_file(self, file_id):
        """Xoa bytes + sidecar cua 1 file (khi phat hien secret trong
        file — NS-09 — hoac khi ghi tin tham chieu loi). Tra ve True
        neu xoa duoc it nhat 1 phan. Ban ghi tin trong events.jsonl
        (append-only) GIU NGUYEN — luc do tai ve tra 404 chung."""
        if not isinstance(file_id, str) or not FILE_ID_RE.match(file_id):
            return False
        deleted = False
        with self._locked():
            for path in (self.file_bytes_path(file_id),
                         self._file_meta_path(file_id)):
                try:
                    path.unlink()
                    deleted = True
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
        return deleted

    def _referenced_file_ids_locked(self, events):
        """Tap file_id duoc tin nhan trong events tham chieu."""
        referenced = set()
        for row in events:
            if row.get("kind") != "message":
                continue
            record = row.get("record") or {}
            file_meta = record.get("file")
            if isinstance(file_meta, dict):
                file_id = file_meta.get("file_id")
                if isinstance(file_id, str):
                    referenced.add(file_id)
        return referenced

    def _delete_file_pair_locked(self, file_id):
        """Xoa cap bytes+sidecar (da giu khoa). Tra ve so byte giai
        phong (0 neu khong co gi de xoa)."""
        freed = 0
        target = self.file_bytes_path(file_id)
        try:
            freed = target.stat().st_size
        except OSError:
            freed = 0
        for path in (target, self._file_meta_path(file_id)):
            try:
                path.unlink()
            except OSError:
                continue
        return freed

    def prune_files(self, now=None):
        """Quet don file dinh kem — xoa file khong co tin nao tham
        chieu ma da qua 7 ngay (tinh tu ``uploaded_at`` trong sidecar;
        file do chua co sidecar thi tinh tu mtime bytes). File co tin
        tham chieu song theo tin do (tin bi prune -> xoa file o ham
        ``prune``). Tra ve thong ke.

        Tu v0.5.0 (SPEC TT19): 1 lan don day du chay qua
        ``prune_all()`` (prune su kien + prune file trong 1 lan giu
        khoa); ham nay giu lai cho CLI ``prune-files`` + tuong thich
        nguoc."""
        with self._locked():
            return self._prune_files_locked(now=now)

    def _prune_files_locked(self, now=None):
        """Quet don file — CHI goi khi DA GIU ``store.lock`` (khong
        lay khoa lai). Dung trong ``prune_all()`` + ``save_file()``
        (tu don khi kho gan day) de tranh deadlock do flock lay lan 2
        tren fd khac (NS-01 — lay khoa lan 2 treo vinh vien)."""
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=FILE_UNREFERENCED_MAX_AGE_DAYS)
        events = self._read_events_locked()
        referenced = self._referenced_file_ids_locked(events)
        deleted = 0
        kept = 0
        freed = 0
        try:
            entries = list(self.files_dir.iterdir())
        except OSError:
            return {"deleted": 0, "kept": 0, "freed_bytes": 0}
        for entry in entries:
            if entry.suffix == ".json" and entry.name.endswith(
                    ".meta.json"):
                continue  # sidecar xoa kem bytes, khong xet rieng
            if not FILE_ID_RE.match(entry.name):
                continue
            file_id = entry.name
            if file_id in referenced:
                kept += 1
                continue
            stamp = None
            meta = _read_json(self._file_meta_path(file_id))
            if isinstance(meta, dict):
                stamp = parse_iso_timestamp(meta.get("uploaded_at"))
            if stamp is None:
                try:
                    stamp = datetime.fromtimestamp(
                        entry.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    stamp = None
            if stamp is not None and stamp >= cutoff:
                kept += 1
                continue
            freed += self._delete_file_pair_locked(file_id)
            deleted += 1
        return {"deleted": deleted, "kept": kept,
                "freed_bytes": freed}

    # -- seen_at ----------------------------------------------------------
    def record_seen(self, account_id, task_ids, now=None):
        """Ghi seen_at cho lan fetch dau tien (C-2).

        CHI ghi cho viec co assigned_to == account_id va seen_at con
        null. Moc = thoi diem store commit xong ghi. Tra ve danh sach
        task_id vua duoc ghi moc (rong = khong co gi moi).
        """
        if now is None:
            now = now_iso()
        newly = []
        with self._locked():
            tasks = self._read_tasks_locked()
            seen = self._read_seen_locked()
            account_seen = seen.setdefault(account_id, {})
            changed = False
            for task_id in task_ids or []:
                task = tasks.get(task_id)
                if task is None:
                    continue
                if account_id not in _as_assignee_list(
                        task.get("assigned_to")):
                    continue
                if task_id in account_seen:
                    continue
                account_seen[task_id] = now
                task = dict(task)
                task["seen_at"] = now
                tasks[task_id] = task
                newly.append(task_id)
                changed = True
            if changed:
                self._write_seen_locked(seen)
                self._write_tasks_locked(tasks)
        return newly

    # -- rebuild ----------------------------------------------------------
    def rebuild_tasks_state(self):
        """Rebuild snapshot tu events.jsonl (GC-13).

        Tai hien trang thai tung viec theo thu tu seq; seen_at lay tu
        seen_state.json NHUNG chi giu moc sau lan chuyen tay gan nhat
        (nguon moc: history.at — 2B-GC-04); dong thoi bu tin
        giao-viec cho viec mo coi + quet tin tag thieu viec de tao
        viec bu (CHAN-1, §5.4). Tra ve thong ke.
        """
        with self._locked():
            events = self._read_events_locked()
            tasks = {}
            for row in events:
                kind = row.get("kind")
                if kind == "task_created":
                    record = row.get("record") or {}
                    if record.get("task_id"):
                        tasks[record["task_id"]] = _normalize_task(record)
                elif kind in ("task_status", "task_reassigned"):
                    record = row.get("record") or {}
                    if record.get("task_id"):
                        tasks[record["task_id"]] = _normalize_task(record)
            seen = self._read_seen_locked()
            dropped = 0
            for task_id, task in tasks.items():
                handover_at = None
                for entry in task.get("history") or []:
                    if not isinstance(entry, dict):
                        continue
                    old_list = _as_assignee_list(
                        entry.get("from_assigned_to"))
                    new_list = _as_assignee_list(
                        entry.get("to_assigned_to"))
                    if old_list != new_list:
                        stamp = parse_iso_timestamp(entry.get("at"))
                        if stamp is not None and (
                                handover_at is None or stamp > handover_at):
                            handover_at = stamp
                candidates = []
                for account, per_task in list(seen.items()):
                    stamp_text = per_task.get(task_id)
                    if stamp_text is None:
                        continue
                    stamp = parse_iso_timestamp(stamp_text)
                    if stamp is None:
                        del per_task[task_id]
                        dropped += 1
                        continue
                    if handover_at is not None and stamp <= handover_at:
                        # Moc cu hon lan chuyen tay gan nhat: bo.
                        del per_task[task_id]
                        dropped += 1
                        continue
                    candidates.append((stamp, stamp_text))
                if candidates:
                    task["seen_at"] = min(candidates)[1]
                else:
                    task["seen_at"] = None
            for account in list(seen):
                if not seen[account]:
                    del seen[account]
            self._write_seen_locked(seen)
            self._write_tasks_locked(tasks)
            seq = max((row["seq"] for row in events), default=0)
            repaired = 0
            for task_id, task in tasks.items():
                before = seq
                new_seq = self._ensure_assign_message_locked(
                    events, seq, task)
                if new_seq is not None:
                    seq = new_seq
                    repaired += 1
            # Quet tin tag thieu viec (CHAN-1, §5.4): tin hoi o khung
            # chung co mentions ma khong co viec nao voi source_msg_id
            # tuong ung -> tao viec bu (loc tai thoi diem rebuild).
            tag_repaired = 0
            have_source = {t.get("source_msg_id") for t in tasks.values()
                           if t.get("source_msg_id")}
            accounts = read_accounts(self.root)
            for row in list(events):
                if row.get("kind") != "message":
                    continue
                record = row.get("record") or {}
                if record.get("kind") != "hoi":
                    continue
                if (record.get("room") or ROOM_CHUNG) != ROOM_CHUNG:
                    continue
                mentions = record.get("mentions") or []
                if not mentions:
                    continue
                body = record.get("body") or ""
                if not body.strip():
                    continue
                msg_id = row.get("msg_id")
                if not isinstance(msg_id, str) \
                        or not MSG_ID_RE.match(msg_id):
                    continue
                if msg_id in have_source:
                    continue
                # (v0.6.0, T3l — Luat 2, mo rong theo D14) Tin
                # da ghi ro to_account (ke ca tin chu nhan qua o
                # "Gui cho:") thi tag chi nhac ten — rebuild khong
                # tao viec bu cho tin nay.
                if _tag_task_suppressed_by_law2(
                        record.get("from_account"),
                        record.get("to_account")):
                    continue
                assignees = self._filter_tag_assignees(
                    mentions, record.get("from_account"), accounts)
                if not assignees:
                    continue
                _task, seq = self._create_tag_task_locked(
                    events, tasks, seq, record.get("from_account"),
                    body, msg_id, assignees, orphan_repair=True)
                have_source.add(msg_id)
                tag_repaired += 1
            _floor = self._read_seq_locked(events)[1]
            self._write_seq_locked(seq, _floor)
            return {"tasks": len(tasks), "events": len(events),
                    "seen_dropped": dropped, "orphan_repaired": repaired,
                    "tag_repaired": tag_repaired}

    # -- prune ------------------------------------------------------------
    def prune(self, max_events=PRUNE_MAX_EVENTS,
              max_age_days=PRUNE_MAX_AGE_DAYS, cursors=None,
              last_fetch_at=None, now=None):
        """Cat tia events.jsonl (SUY LUAN): toi da ``max_events`` su
        kien moi nhat + toi da ``max_age_days`` ngay.

        Chi cat su kien co seq <= watermark (min cursor cac tai khoan
        da keo trong 30 ngay gan nhat; tai khoan im lang qua 30 ngay
        bi loai khoi min — NS-10). Chua co cursor thi chi cat theo
        tuoi. Truoc khi cat, chep sang archive/ (ca viec + tin).
        VieC xong/huy qua 30 ngay duoc chep snapshot sang archive
        (ban sao luu tru; snapshot song van giu de tra GET).
        Tra ve thong ke.

        Tu v0.5.0 (SPEC TT19): 1 lan don day du chay qua
        ``prune_all()`` (prune su kien + prune file trong 1 lan giu
        khoa) — ham nay la wrapper giu khoa roi goi ban noi bo."""
        with self._locked():
            return self._prune_locked(
                max_events=max_events, max_age_days=max_age_days,
                cursors=cursors, last_fetch_at=last_fetch_at, now=now)

    def _prune_locked(self, max_events=PRUNE_MAX_EVENTS,
                      max_age_days=PRUNE_MAX_AGE_DAYS, cursors=None,
                      last_fetch_at=None, now=None):
        """Cat tia events.jsonl — CHI goi khi DA GIU ``store.lock``
        (khong lay khoa lai). Xem ``prune()`` ve luat cat/archive.
        Dung trong ``prune_all()`` + ``save_file()`` (NS-01)."""
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age_days)
        events = self._read_events_locked()
        tasks = self._read_tasks_locked()
        seq, floor = self._read_seq_locked(events)
        watermark = None
        if cursors:
            active = []
            for account, cursor_seq in cursors.items():
                if not isinstance(cursor_seq, int):
                    continue
                last = None
                if last_fetch_at:
                    last = parse_iso_timestamp(
                        last_fetch_at.get(account))
                if last is not None and (now - last).days > 30:
                    continue  # im lang qua 30 ngay: loai khoi min
                active.append(cursor_seq)
            if active:
                watermark = min(active)
        cut = []
        keep = []
        # Cat theo so luong: chi cat su kien cu nhat co seq <=
        # watermark (neu co watermark); khong co watermark thi
        # KHONG cat theo so luong (an toan hon mat tin).
        over_budget = 0
        if watermark is not None and len(events) > max_events:
            over_budget = len(events) - max_events
        for row in events:
            stamp = parse_iso_timestamp(row.get("at"))
            too_old = stamp is not None and stamp < cutoff
            over_count = over_budget > 0 \
                and row["seq"] <= watermark
            if too_old or over_count:
                cut.append(row)
                if over_count:
                    over_budget -= 1
            else:
                keep.append(row)
        # Gioi han so luong: neu van vuot tran ma khong co
        # watermark thi KHONG cat them (an toan hon mat tin).
        if not cut:
            return {"cut": 0, "kept": len(keep), "watermark": watermark,
                    "archived_tasks": 0, "archive_file": None,
                    "files_deleted": 0}
        stamp_tag = now.strftime("%Y%m%d-%H%M%S")
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.archive_dir, 0o700)
        except OSError:
            pass
        archive_path = self.archive_dir / (
            "events-%s.jsonl" % stamp_tag)
        with open(archive_path, "a", encoding="utf-8") as handle:
            for row in cut:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(archive_path, 0o600)
        except OSError:
            pass
        # Archive snapshot viec xong/huy qua 30 ngay (ban sao).
        archived_tasks = 0
        task_archive_path = self.archive_dir / (
            "tasks-%s.jsonl" % stamp_tag)
        with open(task_archive_path, "a",
                  encoding="utf-8") as handle:
            for task in tasks.values():
                if task.get("status") not in TERMINAL_STATUSES:
                    continue
                updated = parse_iso_timestamp(task.get("updated_at"))
                if updated is not None and updated < cutoff:
                    handle.write(json.dumps(
                        task, ensure_ascii=False) + "\n")
                    archived_tasks += 1
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(task_archive_path, 0o600)
        except OSError:
            pass
        # Ghi lai events con lai (tmp+rename) + floor_seq.
        tmp_path = self.events_path.with_suffix(".jsonl.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        try:
            for row in keep:
                os.write(fd, (json.dumps(row, ensure_ascii=False)
                              + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, self.events_path)
        new_floor = max((row["seq"] for row in cut), default=floor)
        self._write_seq_locked(seq, new_floor)
        # Don file (v0.2.5): tin bi prune -> file cua tin do bi
        # xoa (metadata trong ban ghi tin da archive van giu,
        # bytes khong archive). Chi xoa file KHONG con tin nao
        # giu lai tham chieu (cung file_id co the gan 2 tin).
        kept_refs = self._referenced_file_ids_locked(keep)
        cut_refs = self._referenced_file_ids_locked(cut)
        files_deleted = 0
        for file_id in sorted(cut_refs - kept_refs):
            existed = (self.file_bytes_path(file_id).exists()
                       or self._file_meta_path(file_id).exists())
            self._delete_file_pair_locked(file_id)
            if existed:
                files_deleted += 1
        return {"cut": len(cut), "kept": len(keep),
                "watermark": watermark,
                "archived_tasks": archived_tasks,
                "archive_file": archive_path.name,
                "files_deleted": files_deleted}

    # -- don dep tu dong (v0.5.0, T3f — SPEC TT19, DESIGN §3.8) --------
    def _merge_prune_results(self, pruned, files_pruned):
        """Gop thong ke 1 lan don ve dang ghi prune_state.json."""
        return {"files_deleted": pruned.get("files_deleted", 0)
                + files_pruned.get("deleted", 0),
                "bytes_freed": files_pruned.get("freed_bytes", 0),
                "tasks_archived": pruned.get("archived_tasks", 0),
                "events_pruned": pruned.get("cut", 0),
                "errors": []}

    def _write_prune_state_locked(self, result, trigger, now=None):
        """Ghi data/center/prune_state.json (tmp+rename, 600) — CHI
        goi khi da giu store.lock. Chi lan don moi ghi file nay;
        UI chi doc de hien "Lan don cuoi" (DESIGN §6)."""
        if now is None:
            now = datetime.now(timezone.utc)
        state = {"last_prune_at": now.isoformat(),
                 "last_prune_trigger": trigger,
                 "last_prune_result": result}
        _write_json_atomic(self.center_dir / "prune_state.json", state,
                           mode=0o600)
        return state

    def prune_all(self, max_age_days=None, trigger="manual", now=None,
                  cursors=None):
        """1 lan don day du (SPEC TT19): prune su kien + prune file
        trong 1 LAN GIU ``store.lock`` (su kien truoc, file sau — file
        cua tin vua bi prune duoc xoa trong cung lan don), ghi
        ``prune_state.json``.

        ``max_age_days`` None -> lay ``prune_retention_days`` tu
        center.json. ``trigger``: "schedule" | "near_full" | "manual".
        ``cursors``: dict {account_id: seq} tu
        ``load_center_cursors`` (watermark = min cursor — GC-F02);
        None -> khong co du kien, chi cat theo tuoi.
        Tra ve thong ke gop (khong gom watermark/archive_file)."""
        if now is None:
            now = datetime.now(timezone.utc)
        if max_age_days is None:
            max_age_days = load_center_config(
                self.root)["prune_retention_days"]
        if trigger not in PRUNE_TRIGGER_VALUES:
            raise ValueError("trigger don dep khong hop le: %s" % trigger)
        with self._locked():
            pruned = self._prune_locked(max_age_days=max_age_days,
                                        cursors=cursors, now=now)
            files_pruned = self._prune_files_locked(now=now)
            result = self._merge_prune_results(pruned, files_pruned)
            self._write_prune_state_locked(result, trigger, now=now)
            return {"trigger": trigger,
                    "events_pruned": result["events_pruned"],
                    "tasks_archived": result["tasks_archived"],
                    "files_deleted": result["files_deleted"],
                    "bytes_freed": result["bytes_freed"],
                    "errors": result["errors"]}

    def store_total_bytes(self):
        """Tong dung luong kho file (tinh nhanh, giu khoa) — dung cho
        trang Cai dat chung + kiem tra truoc khi tai len."""
        with self._locked():
            return self._store_total_bytes_locked()


def main(argv=None):
    """CLI don dep kho trung tam (v0.5.0, SPEC TT19):

        python3 -m pc.center.center_store --root ~/multibot prune
        python3 -m pc.center.center_store --root ~/multibot prune-files

    ``prune`` = 1 lan don day du (su kien + file, ghi
    prune_state.json, trigger "manual"); ``prune-files`` = chi don
    file (tuong thich nguoc, T3b). Chi in thong ke JSON. Khong
    secret, khong noi dung file.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Kho trung tam — don dep (T3f).")
    parser.add_argument("--root", default=None,
                        help="thu muc goc multibot (mac dinh ~/multibot/)")
    parser.add_argument("command", choices=["prune", "prune-files"],
                        help="lenh don dep")
    parser.add_argument("--cursors-file", default=None,
                        help="file trang thai fetch de lay cursor cac tai "
                        "khoan (mac dinh <root>/gateway-fetch-state.json); "
                        "file khong co thi chi cat theo tuoi, canh bao ra "
                        "stderr (GC-F02)")
    args = parser.parse_args(argv)
    root = Path(str(args.root or Path.home() / "multibot")).expanduser()
    store = CenterStore(root)
    if args.command == "prune":
        cursors = None
        state_path = Path(args.cursors_file).expanduser() \
            if args.cursors_file else root / "gateway-fetch-state.json"
        if not state_path.exists():
            sys.stderr.write(
                "canh bao: khong co file cursor %s — chi cat theo tuoi\n"
                % state_path)
        else:
            cursors = load_center_cursors(root, state_path=state_path)
        stats = store.prune_all(trigger="manual", cursors=cursors)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    if args.command == "prune-files":
        stats = store.prune_files()
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
