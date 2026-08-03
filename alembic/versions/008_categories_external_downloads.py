"""Categories, external file URL, download links, order item snapshot

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-08-03 21:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "h8i9j0k1l2m3"
down_revision = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("books", sa.Column("external_file_url", sa.Text(), nullable=True))

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_filename", sa.String(), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "show_on_home",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_categories_id", "categories", ["id"])
    op.create_index("ix_categories_name", "categories", ["name"])
    op.create_index("ix_categories_slug", "categories", ["slug"], unique=True)

    op.create_table(
        "book_categories",
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("book_id", "category_id"),
    )

    op.add_column("order_items", sa.Column("book_title", sa.String(), nullable=True))

    # Backfill titles from books before relaxing FK
    op.execute(
        """
        UPDATE order_items AS oi
        SET book_title = COALESCE(
            NULLIF(TRIM(b.title_en), ''),
            NULLIF(TRIM(b.title), ''),
            'کتاب'
        )
        FROM books AS b
        WHERE oi.book_id = b.id AND oi.book_title IS NULL
        """
    )

    # Recreate order_items.book_id FK as nullable ON DELETE SET NULL
    op.alter_column("order_items", "book_id", existing_type=sa.Integer(), nullable=True)
    op.drop_constraint("order_items_book_id_fkey", "order_items", type_="foreignkey")
    op.create_foreign_key(
        "order_items_book_id_fkey",
        "order_items",
        "books",
        ["book_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Expand download_links
    op.add_column(
        "download_links",
        sa.Column(
            "download_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "download_links",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("download_links", sa.Column("note", sa.String(), nullable=True))

    op.alter_column("download_links", "order_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("download_links", "book_id", existing_type=sa.Integer(), nullable=True)

    op.drop_constraint("download_links_book_id_fkey", "download_links", type_="foreignkey")
    op.create_foreign_key(
        "download_links_book_id_fkey",
        "download_links",
        "books",
        ["book_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # carts: CASCADE on book delete
    op.drop_constraint("carts_book_id_fkey", "carts", type_="foreignkey")
    op.create_foreign_key(
        "carts_book_id_fkey",
        "carts",
        "books",
        ["book_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("carts_book_id_fkey", "carts", type_="foreignkey")
    op.create_foreign_key(
        "carts_book_id_fkey",
        "carts",
        "books",
        ["book_id"],
        ["id"],
    )

    op.drop_constraint("download_links_book_id_fkey", "download_links", type_="foreignkey")
    op.create_foreign_key(
        "download_links_book_id_fkey",
        "download_links",
        "books",
        ["book_id"],
        ["id"],
    )
    op.alter_column("download_links", "book_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("download_links", "order_id", existing_type=sa.Integer(), nullable=True)
    op.drop_column("download_links", "note")
    op.drop_column("download_links", "revoked_at")
    op.drop_column("download_links", "download_count")

    op.drop_constraint("order_items_book_id_fkey", "order_items", type_="foreignkey")
    op.create_foreign_key(
        "order_items_book_id_fkey",
        "order_items",
        "books",
        ["book_id"],
        ["id"],
    )
    op.alter_column("order_items", "book_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("order_items", "book_title")

    op.drop_table("book_categories")
    op.drop_index("ix_categories_slug", table_name="categories")
    op.drop_index("ix_categories_name", table_name="categories")
    op.drop_index("ix_categories_id", table_name="categories")
    op.drop_table("categories")

    op.drop_column("books", "external_file_url")
