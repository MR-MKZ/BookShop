"""Add unique SEO slug for books (English-title URLs)

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-03 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
import hashlib
import re


revision = "g7h8i9j0k1l2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def _slugify_name(name: str | None, max_len: int = 80) -> str:
    text = (name or "").strip()
    text = re.sub(r"[^\w\s\-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s\-]+", "_", text).strip("._")
    ascii_text = text.encode("ascii", "ignore").decode("ascii").strip("._")
    parts = [p for p in ascii_text.split("_") if p]
    base = "_".join(parts)[:max_len].strip("._")
    return base or "book"


def upgrade() -> None:
    op.add_column("books", sa.Column("slug", sa.String(), nullable=True))
    op.create_index("ix_books_slug", "books", ["slug"], unique=True)

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, title, title_en FROM books ORDER BY id")
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        book_id = row[0]
        title = row[1]
        title_en = row[2]
        base = _slugify_name(title_en or title)
        slug = f"{base}_{int(book_id)}"
        if slug in seen:
            slug = f"{base}_{int(book_id)}_{hashlib.md5(str(book_id).encode()).hexdigest()[:4]}"
        seen.add(slug)
        conn.execute(
            sa.text("UPDATE books SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": book_id},
        )


def downgrade() -> None:
    op.drop_index("ix_books_slug", table_name="books")
    op.drop_column("books", "slug")
