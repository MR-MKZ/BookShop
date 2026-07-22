import enum
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Index,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

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
    original_price = Column(Numeric(10, 2), nullable=True)

    # File Management
    folder_name = Column(String, unique=True, index=True)
    cover_filename = Column(String, default="cover.jpg")
    file_filename = Column(String, nullable=True)  # e.g. Clean_Code.pdf
    file_format = Column(String, default="pdf")
    file_size = Column(String)
    edition = Column(String)
    availability = Column(String)
    amazon_link = Column(String)
    image_url = Column(String)
    has_pdf = Column(Boolean, default=False, server_default="false", index=True)

    # Status
    is_active = Column(Boolean, default=True, server_default="true")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    order_items = relationship("OrderItem", back_populates="book")

    __table_args__ = (
        Index(
            "ix_books_search_composite",
            "title",
            "author",
            "publisher",
            postgresql_using="btree",
        ),
    )

    @staticmethod
    def storage_folder(book_id: int) -> str:
        """Canonical FTP/local folder: stable ASCII id, never derived from title."""
        return f"book_{int(book_id)}"

    @staticmethod
    def storage_folder_from_isbn_or_url(isbn: str | None, url: str) -> str:
        """Pre-insert folder for scraper (id unknown yet). Prefer ISBN, else URL hash."""
        digits = re.sub(r"[^0-9Xx]", "", (isbn or "").strip())
        suffix = hashlib.md5((url or "").encode()).hexdigest()[:6]
        if len(digits) >= 10:
            return f"isbn_{digits}_{suffix}"
        return f"u_{hashlib.md5((url or digits or 'book').encode()).hexdigest()[:12]}"

    @staticmethod
    def sanitize_file_basename(name: str | None) -> str:
        """ASCII-safe basename (legacy titled files / download ASCII fallback)."""
        clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", (name or "").strip())
        clean = re.sub(r"\s+", "_", clean).strip("._")
        ascii_clean = clean.encode("ascii", "ignore").decode("ascii").strip("._")
        return (ascii_clean or "book")[:180]

    @classmethod
    def build_stored_filename(
        cls,
        book_id: int,
        ext: str | None,
        when: datetime | None = None,
    ) -> str:
        """Short on-disk/FTP name: ``219_20260712_2234.pdf``."""
        ts = (when or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M")
        extension = (ext or "pdf").lstrip(".").lower() or "pdf"
        return f"{int(book_id)}_{ts}.{extension}"

    @classmethod
    def build_legacy_titled_filename(
        cls,
        title_en: str | None,
        title: str | None,
        ext: str | None,
    ) -> str:
        """Old title-based storage names (for resolving existing files)."""
        base = cls.sanitize_file_basename(title_en or title or "book")
        extension = (ext or "pdf").lstrip(".").lower() or "pdf"
        return f"{base}.{extension}"

    # Back-compat alias used by older call sites / tests
    build_file_filename = build_legacy_titled_filename

    @classmethod
    def build_download_filename(
        cls,
        book_id: int,
        title_en: str | None,
        title: str | None,
        ext: str | None,
    ) -> str:
        """Human-readable name for Content-Disposition (may include Unicode)."""
        extension = (ext or "pdf").lstrip(".").lower() or "pdf"
        raw = (title_en or title or f"book_{book_id}").strip()
        clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", raw)
        clean = re.sub(r"\s+", " ", clean).strip(" .")[:120]
        if not clean:
            clean = f"book_{book_id}"
        return f"{clean}.{extension}"

    @staticmethod
    def content_disposition(download_name: str, book_id: int | None = None) -> str:
        """RFC 5987 Content-Disposition with UTF-8 filename*."""
        if "." in download_name:
            stem, ext = download_name.rsplit(".", 1)
        else:
            stem, ext = download_name, "bin"
        ascii_stem = stem.encode("ascii", "ignore").decode("ascii")
        ascii_stem = re.sub(r'["\\\r\n]', "_", ascii_stem).strip(" ._") or (
            f"book_{book_id}" if book_id else "book"
        )
        ascii_name = f"{ascii_stem}.{ext}"
        encoded = quote(download_name, safe="")
        return (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded}"
        )

    @property
    def pdf_filename(self) -> str:
        if self.file_filename:
            return self.file_filename
        # Legacy uploads before titled / id filenames
        return f"book.{self.file_format or 'pdf'}"

    @property
    def download_filename(self) -> str:
        return self.build_download_filename(
            self.id, self.title_en, self.title, self.file_format
        )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    username = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=False)
    first_name = Column(String, nullable=False, default="")
    last_name = Column(String, nullable=False, default="")
    full_name = Column(String, nullable=True)  # kept for backward compatibility
    phone = Column(String, unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True)
    role = Column(
        SQLEnum(UserRole, name="userrole", values_callable=lambda x: [e.value for e in x]),
        default=UserRole.USER,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    orders = relationship("Order", back_populates="user")
    download_links = relationship("DownloadLink", back_populates="user")

    @property
    def display_name(self) -> str:
        name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        if name:
            return name
        return self.full_name or self.phone or "کاربر"


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(
        SQLEnum(
            OrderStatus,
            name="orderstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=OrderStatus.PENDING,
    )
    total_amount = Column(Numeric(10, 2))
    payment_gateway_transaction_id = Column(String, nullable=True, index=True)
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


class ScraperRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ScraperRun(Base):
    __tablename__ = "scraper_runs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(
        SQLEnum(
            ScraperRunStatus,
            name="scraperrunstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=ScraperRunStatus.RUNNING,
        nullable=False,
        index=True,
    )
    mode = Column(String, nullable=True)
    pages_total = Column(Integer, default=0)
    pages_done = Column(Integer, default=0)
    books_saved = Column(Integer, default=0)
    books_skipped = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    pid = Column(Integer, nullable=True)
    hostname = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class HeroSlide(Base):
    """Landing page hero/cover slides (admin-managed)."""

    __tablename__ = "hero_slides"

    id = Column(Integer, primary_key=True, index=True)
    image_filename = Column(String, nullable=False)
    title = Column(String, nullable=True)
    sort_order = Column(Integer, default=0, server_default="0", nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    @property
    def image_url(self) -> str:
        return f"/media/proxy/hero/{self.image_filename}"


class AppSetting(Base):
    """Simple key/value site settings (e.g. hero carousel interval)."""

    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


HERO_CAROUSEL_SECONDS_KEY = "hero_carousel_seconds"
HERO_CAROUSEL_SECONDS_DEFAULT = 10
HERO_FOLDER = "hero"
HERO_RECOMMENDED_SIZE = "1920 × 700"
HERO_MIN_SIZE = "1338 × 600"
