#!/usr/bin/env python3
"""Đặt mật khẩu Web UI lần đầu / đổi mật khẩu (CLI, chạy trên PC).

Đọc mật khẩu bằng ``getpass`` (không hiện trên màn hình), nhập 2 lần
khớp nhau. KHÔNG có cờ ``--password`` — mật khẩu không bao giờ đi qua
argv/lịch sử shell. Đổi mật khẩu thu hồi toàn bộ phiên đang có.

Mật khẩu tối thiểu 12 ký tự (SUY LUẬN — thiết kế không chốt số).
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

# Cho phép chạy trực tiếp dạng script: thêm thư mục gốc chứa shared/
# và pc/ vào sys.path.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from pc.ui import ui_auth  # noqa: E402
from pc.config import default_base_dir  # noqa: E402

MIN_PASSWORD_CHARS = 12


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dat mat khau Web UI (nhap 2 lan, khong hien chu).")
    parser.add_argument("--root", "--base-dir", dest="root", default=None,
                        help="thu muc goc du lieu (mac dinh ~/multibot/)")
    args = parser.parse_args(argv)
    root = args.root or os.environ.get("MULTIBOT_BASE_DIR")
    root = Path(root).expanduser() if root else default_base_dir()
    first = getpass.getpass("Mật khẩu UI mới (tối thiểu 12 ký tự): ")
    if len(first) < MIN_PASSWORD_CHARS:
        sys.stderr.write("Mật khẩu quá ngắn (tối thiểu %d ký tự).\n"
                         % MIN_PASSWORD_CHARS)
        return 2
    second = getpass.getpass("Nhập lại mật khẩu: ")
    if first != second:
        sys.stderr.write("2 lần nhập không khớp. Chưa đổi gì.\n")
        return 2
    ui_auth.set_password(root, first)
    print("Đã đặt mật khẩu UI. Mọi phiên đăng nhập cũ đã bị thu hồi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
