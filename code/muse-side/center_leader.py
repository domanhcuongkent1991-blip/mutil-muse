"""Truong nhom tao viec con co gian nhip (cai tien 28/11/2026).

Thu nghiem EXP-TN-20261128-0915 cho thay tao nhieu viec con don
dap cham gioi han toc do cua hub (429, receiver gioi han
1 request/2 giay/tai khoan). Module nay tao viec con tuan tu,
cach nhau ``pace_seconds``, gap 429/5xx/loi mang thi cho lui
(backoff) roi thu lai CUNG ``client_key`` — hub chong trung theo
client_key nen thu lai khong sinh viec trung.

``client_key`` cua moi viec con suy ra tu ma viec me + tai khoan
nhan viec, nen chay lai ca dot tao viec cung khong sinh trung.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import muse_common  # noqa: F401  (dua repo root len sys.path)
import center_fetcher
import center_outbox
import center_plan
import fetcher
from shared import ids

PACE_SECONDS = 3  # gian cach giua 2 lan tao viec con
BACKOFF_START_SECONDS = 5
BACKOFF_MAX_SECONDS = 60
MAX_ATTEMPTS = 6


def child_client_key(parent_code, account_id):
    """Khoa chong trung on dinh cho 1 viec con cua 1 viec me."""
    ids.validate_bot_id(account_id)
    digest = hashlib.sha256(
        ("%s|%s" % (parent_code, account_id)).encode("utf-8")
    ).hexdigest()
    return "leader-%s" % digest[:24]


def _default_post(config, payload, timeout=30):
    opener = fetcher.build_opener(
        config["receiver_endpoint"], config.get("via_proxy", False),
        lambda _message: None)
    url = "%s/v1/center/%s/tasks" % (
        config["receiver_endpoint"], config["account_id"])
    return center_outbox._http_post(
        opener, url, config["push_secret"], payload, timeout,
        account_label=config.get("account_label"))


def create_child_task(config, parent_code, title, body, assignee,
                      due_at=None, kind="khac", post_fn=None,
                      sleep_fn=None, pace_seconds=PACE_SECONDS):
    """Tao 1 viec con giao cho dung 1 tai khoan. Tra ve dict
    {task_id, duplicate, attempts} khi hub tra 200; raise
    RuntimeError khi het luot thu hoac hub tra loi vinh vien."""
    post_fn = post_fn or _default_post
    sleep_fn = sleep_fn or time.sleep
    payload = {
        "title": title,
        "body": body,
        "kind": kind,
        "assigned_to": [assignee],
        "due_at": due_at,
        "client_key": child_client_key(parent_code, assignee),
    }
    backoff = BACKOFF_START_SECONDS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        status, response, _error = post_fn(config, payload)
        if status == 200 and isinstance(response, dict) \
                and response.get("task_id"):
            return {
                "task_id": response["task_id"],
                "duplicate": bool(response.get("duplicate")),
                "attempts": attempt,
            }
        if status == 429 or status >= 500 or status == 0:
            if attempt < MAX_ATTEMPTS:
                sleep_fn(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
            continue
        raise RuntimeError(
            "hub tra %s khi tao viec con cho %s" % (status, assignee))
    raise RuntimeError(
        "het luot thu tao viec con cho %s (hub van 429/5xx)"
        % assignee)


def create_child_tasks(config, parent_code, children, post_fn=None,
                       sleep_fn=None, pace_seconds=PACE_SECONDS):
    """Tao tuan tu cac viec con, gian nhip giua cac lan tao.

    ``children``: list dict {title, body, assignee, due_at?, kind?}.
    Tra ve list ket qua theo dung thu tu dau vao; viec con loi vinh
    vien thi ghi {"assignee": ..., "error": ...} va di tiep viec
    sau (truong nhom thay ro phan nao hong)."""
    sleep_fn = sleep_fn or time.sleep
    results = []
    for index, child in enumerate(children):
        if index > 0 and pace_seconds:
            sleep_fn(pace_seconds)
        try:
            result = create_child_task(
                config, parent_code, child["title"], child["body"],
                child["assignee"], due_at=child.get("due_at"),
                kind=child.get("kind", "khac"), post_fn=post_fn,
                sleep_fn=sleep_fn, pace_seconds=pace_seconds)
            result["assignee"] = child["assignee"]
        except RuntimeError as exc:
            result = {"assignee": child["assignee"],
                      "error": str(exc)}
        results.append(result)
    return results


def run_cli(argv=None, post_fn=None, config_loader=None):
    """Duong chay that cho truong nhom tao viec con (goi giam tac
    28/11): truong nhom goi CLI nay thay vi enqueue giao-viec tay,
    de gian nhip + backoff + client_key chong trung co hieu luc.
    In JSON ket qua (task_id, duplicate, attempts); tra 0 khi hub
    tra 200, 1 khi loi vinh vien/het luot thu."""
    parser = argparse.ArgumentParser(
        description="Truong nhom tao viec con co gian nhip.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--parent-code", required=True)
    parser.add_argument("--assignee", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--due-at", default=None)
    parser.add_argument("--kind", default="khac")
    args = parser.parse_args(argv)
    loader = config_loader or center_fetcher.load_center_configs
    configs = loader(args.root)
    config = configs.get(args.account_id)
    if config is None:
        print(json.dumps({"error": "no_config"}))
        return 1
    body = Path(args.body_file).read_text(encoding="utf-8")
    due_at = args.due_at
    if not due_at:
        # Hub bat buoc due_at ISO khong rong; mac dinh han 2 gio.
        due_at = (datetime.now(timezone(timedelta(hours=7)))
                  + timedelta(hours=2)).isoformat(timespec="seconds")
    try:
        result = create_child_task(
            config, args.parent_code, args.title, body,
            args.assignee, due_at=due_at, kind=args.kind,
            post_fn=post_fn)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    # Harness mong buoc 1: ghi so ke hoach ben sau khi giao viec
    # con thanh cong. Ghi so loi khong lam hong viec da giao —
    # bao ro plan_recorded=false de truong nhom ghi lai.
    try:
        center_plan.record_child(args.root, args.parent_code,
                                 result["task_id"], args.assignee)
        result["plan_recorded"] = True
    except Exception as exc:  # noqa: BLE001 - ghi so la phu tro
        result["plan_recorded"] = False
        result["plan_error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
