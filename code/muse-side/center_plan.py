"""So ke hoach viec me-con ben (harness mong buoc 1, 28/11/2026).

Truong nhom giao viec nhieu buoc thi ghi ke hoach vao file JSON
ben duoi ``center_plans/<parent_code>.json``: viec con nao da
giao cho ai, bang chung kiem chung nao da co, trang thai xac
nhan. May ao bi thay thi phien sau doc so nay tiep tuc dung cho,
khong giao trung viec con (leader da chong trung bang
client_key; so nay la lop nho ben tren).

Ghi file theo ky luat chong dua: khoa flock quanh chu trinh
doc-sua-ghi, ghi tmp cung thu muc + fsync + os.replace.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

PARENT_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EVIDENCE_KEYS = ("sha256", "tests", "hub_status", "file_id", "note")


def _plan_path(root, parent_code):
    if not isinstance(parent_code, str) \
            or not PARENT_CODE_RE.match(parent_code):
        raise ValueError("parent_code phai khop mau [a-z0-9-]")
    return Path(root) / "center_plans" / ("%s.json" % parent_code)


def _now_iso(now=None):
    epoch = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                         time.gmtime(epoch + 7 * 3600))


def _read_plan(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_plan_locked(path, plan):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(plan, ensure_ascii=False, indent=1)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _mutate(root, parent_code, fn):
    path = _plan_path(root, parent_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        plan = _read_plan(path) or {
            "parent_code": parent_code,
            "created_at": _now_iso(),
            "children": [],
        }
        result = fn(plan)
        _write_plan_locked(path, plan)
        return result
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_plan(root, parent_code):
    """Doc so ke hoach; chua co -> None."""
    return _read_plan(_plan_path(root, parent_code))


def record_child(root, parent_code, task_id, assignee, now=None):
    """Ghi 1 viec con vua giao. Giao lai cung task_id thi cap nhat
    moc thoi gian, khong sinh dong trung."""
    def _fn(plan):
        for child in plan["children"]:
            if child.get("task_id") == task_id:
                child["assignee"] = assignee
                child["updated_at"] = _now_iso(now)
                return child
        child = {"task_id": task_id, "assignee": assignee,
                 "status": "created", "evidence": {},
                 "created_at": _now_iso(now),
                 "updated_at": _now_iso(now)}
        plan["children"].append(child)
        return child

    return _mutate(root, parent_code, _fn)


def record_evidence(root, parent_code, task_id, evidence,
                    status=None, now=None):
    """Ghi bang chung kiem chung cho 1 viec con (cong kiem chung
    harness mong): evidence phai co it nhat 1 khoa co noi dung
    trong EVIDENCE_KEYS; status tuy chon (vd "confirmed")."""
    if not isinstance(evidence, dict) or not any(
            evidence.get(k) for k in EVIDENCE_KEYS):
        raise ValueError(
            "evidence phai co it nhat 1 khoa: %s"
            % ", ".join(EVIDENCE_KEYS))

    def _fn(plan):
        for child in plan["children"]:
            if child.get("task_id") == task_id:
                child.setdefault("evidence", {}).update(evidence)
                if status:
                    child["status"] = status
                child["updated_at"] = _now_iso(now)
                return child
        raise ValueError("khong co viec con %s trong ke hoach"
                         % task_id)

    return _mutate(root, parent_code, _fn)


def plan_summary(root, parent_code):
    """Tong ket ke hoach: tong viec con, da xac nhan, da co bang
    chung kiem chung."""
    plan = load_plan(root, parent_code)
    if plan is None:
        return {"parent_code": parent_code, "total": 0,
                "confirmed": 0, "verified": 0}
    children = plan.get("children") or []
    return {
        "parent_code": parent_code,
        "total": len(children),
        "confirmed": sum(1 for c in children
                         if c.get("status") == "confirmed"),
        "verified": sum(1 for c in children
                        if c.get("evidence")),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="So ke hoach viec me-con (harness mong).")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="in ke hoach + tong ket")
    show.add_argument("--root", required=True)
    show.add_argument("--parent-code", required=True)
    ev = sub.add_parser("record-evidence",
                        help="ghi bang chung cho 1 viec con")
    ev.add_argument("--root", required=True)
    ev.add_argument("--parent-code", required=True)
    ev.add_argument("--task-id", required=True)
    ev.add_argument("--evidence-json", required=True)
    ev.add_argument("--status", default=None)
    args = parser.parse_args(argv)
    if args.command == "show":
        plan = load_plan(args.root, args.parent_code)
        print(json.dumps({"plan": plan,
                          "summary": plan_summary(
                              args.root, args.parent_code)},
                         ensure_ascii=False))
        return 0
    if args.command == "record-evidence":
        child = record_evidence(
            args.root, args.parent_code, args.task_id,
            json.loads(args.evidence_json), status=args.status)
        print(json.dumps(child, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
