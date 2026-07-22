"""Persian labels and Jalali (Shamsi) datetime helpers — Asia/Tehran."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.models import OrderStatus

TEHRAN = ZoneInfo("Asia/Tehran")

ORDER_STATUS_FA: dict[str, str] = {
    OrderStatus.PENDING.value: "در انتظار پرداخت",
    OrderStatus.PAID.value: "پرداخت‌شده",
    OrderStatus.FAILED.value: "ناموفق",
    OrderStatus.CANCELLED.value: "لغو شده",
}

ORDER_STATUS_BADGE: dict[str, str] = {
    OrderStatus.PENDING.value: "badge-pending",
    OrderStatus.PAID.value: "badge-ok",
    OrderStatus.FAILED.value: "badge-no",
    OrderStatus.CANCELLED.value: "badge-muted",
}


def order_status_fa(status) -> str:
    """Return Persian label for an OrderStatus enum or string value."""
    if status is None:
        return "—"
    key = status.value if hasattr(status, "value") else str(status)
    return ORDER_STATUS_FA.get(key, key)


def order_status_badge_class(status) -> str:
    if status is None:
        return "badge"
    key = status.value if hasattr(status, "value") else str(status)
    return ORDER_STATUS_BADGE.get(key, "badge")


def _to_tehran(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TEHRAN)


def _gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert Gregorian date to Jalali (algorithm from jalaali / common public domain)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def format_jalali(dt: datetime | None, with_time: bool = True) -> str:
    """Format datetime as Jalali in Tehran time, e.g. 1404/04/31 17:45."""
    if dt is None:
        return "—"
    t = _to_tehran(dt)
    jy, jm, jd = _gregorian_to_jalali(t.year, t.month, t.day)
    date_part = f"{jy:04d}/{jm:02d}/{jd:02d}"
    if not with_time:
        return date_part
    return f"{date_part} {t.hour:02d}:{t.minute:02d}"
