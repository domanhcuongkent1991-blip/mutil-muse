"""Sao luu kho trung tam (Dot 3 HUB-05, 28/11).

Sao chep data/center vao ~/multibot/backups/center-YYYYmmdd-HHMMSS,
giu 7 ban gan nhat, ghi SHA256SUMS de kiem toan ven. Chay bang
systemd timer center-backup (hang ngay).
"""
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

KEEP = 7


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(root, backup_dir=None, now=None):
    root = Path(root)
    src = root / "data" / "center"
    if not src.is_dir():
        return {"ok": False, "error": "no_center_data"}
    now = now or datetime.now(timezone.utc)
    dest_root = Path(backup_dir) if backup_dir else (
        Path.home() / "multibot" / "backups")
    dest = dest_root / ("center-" + now.strftime("%Y%m%d-%H%M%S"))
    shutil.copytree(src, dest)
    sums = []
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            sums.append("%s  %s" % (_sha256(path),
                                    path.relative_to(dest)))
    (dest / "SHA256SUMS").write_text("\n".join(sums) + "\n",
                                     encoding="utf-8")
    old = sorted(dest_root.glob("center-*"))
    for stale in old[:-KEEP]:
        shutil.rmtree(stale, ignore_errors=True)
    return {"ok": True, "dest": str(dest), "files": len(sums)}


def verify(dest):
    dest = Path(dest)
    bad = []
    for line in (dest / "SHA256SUMS").read_text(
            encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        if _sha256(dest / rel) != digest:
            bad.append(rel)
    return {"ok": not bad, "bad": bad}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    root = argv[0] if argv else "."
    result = backup(root)
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
