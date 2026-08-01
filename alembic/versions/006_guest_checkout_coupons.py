"""Guest checkout fields, coupons, download TTL setting

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-01 11:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same pattern as 001_initial_migration: create enum safely, then reuse
    # postgresql.ENUM(..., create_type=False) so create_table does not recreate it.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'discounttype') THEN
                    CREATE TYPE discounttype AS ENUM ('PERCENT', 'FIXED');
                END IF;
            END
            $$;
            """
        )
    )

    discounttype = postgresql.ENUM(
        "PERCENT",
        "FIXED",
        name="discounttype",
        create_type=False,
    )

    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("discount_type", discounttype, nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("min_order_amount", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    op.create_index("ix_coupons_id", "coupons", ["id"])
    op.create_index("ix_coupons_code", "coupons", ["code"], unique=True)

    op.alter_column("orders", "user_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("orders", sa.Column("subtotal_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("orders", sa.Column("discount_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("orders", sa.Column("coupon_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("billing_first_name", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("billing_last_name", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("billing_phone", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("billing_email", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("customer_note", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("access_token", sa.String(), nullable=True))
    op.add_column("orders", sa.Column("payment_gateway", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_orders_coupon_id", "orders", "coupons", ["coupon_id"], ["id"]
    )
    op.create_index("ix_orders_billing_phone", "orders", ["billing_phone"])
    op.create_index("ix_orders_billing_email", "orders", ["billing_email"])
    op.create_index("ix_orders_access_token", "orders", ["access_token"], unique=True)

    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("coupon_id", sa.Integer(), sa.ForeignKey("coupons.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    op.create_index("ix_coupon_redemptions_id", "coupon_redemptions", ["id"])
    op.create_index(
        "ix_coupon_redemptions_order_id", "coupon_redemptions", ["order_id"], unique=True
    )

    op.alter_column("download_links", "user_id", existing_type=sa.Integer(), nullable=True)

    op.execute(
        sa.text(
            "INSERT INTO app_settings (key, value) VALUES "
            "('download_link_ttl_hours', '6') "
            "ON CONFLICT (key) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM app_settings WHERE key = 'download_link_ttl_hours'"))
    op.alter_column("download_links", "user_id", existing_type=sa.Integer(), nullable=False)
    op.drop_index("ix_coupon_redemptions_order_id", table_name="coupon_redemptions")
    op.drop_index("ix_coupon_redemptions_id", table_name="coupon_redemptions")
    op.drop_table("coupon_redemptions")
    op.drop_index("ix_orders_access_token", table_name="orders")
    op.drop_index("ix_orders_billing_email", table_name="orders")
    op.drop_index("ix_orders_billing_phone", table_name="orders")
    op.drop_constraint("fk_orders_coupon_id", "orders", type_="foreignkey")
    op.drop_column("orders", "payment_gateway")
    op.drop_column("orders", "access_token")
    op.drop_column("orders", "customer_note")
    op.drop_column("orders", "billing_email")
    op.drop_column("orders", "billing_phone")
    op.drop_column("orders", "billing_last_name")
    op.drop_column("orders", "billing_first_name")
    op.drop_column("orders", "coupon_id")
    op.drop_column("orders", "discount_amount")
    op.drop_column("orders", "subtotal_amount")
    op.alter_column("orders", "user_id", existing_type=sa.Integer(), nullable=False)
    op.drop_index("ix_coupons_code", table_name="coupons")
    op.drop_index("ix_coupons_id", table_name="coupons")
    op.drop_table("coupons")
    op.execute(sa.text("DROP TYPE IF EXISTS discounttype"))
