"""Do kich thuoc brief (Dot 5, 28/11): ghi so ky tu moi luot de
co so lieu truoc khi cat brief."""
import json
import time
from pathlib import Path


def record(root, kind, text, now=None):
    path = Path(root) / "center_brief_sizes.jsonl"
    row = {"at": now or time.time(), "kind": kind, "chars": len(text or "")}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row
