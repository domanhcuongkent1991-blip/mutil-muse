#!/usr/bin/env python3
"""Entry point của 1 tai trên PC: 1 tiến trình / 1 bot.

Đọc cấu hình -> kiểm tra singleton -> vòng lặp poller -> ghi state.
Xử lý SIGTERM/SIGINT để dừng êm. KHÔNG có HTTP server trong file này
(gateway là tiến trình riêng). KHÔNG log nội dung tin nhắn.

Nền tảng lấy từ ``config/<bot_id>.json`` (``platform`` = zalo hoặc
telegram); mỗi nền tảng dùng poller riêng, chung singleton/state/
hàng đợi. Cách chạy (trên PC, sau khi đóng gói)::

    python3 ear_main.py --bot-id bot-a

Cấu hình lấy từ file ``config/<bot_id>.json`` + biến môi trường (xem
``pc/config.py``); ``api_base_url`` bắt buộc có trong file cấu hình
hoặc ``MULTIBOT_API_BASE_URL`` (test dùng máy chủ giả localhost).

Mã thoát: 0 = dừng êm hoặc đã có tai khác chạy (không restart lặp);
2 = lỗi cấu hình/token, hoặc webhook Telegram đang bật (người vận
hành tắt tay rồi khởi động lại); 42 = xung đột poller/webhook
Telegram (systemd không restart lặp — RestartPreventExitStatus=42).
"""

import argparse
import os
import signal
import sys
import threading
import time

# Cho phép chạy trực tiếp dạng script: thêm thư mục gốc chứa shared/
# và pc/ vào sys.path.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from pc.config import ensure_dirs, load_config  # noqa: E402
from pc.ear.dedupe import SeenStore  # noqa: E402
from pc.ear.poller import (  # noqa: E402
    check_token_file_permissions,
    load_token,
    make_get_me_fn,
    make_poll_fn,
    Poller,
)
from pc.ear.queue_store import QueueStore  # noqa: E402
from pc.ear.singleton import SingletonError, SingletonLock, token_sha256_8  # noqa: E402
from pc.ear.state import StateWriter  # noqa: E402
from pc.ear.telegram_poller import (  # noqa: E402
    TelegramPoller,
    make_download_fn as make_telegram_download_fn,
    make_get_file_fn as make_telegram_get_file_fn,
    make_get_me_fn as make_telegram_get_me_fn,
    make_get_webhook_info_fn as make_telegram_get_webhook_info_fn,
    make_poll_fn as make_telegram_poll_fn,
)


def log(message):
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tai đa-bot: 1 tiến trình/1 bot")
    parser.add_argument("--bot-id", default=None, help="id bot (hoặc env)")
    parser.add_argument("--base-dir", default=None,
                        help="thư mục gốc multibot (mặc định ~/multibot/)")
    parser.add_argument("--api-base-url", default=None,
                        help="scheme + host API (ghi đè cấu hình/env)")
    args = parser.parse_args(argv)

    try:
        config = load_config(bot_id=args.bot_id, base_dir=args.base_dir)
    except (ValueError, OSError) as exc:
        print("lỗi cấu hình: %s" % (exc,), file=sys.stderr, flush=True)
        return 2
    api_base_url = args.api_base_url or config.api_base_url
    if not api_base_url:
        print("lỗi cấu hình: thiếu api_base_url (ghi trong file cấu hình "
              "hoặc đặt MULTIBOT_API_BASE_URL)", file=sys.stderr, flush=True)
        return 2
    ensure_dirs(config)

    check_token_file_permissions(config.token_path, log)
    try:
        token = load_token(config.token_path)
    except (OSError, ValueError) as exc:
        print("lỗi token: %s" % (type(exc).__name__,), file=sys.stderr, flush=True)
        print("đặt token vào file %s (quyền 600, 1 dòng)"
              % (config.token_path,), file=sys.stderr, flush=True)
        return 2
    token_hash = token_sha256_8(token)
    del token

    try:
        singleton = SingletonLock(config.locks_dir, config.bot_id, token_hash)
        singleton.acquire()
    except SingletonError as exc:
        log(str(exc))
        return 0

    stop_event = threading.Event()

    def _stop(signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    exit_code = 0
    try:
        queue_store = QueueStore(
            config.data_dir,
            watermark_path=config.gateway_fetch_state_path,
            bot_id=config.bot_id,
            log=log,
        )
        state_writer = StateWriter(config.state_path, config.bot_id)
        if config.platform == "telegram":
            seen_store = SeenStore(config.seen_updates_path)
            poller = TelegramPoller(
                bot_id=config.bot_id,
                token_path=config.token_path,
                queue_store=queue_store,
                seen_store=seen_store,
                state_writer=state_writer,
                data_dir=config.data_dir,
                files_dir=config.files_dir,
                poll_fn=make_telegram_poll_fn(api_base_url),
                get_me_fn=make_telegram_get_me_fn(api_base_url),
                get_webhook_info_fn=make_telegram_get_webhook_info_fn(
                    api_base_url),
                get_file_fn=make_telegram_get_file_fn(api_base_url),
                download_fn=make_telegram_download_fn(api_base_url),
                poll_timeout=config.poll_timeout,
                allowed_updates=config.allowed_updates,
                log=log,
                capability_path=config.capability_path,
                offset_path=config.offset_path,
            )
        else:
            seen_store = SeenStore(config.seen_path)
            poller = Poller(
                bot_id=config.bot_id,
                token_path=config.token_path,
                queue_store=queue_store,
                seen_store=seen_store,
                state_writer=state_writer,
                data_dir=config.data_dir,
                poll_fn=make_poll_fn(api_base_url),
                get_me_fn=make_get_me_fn(api_base_url),
                poll_timeout=config.poll_timeout,
                log=log,
                capability_path=config.capability_path,
                dm_only_path=config.dm_only_path,
            )
        log("ear started bot_id=%s platform=%s latest_seq=%d floor_seq=%d"
            % (config.bot_id, config.platform, queue_store.latest_seq,
               queue_store.floor_seq))
        # Poller Zalo tra None (luon 0); poller Telegram tra ma thoat.
        exit_code = poller.run(stop_event) or 0
    finally:
        singleton.release()
    log("ear stopped bot_id=%s exit_code=%d" % (config.bot_id, exit_code))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
