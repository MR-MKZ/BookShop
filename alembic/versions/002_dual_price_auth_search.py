"""Add dual pricing, has_pdf, cover_filename, user first/last name, phone unique

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-19 17:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "books",
        sa.Column("original_price", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        "books",
        sa.Column("cover_filename", sa.String(), nullable=True, server_default="cover.jpg"),
    )
    op.add_column(
        "books",
        sa.Column(
            "has_pdf",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )
    op.create_index("ix_books_has_pdf", "books", ["has_pdf"], unique=False)

    # Backfill original_price where missing: price + 35000
    op.execute(
        sa.text(
            """
            UPDATE books
            SET original_price = COALESCE(price, 0) + 35000
            WHERE original_price IS NULL AND price IS NOT NULL
            """
        )
    )

    op.add_column(
        "users",
        sa.Column("first_name", sa.String(), nullable=True, server_default=""),
    )
    op.add_column(
        "users",
        sa.Column("last_name", sa.String(), nullable=True, server_default=""),
    )

    # Split existing full_name into first/last when possible
    op.execute(
        sa.text(
            """
            UPDATE users
            SET first_name = CASE
                    WHEN full_name IS NULL OR full_name = '' THEN COALESCE(username, 'کاربر')
                    WHEN position(' ' in full_name) > 0 THEN split_part(full_name, ' ', 1)
                    ELSE full_name
                END,
                last_name = CASE
                    WHEN full_name IS NULL OR full_name = '' THEN ''
                    WHEN position(' ' in full_name) > 0 THEN substring(full_name from position(' ' in full_name) + 1)
                    ELSE ''
                END
            WHERE first_name IS NULL OR first_name = ''
            """
        )
    )

    # Ensure phones exist for uniqueness; fill placeholder for empties
    op.execute(
        sa.text(
            """
            UPDATE users
            SET phone = '09' || lpad(id::text, 9, '0')
            WHERE phone IS NULL OR phone = ''
            """
        )
    )

    op.alter_column("users", "phone", existing_type=sa.String(), nullable=False)
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    op.create_index(
        "ix_orders_payment_gateway_transaction_id",
        "orders",
        ["payment_gateway_transaction_id"],
        unique=False,
    )

    # Trigram extension + indexes for fast search
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_books_title_trgm ON books USING gin (title gin_trgm_ops)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_books_author_trgm ON books USING gin (author gin_trgm_ops)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_books_publisher_trgm ON books USING gin (publisher gin_trgm_ops)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_books_publisher_trgm"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_books_author_trgm"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_books_title_trgm"))
    op.drop_index("ix_orders_payment_gateway_transaction_id", table_name="orders")
    op.drop_index("ix_users_phone", table_name="users")
    op.alter_column("users", "phone", existing_type=sa.String(), nullable=True)
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
    op.drop_index("ix_books_has_pdf", table_name="books")
    op.drop_column("books", "has_pdf")
    op.drop_column("books", "cover_filename")
    op.drop_column("books", "original_price")
