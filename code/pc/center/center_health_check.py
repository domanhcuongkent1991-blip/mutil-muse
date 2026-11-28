"""Bo kiem tra suc khoe trung tam phia PC (cai tien 28/11/2026).

Doc du lieu PC da co san — ``gateway-fetch-state.json`` (lan keo
tin cuoi tung tai khoan, gateway ghi) + danh sach viec tren hub —
va sinh canh bao 2 muc:

- S1 khong keo tin: >30 phut vang, >2 gio do (dai hon cua so thay
  may ao 15-110 phut da biet).
- S4 viec Moi khong ai nhan: >30 phut vang, >2 gio do, theo tung
  tai khoan duoc giao chua co lich su nhan. Viec chung nhieu
  nguoi: chi tinh tai khoan chua bao xong sau khi nguoi khac da
  bao (khong bao oan ca doi khi viec moi giao).

An han 30 phut sau khi PC/WSL bat (``boot_at``): trong an han
khong sinh canh bao S1/S4 vi may Muse co the dang tinh lai.
WSL tat theo y chu nhan thi bo kiem tra khong chay (no nam trong
WSL) nen tu nhien khong bao dong — dung quyet 28/11.

Ket qua ghi ra ``data/center/health_alerts.json`` (tmp+rename) de
UI doc; ham ``evaluate`` thuan de test bang so lieu gia lap.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

YELLOW_SECONDS = 1800  # 30 phut
RED_SECONDS = 7200  # 2 gio
BOOT_GRACE_SECONDS = 1800  # an han 30 phut sau khi PC/WSL bat

LEVEL_YELLOW = "yellow"
LEVEL_RED = "red"


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _level(age_seconds):
    if age_seconds is None:
        return None
    if age_seconds > RED_SECONDS:
        return LEVEL_RED
    if age_seconds > YELLOW_SECONDS:
        return LEVEL_YELLOW
    return None


def evaluate(fetch_state, tasks, accounts, now, boot_at=None):
    """Tinh danh sach canh bao tu so lieu tho.

    ``fetch_state``: noi dung gateway-fetch-state.json (dict).
    ``tasks``: list ban ghi viec (dict co status/assigned_to/
    reported_by/history/created_at).
    ``accounts``: list account_id dang tham gia.
    ``now``/``boot_at``: epoch giay. Tra ve list alert dict:
    {account_id, kind, level, age_seconds, detail}.
    """
    alerts = []
    if boot_at is not None and now - boot_at < BOOT_GRACE_SECONDS:
        return alerts
    center_fetch = {}
    if isinstance(fetch_state, dict):
        center_fetch = fetch_state.get("center") or {}
        if not isinstance(center_fetch, dict):
            center_fetch = {}

    # S1: lan keo tin cuoi tung tai khoan.
    for account_id in accounts or []:
        entry = center_fetch.get(account_id)
        last_fetch = None
        if isinstance(entry, dict):
            last_fetch = _parse_iso(entry.get("last_fetch_at"))
        if last_fetch is None:
            continue  # chua tung keo: UI da hien "chua thay keo"
        age = now - last_fetch
        level = _level(age)
        if level:
            alerts.append({
                "account_id": account_id,
                "kind": "no_fetch",
                "level": level,
                "age_seconds": int(age),
                "detail": "khong keo tin %d phut" % (age // 60),
            })

    # S4: viec Moi khong ai nhan theo tung tai khoan.
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if task.get("status") != "moi":
            continue
        created = _parse_iso(task.get("created_at"))
        if created is None:
            continue
        age = now - created
        level = _level(age)
        if not level:
            continue
        assigned = task.get("assigned_to") or []
        if isinstance(assigned, str):
            assigned = [assigned]
        reported = set(task.get("reported_by") or [])
        history = task.get("history") or []
        accepted_by = {
            entry.get("by") for entry in history
            if isinstance(entry, dict)
        }
        for account_id in assigned:
            if account_id in reported or account_id in accepted_by:
                continue
            # Viec chung: chi bao tai khoan chua bao xong khi da
            # co nguoi khac bao xong (chua ai bao thi S1 da phu).
            if len(assigned) > 1 and not reported:
                continue
            alerts.append({
                "account_id": account_id,
                "kind": "task_unclaimed",
                "level": level,
                "age_seconds": int(age),
                "task_id": task.get("task_id"),
                "detail": "viec Moi %d phut chua ai nhan"
                          % (age // 60),
            })
    return alerts


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _boot_at(now):
    """Moc WSL/PC bat tu /proc/uptime; khong doc duoc -> None."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return now - uptime


def run(root, now=None):
    """Doc so lieu that tren PC, ghi health_alerts.json, tra alerts."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    root = Path(root)
    fetch_state = _read_json(root / "gateway-fetch-state.json") or {}
    tasks = []
    accounts = []
    try:
        from pc.center.center_store import CenterStore, read_accounts
        store = CenterStore(root)
        tasks = list(store.list_tasks().values())
        accounts = sorted(read_accounts(root).keys())
    except Exception:  # noqa: BLE001 - checker khong duoc crash PC
        tasks = []
    if not accounts and isinstance(fetch_state.get("center"), dict):
        accounts = sorted(fetch_state["center"].keys())
    alerts = evaluate(fetch_state, tasks, accounts, now,
                      boot_at=_boot_at(now))
    out_path = root / "data" / "center" / "health_alerts.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".json.tmp")
    payload = {
        "checked_at": datetime.fromtimestamp(
            now, tz=timezone.utc).isoformat(),
        "alerts": alerts,
    }
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, out_path)
    return alerts


if __name__ == "__main__":
    import sys
    result = run(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("center_health_check: %d canh bao" % len(result))
