"""Center fetcher phía Muse — mở rộng K1, SPEC-TRUNG-TAM TT7, doc 10 §6.2.

Kéo feed trung tâm mỗi 10s (cùng chu kỳ hook bot), timeout 15s/lần,
flock (chu kỳ trước chưa xong thì bỏ qua). Cursor riêng/tài khoản
(``center_cursors/<account_id>.json``), chỉ tiến SAU KHI ghi xong đợt.

- Ghi việc vào ``center_tasks/<task_id>.json`` (tmp+rename; ghi đè
  bằng bản ghi mới nhất — trạng thái việc thay đổi theo feed).
- Ghi tin vào ``center_messages.jsonl`` (append-only; chống trùng
  theo ``seq`` khi kéo lại cùng đợt).
- 401/403: chỉ dừng kéo trung tâm của tài khoản đó (CẤM thoát toàn
  bộ, CẤM tiến cursor, CẤM retry dồn); 401 liền 3 chu kỳ -> báo.
  Riêng 403 ``{"error": "not_participating"}`` (tài khoản tạm ngưng
  có chủ đích, cờ tắt — SPEC TT9 CHẶN-1): chỉ dừng kéo, KHÔNG báo
  động, KHÔNG tính vào bộ đếm chu kỳ lỗi 401.
- Cursor quá mới (``stale_center_cursor``): ghi log + báo, KHÔNG tự
  sửa cursor.
- Mốc "đã thấy" của việc do module store của hub ghi khi trả feed
  (doc 10 §6.2, C-2). Fetcher phía Muse CẤM tự ghi mốc đó dưới mọi
  hình thức — module này không có đường ghi nào tới kho hub ngoài
  việc đọc feed.
- Cấu hình từng tài khoản: ``center-config/<account_id>.json`` (600):
  endpoint gateway + endpoint receiver + fetch secret + push secret
  + cờ ``via_proxy``. CẤM dùng lại secret bot (TS1).
- Nhãn tài khoản tự khai (TS5/MA9): gửi header ``X-Muse-Account``
  trên mọi request, fail-open như fetcher bot; nhãn không quyết
  định quyền.
- CẤM in secret, CẤM log nội dung việc/tin. Log mỗi chu kỳ: số sự
  kiện/tài khoản, số việc/tin mới, cursor trước->sau.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import muse_common  # noqa: F401  (đưa repo root lên sys.path)
import fetcher
from muse_common import (
    atomic_write_json,
    append_jsonl,
    ensure_dir,
    now_iso,
    read_json,
    read_jsonl,
)
from shared import ids

FETCH_TIMEOUT_SECONDS = 15  # TT7: timeout 15s/lần
FEED_LIMIT = 100  # TT7: tối đa 100 sự kiện/lần
AUTH_FAILURE_ALERT_AFTER = 3  # SUY LUẬN, theo mẫu fetcher bot


def _default_log(message):
    print(message, flush=True)


def _default_alert(kind, account_id, detail):
    print("ALERT kind=%s account=%s detail=%s"
          % (kind, account_id, detail), flush=True)


def _valid_task_id(value):
    """task_id do hub sinh (``t-`` + 12 hex); sai mẫu thì None."""
    if isinstance(value, str) and value.startswith("t-") \
            and len(value) == 14:
        hexpart = value[2:]
        if all(ch in "0123456789abcdef" for ch in hexpart):
            return value
    return None


def discover_center_accounts(root, config_dir=None):
    """Danh sách account_id có file cấu hình (chỉ đọc tên file,
    không đọc secret)."""
    config_dir = Path(config_dir) if config_dir else Path(root) / "center-config"
    accounts = []
    if not config_dir.is_dir():
        return accounts
    for path in sorted(config_dir.glob("*.json")):
        try:
            accounts.append(ids.validate_bot_id(path.stem))
        except ValueError:
            continue
    return accounts


def load_center_configs(root, config_dir=None, log_fn=None):
    """Đọc mọi file cấu hình trung tâm. Trả về {account_id: config}.

    File sai tên (account_id sai mẫu), thiếu endpoint/fetch secret,
    hoặc trường ``account_id`` trong file lệch tên file thì bỏ qua +
    ghi log (không crash cả chu kỳ). Secret KHÔNG bao giờ được ghi
    log. Trường ``account_id`` trong file là tự nguyện (như fetcher
    bot, MA9): thiếu thì lấy tên file; sai định dạng thì bỏ file +
    cảnh báo CHỈ ghi lớp lỗi, CẤM ghi giá trị thô.
    """
    log_fn = log_fn or _default_log
    config_dir = Path(config_dir) if config_dir else Path(root) / "center-config"
    configs = {}
    if not config_dir.is_dir():
        return configs
    for path in sorted(config_dir.glob("*.json")):
        account_id = path.stem
        try:
            ids.validate_bot_id(account_id)
        except ValueError:
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        endpoint = data.get("endpoint")
        fetch_secret = data.get("fetch_secret")
        if not endpoint or not fetch_secret:
            continue
        raw_label = data.get("account_id")
        if raw_label is not None and raw_label != account_id:
            if fetcher._valid_account_label(raw_label) is None:
                log_fn(
                    "center_fetcher account=%s: account_id trong file "
                    "sai dinh dang, bo qua file (fail-open)" % account_id
                )
                continue
            # Nhãn trong file khác tên file nhưng hợp lệ: tên file
            # thắng (1 file = 1 tài khoản), không đoán.
            log_fn(
                "center_fetcher account=%s: account_id trong file lech "
                "ten file, dung ten file" % account_id
            )
        configs[account_id] = {
            "endpoint": str(endpoint).rstrip("/"),
            "receiver_endpoint": (
                str(data.get("receiver_endpoint")).rstrip("/")
                if isinstance(data.get("receiver_endpoint"), str)
                and data.get("receiver_endpoint") else None
            ),
            "fetch_secret": fetch_secret,
            "push_secret": data.get("push_secret"),
            "via_proxy": bool(data.get("via_proxy", False)),
            "account_id": account_id,
        }
    return configs


def _cursor_path(root, account_id):
    return Path(root) / "center_cursors" / ("%s.json" % account_id)


def load_cursor(path):
    data = read_json(path)
    if isinstance(data, dict):
        seq = data.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            return seq
    return 0


def save_cursor(path, seq):
    atomic_write_json(path, {"seq": seq})


def _fetcher_state_path(root, account_id):
    return Path(root) / "center_fetcher_state" / ("%s.json" % account_id)


def _load_auth_state(root, account_id):
    data = read_json(_fetcher_state_path(root, account_id))
    if isinstance(data, dict):
        return data
    return {"consec_auth_failures": 0, "alerted": False}


def _save_auth_state(root, account_id, state):
    atomic_write_json(_fetcher_state_path(root, account_id), state)


def _note_auth_failure(root, account_id, log_fn, alert_fn):
    """Ghi nhận 1 chu kỳ 401 hoặc 403 (trừ 403 not_participating —
    trường hợp đó gọi riêng, không qua đây). Đạt 3 chu kỳ liên tiếp
    -> báo 1 lần (tới khi có lần thành công reset lại). Chỉ dừng kéo
    trung tâm của tài khoản này; fetcher bot không bị chạm.

    (Fix gộp 21/11, §4.2 mục 8 — Q-FIX-10) Khi chạm ngưỡng: ghi
    alert qua đường cảnh báo chung (``center_outbox.record_alert``,
    file ``center/auth_alerts.jsonl`` là kênh CHÍNH + DM
    best-effort có TTL; file dedup riêng
    ``center/auth_alerts_reported.json``). Tái dùng bộ đếm + cờ
    ``alerted`` sẵn có, không đếm mới."""
    state = _load_auth_state(root, account_id)
    count = int(state.get("consec_auth_failures") or 0) + 1
    state["consec_auth_failures"] = count
    if count >= AUTH_FAILURE_ALERT_AFTER and not state.get("alerted"):
        state["alerted"] = True
        alert_fn(
            "center_fetcher_auth_failure",
            account_id,
            {"consec_cycles": count, "hint": "secret trung tam lech"},
        )
        try:
            import center_outbox  # noqa: PLC0415 - tránh import vòng
            center_outbox.record_alert(
                root, account_id, "auth",
                "auth|%s" % account_id,
                "Cảnh báo lỗi mã trung tâm: tài khoản %s kéo feed lỗi "
                "401/403 %d chu kỳ liên tiếp (mã truy cập có thể lệch). "
                "Xem file cảnh báo trên tab Cảnh báo; tin này chỉ gửi "
                "được khi đường đẩy còn sống."
                % (account_id, count),
                detail={"consec_cycles": count,
                        "hint": "secret trung tam lech"})
        except Exception:  # noqa: BLE001 - file alert là kênh chính,
            # DM best-effort; lỗi enqueue không được làm hỏng chu kỳ
            # fetch (alert_fn ở trên đã ghi log hook).
            pass
    _save_auth_state(root, account_id, state)
    log_fn("center_fetcher account=%s: 401/403 chu ky lien tiep=%d"
           % (account_id, count))


def _reset_auth_state(root, account_id):
    state = _load_auth_state(root, account_id)
    if state.get("consec_auth_failures") or state.get("alerted"):
        _save_auth_state(
            root, account_id, {"consec_auth_failures": 0, "alerted": False}
        )
        # (Fix gộp 21/11 — luật re-arm) Hết lỗi mã thì xóa entry
        # dedup auth để đợt lỗi mới sau đó lại báo.
        try:
            import center_outbox  # noqa: PLC0415 - tránh import vòng
            center_outbox.clear_alert_reported(
                root, "auth", "auth|%s" % account_id)
        except Exception:  # noqa: BLE001 - dedup chỉ là chống lặp,
            # lỗi xóa không được làm hỏng chu kỳ fetch.
            pass


def _is_not_participating(body):
    """True khi body lỗi là JSON ``{"error": "not_participating"}``
    (SPEC TT9 CHẶN-1: gateway trả 403 này khi secret đúng nhưng cờ
    ``center_participating`` tắt — tạm ngưng có chủ đích). Body
    không phải JSON / không có trường error đó -> False (giữ hành
    vi cũ: tính như lỗi xác thực thường)."""
    data = fetcher._parse_json_body(body)
    return isinstance(data, dict) and data.get("error") == "not_participating"


def _write_task_file(root, event):
    """Ghi 1 việc (bản ghi mới nhất) vào center_tasks/<task_id>.json
    tmp+rename. Trả về (task_id, True) nếu ghi được."""
    record = event.get("record")
    task_id = _valid_task_id(event.get("task_id"))
    if not isinstance(record, dict) or task_id is None:
        return None, False
    path = ensure_dir(Path(root) / "center_tasks") / ("%s.json" % task_id)
    content = {
        "seq": event.get("seq"),
        "task_id": task_id,
        "at": event.get("at"),
        "record": record,
        "fetched_at": now_iso(),
    }
    atomic_write_json(path, content)
    return task_id, True


def _append_messages(root, events, log_fn):
    """Append các sự kiện tin vào center_messages.jsonl, chống trùng
    theo seq (kéo lại cùng đợt không ghi thêm). Trả về số dòng mới."""
    message_events = [
        e for e in events
        if isinstance(e, dict) and e.get("kind") == "message"
        and isinstance(e.get("seq"), int)
    ]
    if not message_events:
        return 0
    path = Path(root) / "center_messages.jsonl"
    existing, _skipped = read_jsonl(path)
    seen_seqs = {
        e.get("seq") for e in existing
        if isinstance(e, dict) and isinstance(e.get("seq"), int)
    }
    added = 0
    for event in message_events:
        if event["seq"] in seen_seqs:
            continue
        record = event.get("record")
        if not isinstance(record, dict):
            continue
        append_jsonl(path, {
            "seq": event["seq"],
            "at": event.get("at"),
            "msg_id": event.get("msg_id"),
            "record": record,
        })
        seen_seqs.add(event["seq"])
        added += 1
    return added


def _fetch_one_account(root, account_id, config, log_fn, alert_fn):
    """1 chu kỳ kéo feed cho 1 tài khoản. Trả về dict thống kê (không
    nội dung việc/tin, không secret). Mọi lỗi đều cô lập trong tài
    khoản này — fetcher bot và tài khoản khác không bị chạm."""
    stats = {
        "account_id": account_id,
        "events": 0,
        "new_tasks": 0,
        "new_messages": 0,
        "cursor_before": 0,
        "cursor_after": 0,
        "error": None,
    }
    try:
        opener = fetcher.build_opener(
            config["endpoint"], config["via_proxy"], log_fn)
    except RuntimeError as exc:
        stats["error"] = str(exc)
        log_fn("center_fetcher account=%s: %s" % (account_id, exc))
        return stats
    cursor_file = _cursor_path(root, account_id)
    cursor = load_cursor(cursor_file)
    stats["cursor_before"] = cursor
    stats["cursor_after"] = cursor
    account_label = config.get("account_id")
    url = "%s/v1/center/%s/feed?since=%d&limit=%d" % (
        config["endpoint"], account_id, cursor, FEED_LIMIT,
    )
    status, body, error = fetcher._gateway_get(
        opener, url, config["fetch_secret"], FETCH_TIMEOUT_SECONDS,
        account_label,
    )
    if status in (401, 403):
        if status == 403 and _is_not_participating(body):
            # Tạm ngưng có chủ đích (cờ tắt), không phải lộ mã:
            # chỉ dừng kéo trung tâm của tài khoản này, KHÔNG báo
            # động, KHÔNG tăng bộ đếm chu kỳ lỗi, KHÔNG reset gì
            # khác, CẤM tiến cursor. Fetcher bot vẫn chạy.
            log_fn("center_fetcher account=%s: tam ngung "
                   "(not_participating), dung keo, khong bao dong"
                   % account_id)
            stats["error"] = "not_participating"
            return stats
        # Cô lập lỗi: chỉ tài khoản này dừng kéo trung tâm, cấm tiến
        # cursor, cấm retry dồn. Fetcher bot vẫn chạy bình thường.
        _note_auth_failure(root, account_id, log_fn, alert_fn)
        stats["error"] = "auth_%d" % status
        return stats
    if status == 0:
        log_fn("center_fetcher account=%s: transport loi (%s), thu lai "
               "chu ky sau" % (account_id, error))
        stats["error"] = "transport"
        return stats
    if status != 200:
        log_fn("center_fetcher account=%s: feed status=%s"
               % (account_id, status))
        stats["error"] = "http_%d" % status
        return stats
    data = fetcher._parse_json_body(body)
    if data is None:
        log_fn("center_fetcher account=%s: body feed khong phai JSON"
               % account_id)
        stats["error"] = "bad_json"
        return stats
    _reset_auth_state(root, account_id)
    if data.get("stale_center_cursor"):
        # Cursor vượt latest: log + báo, KHÔNG tự sửa cursor.
        log_fn(
            "center_fetcher account=%s: stale_center_cursor (since=%d, "
            "latest_seq=%s), khong tien cursor"
            % (account_id, cursor, data.get("latest_seq"))
        )
        alert_fn(
            "center_fetcher_stale_cursor",
            account_id,
            {"since": cursor, "latest_seq": data.get("latest_seq")},
        )
        stats["error"] = "stale_center_cursor"
        return stats
    events = data.get("events")
    if not isinstance(events, list):
        events = []
    stats["events"] = len(events)
    max_seq = cursor
    write_failed = False
    for event in events:
        if not isinstance(event, dict):
            continue
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        max_seq = max(max_seq, seq)
        if event.get("kind") == "task":
            try:
                _task_id, ok = _write_task_file(root, event)
            except OSError:
                ok = False
            if ok:
                stats["new_tasks"] += 1
            elif isinstance(event.get("record"), dict):
                # (Dot 1 G3, 28/11) Ghi loi thi KHONG tien cursor:
                # dot sau keo lai, khong mat tin am tham.
                write_failed = True
        elif event.get("kind") == "message":
            pass  # ghi tập trung ở bước append bên dưới (chống trùng)
        else:
            log_fn("center_fetcher account=%s: bo su kien la kind=%s"
                   % (account_id, event.get("kind")))
    try:
        stats["new_messages"] = _append_messages(root, events, log_fn)
    except OSError:
        write_failed = True
    if write_failed:
        stats["error"] = "write_failed"
        log_fn("center_fetcher account=%s: ghi loi, giu cursor %d"
               % (account_id, cursor))
    elif events:
        # Chỉ tiến cursor SAU KHI ghi xong tất cả của đợt.
        save_cursor(cursor_file, max_seq)
        stats["cursor_after"] = max_seq
    _sync_health(opener, root, account_id, config, log_fn, account_label)
    log_fn(
        "center_fetcher account=%s: %d events, %d viec, %d tin moi, "
        "cursor %d -> %d"
        % (
            account_id,
            stats["events"],
            stats["new_tasks"],
            stats["new_messages"],
            stats["cursor_before"],
            stats["cursor_after"],
        )
    )
    return stats


def _sync_health(opener, root, account_id, config, log_fn, account_label):
    """Kéo /health (chỉ số đếm, không nội dung nhạy cảm), ghi
    center_health/<account_id>.json (tmp+rename). Lỗi thì giữ bản cũ."""
    url = "%s/v1/center/%s/health" % (config["endpoint"], account_id)
    status, body, _err = fetcher._gateway_get(
        opener, url, config["fetch_secret"], FETCH_TIMEOUT_SECONDS,
        account_label,
    )
    if status == 200:
        data = fetcher._parse_json_body(body)
        if data is not None:
            atomic_write_json(
                Path(root) / "center_health" / ("%s.json" % account_id),
                dict(data, fetched_at=now_iso()),
            )
    else:
        log_fn("center_fetcher account=%s: khong keo duoc health "
               "status=%s (giu ban cu)" % (account_id, status))


@contextlib.contextmanager
def _cycle_lock(root):
    """Flock chống chạy chồng cho cả chu kỳ. Hết lượt giữ mà không
    lấy được khóa -> raise RuntimeError (caller bỏ chu kỳ)."""
    lock_path = ensure_dir(Path(root) / "locks") / "center_fetcher.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("chu ky truoc chua xong")
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def run_cycle(root, config_dir=None, log_fn=None, alert_fn=None):
    """Chạy 1 chu kỳ kéo feed trung tâm cho mọi tài khoản (tuần tự —
    mỗi tài khoản 1 request, không song song như fetcher bot vì số
    tài khoản nhỏ và mỗi lần kéo đều qua cùng hub).

    Trả về::

        {"skipped": bool, "accounts": {account_id: stats}, "errors": [...]}

    Chu kỳ trước chưa xong (flock bận) -> {"skipped": True}, không dồn.
    """
    log_fn = log_fn or _default_log
    alert_fn = alert_fn or _default_alert
    try:
        with _cycle_lock(root):
            return _run_cycle_locked(root, config_dir, log_fn, alert_fn)
    except RuntimeError:
        log_fn("center_fetcher: chu ky truoc chua xong, bo chu ky nay "
               "(khong don)")
        return {"skipped": True, "accounts": {}, "errors": []}


def _run_cycle_locked(root, config_dir, log_fn, alert_fn):
    """Phần thân chu kỳ, chạy bên trong flock."""
    configs = load_center_configs(root, config_dir, log_fn)
    if not configs:
        log_fn("center_fetcher: khong co tai khoan nao co cau hinh")
        return {"skipped": False, "accounts": {}, "errors": []}
    results = {}
    errors = []
    for account_id in sorted(configs):
        try:
            results[account_id] = _fetch_one_account(
                root, account_id, configs[account_id], log_fn, alert_fn)
        except Exception as exc:  # noqa: BLE001 - cô lập lỗi từng tài khoản
            log_fn("center_fetcher account=%s: loi khong ngo class=%s"
                   % (account_id, type(exc).__name__))
            results[account_id] = {
                "account_id": account_id, "events": 0, "new_tasks": 0,
                "new_messages": 0, "cursor_before": 0, "cursor_after": 0,
                "error": "exception",
            }
            errors.append(account_id)
    return {"skipped": False, "accounts": results, "errors": errors}


def main(argv=None):
    """CLI: python3 center_fetcher.py --root <thư mục gốc phía Muse>."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Center fetcher phía Muse (token-free).")
    parser.add_argument("--root", default=".",
                        help="thư mục gốc phía Muse (mặc định: .)")
    parser.add_argument("--config-dir", default=None,
                        help="thư mục center-config (mặc định: "
                             "<root>/center-config)")
    args = parser.parse_args(argv)
    result = run_cycle(args.root, config_dir=args.config_dir)
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
