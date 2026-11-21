#!/usr/bin/env python3
"""Bat tai@<bot_id> cho moi bot enabled=True trong registry.json.

Duoc keo boi multibot-tai-enabler.service (Type=oneshot) khi
multibot-core.target duoc START. Giu cong tac tong tu dong phu voi
so bot moi them — khong can sua target khi them bot. Token-free:
chi doc registry.json + goi systemctl --user (khong qua shell).
"""
import json
import subprocess
from pathlib import Path

ROOT = Path("/home/cuongkent/multibot")

def main():
    reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    bots = reg.get("bots") or {}
    ok = 0
    for bot_id, entry in sorted(bots.items()):
        if not (entry or {}).get("enabled"):
            continue
        unit = "tai@%s.service" % bot_id
        r = subprocess.run(["systemctl", "--user", "start", unit],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print("start %s: ok (no-op neu da active)" % unit)
            ok += 1
        else:
            print("LOI %s: %s" % (unit, r.stderr.strip()[:120]))
    print("tong: %d/%d tai da duoc lenh start" % (ok, len(bots)))

if __name__ == "__main__":
    main()
