# -*- coding: utf-8 -*-
"""显示用的数字格式化。"""

from decimal import Decimal

__all__ = ["human_tokens", "human_count", "money", "percent", "trim_decimal"]


def human_tokens(n):
    """把 token 数变成紧凑写法：1,234 -> 1.2K，551221755 -> 551.2M。"""
    n = int(n or 0)
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a < 1000:
        return f"{sign}{a}"
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if a >= div:
            v = a / div
            if v >= 100:
                return f"{sign}{v:.0f}{unit}"
            if v >= 10:
                return f"{sign}{v:.1f}{unit}"
            return f"{sign}{v:.2f}{unit}"
    return f"{sign}{a}"


def human_count(n):
    """请求数等普通计数，加千分位。"""
    return f"{int(n or 0):,}"


def trim_decimal(value, places=2):
    """Decimal -> 定长小数字符串，去掉多余的 0（但至少保留 2 位）。"""
    if value is None:
        return "—"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    q = d.quantize(Decimal(1).scaleb(-places))
    return f"{q:,f}"


def money(value, symbol="¥", places=2):
    """金额显示。"""
    if value is None:
        return "—"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if d == 0:
        return f"{symbol}0.00"
    sign = "-" if d < 0 else ""
    body = trim_decimal(abs(d), places)
    return f"{sign}{symbol}{body}"


def percent(x, places=1):
    """比率 -> 百分比字符串；None 显示为 —。"""
    if x is None:
        return "—"
    return f"{x * 100:.{places}f}%"
