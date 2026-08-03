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
    Table,
    Text,
    Index,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


book_categories = Table(
    "book_categories",
    Base.metadata,
    Column(
        "book_id",
        Integer,
        ForeignKey("books.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "category_id",
        Integer,
        ForeignKey("categories.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


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


class DiscountType(str, enum.Enum):
    PERCENT = "PERCENT"
    FIXED = "FIXED"


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True)
    title = Column(String, index=True)
    title_en = Column(String)
    slug = Column(String, unique=True, index=True, nullable=True)
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
    # Direct host URL (never exposed to clients; streamed via media proxy)
    external_file_url = Column(Text, nullable=True)

    # Status
    is_active = Column(Boolean, default=True, server_default="true")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    order_items = relationship("OrderItem", back_populates="book")
    categories = relationship(
        "Category",
        secondary=book_categories,
        back_populates="books",
    )

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

    @staticmethod
    def slugify_title(name: str | None, max_len: int = 80) -> str:
        """SEO path segment from English title: The_World_of_Scary_Video."""
        text = (name or "").strip()
        text = re.sub(r"[^\w\s\-]+", "", text, flags=re.UNICODE)
        text = re.sub(r"[\s\-]+", "_", text).strip("._")
        ascii_text = text.encode("ascii", "ignore").decode("ascii").strip("._")
        parts = [p for p in ascii_text.split("_") if p]
        base = "_".join(parts)[:max_len].strip("_")
        return base or "book"

    @classmethod
    def build_slug(
        cls,
        title_en: str | None,
        title: str | None = None,
        book_id: int | None = None,
        unique_key: str | None = None,
    ) -> str:
        """Unique storefront slug from English title; append id (or key) for uniqueness."""
        base = cls.slugify_title(title_en or title)
        if book_id is not None:
            return f"{base}_{int(book_id)}"
        if unique_key:
            suffix = hashlib.md5(unique_key.encode()).hexdigest()[:8]
            return f"{base}_{suffix}"
        return base

    @property
    def display_title(self) -> str:
        """Listing / card title — English first (main catalog titles)."""
        return (self.title_en or self.title or "بدون عنوان").strip() or "بدون عنوان"

    @property
    def seo_title_name(self) -> str:
        return (self.title_en or self.title or "بدون عنوان").strip() or "بدون عنوان"

    @property
    def path(self) -> str:
        """Public detail URL path (/book/The_World_…_42)."""
        if self.slug:
            return f"/book/{self.slug}"
        if self.id:
            return f"/book/{self.build_slug(self.title_en, self.title, book_id=self.id)}"
        return f"/book/{self.id}" if self.id else "/search"

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
        """RFC 5987 Content-Disposition with UTF-8 filename*.

        The ``filename=`` fallback MUST be latin-1/ASCII-safe (Starlette encodes
        header values as latin-1). Unicode belongs only in ``filename*=``.
        """
        raw = (download_name or "").strip() or (
            f"book_{book_id}.bin" if book_id else "book.bin"
        )
        if "." in raw:
            stem, ext = raw.rsplit(".", 1)
        else:
            stem, ext = raw, "bin"
        ascii_stem = stem.encode("ascii", "ignore").decode("ascii")
        ascii_stem = re.sub(r'["\\\r\n]', "_", ascii_stem).strip(" ._") or (
            f"book_{book_id}" if book_id else "book"
        )
        ascii_ext = ext.encode("ascii", "ignore").decode("ascii")
        ascii_ext = re.sub(r'[^A-Za-z0-9]+', "", ascii_ext).lower() or "bin"
        ascii_name = f"{ascii_stem}.{ascii_ext}"
        # Percent-encode the full UTF-8 name for filename*
        encoded = quote(raw, safe="")
        header = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded}"
        )
        # Final guard: ASGI requires latin-1 header values
        try:
            header.encode("latin-1")
        except UnicodeEncodeError:
            fallback = f"book_{book_id}.{ascii_ext}" if book_id else f"book.{ascii_ext}"
            header = (
                f'attachment; filename="{fallback}"; '
                f"filename*=UTF-8''{encoded}"
            )
        return header

    def ensure_slug(self) -> str:
        """Set slug from title_en/id if missing; return current slug."""
        if self.slug:
            return self.slug
        self.slug = self.build_slug(self.title_en, self.title, book_id=self.id)
        return self.slug

    @property
    def detail_path(self) -> str:
        """Public book URL path (/book/The_World_…_42)."""
        return self.path

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

    @property
    def has_file_ready(self) -> bool:
        """True when a local upload or external download URL is configured."""
        if self.external_file_url and str(self.external_file_url).strip():
            return True
        if self.file_filename:
            return True
        return bool(self.has_pdf)

    def sync_has_pdf(self) -> None:
        """Keep has_pdf aligned with local file or external URL presence."""
        self.has_pdf = bool(
            (self.external_file_url and str(self.external_file_url).strip())
            or self.file_filename
        )


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    image_filename = Column(String, nullable=True)
    sort_order = Column(Integer, default=0, server_default="0", nullable=False)
    show_on_home = Column(Boolean, default=False, server_default="false", nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    books = relationship(
        "Book",
        secondary=book_categories,
        back_populates="categories",
    )

    @staticmethod
    def slugify(name: str | None, max_len: int = 80) -> str:
        text = (name or "").strip()
        text = re.sub(r"[^\w\s\-]+", "", text, flags=re.UNICODE)
        text = re.sub(r"[\s\-]+", "-", text).strip("-_")
        ascii_text = text.encode("ascii", "ignore").decode("ascii").strip("-_")
        if ascii_text:
            return ascii_text[:max_len].strip("-_") or "category"
        # Persian / non-ASCII: hash-based fallback for URL safety
        digest = hashlib.md5((name or "category").encode()).hexdigest()[:10]
        return f"cat-{digest}"

    @property
    def path(self) -> str:
        return f"/category/{self.slug}"

    @property
    def image_url(self) -> str | None:
        if not self.image_filename:
            return None
        return f"/media/proxy/category/{self.image_filename}"


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


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    discount_type = Column(
        SQLEnum(
            DiscountType,
            name="discounttype",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    amount = Column(Numeric(10, 2), nullable=False)
    max_uses = Column(Integer, nullable=True)
    used_count = Column(Integer, default=0, server_default="0", nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
    min_order_amount = Column(Numeric(10, 2), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    redemptions = relationship("CouponRedemption", back_populates="coupon")
    orders = relationship("Order", back_populates="coupon")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(
        SQLEnum(
            OrderStatus,
            name="orderstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=OrderStatus.PENDING,
    )
    total_amount = Column(Numeric(10, 2))
    subtotal_amount = Column(Numeric(10, 2), nullable=True)
    discount_amount = Column(Numeric(10, 2), nullable=True, default=0)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=True)
    billing_first_name = Column(String, nullable=True)
    billing_last_name = Column(String, nullable=True)
    billing_phone = Column(String, nullable=True, index=True)
    billing_email = Column(String, nullable=True, index=True)
    customer_note = Column(Text, nullable=True)
    access_token = Column(String, unique=True, index=True, nullable=True)
    payment_gateway = Column(String, nullable=True)
    payment_gateway_transaction_id = Column(String, nullable=True, index=True)
    payment_gateway_ref_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")
    download_links = relationship("DownloadLink", back_populates="order")
    coupon = relationship("Coupon", back_populates="orders")
    coupon_redemption = relationship(
        "CouponRedemption", back_populates="order", uselist=False
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    book_id = Column(
        Integer,
        ForeignKey("books.id", ondelete="SET NULL"),
        nullable=True,
    )
    book_title = Column(String, nullable=True)
    price = Column(Numeric(10, 2))
    quantity = Column(Integer, default=1)

    # Relationships
    order = relationship("Order", back_populates="items")
    book = relationship("Book", back_populates="order_items")

    @property
    def display_title(self) -> str:
        if self.book is not None:
            return self.book.display_title
        return (self.book_title or "کتاب حذف‌شده").strip() or "کتاب حذف‌شده"


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"

    id = Column(Integer, primary_key=True, index=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    coupon = relationship("Coupon", back_populates="redemptions")
    order = relationship("Order", back_populates="coupon_redemption")


class DownloadLink(Base):
    __tablename__ = "download_links"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    book_id = Column(
        Integer,
        ForeignKey("books.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_used = Column(Boolean, default=False)
    download_count = Column(Integer, default=0, server_default="0", nullable=False)
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(String, nullable=True)
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
    book_id = Column(
        Integer,
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
    )
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

DOWNLOAD_LINK_TTL_HOURS_KEY = "download_link_ttl_hours"
DOWNLOAD_LINK_TTL_HOURS_DEFAULT = 6

PENDING_FILE_CUSTOMER_MESSAGE_KEY = "pending_file_customer_message"
PENDING_FILE_CUSTOMER_MESSAGE_DEFAULT = (
    "فایل کتاب «{book_title}» هنوز آماده نیست. لطفاً در پیامرسان با پشتیبانی تماس بگیرید "
    "و شماره سفارش {order_id} و شماره تماس {phone} را ارسال کنید تا فایل برایتان آماده شود."
)

HOME_CATEGORY_BOOKS_LIMIT_KEY = "home_category_books_limit"
HOME_CATEGORY_BOOKS_LIMIT_DEFAULT = 8

CATEGORY_FOLDER = "categories"
