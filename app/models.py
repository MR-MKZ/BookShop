import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Index
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.database import Base


class UserRole(str, enum.Enum):
    # Values must match PostgreSQL enum labels from Alembic migration
    ADMIN = "ADMIN"
    USER = "USER"


class OrderStatus(str, enum.Enum):
    # Values must match PostgreSQL enum labels from Alembic migration
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True)
    title = Column(String, index=True)
    title_en = Column(String)
    author = Column(String, index=True)
    publisher = Column(String, index=True)
    isbn = Column(String, index=True)
    publish_year = Column(String)
    language = Column(String)
    pages = Column(String)
    description = Column(Text)
    price = Column(Numeric(10, 2))

    # File Management
    folder_name = Column(String, unique=True, index=True)
    file_format = Column(String, default="pdf")
    file_size = Column(String)
    edition = Column(String)
    availability = Column(String)
    amazon_link = Column(String)
    image_url = Column(String)

    # Status
    # Ensure server_default is set so raw SQL inserts get true if omitted
    is_active = Column(Boolean, default=True, server_default="true")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    order_items = relationship("OrderItem", back_populates="book")

    # Composite Index for Search
    __table_args__ = (
        Index('ix_books_search_composite', 'title', 'author', 'publisher', postgresql_using='btree'),
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    full_name = Column(String)
    phone = Column(String)
    is_active = Column(Boolean, default=True)
    role = Column(
        SQLEnum(UserRole, name="userrole", values_callable=lambda x: [e.value for e in x]),
        default=UserRole.USER,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    orders = relationship("Order", back_populates="user")
    download_links = relationship("DownloadLink", back_populates="user")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(
        SQLEnum(
            OrderStatus,
            name="orderstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=OrderStatus.PENDING,
    )
    total_amount = Column(Numeric(10, 2))
    payment_gateway_transaction_id = Column(String, nullable=True)
    payment_gateway_ref_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")
    download_links = relationship("DownloadLink", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    book_id = Column(Integer, ForeignKey("books.id"))
    price = Column(Numeric(10, 2))
    quantity = Column(Integer, default=1)

    # Relationships
    order = relationship("Order", back_populates="items")
    book = relationship("Book", back_populates="order_items")


class DownloadLink(Base):
    __tablename__ = "download_links"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    order_id = Column(Integer, ForeignKey("orders.id"))
    book_id = Column(Integer, ForeignKey("books.id"))
    is_used = Column(Boolean, default=False)
    expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    used_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="download_links")
    order = relationship("Order", back_populates="download_links")
    book = relationship("Book")


class Cart(Base):
    __tablename__ = "carts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    session_id = Column(String, index=True, nullable=True)
    book_id = Column(Integer, ForeignKey("books.id"))
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
