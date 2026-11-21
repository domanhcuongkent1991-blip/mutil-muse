"""Nạp cấu hình phía PC từ file JSON + biến môi trường.

Bố cục theo DESIGN.md §5, mọi đường dẫn suy ra từ MỘT ``base_dir``
(mặc định ``~/multibot/``). Module này KHÔNG chứa đường dẫn tuyệt đối
riêng của bất kỳ máy nào và KHÔNG ĐỌC secret — nó chỉ trả về ĐƯỜNG
DẪN file secret; code cần secret phải đọc file 600 tại lúc cần.

Thứ tự ưu tiên: tham số truyền vào > biến môi trường > file JSON >
mặc định.

Biến môi trường:

- ``MULTIBOT_BASE_DIR``: thư mục gốc (mặc định ``~/multibot/``).
- ``MULTIBOT_BOT_ID``: id bot.
- ``MULTIBOT_POLL_TIMEOUT``: timeout long-poll giây (mặc định 50).
- ``MULTIBOT_GATEWAY_PORT``: cổng gateway (mặc định 8765).
- ``MULTIBOT_API_BASE_URL``: scheme + host API của nền tảng (không có
  mặc định — test dùng máy chủ giả localhost, chạy thật do cấu hình
  cung cấp).

File JSON ``config/<bot_id>.json`` (không secret) có thể ghi:
``bot_id``, ``platform``, ``display_name``, ``poll_timeout``,
``gateway_port``, ``api_base_url``. Riêng Telegram (``03`` §1/§4):

- ``group_privacy``: ``"on"`` (mặc định, privacy BẬT — D4) hoặc
  ``"off"``; trường có cổng duyệt, ghi rõ từng bot nhóm.
- ``allowed_updates``: danh sách loại update muốn nhận (mặc định
  ``["message"]``); CẤM ``"callback_query"`` (D6) và
  ``"edited_message"`` (cấm giai đoạn đầu) — cấu hình vi phạm thì
  ``load_config`` raise.
- ``parse_mode``: ``null`` (mặc định, không dùng) hoặc ``"Markdown"``,
  ``"MarkdownV2"``, ``"HTML"``; quyết riêng từng bot. Đầu nối gửi
  phía Muse đọc trường này qua cấu hình đã đồng bộ.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from shared.ids import validate_bot_id

DEFAULT_POLL_TIMEOUT = 50
DEFAULT_GATEWAY_PORT = 8765

# Loại update CẤM ở giai đoạn đầu (03 §1): nút bấm (D6) và tin đã sửa.
FORBIDDEN_UPDATE_TYPES = ("callback_query", "edited_message")

# parse_mode Telegram chấp nhận (03 §4).
PARSE_MODES = ("Markdown", "MarkdownV2", "HTML")


def default_base_dir():
    """Thư mục gốc mặc định theo DESIGN §5: ``~/multibot/``."""
    return Path.home() / "multibot"


@dataclass(frozen=True)
class BotConfig:
    """Cấu hình đã giải quyết xong cho 1 bot trên PC."""

    bot_id: str
    platform: str
    display_name: str
    base_dir: Path
    config_dir: Path
    registry_path: Path
    secrets_dir: Path
    token_path: Path
    queue_secret_path: Path
    sync_secret_path: Path
    data_dir: Path
    queue_path: Path
    seen_path: Path
    state_path: Path
    capability_path: Path
    dm_only_path: Path
    locks_dir: Path
    lock_path: Path
    owner_path: Path
    gateway_fetch_state_path: Path
    gateway_port: int
    poll_timeout: int
    api_base_url: str | None
    # Truong rieng Telegram (co mac dinh — code cu dung BotConfig ma
    # khong truyen cac truong nay van chay binh thuong).
    group_privacy: str = "on"
    allowed_updates: tuple = ("message",)
    parse_mode: str | None = None

    @property
    def http_timeout(self):
        """Timeout HTTP = poll timeout + 15s đệm (giá trị baseline)."""
        return self.poll_timeout + 15

    @property
    def files_dir(self):
        """Thư mục file đính kèm Telegram: ``data/<bot_id>/files/``."""
        return self.data_dir / "files"

    @property
    def offset_path(self):
        """File offset bền của tai Telegram: ``data/<bot_id>/offset.txt``."""
        return self.data_dir / "offset.txt"

    @property
    def seen_updates_path(self):
        """File update_id đã thấy: ``data/<bot_id>/seen_updates.txt``."""
        return self.data_dir / "seen_updates.txt"


def _parse_int(value, name, minimum, maximum):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("%s phải là số nguyên: %r" % (name, value))
    if not minimum <= number <= maximum:
        raise ValueError(
            "%s phải trong khoảng %d..%d: %r" % (name, minimum, maximum, value)
        )
    return number


def _read_json_file(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("file cấu hình phải là object JSON: %s" % (path,))
    return data


def load_config(bot_id=None, base_dir=None, config_file=None, environ=None):
    """Nạp cấu hình cho 1 bot.

    :param bot_id: id bot (ưu tiên cao nhất; vắng thì lấy từ biến môi
        trường rồi file cấu hình).
    :param base_dir: thư mục gốc (ưu tiên cao nhất; vắng thì lấy từ
        ``MULTIBOT_BASE_DIR`` rồi ``~/multibot/``).
    :param config_file: đường dẫn file JSON cấu hình của bot; vắng thì
        dùng ``config/<bot_id>.json`` dưới base_dir (file vắng thì
        dùng mặc định, không lỗi).
    :param environ: mapping biến môi trường (mặc định ``os.environ``;
        tham số này chỉ để test).
    """
    env = os.environ if environ is None else environ
    if base_dir is None:
        raw_base = env.get("MULTIBOT_BASE_DIR", "").strip()
        base = Path(raw_base).expanduser() if raw_base else default_base_dir()
    else:
        base = Path(base_dir).expanduser()
    resolved_bot_id = bot_id or env.get("MULTIBOT_BOT_ID", "").strip() or None

    file_data = {}
    file_path = None
    if config_file is not None:
        file_path = Path(config_file).expanduser()
        if file_path.exists():
            file_data = _read_json_file(file_path)
    elif resolved_bot_id is not None:
        candidate = base / "config" / ("%s.json" % resolved_bot_id)
        if candidate.exists():
            file_path = candidate
            file_data = _read_json_file(candidate)
    if resolved_bot_id is None:
        resolved_bot_id = str(file_data.get("bot_id") or "").strip() or None
    if resolved_bot_id is None:
        raise ValueError(
            "thiếu bot_id: truyền --bot-id, đặt MULTIBOT_BOT_ID, hoặc "
            "ghi bot_id trong file cấu hình"
        )
    validate_bot_id(resolved_bot_id)

    platform = str(file_data.get("platform") or "zalo").strip()
    if platform not in ("zalo", "telegram"):
        raise ValueError("platform phải là zalo hoặc telegram: %r" % (platform,))
    display_name = str(file_data.get("display_name") or resolved_bot_id)

    raw_poll = env.get("MULTIBOT_POLL_TIMEOUT", "").strip()
    if raw_poll:
        poll_timeout = _parse_int(raw_poll, "MULTIBOT_POLL_TIMEOUT", 1, 300)
    elif "poll_timeout" in file_data:
        poll_timeout = _parse_int(
            file_data["poll_timeout"], "poll_timeout", 1, 300
        )
    else:
        poll_timeout = DEFAULT_POLL_TIMEOUT

    raw_port = env.get("MULTIBOT_GATEWAY_PORT", "").strip()
    if raw_port:
        gateway_port = _parse_int(raw_port, "MULTIBOT_GATEWAY_PORT", 1, 65535)
    elif "gateway_port" in file_data:
        gateway_port = _parse_int(
            file_data["gateway_port"], "gateway_port", 1, 65535
        )
    else:
        gateway_port = DEFAULT_GATEWAY_PORT

    raw_api = env.get("MULTIBOT_API_BASE_URL", "").strip()
    if raw_api:
        api_base_url = raw_api
    elif file_data.get("api_base_url"):
        api_base_url = str(file_data["api_base_url"]).strip()
    else:
        api_base_url = None

    group_privacy = str(file_data.get("group_privacy") or "on").strip()
    if group_privacy not in ("on", "off"):
        raise ValueError(
            "group_privacy phải là \"on\" hoặc \"off\": %r" % (group_privacy,)
        )
    raw_allowed = file_data.get("allowed_updates", ["message"])
    if not isinstance(raw_allowed, list) or not raw_allowed:
        raise ValueError(
            "allowed_updates phải là danh sách không rỗng: %r" % (raw_allowed,)
        )
    allowed_updates = []
    for item in raw_allowed:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("allowed_updates chứa mục không hợp lệ: %r" % (item,))
        name = item.strip()
        if name in FORBIDDEN_UPDATE_TYPES:
            raise ValueError(
                "allowed_updates cấm %r ở giai đoạn đầu (D6/thiết kế 03)" % (name,)
            )
        allowed_updates.append(name)
    parse_mode = file_data.get("parse_mode")
    if parse_mode is not None:
        parse_mode = str(parse_mode).strip()
        if parse_mode not in PARSE_MODES:
            raise ValueError(
                "parse_mode phải là một trong %s: %r" % (PARSE_MODES, parse_mode)
            )

    config_dir = base / "config"
    secrets_dir = base / "secrets"
    data_dir = base / "data" / resolved_bot_id
    locks_dir = base / "locks"
    return BotConfig(
        bot_id=resolved_bot_id,
        platform=platform,
        display_name=display_name,
        base_dir=base,
        config_dir=config_dir,
        registry_path=base / "registry.json",
        secrets_dir=secrets_dir,
        token_path=secrets_dir / ("%s.token" % resolved_bot_id),
        queue_secret_path=secrets_dir / ("%s.queue_secret" % resolved_bot_id),
        sync_secret_path=secrets_dir / ("%s.sync_secret" % resolved_bot_id),
        data_dir=data_dir,
        queue_path=data_dir / "queue.jsonl",
        seen_path=data_dir / "seen.txt",
        state_path=data_dir / "state.json",
        capability_path=data_dir / "capability.json",
        dm_only_path=data_dir / "dm_only.json",
        locks_dir=locks_dir,
        lock_path=locks_dir / ("%s.lock" % resolved_bot_id),
        owner_path=locks_dir / ("%s.owner" % resolved_bot_id),
        gateway_fetch_state_path=base / "gateway-fetch-state.json",
        gateway_port=gateway_port,
        poll_timeout=poll_timeout,
        api_base_url=api_base_url,
        group_privacy=group_privacy,
        allowed_updates=tuple(allowed_updates),
        parse_mode=parse_mode,
    )


def ensure_dirs(config):
    """Tạo các thư mục của bot; thư mục nhạy cảm đặt quyền 700.

    Chỉ tạo thư mục — không tạo, không đọc, không ghi file secret.
    """
    for path in (config.config_dir, config.secrets_dir, config.data_dir,
                 config.locks_dir, config.files_dir):
        path.mkdir(parents=True, exist_ok=True)
    for path in (config.secrets_dir, config.data_dir, config.locks_dir,
                 config.files_dir):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
