#!/usr/bin/env python3
"""Gateway chi-doc cho he da-bot V2 (chay tren PC, cong 8765).

Mot tien trinh RIENG, chi dung thu vien chuan Python, khong doc token bot,
khong goi API nen tang, khong co endpoint ghi nao. Hop dong: thiet ke v0.2,
``design-docs/04-gateway-and-fetcher.md`` §1.

Endpoints (tat ca GET, deu yeu cau Bearer queue secret cua DUNG bot):
  GET /v1/bots/<bot_id>/health
  GET /v1/bots/<bot_id>/queue?since=N&limit=100
  GET /v1/bots/<bot_id>/config
  GET /v1/bots/<bot_id>/files/<ten-file>

Endpoints trung tam (module trung tam v0.2.2, chặng T1 — cung chi GET,
Bearer = center fetch secret cua DUNG tai khoan):
  GET /v1/center/<account_id>/feed?since=N&limit=100[&room=<room>]
  GET /v1/center/<account_id>/tasks/<task_id>
  GET /v1/center/<account_id>/health
  GET /v1/center/<account_id>/files/<file_id>   (v0.2.5, Q-C7)

Feed loc theo phong (v0.3.0, T3c): tham so ``room`` tuy chon; tai
khoan chi thay khung chung + phong rieng cua minh; xin phong ngoai
tam thay -> 403 ``room_forbidden``; room sai mau -> 400.
Ngoai le co chu dich cua "gateway chi doc" (skill
multibot_gateway_sync, DESIGN-TRUNG-TAM §4, doc 10 §6.2): khi tra
feed, gateway goi module store (import truc tiep, khoa cua store)
de store ghi ``seen_at`` cho tung task_id ma response thuc su chua,
do chinh ``assigned_to`` fetch lan dau. Gateway KHONG tu ghi file
kho nao; moi ghi khac van CAM qua gateway.

Khong co endpoint health tong hop. Khong giu endpoint don-bot cua baseline
(``/health``, ``/queue``) — goi toi do tra 404.

Xac thuc (tai dung R3 cua baseline): ``hmac.compare_digest`` + allowlist IP
nguon ``ip_network``. Secret doc tu file ``secrets/<bot_id>.queue_secret``
(600) theo moi request — xoay secret khong can khoi dong lai gateway.
Secret bot A mo queue bot B -> 401. Bot khong ton tai -> 404 chung chung.

Cau hinh (toi thieu, tu dinh nghia trong file nay vi ``pc/config.py`` chua
ton tai luc viet gateway — vong sau thong nhat voi file do; ten bien moi
truong goc du lieu da doi chieu voi ``pc/config.py`` sau khi file do xuat
hien: ``MULTIBOT_BASE_DIR`` duoc uu tien truoc cac alias cu):
  Thu tu uu tien: tham so dong lenh > bien moi truong > file cau hinh JSON
  (chi doc khi duoc chi ro) > mac dinh.
  Goc du lieu:  --root/--base-dir | MULTIBOT_BASE_DIR | MULTIBOT_DIR |
                MULTIBOT_ROOT | ~/multibot/
  Cong:         --port | MULTIBOT_GATEWAY_PORT | GATEWAY_PORT | 8765
  Bind:         --bind | GATEWAY_BIND | 0.0.0.0
  Allowlist IP: --allow-cidrs | MULTIBOT_ALLOW_CIDRS | ALLOW_CIDRS
                | 127.0.0.0/8,::1/128,100.64.0.0/10
  File cau hinh JSON: --config | MULTIBOT_CONFIG, khoa "root", "port",
  "bind", "allow_cidrs" (chuoi phay nhau hoac danh sach). Khong co duong
  dan mac dinh cho file nay.

Gateway CHI DOC ``state.json``/``queue.jsonl`` cua tai (doc file truc tiep,
khong import code cua tai). "Lan fetch gan nhat + toi seq nao" va watermark
"da phuc vu" gateway tu ghi vao file RIENG ``gateway-fetch-state.json`` o
goc du lieu — CAM ghi vao state.json cua tai (loi 2-writer).

Nhan tai khoan tu khai (doc 09 §3, thiet ke v0.4): fetcher co the gui kem
header ``X-Muse-Account: <account_id>`` tren moi request. Gateway CHI doc
header nay SAU khi xac thuc Bearer thanh cong, CHI ghi nhan nhan tren
endpoint ``/queue`` (mo rong ``gateway-fetch-state.json`` trong dung lan
ghi ``record_fetch`` da co: ``fetch_labels`` + ``unlabelled``), va CHI ghi
nhan da kiem dinh (khop mau ``account_id`` o DESIGN §10). Ngoai le hep S25
(SPEC S6): nhan da kiem dinh duoc ghi day du vao log gateway
(``label=<nhan|->``) + fetch-state; CAM ghi gia tri header tho chua kiem
dinh; CAM ghi nhan vao log khi request chua xac thuc. Request ``/queue``
da xac thuc nhung tra 409/400 chi cap nhat ``last_seen_at`` cua nhan (R3),
tuyet doi khong cham ``fetch_count``/``last_fetch_at``/``served_seq``/
``recent_seen_at``. 401/403/404 khong ghi gi.

Log chi metadata (thoi gian + bot_id + endpoint + since + so event + ket
qua + nhan da kiem dinh o endpoint queue). CAM log secret/header tho/noi
dung tin (SPEC S6).
"""

import argparse
import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import stat
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GATEWAY_VERSION = "0.1.0"
DEFAULT_PORT = 8765
DEFAULT_BIND = "0.0.0.0"
DEFAULT_ALLOW_CIDRS = "127.0.0.0/8,::1/128,100.64.0.0/10"
MAX_LIMIT = 100
# SUY LUAN — do o GĐ1: state cu qua 5 phut thi health ghi co stale (04 §1.1).
STALE_AFTER_SECONDS = 300

