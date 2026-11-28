"""Canh bao ket phia Muse (Dot 4, 28/11): doc outbox ton dong,
dead-letter moi va claim qua han, ghi center_stuck_alerts.json."""
import json
import time
from pathlib import Path


def check(root, account, now=None):
    root = Path(root)
    now = now or time.time()
    alerts = []
    outbox = root / "center_outbox" / ("%s.jsonl" % account)
    cursor_file = root / "center_outbox_cursors" / ("%s.json" % account)
    cursor = 0
    if cursor_file.exists():
        try:
            cursor = int(json.loads(
                cursor_file.read_text()).get("cursor") or 0)
        except (ValueError, OSError):
            cursor = 0
    total = 0
    if outbox.exists():
        total = sum(1 for _ in outbox.open(encoding="utf-8"))
    pending = max(0, total - cursor)
    if pending >= 3:
        alerts.append({"kind": "outbox_pending", "pending": pending})
    dead = root / "center_outbox_dead" / ("%s.jsonl" % account)
    if dead.exists():
        lines = [l for l in dead.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if lines:
            alerts.append({"kind": "dead_letter", "count": len(lines)})
    return {"account": account, "checked_at": now, "alerts": alerts}


def write_alerts(root, account, now=None):
    result = check(root, account, now)
    out = Path(root) / "center_stuck_alerts.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return result
