"""Add billing address columns for Torob Pay CPG

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-08-05 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "j0k1l2m3n4o5"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("billing_address", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("billing_city", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("billing_province", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("billing_postal_code", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "billing_postal_code")
    op.drop_column("orders", "billing_province")
    op.drop_column("orders", "billing_city")
    op.drop_column("orders", "billing_address")
