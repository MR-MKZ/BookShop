"""Toman price helpers: round down to whole thousands and format for display."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def round_toman(value) -> Decimal:
    """Floor price to the nearest 1,000 toman (drop odd remainders)."""
    if value is None:
        return Decimal("0")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    if amount <= 0:
        return Decimal("0")
    return (amount // 1000) * 1000


def format_price(value) -> str:
    """Round to thousands and format with thousands separators."""
    if value is None:
        return "۰"
    try:
        rounded = round_toman(value)
        return f"{rounded:,.0f}"
    except (TypeError, ValueError, InvalidOperation):
        return str(value)
