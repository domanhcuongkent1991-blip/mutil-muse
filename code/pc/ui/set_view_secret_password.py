#!/usr/bin/env python3
"""Đặt mật khẩu xem mã bí mật trung tâm lần đầu / đổi / đặt lại
(CLI, chạy trên máy tính — v0.6.0, T3j, ngoại lệ F18(3)).

Mật khẩu xem mã là mật khẩu thứ 2, khác mật khẩu giao diện, 1 mật
khẩu chung cho tất cả tài khoản (Q-V060-15). Đọc mật khẩu bằng
``getpass`` (không hiện trên màn hình), nhập 2 lần khớp nhau. KHÔNG
có cờ ``--password`` — mật khẩu không bao giờ đi qua argv/lịch sử
shell. Từ chối khi trùng mật khẩu giao diện (PLAN T3j tiêu chí 8).
Không có mật khẩu mặc định; quên mật khẩu thì đặt lại bằng CLI này
(ghi đè file hash — Q-V060-16).

Phân biệt 3 trường hợp (audit khác nhau, SPEC TS3):
- chưa có file hash -> đặt lần đầu (``center_view_password_set``);
- đã có hash + nhập đúng mật khẩu cũ -> đổi
  (``center_view_password_changed``);
- đã có hash + quên mật khẩu cũ -> đặt lại, ghi đè hash
  (``center_view_password_reset`` — cần chủ nhân xác nhận trước khi
  ghi đè).

Đổi mật khẩu xem mã KHÔNG thu hồi phiên đăng nhập giao diện và
KHÔNG vô hiệu lượt xem đang mở (Q-V060-22). Mật khẩu tối thiểu
8 ký tự. Không hết hạn cứng; đổi mềm mỗi 3 tháng cùng kỳ mật khẩu
giao diện (Q-V060-23).
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

from pc.ui import ui_auth, ui_store  # noqa: E402
from pc.config import default_base_dir  # noqa: E402

MIN_PASSWORD_CHARS = ui_auth.VIEW_SECRET_MIN_PASSWORD_CHARS


def _confirm_overwrite():
    """Hỏi chủ nhân xác nhận ghi đè khi quên mật khẩu cũ.

    Trả True chỉ khi nhập đúng "co" (không phân biệt hoa/thường).
    """
    answer = input(
        "Quên mật khẩu xem mã cũ — ghi đè mật khẩu xem mã cũ "
        "bằng mật khẩu mới? Gõ \"co\" để xác nhận: ")
    return answer.strip().lower() == "co"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dat mat khau xem ma bi mat trung tam "
                    "(nhap 2 lan, khong hien chu).")
    parser.add_argument("--root", "--base-dir", dest="root", default=None,
                        help="thu muc goc du lieu (mac dinh ~/multibot/)")
    args = parser.parse_args(argv)
    root = args.root or os.environ.get("MULTIBOT_BASE_DIR")
    root = Path(root).expanduser() if root else default_base_dir()

    existing = ui_auth.load_view_secret_password_hash(root)
    action = "center_view_password_set"
    if existing is not None:
        current = getpass.getpass(
            "Mật khẩu xem mã hiện tại (để trống nếu quên): ")
        if current:
            if not ui_auth.verify_password(current, existing):
                sys.stderr.write(
                    "Mật khẩu xem mã hiện tại chưa đúng. Chưa đổi gì.\n")
                return 2
            action = "center_view_password_changed"
        else:
            if not _confirm_overwrite():
                sys.stderr.write(
                    "Chưa xác nhận ghi đè. Chưa đổi gì.\n")
                return 2
            action = "center_view_password_reset"
    first = getpass.getpass(
        "Mật khẩu xem mã mới (tối thiểu %d ký tự): "
        % MIN_PASSWORD_CHARS)
    if len(first) < MIN_PASSWORD_CHARS:
        sys.stderr.write("Mật khẩu quá ngắn (tối thiểu %d ký tự).\n"
                         % MIN_PASSWORD_CHARS)
        return 2
    ui_hash = ui_auth.load_password_hash(root)
    if ui_hash is not None and ui_auth.verify_password(first, ui_hash):
        sys.stderr.write(
            "Mật khẩu xem mã trùng mật khẩu giao diện — "
            "chọn mật khẩu khác. Chưa đổi gì.\n")
        return 2
    second = getpass.getpass("Nhập lại mật khẩu xem mã mới: ")
    if first != second:
        sys.stderr.write("2 lần nhập không khớp. Chưa đổi gì.\n")
        return 2
    ui_auth.set_view_secret_password(root, first)
    ui_store.append_audit_line(
        root, "cli", action, None, {}, {"scope": "all_accounts"}, "ok")
    if action == "center_view_password_set":
        print("Đã đặt mật khẩu xem mã (dùng chung cho mọi tài khoản).")
    elif action == "center_view_password_changed":
        print("Đã đổi mật khẩu xem mã.")
    else:
        print("Đã đặt lại mật khẩu xem mã (mật khẩu cũ hết hiệu lực).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
