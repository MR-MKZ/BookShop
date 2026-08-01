#!/usr/bin/env python3
"""Ensure the first admin user exists from ADMIN_* env settings."""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth import get_password_hash
from app.config import settings
from app.models import User, UserRole
from app.utils.phone import validate_iran_phone


def ensure_admin() -> int:
    phone_raw = (settings.ADMIN_PHONE or "").strip()
    password = settings.ADMIN_PASSWORD or ""
    if not phone_raw or not password:
        print("ADMIN_PHONE / ADMIN_PASSWORD not set — skipping admin bootstrap.")
        return 0

    ok, phone_or_err = validate_iran_phone(phone_raw)
    if not ok:
        print(f"Invalid ADMIN_PHONE: {phone_or_err}", file=sys.stderr)
        return 1

    phone = phone_or_err
    if not settings.SYNC_DATABASE_URL:
        print("SYNC_DATABASE_URL is not configured.", file=sys.stderr)
        return 1

    engine = create_engine(settings.SYNC_DATABASE_URL)
    with Session(engine) as session:
        admin_count = session.scalar(
            select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
        ) or 0

        existing = session.scalar(select(User).where(User.phone == phone))

        if admin_count > 0:
            if existing and existing.role == UserRole.ADMIN:
                print(f"Admin already present (phone={phone}, id={existing.id}).")
            else:
                print(f"Admin users already exist (count={admin_count}) — skipping create.")
            return 0

        first_name = (settings.ADMIN_FIRST_NAME or "مدیر").strip() or "مدیر"
        last_name = (settings.ADMIN_LAST_NAME or "سیستم").strip() or "سیستم"
        email = (settings.ADMIN_EMAIL or "").strip() or None

        if existing:
            existing.role = UserRole.ADMIN
            existing.is_active = True
            existing.hashed_password = get_password_hash(password)
            existing.first_name = first_name
            existing.last_name = last_name
            existing.full_name = f"{first_name} {last_name}".strip()
            if email:
                existing.email = email
            session.commit()
            print(f"Promoted existing user to admin (phone={phone}, id={existing.id}).")
            return 0

        admin = User(
            email=email,
            username=None,
            hashed_password=get_password_hash(password),
            first_name=first_name,
            last_name=last_name,
            full_name=f"{first_name} {last_name}".strip(),
            phone=phone,
            is_active=True,
            role=UserRole.ADMIN,
        )
        session.add(admin)
        session.commit()
        print(f"Created first admin (phone={phone}, id={admin.id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(ensure_admin())
