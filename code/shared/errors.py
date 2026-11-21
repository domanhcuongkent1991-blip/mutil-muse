"""Phân loại lỗi chung, trung lập nền tảng (DESIGN.md §7).

Đầu nối (Zalo/Telegram) phân loại tín hiệu riêng của nền tảng thành một
trong các phân loại này; lõi chỉ thấy phân loại chung, không bao giờ
thấy mã lỗi riêng của nền tảng.
"""

from __future__ import annotations

from enum import Enum


class ErrorClass(str, Enum):
    """Phân loại lỗi chung — lõi chỉ thấy 6 giá trị này."""

    RETRYABLE_NETWORK = "RETRYABLE_NETWORK"
    TOKEN_INVALID = "TOKEN_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    CHAT_GONE = "CHAT_GONE"
    PERMANENT = "PERMANENT"
    UNKNOWN = "UNKNOWN"


ALL_ERROR_CLASSES = tuple(ErrorClass)
