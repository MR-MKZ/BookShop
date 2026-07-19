#!/usr/bin/env python3
"""Seed development database from books_data_backup.db and create an admin user."""

from __future__ import annotations

import hashlib
import random
import re
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import get_password_hash  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Book, User, UserRole  # noqa: E402

BACKUP_DB = ROOT / "books_data_backup.db"
DEFAULT_ADMIN_PHONE = "09153276607"
DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_ADMIN_FIRST_NAME = "مدیر"
DEFAULT_ADMIN_LAST_NAME = "سیستم"


def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name or "")
    clean = clean.replace(" ", "_").strip()
    return clean[:80] or "book"


def parse_price(raw: str | None) -> Decimal:
    if not raw:
        return Decimal("0")
    digits = re.sub(r"[^\d.]", "", str(raw))
    if not digits:
        return Decimal("0")
    try:
        return Decimal(digits)
    except InvalidOperation:
        return Decimal("0")


def make_folder_name(title: str, url: str) -> str:
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:6]
    return f"{sanitize_filename(title)}_{digest}"


def apply_dual_pricing(raw_price: Decimal) -> tuple[Decimal, Decimal]:
    """Sale price 2-3k below source; original 30-40k above sale."""
    discount = Decimal(random.randint(2000, 3000))
    price = max(Decimal("0"), raw_price - discount)
    original = price + Decimal(random.randint(30000, 40000))
    return price, original


def seed_books(session: Session, sqlite_path: Path) -> int:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"Backup database not found: {sqlite_path}")

    existing = session.scalar(select(Book.id).limit(1))
    if existing is not None:
        print("Books already present — skipping book seed.")
        return 0

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT url, title_fa, title_en, author, publisher, isbn, publish_year,
               language, pages, file_format, file_size, edition, price,
               availability, amazon_link, image_url, description
        FROM books
        """
    ).fetchall()
    conn.close()

    books: list[Book] = []
    seen_folders: set[str] = set()

    for row in rows:
        url = row["url"]
        if not url:
            continue

        title = (row["title_fa"] or row["title_en"] or "بدون عنوان").strip()
        title = title.lstrip("\ufeff").strip()

        folder_name = make_folder_name(title, url)
        if folder_name in seen_folders:
            folder_name = f"{folder_name}_{len(seen_folders)}"
        seen_folders.add(folder_name)

        raw_price = parse_price(row["price"])
        price, original_price = apply_dual_pricing(raw_price)

        books.append(
            Book(
                url=url,
                title=title,
                title_en=row["title_en"],
                author=row["author"],
                publisher=row["publisher"],
                isbn=row["isbn"],
                publish_year=row["publish_year"],
                language=row["language"],
                pages=row["pages"],
                file_format=row["file_format"] or "pdf",
                file_size=row["file_size"],
                edition=row["edition"],
                price=price,
                original_price=original_price,
                availability=row["availability"],
                amazon_link=row["amazon_link"],
                image_url=row["image_url"],
                description=row["description"],
                folder_name=folder_name,
                cover_filename="cover.jpg",
                has_pdf=False,
                is_active=True,
            )
        )

    session.add_all(books)
    session.commit()
    print(f"Seeded {len(books)} books from {sqlite_path.name}.")
    return len(books)


def seed_admin(session: Session) -> None:
    existing = session.scalar(select(User).where(User.phone == DEFAULT_ADMIN_PHONE))
    if existing:
        print(f"Admin user already exists (id={existing.id}) — skipping.")
        return

    admin = User(
        email="admin@kabana.local",
        username="admin",
        hashed_password=get_password_hash(DEFAULT_ADMIN_PASSWORD),
        first_name=DEFAULT_ADMIN_FIRST_NAME,
        last_name=DEFAULT_ADMIN_LAST_NAME,
        full_name=f"{DEFAULT_ADMIN_FIRST_NAME} {DEFAULT_ADMIN_LAST_NAME}",
        phone=DEFAULT_ADMIN_PHONE,
        is_active=True,
        role=UserRole.ADMIN,
    )
    session.add(admin)
    session.commit()
    print(
        f"Created admin user phone={DEFAULT_ADMIN_PHONE} "
        f"(password: {DEFAULT_ADMIN_PASSWORD})."
    )


def main() -> int:
    if not settings.SYNC_DATABASE_URL:
        print("SYNC_DATABASE_URL is not configured.", file=sys.stderr)
        return 1

    engine = create_engine(settings.SYNC_DATABASE_URL)
    with Session(engine) as session:
        seed_books(session, BACKUP_DB)
        seed_admin(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
