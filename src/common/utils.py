"""通用小工具函数。"""

from __future__ import annotations

__all__ = ["escape_like", "like_pattern"]


def escape_like(keyword: str) -> str:
    """转义 SQL LIKE 通配符。

    用户输入中的 ``%`` 与 ``_`` 会被当作普通字符，避免出现"输入 ``a_b`` 命中 ``axb``"
    这类意外匹配；使用方需配合 ``like(..., escape="\\\\")``。
    """
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(keyword: str) -> str:
    """构造两端模糊匹配的 LIKE 模式（已转义通配符）。"""
    return f"%{escape_like(keyword.strip())}%"
