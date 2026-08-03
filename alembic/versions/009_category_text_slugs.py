"""Regenerate category slugs as readable text (Unicode), not MD5 hashes

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-08-03 23:40:00.000000

"""
from alembic import op
import sqlalchemy as sa
import re


revision = "i9j0k1l2m3n4"
down_revision = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None


def _slugify(name: str | None, max_len: int = 80) -> str:
    text = (name or "").strip()
    text = re.sub(r"[^\w\s\-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_\-]+", "-", text).strip("-")
    if text:
        return text[:max_len].strip("-") or "category"
    return "category"


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, name, slug FROM categories ORDER BY id")
    ).fetchall()
    if not rows:
        return

    seen: set[str] = set()
    for row in rows:
        cat_id = int(row[0])
        name = row[1]
        old_slug = row[2] or ""
        # Prefer regenerating hash-style slugs; still normalize others from name
        if old_slug.startswith("cat-") and len(old_slug) >= 10:
            base = _slugify(name)
        else:
            base = _slugify(old_slug) if old_slug else _slugify(name)
            if not base or base == "category":
                base = _slugify(name)

        slug = base
        n = 2
        while slug in seen:
            slug = f"{base}-{n}"
            n += 1
        seen.add(slug)
        if slug != old_slug:
            conn.execute(
                sa.text("UPDATE categories SET slug = :slug WHERE id = :id"),
                {"slug": slug, "id": cat_id},
            )


def downgrade() -> None:
    # Irreversible data fix; leave slugs as-is
    pass
