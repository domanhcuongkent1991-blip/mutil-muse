#!/usr/bin/env python3
"""Helper kiểm tra token — ngoại lệ hẹp duy nhất của F18.

Tiến trình riêng, CHỈ gọi ``getMe``, token qua stdin, không log,
không ghi file, không có khả năng gửi tin/thu hồi/tạo token. UI gọi
helper này TRƯỚC khi lưu token mới (thêm bot + thay token đã lưu).

Cách dùng::

    echo -n "<token>" | python3 verify_token.py \
        --platform zalo --base-url <scheme+host>

``base_url`` bắt buộc truyền tường minh (không có host mặc định):
chạy thật do cấu hình cung cấp, test dùng máy chủ giả localhost.

In ra stdout 1 dòng JSON duy nhất:

- Thành công (mã thoát 0)::

      {"ok": true, "id": "...", "name": "...", "username": "..."}

  ``id`` là id bot nền tảng trả về (dạng chuỗi); ``name`` là tên hiển
  thị (Zalo: ``display_name``; Telegram: ``first_name``); ``username``
  chỉ có ở Telegram, Zalo để null.
- Lỗi (mã thoát 1)::

      {"ok": false, "error_code": 401}

  Chỉ mang mã lỗi (HTTP status hoặc mã trong body), KHÔNG mang body
  hay câu chữ lỗi của nền tảng — tránh lặp lại bất cứ thứ gì có thể
  chứa token. Lỗi transport -> ``error_code`` null.
- Lỗi dùng sai (mã thoát 2): in hướng dẫn ra stderr, không in JSON.

Token KHÔNG BAO GIỜ xuất hiện trong output, log, hay thông báo lỗi:
helper không in gì ra stderr ngoài hướng dẫn dùng sai (không chứa
token), và transport chung đã tẩy token khỏi chuỗi lỗi.
"""

import argparse
import json
import os
import sys

# Cho phép chạy trực tiếp dạng script: thêm thư mục gốc chứa shared/
# và pc/ vào sys.path.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _read_token_from_stdin():
    try:
        raw = sys.stdin.buffer.read()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    token = lines[0].strip()
    return token or None


def verify(platform, base_url, token, timeout=15):
    """Gọi getMe, trả về (ok, info_dict).

    ``info_dict`` thành công: {"id", "name", "username"}.
    ``info_dict`` lỗi: {"error_code"} (mã HTTP hoặc mã body, None nếu
    lỗi transport). Không raise vì lỗi API — chỉ raise khi tham số
    sai.
    """
    if platform == "zalo":
        from shared import zalo_client

        result = zalo_client.call_api(base_url, token, "getMe", {},
                                      timeout=timeout)
    elif platform == "telegram":
        from shared import telegram_client

        result = telegram_client.call_api(base_url, token, "getMe", {},
                                          timeout=timeout)
    else:
        raise ValueError("platform phải là zalo hoặc telegram")
    body = result.get("body")
    if isinstance(body, dict) and body.get("ok") is True:
        data = body.get("result")
        if isinstance(data, dict):
            bot_id = data.get("id")
            name = data.get("display_name") or data.get("first_name")
            username = data.get("username")
            return True, {
                "id": "" if bot_id is None else str(bot_id),
                "name": name if isinstance(name, str) else None,
                "username": username if isinstance(username, str) else None,
            }
        return True, {"id": "", "name": None, "username": None}
    error_code = result.get("error_code")
    if error_code is None:
        error_code = result.get("http_status")
    if isinstance(error_code, bool):
        error_code = None
    return False, {"error_code": error_code}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Kiem tra token bang getMe (chi doc, khong luu).")
    parser.add_argument("--platform", required=True,
                        choices=("zalo", "telegram"),
                        help="nen tang cua bot")
    parser.add_argument("--base-url", required=True,
                        help="scheme + host API (khong co mac dinh)")
    parser.add_argument("--timeout", type=int, default=15,
                        help="timeout HTTP giay (mac dinh 15)")
    args = parser.parse_args(argv)
    token = _read_token_from_stdin()
    if token is None:
        print("thieu token: truyen token qua stdin (1 dong)",
              file=sys.stderr)
        return 2
    if not args.base_url.strip():
        print("thieu base-url", file=sys.stderr)
        return 2
    try:
        ok, info = verify(args.platform, args.base_url.strip(), token,
                          timeout=args.timeout)
    except ValueError as exc:
        print("tham so sai: %s" % (exc,), file=sys.stderr)
        return 2
    del token
    output = {"ok": ok}
    output.update(info)
    print(json.dumps(output, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
