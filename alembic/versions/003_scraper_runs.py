"""Add scraper_runs table for admin status monitoring

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-22 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    scraperrunstatus = postgresql.ENUM(
        "RUNNING",
        "COMPLETED",
        "FAILED",
        name="scraperrunstatus",
        create_type=False,
    )
    scraperrunstatus.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "scraper_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "status",
            scraperrunstatus,
            nullable=False,
            server_default="RUNNING",
        ),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("pages_total", sa.Integer(), server_default="0"),
        sa.Column("pages_done", sa.Integer(), server_default="0"),
        sa.Column("books_saved", sa.Integer(), server_default="0"),
        sa.Column("books_skipped", sa.Integer(), server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_scraper_runs_id", "scraper_runs", ["id"])
    op.create_index("ix_scraper_runs_status", "scraper_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_scraper_runs_status", table_name="scraper_runs")
    op.drop_index("ix_scraper_runs_id", table_name="scraper_runs")
    op.drop_table("scraper_runs")
    postgresql.ENUM(name="scraperrunstatus").drop(op.get_bind(), checkfirst=True)
