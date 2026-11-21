#!/usr/bin/env python3
"""Lenh boc don dep theo lich (v0.5.0, T3f — SPEC TT19, DESIGN §3.8
luong 1).

Hen gio he thong (``center-prune.timer``) chay file nay MOI NGAY
21:00 gio dia phuong cua PC (gio Asia/Saigon — agent PC kiem tra
``timedatectl`` khi cai timer). Hen gio chay moi ngay la co chu dich
— lich he thong KHONG doc duoc ngay don trong ``center.json`` — nen
lenh boc moi la noi quyet dinh:

- hom nay la ngay da chon (``prune_day``) -> don, ghi
  ``last_prune_trigger = "schedule"``;
- hom nay KHONG phai ngay da chon nhung da qua ngay don gan nhat ma
  ``last_prune_at`` con truoc ngay do (PC tat dung gio don — Q6) ->
  don BU NGAY, ghi cung nhan "schedule" (lan don bu van la lan don
  theo lich, chi chay muon);
- con lai -> thoat 0, khong lam gi.

Doi ``prune_day`` co the gay 1 lan don bu thua (vd: don Chu nhat
xong, thu Bay doi ngay thanh thu Sau -> thu Bay 21:00 thay ngay don
gan nhat (thu Sau) da qua ma ``last_prune_at`` con truoc do nen don
bu) — vo hai (don theo ngay, lan thua khong xoa them gi moi — GC-01),
dung tuong la loi khi test.

Chay: python3 -m pc.center.prune_runner --root ~/multibot
Chi in JSON trang thai (co don hay khong + ly do + thong ke neu co).
Khong secret, khong noi dung tin/viec. Thoat 0 ca khi bo qua.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from pc.center.center_store import (
    CenterStore,
    load_center_config,
    load_center_cursors,
    parse_iso_timestamp,
    read_prune_state,
)

# Thu 2 = 0 ... Chu nhat = 6 (datetime.weekday()).
PRUNE_DAY_WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def most_recent_prune_day(today, prune_day):
    """Ngay don gan nhat TRUOC hom nay (date). Hom nay la ngay don
    thi khong dung ham nay (nhanh (a) xu ly truoc)."""
    target = PRUNE_DAY_WEEKDAYS[prune_day]
    days_back = (today.weekday() - target) % 7
    if days_back == 0:
        days_back = 7  # hom nay la ngay don -> ngay gan nhat truoc do
    return today - timedelta(days=days_back)


def decide_prune(config, prune_state, now):
    """Quyet co don hay khong. Tra ve (co_don, ly_do).

    ``config``: dict load_center_config; ``prune_state``: dict
    read_prune_state hoac None; ``now``: datetime (gio dia phuong
    cua PC — timer chay 21:00 gio dia phuong)."""
    today = now.date()
    prune_day = config.get("prune_day", "sun")
    if today.weekday() == PRUNE_DAY_WEEKDAYS[prune_day]:
        return True, "today_is_prune_day"
    recent = most_recent_prune_day(today, prune_day)
    last = None
    if isinstance(prune_state, dict):
        last = parse_iso_timestamp(prune_state.get("last_prune_at"))
    if last is None or last.date() < recent:
        return True, "missed_prune_day"
    return False, "not_prune_day"


def run(root, now=None):
    """Chay 1 luot lenh boc: quyet dinh + don neu can. Tra ve dict
    trang thai (in JSON ra stdout o main)."""
    root = Path(root)
    if now is None:
        now = datetime.now().astimezone()
    config = load_center_config(root)
    state = read_prune_state(root)
    should_run, reason = decide_prune(config, state, now)
    result = {"ran": should_run, "reason": reason,
              "prune_day": config.get("prune_day"),
              "today": now.date().isoformat()}
    if not should_run:
        return result
    store = CenterStore(root)
    # GC-F02: truyen cursor (watermark = min cursor cac tai khoan
    # dang tham gia); thieu cursor -> None -> chi cat theo tuoi.
    stats = store.prune_all(trigger="schedule", now=now,
                            cursors=load_center_cursors(root))
    result["result"] = stats
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Lenh boc don dep theo lich (T3f).")
    parser.add_argument("--root", default=None,
                        help="thu muc goc multibot (mac dinh ~/multibot/)")
    parser.add_argument("--now", default=None,
                        help="ISO-8601 de kiem thu (mac dinh: gio hien tai)")
    args = parser.parse_args(argv)
    root = Path(str(args.root or Path.home() / "multibot")).expanduser()
    now = None
    if args.now:
        now = parse_iso_timestamp(args.now)
        if now is None:
            parser.error("--now khong phai ISO-8601 hop le")
    print(json.dumps(run(root, now=now), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
