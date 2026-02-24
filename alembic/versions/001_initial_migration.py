"""Initial migration

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2023-10-27 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Use raw SQL to create types safely
    # This avoids sqlalchemy trying to create them again when they are used in columns
    conn = op.get_bind()

    # UserRole Enum
    try:
        conn.execute(sa.text("CREATE TYPE userrole AS ENUM ('ADMIN', 'USER')"))
    except Exception:
        # Ignore if exists (Checking via exception is safer across different PG versions/drivers than querying pg_type manually here)
        pass

    # OrderStatus Enum
    try:
        conn.execute(sa.text("CREATE TYPE orderstatus AS ENUM ('PENDING', 'PAID', 'FAILED', 'CANCELLED')"))
    except Exception:
        pass

    # Users
    op.create_table('users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('username', sa.String(), nullable=True),
        sa.Column('hashed_password', sa.String(), nullable=True),
        sa.Column('full_name', sa.String(), nullable=True),
        sa.Column('phone', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        # Use postgresql.ENUM with create_type=False since we handled creation above
        sa.Column('role', postgresql.ENUM('ADMIN', 'USER', name='userrole', create_type=False), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)

    # Books
    op.create_table('books',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('author', sa.String(), nullable=True),
        sa.Column('publisher', sa.String(), nullable=True),
        sa.Column('isbn', sa.String(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('folder_name', sa.String(), nullable=True),
        sa.Column('file_format', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_books_author'), 'books', ['author'], unique=False)
    op.create_index(op.f('ix_books_folder_name'), 'books', ['folder_name'], unique=True)
    op.create_index(op.f('ix_books_id'), 'books', ['id'], unique=False)
    op.create_index(op.f('ix_books_isbn'), 'books', ['isbn'], unique=False)
    op.create_index(op.f('ix_books_publisher'), 'books', ['publisher'], unique=False)
    op.create_index(op.f('ix_books_title'), 'books', ['title'], unique=False)
    op.create_index('ix_books_search_composite', 'books', ['title', 'author', 'publisher'], unique=False, postgresql_using='btree')

    # Orders
    op.create_table('orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        # Use postgresql.ENUM with create_type=False
        sa.Column('status', postgresql.ENUM('PENDING', 'PAID', 'FAILED', 'CANCELLED', name='orderstatus', create_type=False), nullable=True),
        sa.Column('total_amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('payment_gateway_transaction_id', sa.String(), nullable=True),
        sa.Column('payment_gateway_ref_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_orders_id'), 'orders', ['id'], unique=False)

    # Order Items
    op.create_table('order_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('book_id', sa.Integer(), nullable=True),
        sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['book_id'], ['books.id'], ),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_order_items_id'), 'order_items', ['id'], unique=False)

    # Download Links
    op.create_table('download_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('book_id', sa.Integer(), nullable=True),
        sa.Column('is_used', sa.Boolean(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['book_id'], ['books.id'], ),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_download_links_id'), 'download_links', ['id'], unique=False)
    op.create_index(op.f('ix_download_links_token'), 'download_links', ['token'], unique=True)

    # Carts
    op.create_table('carts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('session_id', sa.String(), nullable=True),
        sa.Column('book_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['book_id'], ['books.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_carts_id'), 'carts', ['id'], unique=False)
    op.create_index(op.f('ix_carts_session_id'), 'carts', ['session_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_carts_session_id'), table_name='carts')
    op.drop_index(op.f('ix_carts_id'), table_name='carts')
    op.drop_table('carts')
    op.drop_index(op.f('ix_download_links_token'), table_name='download_links')
    op.drop_index(op.f('ix_download_links_id'), table_name='download_links')
    op.drop_table('download_links')
    op.drop_index(op.f('ix_order_items_id'), table_name='order_items')
    op.drop_table('order_items')
    op.drop_index(op.f('ix_orders_id'), table_name='orders')
    op.drop_table('orders')
    op.drop_index('ix_books_search_composite', table_name='books')
    op.drop_index(op.f('ix_books_title'), table_name='books')
    op.drop_index(op.f('ix_books_publisher'), table_name='books')
    op.drop_index(op.f('ix_books_isbn'), table_name='books')
    op.drop_index(op.f('ix_books_id'), table_name='books')
    op.drop_index(op.f('ix_books_folder_name'), table_name='books')
    op.drop_index(op.f('ix_books_author'), table_name='books')
    op.drop_table('books')
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_index(op.f('ix_users_id'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')

    # Drop types if needed (careful in production)
    # op.execute("DROP TYPE IF EXISTS userrole")
    # op.execute("DROP TYPE IF EXISTS orderstatus")
