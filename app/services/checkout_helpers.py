"""Shared helpers for checkout, coupons, and download TTL."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DOWNLOAD_LINK_TTL_HOURS_DEFAULT,
    DOWNLOAD_LINK_TTL_HOURS_KEY,
    AppSetting,
    Coupon,
    DiscountType,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ORDER_ACCESS_COOKIE = "order_access"
COUPON_COOKIE = "checkout_coupon"
ORDER_ACCESS_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def new_order_access_token() -> str:
    return secrets.token_urlsafe(32)


def validate_email(raw: str | None) -> tuple[bool, str]:
    email = (raw or "").strip().lower()
    if not email:
        return False, "آدرس ایمیل الزامی است"
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return False, "آدرس ایمیل معتبر نیست"
    return True, email


async def get_download_ttl_hours(db: AsyncSession) -> int:
    result = await db.execute(
        select(AppSetting).where(AppSetting.key == DOWNLOAD_LINK_TTL_HOURS_KEY)
    )
    row = result.scalar_one_or_none()
    if not row:
        return DOWNLOAD_LINK_TTL_HOURS_DEFAULT
    try:
        hours = int(str(row.value).strip())
        return max(1, min(hours, 24 * 30))
    except (TypeError, ValueError):
        return DOWNLOAD_LINK_TTL_HOURS_DEFAULT


async def get_download_ttl_seconds(db: AsyncSession) -> int:
    return (await get_download_ttl_hours(db)) * 3600


def compute_discount(coupon: Coupon, subtotal: Decimal) -> Decimal:
    if coupon.discount_type == DiscountType.PERCENT:
        discount = (subtotal * Decimal(coupon.amount) / Decimal("100")).quantize(
            Decimal("1")
        )
    else:
        discount = Decimal(coupon.amount).quantize(Decimal("1"))
    if discount < 0:
        discount = Decimal("0")
    if discount > subtotal:
        discount = subtotal
    return discount


async def find_valid_coupon(
    db: AsyncSession,
    code: str | None,
    subtotal: Decimal,
) -> tuple[Coupon | None, str | None]:
    """Return (coupon, error_message)."""
    raw = (code or "").strip().upper()
    if not raw:
        return None, None

    result = await db.execute(select(Coupon).where(Coupon.code == raw))
    coupon = result.scalar_one_or_none()
    if not coupon or not coupon.is_active:
        return None, "کد تخفیف معتبر نیست"

    now = datetime.now(timezone.utc)
    if coupon.starts_at and now < coupon.starts_at:
        return None, "کد تخفیف هنوز فعال نشده است"
    if coupon.ends_at and now > coupon.ends_at:
        return None, "کد تخفیف منقضی شده است"
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        return None, "ظرفیت استفاده از این کد تکمیل شده است"
    if coupon.min_order_amount is not None and subtotal < Decimal(
        coupon.min_order_amount
    ):
        return None, "مبلغ سفارش برای این کد تخفیف کافی نیست"

    return coupon, None
