"""Add books.file_filename for titled ebook storage

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-22 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("books", sa.Column("file_filename", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("books", "file_filename")