# DESIGN §10: mau bot_id bat buoc, kiem tra o moi entry point.
BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Doc 09 §3 (v0.4): mau account_id (nhan tai khoan tu khai) — dung mau
# bat buoc nhu bot_id (DESIGN §10). Chi nhan khop mau moi duoc ghi.
ACCOUNT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Doc 09 §7: hang so nen nhan (SUY LUAN — ghi ro de dieu chinh sau khi do).
LABEL_RECENT_MAX = 5  # so moc gan nhat giu lai moi nhan
LABEL_MAX_PER_BOT = 10  # tran nhan/bot (vuot thi loai nhan cu nhat)
LABEL_PRUNE_AFTER_SECONDS = 7 * 24 * 3600  # cat tia nhan cu hon 7 ngay
# 04 §1.2: ten file chi ky tu an toan; ".." / "." bi chan rieng ben duoi.
FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Truong duoc phep tra ve qua /config — moi truong khac (ke ca truong chua
# secret neu ai do vo tinh ghi vao file cau hinh) deu bi loai.
ALLOWLIST_FIELDS = (
    "bot_id",
    "bot_kind",
    "owner_sender_ids",
    "allowed_chats",
    "sender_roles",
    "default_sender_policy",
    "admin_commands_owner_only",
    "memory_tier_a_owner_only",
    "chat_control_commands_enabled",
    "version",
)
_SECRET_KEY_HINTS = ("secret", "token", "password", "passwd")


def _load_center_store():
    """Import module store trung tam (pc.center.center_store).

    Chay dang script truc tiep thi sys.path khong co goc code — them
    goc code (thu muc chua pc/) roi import lai. Chi import, khong ghi
    file kho o day: moi ghi kho cua gateway deu qua store.
    """
    try:
        from pc.center import center_store
        return center_store
    except ImportError:
        code_root = Path(__file__).resolve().parents[2]
        if str(code_root) not in sys.path:
            sys.path.insert(0, str(code_root))
        from pc.center import center_store
        return center_store


def default_log(message):
    """Ghi 1 dong log metadata ra stdout (journald thu lai)."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    print("%s %s" % (now, message), flush=True)


def parse_cidrs(text):
    """Parse chuoi CIDR phay nhau thanh danh sach network. Sai -> ValueError."""
    networks = []
    for part in text.split(","):
        part = part.strip()
        if part:
            networks.append(ipaddress.ip_network(part, strict=False))
    if not networks:
        raise ValueError("allowlist IP rong")
    return networks


def ip_allowed(networks, address_text):
    """True neu dia chi nguon nam trong allowlist. Khong parse duoc -> False."""
    try:
        addr = ipaddress.ip_address(address_text)
    except ValueError:
        return False
    # IPv4-map IPv6 (::ffff:127.0.0.1) -> xet nhu IPv4 goc.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for net in networks:
        try:
            if addr in net:
                return True
        except TypeError:
            continue
    return False


def parse_iso_timestamp(text):
    """Parse ISO-8601 thanh datetime co mui gio; khong parse duoc -> None.

    Chuoi khong kem mui gio (dang baseline cu) duoc hieu la gio dia phuong
    cua PC. Dinh dang v0.2 bat buoc kem mui gio.
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
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def read_json_file(path):
    """Doc JSON khoan dung: thieu/hong/khong phai JSON -> None."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _valid_seq(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_account_label(value):
    """Kiem dinh nhan tai khoan tu khai (doc 09 §3.1): khop mau DESIGN
    §10 thi tra ve chuoi do, sai/thieu/khong phai chuoi thi None (coi
    nhu khong nhan, fail-open MA9). Ham nay KHONG log gi ca — gia tri
    tho khong bao gio duoc ghi ra (S25, chong chen log)."""
    if isinstance(value, str) and ACCOUNT_LABEL_RE.match(value):
        return value
    return None


def _new_label_entry(now_iso):
    return {"first_seen_at": now_iso, "last_seen_at": now_iso,
            "fetch_count": 0, "recent_seen_at": []}


def _ensure_labels_dict(entry):
    labels = entry.get("fetch_labels")
    if not isinstance(labels, dict):
        labels = {}
        entry["fetch_labels"] = labels
    return labels


def _ensure_label_entry(labels, label, now_iso):
    """Lay muc cua nhan, tao moi neu chua co. Muc hong dang thi sua dang
    (dien gia tri mac dinh), khong xoa truong hop le da co."""
    item = labels.get(label)
    if not isinstance(item, dict):
        item = _new_label_entry(now_iso)
        labels[label] = item
        return item
    if not isinstance(item.get("first_seen_at"), str):
        previous = item.get("last_seen_at")
        item["first_seen_at"] = previous if isinstance(previous, str) \
            else now_iso
    if not isinstance(item.get("last_seen_at"), str):
        item["last_seen_at"] = now_iso
    if not _valid_seq(item.get("fetch_count")):
        item["fetch_count"] = 0
    if not isinstance(item.get("recent_seen_at"), list):
        item["recent_seen_at"] = []
    return item


def _ensure_unlabelled_entry(entry, now_iso):
    """Muc 'khong nhan' (doi xung fetch_labels, doc 09 §3.3): dem + moc
    cuoi + 5 moc gan nhat. Tao moi neu chua co; muc hong dang thi sua."""
    item = entry.get("unlabelled")
    if not isinstance(item, dict):
        item = {"last_seen_at": now_iso, "fetch_count": 0,
                "recent_seen_at": []}
        entry["unlabelled"] = item
        return item
    if not isinstance(item.get("last_seen_at"), str):
        item["last_seen_at"] = now_iso
    if not _valid_seq(item.get("fetch_count")):
        item["fetch_count"] = 0
    if not isinstance(item.get("recent_seen_at"), list):
        item["recent_seen_at"] = []
    return item


def _append_recent(item, now_iso):
    recent = item["recent_seen_at"]
    recent.append(now_iso)
    item["recent_seen_at"] = recent[-LABEL_RECENT_MAX:]


def _prune_and_cap_labels(entry, now_dt, keep_label):
    """Cat tia + tran nhan (doc 09 §3.3, §7). CHI goi ben trong khoa ghi
    fetch-state. Loai nhan co last_seen_at cu hon 7 ngay; vuot tran 10
    nhan/bot thi loai nhan last_seen_at cu nhat (tru nhan vua thay trong
    lan ghi nay). Muc unlabelled KHONG bi cat tia (UI phan biet 'truong
    ton tai' voi 'gateway cu' — doc 09 §5.3)."""
    labels = entry.get("fetch_labels")
    if not isinstance(labels, dict):
        return
    for label in list(labels):
        item = labels[label]
        seen = parse_iso_timestamp(item.get("last_seen_at")) \
            if isinstance(item, dict) else None
        if seen is None or (now_dt - seen).total_seconds() \
                > LABEL_PRUNE_AFTER_SECONDS:
            del labels[label]
    floor_time = datetime.min.replace(tzinfo=timezone.utc)
    while len(labels) > LABEL_MAX_PER_BOT:
        candidates = [name for name in labels if name != keep_label]
        if not candidates:
            break

        def _age_key(name):
            item = labels[name]
            seen = parse_iso_timestamp(item.get("last_seen_at")) \
                if isinstance(item, dict) else None
            return (seen if seen is not None else floor_time, name)

        del labels[min(candidates, key=_age_key)]


def registry_entry(root, bot_id):
    """Tim muc cua bot trong registry.json; ho tro ca dang dict va list."""
    data = read_json_file(root / "registry.json")
    if not isinstance(data, dict):
        return None
    bots = data.get("bots")
    if isinstance(bots, dict):
        entry = bots.get(bot_id)
        return entry if isinstance(entry, dict) else None
    if isinstance(bots, list):
        for entry in bots:
            if isinstance(entry, dict) and entry.get("bot_id") == bot_id:
                return entry
        return None
    entry = data.get(bot_id)
    return entry if isinstance(entry, dict) else None


def bot_exists(root, bot_id):
    """Bot 'ton tai' neu co bat ky dau vet nao: registry, secret, cau hinh,
    hoac thu muc du lieu. Dung de phan biet 404 (khong ton tai) voi 401
    (co bot nhung secret sai) theo 04 §1.2."""
    if registry_entry(root, bot_id) is not None:
        return True
    if (root / "secrets" / ("%s.queue_secret" % bot_id)).is_file():
        return True
    if (root / "config" / ("%s.json" % bot_id)).is_file():
        return True
    if (root / "config" / ("%s.allowlist.json" % bot_id)).is_file():
        return True
    return (root / "data" / bot_id).is_dir()


def read_queue_secret(root, bot_id):
    """Doc queue secret cua bot tu file (600), moi request doc lai de xoay
    secret khong can restart. Thieu/rong -> None (khong bao gio chap nhan
    secret rong)."""
    path = root / "secrets" / ("%s.queue_secret" % bot_id)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first_line:
        return None
    return first_line.encode("utf-8")


def read_queue_file(path):
    """Doc queue.jsonl chiu dong do cuoi file, khong khoa ghi cua poller.

    Tra ve (records, floor_seq, latest_seq, skipped): records la cac dong
    JSON hop le co truong seq so nguyen, theo thu tu file; dong hong/dang
    viet do bi bo qua (dem vao skipped), khong crash. Dong cuoi chua co
    ky tu xuong dong nhung parse tron ven van duoc tinh (fetcher chong
    trung theo ten file).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return [], 0, 0, 0
    if not data:
        return [], 0, 0, 0
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines = lines[:-1]
    records = []
    skipped = 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            skipped += 1
            continue
        if not isinstance(record, dict) or not _valid_seq(record.get("seq")):
            skipped += 1
            continue
        records.append(record)
    if not records:
        return [], 0, 0, skipped
    floor_seq = records[0]["seq"]
    latest_seq = max(record["seq"] for record in records)
    return records, floor_seq, latest_seq, skipped


def apply_state_bounds(state, file_floor, file_latest):
    """Hop nhat bien queue tu state.json cua tai (nguon chan ly ve cat tia)
    voi bien doc duoc tu file: floor lay theo state neu co, latest lay max
    cua hai ben. Dung chung cho /health va /queue de hai endpoint nhat quan.
    """
    floor_seq = file_floor
    latest_seq = file_latest
    if isinstance(state, dict):
        state_floor = state.get("queue_floor_seq")
        if _valid_seq(state_floor):
            floor_seq = state_floor
        state_latest = state.get("queue_latest_seq")
        if _valid_seq(state_latest):
            latest_seq = max(latest_seq, state_latest)
    return floor_seq, latest_seq


class GatewayContext:
    """Trang thai dung chung cua gateway: goc du lieu, allowlist IP, khoa
    ghi fetch-state, ham log. Khong giu secret trong bo nho."""

    def __init__(self, root, networks, log_fn=None):
        self.root = Path(root)
        self.networks = list(networks)
        self.fetch_state_path = self.root / "gateway-fetch-state.json"
        self.fetch_state_lock = threading.Lock()
        self.log = log_fn or default_log
        # Moc khoi dong gateway (tinh hub_uptime cho /center/health).
        self.started_at = datetime.now(timezone.utc)

    def fetch_info(self, bot_id):
        """Doc muc fetch gan nhat cua bot tu file RIENG cua gateway."""
        with self.fetch_state_lock:
            data = read_json_file(self.fetch_state_path)
        if not isinstance(data, dict):
            return {}
        bots = data.get("bots")
        if not isinstance(bots, dict):
            return {}
        entry = bots.get(bot_id)
        return dict(entry) if isinstance(entry, dict) else {}

    def _write_fetch_state_locked(self, data):
        """Ghi fetch-state (tmp+rename, 600, fsync). CHI goi khi dang giu
        fetch_state_lock. Ghi hong thi chi log canh bao."""
        tmp_path = self.fetch_state_path.with_suffix(".tmp")
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
            try:
                os.write(fd, json.dumps(
                    data, ensure_ascii=False).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, self.fetch_state_path)
        except OSError as exc:
            self.log("gateway fetch-state write failed class=%s"
                     % type(exc).__name__)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _load_fetch_state_locked(self):
        """Doc fetch-state, chuan hoa ve dang {"version": 1, "bots": {}}.
        CHI goi khi dang giu fetch_state_lock. version giu 1 vi mo rong
        nhan la cong them (doc 09 §3.3)."""
        data = read_json_file(self.fetch_state_path)
        if not isinstance(data, dict) or not isinstance(
            data.get("bots"), dict
        ):
            data = {"version": 1, "bots": {}}
        return data

    def _bot_fetch_entry_locked(self, data, bot_id):
        entry = data["bots"].get(bot_id)
        if not isinstance(entry, dict):
            entry = {}
            data["bots"][bot_id] = entry
        return entry

    def record_fetch(self, bot_id, served_max_seq, label=None):
        """Ghi 'lan fetch gan nhat + toi seq nao' + watermark 'da phuc vu'
        vao file RIENG cua gateway (tmp+rename, 600). served_max_seq=None
        nghia la dot nay khong co event nao — chi cap nhat thoi diem, khong
        tien watermark. Ghi hong thi chi log canh bao, khong lam hong dot
        fetch vua phuc vu xong.

        label: nhan da kiem dinh (validate_account_label) hoac None.
        Cung lan ghi nay mo rong them phan nhan (doc 09 §3.3): nhan co
        thi tang fetch_labels[nhan], khong nhan thi tang unlabelled —
        cung 1 khoa, ghi TRUOC response. label=None tu loi goi truc tiep
        (khong qua HTTP) cung duoc tinh la khong nhan."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self.fetch_state_lock:
            data = self._load_fetch_state_locked()
            entry = self._bot_fetch_entry_locked(data, bot_id)
            entry["last_fetch_at"] = now_iso
            previous_count = entry.get("fetch_count", 0)
            entry["fetch_count"] = (
                previous_count + 1 if _valid_seq(previous_count) else 1
            )
            if served_max_seq is not None:
                entry["last_fetch_seq"] = served_max_seq
                # Alias "last_fetched_seq": khoa dau tien ma ham doc
                # watermark cua tai (pc/ear/queue_store.py) tim — cung gia
                # tri voi served_seq, de cat tia cua tai doc dung so.
                entry["last_fetched_seq"] = served_max_seq
                previous_served = entry.get("served_seq", 0)
                if not _valid_seq(previous_served):
                    previous_served = 0
                entry["served_seq"] = max(previous_served, served_max_seq)
            if label is not None:
                labels = _ensure_labels_dict(entry)
                item = _ensure_label_entry(labels, label, now_iso)
                item["last_seen_at"] = now_iso
                item["fetch_count"] = item["fetch_count"] + 1
                _append_recent(item, now_iso)
            else:
                item = _ensure_unlabelled_entry(entry, now_iso)
                item["last_seen_at"] = now_iso
                item["fetch_count"] = item["fetch_count"] + 1
                _append_recent(item, now_iso)
            _prune_and_cap_labels(entry, now, label)
            self._write_fetch_state_locked(data)

    def record_center_fetch(self, account_id, served_max_seq=None):
        """Ghi 'lan keo tin trung tam gan nhat' vao muc ``center`` rieng
        trong cung file fetch-state (DESIGN-TRUNG-TAM §9 CHAN-2, v0.2.4):
        ``{"center": {account_id: {"last_fetch_at": ISO kem offset,
        "fetch_count": N, "last_fetch_seq": S}}}``. Khoa theo account_id trong muc center
        rieng, KHONG tron vao muc ``bots`` (account_id va bot_id cung
        mau nen ghi chung khoa tran co nguy co dung ten). Chi goi khi
        feed GET 200 da xac thuc, ghi TRUOC khi gui response (nhu
        record_fetch cua bot); health/tasks/<id> va moi response loi
        KHONG goi. File cu thieu muc center thi doc ve {} (tuong thich
        nguoc). Cung khoa/tmp+rename/600/fsync nhu record_fetch.

        (GC-F02, T3m) ``served_max_seq``: seq lon nhat thuc su phuc
        vu trong dot keo nay (None khi dot nay khong co su kien nao
        — chi cap nhat thoi diem, khong tien cursor). Luu don dieu
        (max) de watermark don dep (``load_center_cursors``) dung
        duoc: chi cat su kien co seq <= min cursor cac tai khoan
        dang tham gia.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self.fetch_state_lock:
            data = self._load_fetch_state_locked()
            center = data.get("center")
            if not isinstance(center, dict):
                center = {}
                data["center"] = center
            entry = center.get(account_id)
            if not isinstance(entry, dict):
                entry = {}
                center[account_id] = entry
            entry["last_fetch_at"] = now_iso
            previous_count = entry.get("fetch_count", 0)
            entry["fetch_count"] = (
                previous_count + 1 if _valid_seq(previous_count) else 1
            )
            if served_max_seq is not None and _valid_seq(served_max_seq):
                previous = entry.get("last_fetch_seq", 0)
                if not _valid_seq(previous):
                    previous = 0
                entry["last_fetch_seq"] = max(previous, served_max_seq)
            self._write_fetch_state_locked(data)

    def record_label_seen(self, bot_id, label):
        """R3 (doc 09 §3.3): request /queue DA XAC THUC thanh cong nhung
        tra 409/400 — CHI cap nhat last_seen_at cua nhan (tao muc moi voi
        first_seen_at = moc do neu chua co). TUYET DOI KHONG cham
        fetch_count (cap bot lan cap nhan), last_fetch_at, served_seq,
        recent_seen_at, hay bat ky truong nao has_muse_fetched doc — giu
        nguyen ngu nghia K3 'da fetch' (chi request queue thanh cong moi
        tinh). label=None thi chi cap nhat unlabelled.last_seen_at. Cung
        khoa/tmp+rename/600 nhu record_fetch, ghi TRUOC response. CHI
        goi sau khi xac thuc thanh cong; 401/403/404 khong goi ham nay."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self.fetch_state_lock:
            data = self._load_fetch_state_locked()
            entry = self._bot_fetch_entry_locked(data, bot_id)
            if label is not None:
                labels = _ensure_labels_dict(entry)
                item = _ensure_label_entry(labels, label, now_iso)
                item["last_seen_at"] = now_iso
            else:
                item = _ensure_unlabelled_entry(entry, now_iso)
                item["last_seen_at"] = now_iso
            _prune_and_cap_labels(entry, now, label)
            self._write_fetch_state_locked(data)


def make_handler(ctx):
    """Tao handler HTTP chi-doc. Chi dinh nghia xu ly GET; moi phuong thuc
    khac tra 501 mac dinh cua thu vien — day la bang chung gateway khong
    co endpoint ghi."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "multibot-gateway/%s" % GATEWAY_VERSION
        sys_version = ""

        def log_message(self, *args):
            # Khong dung log mac dinh (no in duong dan tho); gateway tu log
            # metadata qua ctx.log o moi diem tra ve.
            pass

        def _log(self, bot_label, endpoint, status, since=None, count=None,
                 extra=""):
            parts = ["gateway", "bot=%s" % bot_label,
                     "endpoint=%s" % endpoint, "status=%d" % status]
            if since is not None:
                parts.append("since=%d" % since)
            if count is not None:
                parts.append("count=%d" % count)
            if extra:
                parts.append(extra)
            ctx.log(" ".join(parts))

        def _send_json(self, code, obj):
            try:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                body = b'{"error": "internal_error"}'
                code = 500
            try:
                self.send_response(code)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def _send_file(self, path, size, filename=None):
            """Tra file ve dang octet-stream + nosniff.

            ``filename`` (v0.2.5, file trung tam): them
            ``Content-Disposition: attachment`` (RFC 5987) de trinh
            duyet tai ve thay vi mo inline — file HTML khong chay
            script. Khong truyen ``filename`` thi giu nguyen hanh vi
            cu (tai file bot).
            """
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                if filename is not None:
                    store = _load_center_store()
                    self.send_header(
                        "Content-Disposition",
                        store.attachment_disposition(filename))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with open(path, "rb") as handle:
                    shutil.copyfileobj(handle, self.wfile, length=65536)
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def _authenticated(self, bot_id):
            expected = read_queue_secret(ctx.root, bot_id)
            if expected is None:
                return False
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            given = auth[len("Bearer "):].strip()
            if not given or any(ch.isspace() for ch in given):
                return False
            return hmac.compare_digest(given.encode("utf-8"), expected)

        # -- Endpoint trung tam (chặng T1, chi GET) ---------------------
        def _authenticated_center(self, account_id):
            """Xac thuc Bearer = center fetch secret cua dung tai khoan
            (TS1). Secret doc lai moi request de xoay khong can restart.
            """
            store = _load_center_store()
            expected = store.read_center_secret(ctx.root, account_id,
                                                "fetch")
            if expected is None:
                return False
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            given = auth[len("Bearer "):].strip()
            if not given or any(ch.isspace() for ch in given):
                return False
            return hmac.compare_digest(given.encode("utf-8"), expected)

        def _center_log(self, account_label, endpoint, status, extra=""):
            parts = ["gateway", "center", "account=%s" % account_label,
                     "endpoint=%s" % endpoint, "status=%d" % status]
            if extra:
                parts.append(extra)
            ctx.log(" ".join(parts))

        def _handle_center(self, parts, query):
            """Dinh tuyen /v1/center/<account>/feed|tasks/<id>|health|
            files/<file_id>."""
            store = _load_center_store()
            if len(parts) not in (5, 6) or parts[3] == "":
                self._send_json(404, {"error": "not_found"})
                self._center_log("none", "unknown", 404)
                return
            account_id = parts[3]
            endpoint = parts[4]
            if endpoint not in ("feed", "tasks", "health", "files"):
                self._send_json(404, {"error": "not_found"})
                self._center_log("none", "unknown", 404)
                return
            if endpoint in ("tasks", "files") and len(parts) != 6:
                self._send_json(404, {"error": "not_found"})
                self._center_log("none", endpoint, 404)
                return
            if endpoint not in ("tasks", "files") and len(parts) != 5:
                self._send_json(404, {"error": "not_found"})
                self._center_log("none", endpoint, 404)
                return
            # account_id sai mau -> tu choi toan bo.
            if not BOT_ID_RE.match(account_id):
                self._send_json(400, {"error": "bad_account_id"})
                self._center_log("invalid", endpoint, 400)
                return
            # Tai khoan khong ton tai (theo accounts.json) -> 404 chung.
            accounts = store.read_accounts(ctx.root)
            if account_id not in accounts:
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, endpoint, 404)
                return
            # Fetch secret cua DUNG tai khoan; sai/thieu -> 401.
            if not self._authenticated_center(account_id):
                self._send_json(401, {"error": "unauthorized"})
                self._center_log(account_id, endpoint, 401)
                return
            # Co tham gia trung tam (TT9 CHAN-1, v0.2.4): secret dung
            # nhung center_participating=false -> 403 rieng biet voi
            # 401. Thu tu kiem tra bat buoc: ton tai (404) -> secret
            # (401) -> co (403). Tam ngung phai dung han viec keo tin.
            if not accounts[account_id].get("center_participating"):
                self._send_json(403, {"error": "not_participating"})
                self._center_log(account_id, endpoint, 403)
                return
            try:
                if endpoint == "feed":
                    self._handle_center_feed(account_id, query, store)
                elif endpoint == "health":
                    self._handle_center_health(account_id, store)
                elif endpoint == "files":
                    self._handle_center_file(account_id, parts[5], store)
                else:
                    self._handle_center_task(account_id, parts[5], store)
            except Exception as exc:  # noqa: BLE001 - chi log ten lop loi
                self._send_json(500, {"error": "internal_error"})
                self._center_log(account_id, endpoint, 500,
                                 extra="error_class=%s"
                                 % type(exc).__name__)

        def _handle_center_file(self, account_id, file_id, store):
            """GET /v1/center/<account>/files/<file_id> — tai file ve
            (v0.2.5, Q-C7).

            ``file_id`` sai mau -> 404 chung (khong lo mau); file
            khong ton tai (thieu bytes/sidecar) hoac bytes lech
            sidecar -> 404 chung. Co tham gia da kiem tra o
            ``_handle_center`` (403 ``not_participating``). Tra bytes
            octet-stream + attachment + nosniff (trinh duyet tai ve,
            khong mo inline — file HTML khong chay script). Khong
            log ten file.
            """
            if not isinstance(file_id, str) \
                    or not store.FILE_ID_RE.match(file_id):
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, "files", 404)
                return
            center = store.CenterStore(ctx.root)
            meta = center.get_file_meta(file_id)
            if meta is None:
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, "files", 404)
                return
            # Quyen file theo phong (v0.3.0, T3c — SPEC TT16): file
            # kem tin trong phong rieng chi tai khoan trong phong +
            # chu nhan tai duoc; file trong phong minh khong thay ->
            # 404 chung (khong phan biet voi file khong ton tai).
            # File chua co tin tham chieu (NS-14): chi nguoi tai len
            # (uploaded_by) + chu nhan tai duoc.
            visible_rooms = center.file_visible_rooms(file_id)
            account_rooms = {store.ROOM_CHUNG, "dm:" + account_id}
            if visible_rooms is None:
                allowed = meta.get("uploaded_by") == account_id
            else:
                allowed = bool(visible_rooms & account_rooms)
            if not allowed:
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, "files", 404)
                return
            path = center.file_bytes_path(file_id)
            try:
                size = path.stat().st_size
            except OSError:
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, "files", 404)
                return
            if size != meta.get("file_size"):
                # Bytes lech sidecar (kho hong) — tu choi thay vi tra
                # file sai (downloader phia Muse cung kiem sha256).
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, "files", 404)
                return
            sent = self._send_file(path, size,
                                   filename=meta.get("file_name"))
            self._center_log(account_id, "files",
                             200 if sent else 499,
                             extra="file_size=%d" % size)

        def _handle_center_feed(self, account_id, query, store):
            qs = parse_qs(query)
            try:
                since = int(qs.get("since", ["0"])[0])
                limit = int(qs.get("limit", ["100"])[0])
            except (ValueError, TypeError):
                self._send_json(400, {"error": "bad_params"})
                self._center_log(account_id, "feed", 400)
                return
            if since < 0 or limit < 1:
                self._send_json(400, {"error": "bad_params"})
                self._center_log(account_id, "feed", 400)
                return
            limit = min(limit, MAX_LIMIT)
            # Loc theo phong (v0.3.0, T3c — SPEC TT7, CHANG-3):
            # tai khoan chi thay khung chung + phong rieng cua chinh
            # minh. Khong kem room -> ca 2 phong thay duoc. Kem room
            # ngoai tam thay -> 403 (khong loc lang le, khong 404);
            # room sai mau -> 400. Viec (kind task) khong theo phong.
            room_values = qs.get("room", [])
            room = None
            if room_values:
                try:
                    room = store.parse_room(room_values[0])
                except store.CenterError:
                    self._send_json(400, {"error": "bad_room"})
                    self._center_log(account_id, "feed", 400)
                    return
            visible_rooms = {store.ROOM_CHUNG, "dm:" + account_id}
            if room is not None and room not in visible_rooms:
                self._send_json(403, {"error": "room_forbidden"})
                self._center_log(account_id, "feed", 403,
                                 extra="room_forbidden")
                return
            center = store.CenterStore(ctx.root)
            events, latest_seq, floor_seq = center.get_feed(
                since, limit, room=room, visible_rooms=visible_rooms)
            if since > latest_seq:
                # Chi feed GET 200 moi ghi lan keo (CHAN-2); ghi TRUOC
                # response, ke ca khi cursor stale (nhu record_fetch
                # cua bot o /queue).
                ctx.record_center_fetch(account_id)
                self._send_json(200, {
                    "events": [],
                    "stale_center_cursor": True,
                    "latest_seq": latest_seq,
                    "floor_seq": floor_seq,
                })
                self._center_log(account_id, "feed", 200,
                                 extra="count=0 stale_center_cursor")
                return
            # NGOAI LE seen_at (C-2): truoc khi gui response, goi module
            # store de store ghi seen_at cho tung task_id ma response
            # thuc su chua, do chinh assigned_to fetch lan dau. Gateway
            # KHONG tu ghi file kho nao.
            task_ids = [event.get("task_id") for event in events
                        if event.get("kind") == "task"
                        and event.get("task_id")]
            center.record_seen(account_id, task_ids)
            # Ghi lan keo tin trung tam TRUOC khi gui response (CHAN-2).
            # GC-F02: truyen seq lon nhat vua phuc vu de watermark don
            # dep dung duoc (dot khong co su kien thi khong tien
            # cursor).
            served = events[-1].get("seq") if events else None
            ctx.record_center_fetch(account_id, served)
            self._send_json(200, {
                "events": events,
                "latest_seq": latest_seq,
                "floor_seq": floor_seq,
            })
            self._center_log(account_id, "feed", 200,
                             extra="count=%d" % len(events))

        def _handle_center_task(self, account_id, task_id, store):
            # Lay le KHONG tinh seen_at (V2-GC-08).
            if not store.TASK_ID_RE.match(task_id):
                self._send_json(400, {"error": "bad_task_id"})
                self._center_log(account_id, "tasks", 400)
                return
            center = store.CenterStore(ctx.root)
            task = center.get_task(task_id)
            if task is None:
                self._send_json(404, {"error": "not_found"})
                self._center_log(account_id, "tasks", 404)
                return
            self._send_json(200, {"task": task})
            self._center_log(account_id, "tasks", 200)

        def _handle_center_health(self, account_id, store):
            center = store.CenterStore(ctx.root)
            health = center.center_health()
            config = store.load_center_config(ctx.root)
            uptime = int((datetime.now(timezone.utc)
                          - ctx.started_at).total_seconds())
            # (v0.6.0, T3l — Q-V060-32 ĐÃ CHỐT) Cờ thảo luận của
            # tài khoản gọi: đọc từ accounts.json khóa
            # center_discussion_enabled, mặc định true. Dùng
            # store.read_accounts (đã chuẩn hóa cờ về bool, vắng/lạ
            # -> True) thay vì đọc file thô — không cần thêm import.
            accounts = store.read_accounts(ctx.root)
            record = accounts.get(account_id) or {}
            body = {
                "accounts": health["accounts"],
                "open_tasks": health["open_tasks"],
                "overdue_tasks": health["overdue_tasks"],
                "latest_seq": health["latest_seq"],
                "last_write_at": health["last_write_at"],
                "overdue_severe_hours": config["overdue_severe_hours"],
                "hub_uptime_seconds": uptime,
                "discussion_enabled": bool(record.get(
                    "center_discussion_enabled", True)),
            }
            self._send_json(200, body)
            self._center_log(account_id, "health", 200)

        def do_GET(self):
            # Nhan cua request hien tai (chi dat sau khi xac thuc, trong
            # _handle_queue) — reset moi request vi 1 handler co the phuc
            # vu nhieu request tren cung ket noi keep-alive. None = chua
            # doc nhan (request chua xac thuc / khong phai endpoint queue).
            self._queue_label_part = None
            # 1. Allowlist IP nguon — ngoai allowlist -> 403, truoc moi thu.
            client_ip = self.client_address[0]
            if not ip_allowed(ctx.networks, client_ip):
                self._send_json(403, {"error": "forbidden"})
                self._log("none", "unknown", 403)
                return
            parsed = urlparse(self.path)
            parts = parsed.path.split("/")
            # 1b. Dinh tuyen trung tam (chặng T1): /v1/center/<account>/...
            # Chi GET; khong them POST vao gateway (grep "gateway module
            # has no POST" van dung).
            if len(parts) >= 4 and parts[0] == "" and parts[1] == "v1" \
                    and parts[2] == "center":
                self._handle_center(parts, parsed.query)
                return
            # 2. Dinh tuyen nghiem: /v1/bots/<bot_id>/<endpoint>[/<file>].
            # Duong dan khong dung mau (ke ca endpoint don-bot cu) -> 404.
            if (len(parts) not in (5, 6) or parts[0] != ""
                    or parts[1] != "v1" or parts[2] != "bots"):
                self._send_json(404, {"error": "not_found"})
                self._log("none", "unknown", 404)
                return
            bot_id = parts[3]
            endpoint = parts[4]
            if endpoint not in ("health", "queue", "config", "files",
                                "retry-dead"):
                self._send_json(404, {"error": "not_found"})
                self._log("none", "unknown", 404)
                return
            if endpoint == "files" and len(parts) != 6:
                self._send_json(404, {"error": "not_found"})
                self._log("none", "files", 404)
                return
            if endpoint != "files" and len(parts) != 5:
                self._send_json(404, {"error": "not_found"})
                self._log("none", endpoint, 404)
                return
            # 3. bot_id sai mau -> tu choi toan bo, khong "lam sach roi tiep".
            # Log nhan "invalid", khong log gia tri tho (chong chen log).
            if not BOT_ID_RE.match(bot_id):
                self._send_json(400, {"error": "bad_bot_id"})
                self._log("invalid", endpoint, 400)
                return
            # 4. Bot khong ton tai -> 404 chung chung.
            if not bot_exists(ctx.root, bot_id):
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, endpoint, 404)
                return
            # 5. Secret cua DUNG bot, so sanh constant-time; sai/thieu -> 401.
            if not self._authenticated(bot_id):
                self._send_json(401, {"error": "unauthorized"})
                self._log(bot_id, endpoint, 401)
                return
            try:
                if endpoint == "health":
                    self._handle_health(bot_id)
                elif endpoint == "queue":
                    self._handle_queue(bot_id, parsed.query)
                elif endpoint == "config":
                    self._handle_config(bot_id)
                elif endpoint == "retry-dead":
                    self._handle_retry_dead(bot_id, parsed.query)
                else:
                    self._handle_files(bot_id, parts[5])
            except Exception as exc:  # noqa: BLE001 - chi log ten lop loi
                self._send_json(500, {"error": "internal_error"})
                extra = "error_class=%s" % type(exc).__name__
                if endpoint == "queue" \
                        and self._queue_label_part is not None:
                    # Nhan da kiem dinh cua request nay (dat o dau
                    # _handle_queue, sau xac thuc); request chua xac thuc
                    # khong bao gio toi day voi _queue_label_part khac None.
                    extra += " " + self._queue_label_part
                self._log(bot_id, endpoint, 500, extra=extra)

        def _bot_state(self, bot_id):
            state = read_json_file(ctx.root / "data" / bot_id / "state.json")
            return state if isinstance(state, dict) else {}

        def _handle_health(self, bot_id):
            state = self._bot_state(bot_id)
            entry = registry_entry(ctx.root, bot_id) or {}
            paused = bool(entry.get("paused", False))
            paused_since = entry.get("paused_since")
            if not isinstance(paused_since, str):
                paused_since = None
            enabled = entry.get("enabled")
            if not isinstance(enabled, bool):
                enabled = None
            # Co stale: state cu qua 5 phut (SUY LUAN). Khong co updated_at
            # hop le thi dung mtime file; khong co file thi stale=true.
            state_path = ctx.root / "data" / bot_id / "state.json"
            state_time = parse_iso_timestamp(state.get("updated_at"))
            if state_time is None:
                try:
                    state_time = datetime.fromtimestamp(
                        state_path.stat().st_mtime).astimezone()
                except OSError:
                    state_time = None
            if state_time is None:
                stale = True
            else:
                age = (datetime.now().astimezone()
                       - state_time).total_seconds()
                stale = age > STALE_AFTER_SECONDS
            records, file_floor, file_latest, _skipped = read_queue_file(
                ctx.root / "data" / bot_id / "queue.jsonl")
            del records  # health khong can noi dung queue
            floor_seq, latest_seq = apply_state_bounds(
                state, file_floor, file_latest)
            fetch = ctx.fetch_info(bot_id)
            last_error_class = state.get("last_error_class")
            token_state = state.get("token_state")
            # Co 401/429 SUY LUAN tu truong state hien co (chua co truong co
            # rieng trong schema state.json v0.2).
            flag_401 = (token_state == "WAIT_TOKEN"
                        or last_error_class == "TOKEN_INVALID")
            flag_429 = last_error_class == "RATE_LIMITED"

            def _text_field(name):
                value = state.get(name)
                return value if isinstance(value, str) else None

            consec_err = state.get("consec_err")
            if not _valid_seq(consec_err):
                consec_err = None
            last_poll_ok = state.get("last_poll_ok")
            if not isinstance(last_poll_ok, bool):
                last_poll_ok = None
            # version cua state la SO (STATE_VERSION cua tai), khong phai
            # chuoi — chap nhan ca hai dang.
            version = state.get("version")
            if not isinstance(version, (str, int)) \
                    or isinstance(version, bool):
                version = None
            body = {
                "bot_id": bot_id,
                "updated_at": _text_field("updated_at"),
                "last_poll_ok": last_poll_ok,
                "last_poll_at": _text_field("last_poll_at"),
                "consec_err": consec_err,
                "last_error_class": _text_field("last_error_class"),
                "last_error_at": _text_field("last_error_at"),
                "token_state": _text_field("token_state"),
                "queue_latest_seq": latest_seq,
                "queue_floor_seq": floor_seq,
                "started_at": _text_field("started_at"),
                "version": version,
                "paused": paused,
                "paused_since": paused_since,
                "enabled": enabled,
                "stale": stale,
                "flag_401": flag_401,
                "flag_429": flag_429,
                "last_fetch_at": fetch.get("last_fetch_at"),
                "last_fetch_seq": fetch.get("last_fetch_seq"),
                "served_seq": fetch.get("served_seq"),
            }
            self._send_json(200, body)
            self._log(bot_id, "health", 200)

        def _handle_queue(self, bot_id, query):
            # Doc 09 §3.1: header nhan CHI duoc doc SAU khi xac thuc Bearer
            # thanh cong — do_GET da xac thuc truoc khi dieu phoi toi day.
            # Gia tri tho KHONG BAO GIO duoc ghi vao file/log (S25): chi
            # nhan da kiem dinh moi duoc dung; sai dinh dang/thieu header
            # thi coi nhu khong nhan (fail-open MA9).
            label = validate_account_label(
                self.headers.get("X-Muse-Account"))
            label_part = "label=%s" % (label if label is not None else "-")
            self._queue_label_part = label_part
            qs = parse_qs(query)
            try:
                since = int(qs.get("since", ["0"])[0])
                limit = int(qs.get("limit", ["100"])[0])
            except (ValueError, TypeError):
                # R3: da xac thuc nhung tra 400 — chi ghi moc nhin thay
                # nhan, TRUOC response.
                ctx.record_label_seen(bot_id, label)
                self._send_json(400, {"error": "bad_params"})
                self._log(bot_id, "queue", 400, extra=label_part)
                return
            if since < 0 or limit < 1:
                ctx.record_label_seen(bot_id, label)
                self._send_json(400, {"error": "bad_params"})
                self._log(bot_id, "queue", 400, extra=label_part)
                return
            limit = min(limit, MAX_LIMIT)
            state = self._bot_state(bot_id)
            records, file_floor, file_latest, skipped = read_queue_file(
                ctx.root / "data" / bot_id / "queue.jsonl")
            floor_seq, latest_seq = apply_state_bounds(
                state, file_floor, file_latest)
            batch = [rec for rec in records if rec["seq"] > since][:limit]
            extra = "skipped=%d" % skipped if skipped else ""
            # 409 cursor_too_old CHI khi du ca 4 dieu kien (R2/D7 — giu
            # nguyen tu baseline, khong bot dieu kien nao).
            if since < floor_seq and since != 0 and floor_seq > 0 \
                    and not batch:
                # R3: da xac thuc nhung tra 409 — chi ghi moc nhin thay
                # nhan, TRUOC response.
                ctx.record_label_seen(bot_id, label)
                self._send_json(409, {
                    "error": "cursor_too_old",
                    "floor_seq": floor_seq,
                    "latest_seq": latest_seq,
                })
                self._log(bot_id, "queue", 409, since=since, count=0,
                          extra=" ".join(p for p in (extra, label_part)
                                         if p))
                return
            if since > latest_seq:
                # Ghi fetch-state TRUOC khi gui response: nguoi doc file
                # ngay sau khi nhan response phai thay so moi (sua race
                # vong 3 — truoc day ghi sau response nen doc thay so cu).
                ctx.record_fetch(bot_id, None, label)
                self._send_json(200, {
                    "events": [],
                    "stale_cursor": True,
                    "latest_seq": latest_seq,
                    "floor_seq": floor_seq,
                })
                self._log(bot_id, "queue", 200, since=since, count=0,
                          extra=" ".join(
                              p for p in (extra, "stale_cursor", label_part)
                              if p))
                return
            served_max = batch[-1]["seq"] if batch else None
            # Ghi fetch-state TRUOC khi gui response (sua race vong 3).
            ctx.record_fetch(bot_id, served_max, label)
            self._send_json(200, {
                "events": batch,
                "latest_seq": latest_seq,
                "floor_seq": floor_seq,
            })
            self._log(bot_id, "queue", 200, since=since, count=len(batch),
                      extra=" ".join(p for p in (extra, label_part) if p))

        def _handle_retry_dead(self, bot_id, query):
            """Phuc vu yeu cau gui lai thu chet (NS-02, T3m — quyet
            D11 (b)): GET /v1/bots/<bot_id>/retry-dead?since=N&limit=100.

            Doc ``data/<bot_id>/retry_dead_requests.jsonl`` (UI ghi
            khi chu nhan bam "Gui lai"), tra cac yeu cau co seq >
            since. KHONG goi record_fetch: seq o day thuoc luong
            yeu cau rieng, khong phai cursor queue (ghi vao
            fetch-state se lam nhiem watermark queue). Khong co
            409 cursor_too_old: file yeu cau khong bi cat tia.
            Yeu cau chi mang reply_key + new_reply_key, KHONG mang
            noi dung tin (phia Muse tu tra noi dung tu outbox cua
            minh theo reply_key)."""
            qs = parse_qs(query)
            try:
                since = int(qs.get("since", ["0"])[0])
                limit = int(qs.get("limit", ["100"])[0])
            except (ValueError, TypeError):
                self._send_json(400, {"error": "bad_params"})
                self._log(bot_id, "retry-dead", 400)
                return
            if since < 0 or limit < 1:
                self._send_json(400, {"error": "bad_params"})
                self._log(bot_id, "retry-dead", 400)
                return
            limit = min(limit, MAX_LIMIT)
            requests = []
            latest_seq = 0
            try:
                text = (ctx.root / "data" / bot_id
                        / "retry_dead_requests.jsonl").read_text(
                            encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                seq = row.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool):
                    continue
                latest_seq = max(latest_seq, seq)
                if seq > since:
                    requests.append(row)
            batch = requests[:limit]
            self._send_json(200, {
                "requests": batch,
                "latest_seq": latest_seq,
            })
            self._log(bot_id, "retry-dead", 200, since=since,
                      count=len(batch))

        def _handle_config(self, bot_id):
            config = read_json_file(
                ctx.root / "config" / ("%s.json" % bot_id))
            allowlist = read_json_file(
                ctx.root / "config" / ("%s.allowlist.json" % bot_id))
            if config is None and allowlist is None:
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, "config", 404)
                return
            if not isinstance(config, dict):
                config = {}
            if not isinstance(allowlist, dict):
                allowlist = {}
            # Chi tra truong khong-secret, theo danh sach cho phep; bat ky
            # khoa nao goi y secret deu bi loai them mot lop nua.
            allowlist_out = {}
            for key in ALLOWLIST_FIELDS:
                if key not in allowlist:
                    continue
                lowered = key.lower()
                if any(hint in lowered for hint in _SECRET_KEY_HINTS):
                    continue
                allowlist_out[key] = allowlist[key]
            privacy = config.get("privacy", config.get(
                "privacy_enabled", config.get("telegram_privacy")))
            if not isinstance(privacy, (bool, str)):
                privacy = None
            display_name = config.get("display_name")
            if not isinstance(display_name, str):
                display_name = None
            platform = config.get("platform")
            if not isinstance(platform, str):
                platform = None
            bot_kind = config.get("bot_kind", allowlist.get("bot_kind"))
            if not isinstance(bot_kind, str):
                bot_kind = None
            # GĐ2: Sender phía Muse dựng adapter Telegram từ
            # api_base_url + parse_mode trong config đã đồng bộ (03 §4).
            # Cả 2 đều không phải secret; chỉ truyền qua khi đúng dạng
            # (parse_mode phải thuộc 3 giá trị Telegram chấp nhận).
            api_base_url = config.get("api_base_url")
            if not isinstance(api_base_url, str) or not api_base_url.strip():
                api_base_url = None
            parse_mode = config.get("parse_mode")
            if parse_mode not in ("Markdown", "MarkdownV2", "HTML"):
                parse_mode = None
            body = {
                "bot_id": bot_id,
                "platform": platform,
                "display_name": display_name,
                "bot_kind": bot_kind,
                "privacy": privacy,
                "api_base_url": api_base_url,
                "parse_mode": parse_mode,
                "allowlist": allowlist_out,
            }
            self._send_json(200, body)
            self._log(bot_id, "config", 200)

        def _handle_files(self, bot_id, raw_name):
            # Vong nay chi Telegram co file; Zalo khong co endpoint nay
            # (04 §1.2). Bot chua ro nen tang thi van phuc vu neu file co.
            config = read_json_file(
                ctx.root / "config" / ("%s.json" % bot_id))
            if isinstance(config, dict) and config.get("platform") == "zalo":
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, "files", 404)
                return
            # Ten file: 1 doan duy nhat, dung mau, cam ".", "..", ten an bat
            # dau bang "."; ky tu % (ma hoa URL) tu khac mau nen bi loai.
            if raw_name in ("", ".", "..") or raw_name.startswith(".") \
                    or not FILENAME_RE.match(raw_name):
                self._send_json(400, {"error": "bad_filename"})
                self._log(bot_id, "files", 400)
                return
            files_dir = ctx.root / "data" / bot_id / "files"
            candidate = files_dir / raw_name
            try:
                info = os.lstat(candidate)
            except OSError:
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, "files", 404)
                return
            # Cam symlink (kiem tra lstat) va cam thu muc — chi file thuong.
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, "files", 404, extra="blocked_symlink")
                return
            # Resolve + kiem tra tien to: file phai nam trong files/ cua bot.
            resolved = Path(os.path.realpath(candidate))
            base = Path(os.path.realpath(files_dir))
            if resolved == base or base not in resolved.parents:
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, "files", 404, extra="blocked_escape")
                return
            try:
                size = os.stat(candidate).st_size
            except OSError:
                self._send_json(404, {"error": "not_found"})
                self._log(bot_id, "files", 404)
                return
            self._send_file(candidate, size)
            self._log(bot_id, "files", 200, extra="bytes=%d" % size)

    return Handler


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(ctx, bind, port):
    """Tao server (chua chay). Port 0 = cong tam thoi do he dieu hanh cap."""
    return GatewayHTTPServer((bind, port), make_handler(ctx))


def _load_config_file(path_text):
    """Doc file cau hinh JSON tuy chon. Chi goi khi nguoi dung chi ro file."""
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise SystemExit("khong thay file cau hinh: %s" % path)
    data = read_json_file(path)
    if not isinstance(data, dict):
        raise SystemExit("file cau hinh khong phai JSON object: %s" % path)
    return data


def resolve_settings(argv=None):
    """Gop cau hinh: dong lenh > moi truong > file JSON > mac dinh."""
    parser = argparse.ArgumentParser(
        description="Gateway chi-doc he da-bot V2 (cong 8765).")
    parser.add_argument("--root", "--base-dir", dest="root", default=None,
                        help="thu muc goc multibot (mac dinh ~/multibot/)")
    parser.add_argument("--port", type=int, default=None,
                        help="cong lang nghe (mac dinh 8765)")
    parser.add_argument("--bind", default=None,
                        help="dia chi bind (mac dinh 0.0.0.0)")
    parser.add_argument("--allow-cidrs", default=None,
                        help="allowlist IP nguon, phay nhau")
    parser.add_argument("--config", default=None,
                        help="file cau hinh JSON tuy chon")
    args = parser.parse_args(argv)

    file_config = {}
    config_path = args.config or os.environ.get("MULTIBOT_CONFIG")
    if config_path:
        file_config = _load_config_file(config_path)

    def _pick(cli_value, env_names, file_key, default):
        if cli_value is not None:
            return cli_value
        for name in env_names:
            value = os.environ.get(name)
            if value:
                return value
        if file_key in file_config and file_config[file_key] not in (None, ""):
            return file_config[file_key]
        return default

    root_text = _pick(args.root,
                      ("MULTIBOT_BASE_DIR", "MULTIBOT_DIR", "MULTIBOT_ROOT"),
                      "root", str(Path.home() / "multibot"))
    root = Path(str(root_text)).expanduser()
    port_text = _pick(args.port, ("MULTIBOT_GATEWAY_PORT", "GATEWAY_PORT"),
                      "port", DEFAULT_PORT)
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise SystemExit("cong gateway khong hop le: %r" % (port_text,))
    if not 1 <= port <= 65535:
        raise SystemExit("cong gateway phai trong 1..65535: %d" % port)
    bind = str(_pick(args.bind, ("GATEWAY_BIND",), "bind", DEFAULT_BIND))
    cidrs_value = _pick(args.allow_cidrs,
                        ("MULTIBOT_ALLOW_CIDRS", "ALLOW_CIDRS"),
                        "allow_cidrs", DEFAULT_ALLOW_CIDRS)
    if isinstance(cidrs_value, (list, tuple)):
        cidrs_text = ",".join(str(item) for item in cidrs_value)
    else:
        cidrs_text = str(cidrs_value)
    try:
        networks = parse_cidrs(cidrs_text)
    except ValueError as exc:
        raise SystemExit("allowlist IP khong hop le: %s" % exc)
    return root, bind, port, networks


def main(argv=None):
    root, bind, port, networks = resolve_settings(argv)
    if not root.is_dir():
        raise SystemExit(
            "thu muc goc khong ton tai: %s — chay install.sh truoc" % root)
    ctx = GatewayContext(root, networks)
    try:
        server = make_server(ctx, bind, port)
    except OSError as exc:
        print("khong mo duoc cong %d (%s): %s" % (
            port, bind, type(exc).__name__), file=sys.stderr)
        print("Co the mot gateway khac dang chay, hoac cong dang ban.",
              file=sys.stderr)
        return 1

    def _shutdown(signum, _frame):
        del signum, _frame
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    ctx.log("gateway started bind=%s port=%d root=%s" % (bind, port, root))
    try:
        server.serve_forever()
    finally:
        server.server_close()
    ctx.log("gateway stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
