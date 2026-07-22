"""Add hero_slides and app_settings for landing covers

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-22 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hero_slides",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("image_filename", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_hero_slides_id", "hero_slides", ["id"])

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("value", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO app_settings (key, value) VALUES ('hero_carousel_seconds', '10')"
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_hero_slides_id", table_name="hero_slides")
    op.drop_table("hero_slides")
