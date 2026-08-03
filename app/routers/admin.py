"""Admin panel: books, users, orders, reports, files, scraper status."""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import ceil

import aioftp
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, cast, desc, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user_optional, get_password_hash
from app.config import settings
from app.database import get_async_db
from app.models import (
    HERO_CAROUSEL_SECONDS_DEFAULT,
    HERO_CAROUSEL_SECONDS_KEY,
    HERO_FOLDER,
    HERO_MIN_SIZE,
    HERO_RECOMMENDED_SIZE,
    DOWNLOAD_LINK_TTL_HOURS_DEFAULT,
    DOWNLOAD_LINK_TTL_HOURS_KEY,
    PENDING_FILE_CUSTOMER_MESSAGE_DEFAULT,
    PENDING_FILE_CUSTOMER_MESSAGE_KEY,
    AppSetting,
    Book,
    Category,
    Coupon,
    DiscountType,
    DownloadLink,
    HeroSlide,
    Order,
    OrderItem,
    OrderStatus,
    ScraperRun,
    ScraperRunStatus,
    User,
    UserRole,
    book_categories,
)
from app.routers.media import check_file_exists, signer
from app.services.checkout_helpers import get_download_ttl_hours
from app.services.downloads import (
    create_managed_download_link,
    download_url,
    make_download_token,
)
from app.utils.phone import validate_iran_phone
from app.utils.datetime_fa import (
    ORDER_STATUS_FA,
    format_jalali,
    order_status_badge_class,
    order_status_fa,
)


class AdminAuthRedirect(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


async def require_admin(
    request: Request,
    current_user: User | None = Depends(get_current_user_optional),
) -> User:
    if not current_user:
        raise AdminAuthRedirect(request.url.path)
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="دسترسی ادمین لازم است")
    return current_user


router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["jalali"] = format_jalali
templates.env.filters["order_status_fa"] = order_status_fa
templates.env.filters["order_status_badge"] = order_status_badge_class

PAGE_SIZE = 30

ALLOWED_BOOK_EXTS = {
    "pdf",
    "epub",
    "mobi",
    "azw",
    "azw3",
    "fb2",
    "djvu",
    "txt",
    "rar",
    "zip",
    "7z",
    "bin",
}

ALLOWED_COVER_EXTS = {"jpg", "jpeg", "png", "webp"}


def _parse_date(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if end_of_day:
        dt = dt + timedelta(days=1) - timedelta(microseconds=1)
    return dt


CHART_RANGE_PRESETS: list[dict] = [
    {"key": "24h", "label": "۲۴ ساعت", "hours": 24},
    {"key": "7d", "label": "۷ روز", "hours": 24 * 7},
    {"key": "14d", "label": "۱۴ روز", "hours": 24 * 14},
    {"key": "1m", "label": "۱ ماه", "hours": 24 * 30},
    {"key": "2m", "label": "۲ ماه", "hours": 24 * 60},
    {"key": "3m", "label": "۳ ماه", "hours": 24 * 90},
    {"key": "6m", "label": "۶ ماه", "hours": 24 * 180},
    {"key": "12m", "label": "۱۲ ماه", "hours": 24 * 365},
]


def _resolve_chart_range(
    range_key: str = "",
    date_from: str = "",
    date_to: str = "",
) -> tuple[datetime, datetime, str]:
    """Return (start, end, resolved_key) in UTC for chart/report filters."""
    now = datetime.now(timezone.utc)
    key = (range_key or "").strip().lower()

    # Custom dates win when both provided, or when range=custom
    if key == "custom" or (date_from and date_to and key in ("", "custom")):
        start = _parse_date(date_from) or (now - timedelta(days=7))
        end = _parse_date(date_to, end_of_day=True) or now
        if end < start:
            start, end = end, start
        return start, end, "custom"

    preset = next((p for p in CHART_RANGE_PRESETS if p["key"] == key), None)
    if preset is None:
        preset = next(p for p in CHART_RANGE_PRESETS if p["key"] == "7d")
    end = now
    start = now - timedelta(hours=int(preset["hours"]))
    return start, end, str(preset["key"])


def _chart_bucket_unit(start: datetime, end: datetime) -> str:
    span = end - start
    if span <= timedelta(hours=36):
        return "hour"
    if span <= timedelta(days=45):
        return "day"
    if span <= timedelta(days=180):
        return "week"
    return "month"


def _bucket_step(unit: str) -> timedelta:
    if unit == "hour":
        return timedelta(hours=1)
    if unit == "day":
        return timedelta(days=1)
    if unit == "week":
        return timedelta(weeks=1)
    return timedelta(days=30)


def _align_bucket(dt: datetime, unit: str) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if unit == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        # Monday-aligned like Postgres date_trunc('week')
        day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_bucket(dt: datetime, unit: str) -> datetime:
    if unit == "month":
        y, m = dt.year, dt.month + 1
        if m > 12:
            y, m = y + 1, 1
        return dt.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt + _bucket_step(unit)


def _bucket_label(dt: datetime, unit: str) -> str:
    from app.utils.datetime_fa import format_jalali

    if unit == "hour":
        return format_jalali(dt, with_time=True)
    label = format_jalali(dt, with_time=False)
    if unit == "month":
        return label[:7]  # YYYY/MM
    return label


async def _sales_series(
    db: AsyncSession, start: datetime, end: datetime
) -> dict:
    """Time series of paid order totals for a line chart."""
    unit = _chart_bucket_unit(start, end)
    paid_filter = and_(
        Order.status == OrderStatus.PAID,
        Order.paid_at >= start,
        Order.paid_at <= end,
    )
    bucket_col = func.date_trunc(unit, Order.paid_at).label("bucket")
    result = await db.execute(
        select(
            bucket_col,
            func.coalesce(func.sum(Order.total_amount), 0),
            func.count(Order.id),
        )
        .where(paid_filter)
        .group_by(bucket_col)
        .order_by(bucket_col)
    )
    by_bucket: dict[datetime, tuple[float, int]] = {}
    for row in result.all():
        raw_dt, amount, count = row[0], row[1], row[2]
        if raw_dt is None:
            continue
        if getattr(raw_dt, "tzinfo", None) is None:
            raw_dt = raw_dt.replace(tzinfo=timezone.utc)
        key = _align_bucket(raw_dt, unit)
        by_bucket[key] = (float(amount or 0), int(count or 0))

    labels: list[str] = []
    sales: list[float] = []
    orders: list[int] = []
    cursor = _align_bucket(start, unit)
    end_aligned = _align_bucket(end, unit)
    # Include the end bucket
    while cursor <= end_aligned:
        amount, count = by_bucket.get(cursor, (0.0, 0))
        labels.append(_bucket_label(cursor, unit))
        sales.append(round(amount, 0))
        orders.append(count)
        cursor = _next_bucket(cursor, unit)
        if len(labels) > 400:  # safety
            break

    total_sales = sum(sales)
    total_orders = sum(orders)
    return {
        "labels": labels,
        "sales": sales,
        "orders": orders,
        "unit": unit,
        "total_sales": total_sales,
        "total_orders": total_orders,
    }


async def _top_books_series(
    db: AsyncSession, start: datetime, end: datetime, limit: int = 10
) -> dict:
    """Best-selling books for a bar chart."""
    paid_filter = and_(
        Order.status == OrderStatus.PAID,
        Order.paid_at >= start,
        Order.paid_at <= end,
    )
    result = await db.execute(
        select(
            Book.id,
            Book.title,
            func.count(OrderItem.id).label("sold"),
            func.coalesce(func.sum(OrderItem.price), 0).label("revenue"),
        )
        .join(OrderItem, OrderItem.book_id == Book.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(paid_filter)
        .group_by(Book.id, Book.title)
        .order_by(desc("sold"), desc("revenue"))
        .limit(limit)
    )
    rows = result.all()
    labels: list[str] = []
    sold: list[int] = []
    revenue: list[float] = []
    books: list[dict] = []
    for book_id, title, sold_count, rev in rows:
        full = title or f"#{book_id}"
        short = full[:18]
        if len(full) > 18:
            short += "…"
        labels.append(short)
        sold.append(int(sold_count or 0))
        revenue.append(float(rev or 0))
        books.append(
            {
                "id": book_id,
                "title": full,
                "sold": int(sold_count or 0),
                "revenue": float(rev or 0),
            }
        )
    return {"labels": labels, "sold": sold, "revenue": revenue, "books": books}


_FTP_SAFE_FOLDER = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _is_ftp_safe_folder(name: str | None) -> bool:
    """vsftpd rejects CWD/STOR for BOM / non-ASCII / odd title-derived folders."""
    return bool(name and _FTP_SAFE_FOLDER.fullmatch(name))


def _file_ext(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower().strip()


def _parse_book_ids(raw: str, *, max_ids: int = 50_000) -> list[int]:
    """Parse `12, 45, 100-120` (ranges inclusive). Caps total unique ids."""
    ids: list[int] = []
    for part in re.split(r"[\s,;]+", (raw or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            # Soft-cap a single range so a typo like 1-9999999 cannot explode memory
            if end - start + 1 > max_ids:
                end = start + max_ids - 1
            ids.extend(range(start, end + 1))
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
            if len(out) >= max_ids:
                break
    return out


def _book_search_filters(q: str):
    """Build ILIKE / id filters for admin book search."""
    q = (q or "").strip()
    if not q:
        return []
    filters = []
    # Exact id match only when value fits PostgreSQL INTEGER (ISBNs overflow int32).
    if q.isdigit():
        book_id = int(q)
        if book_id <= 2_147_483_647:
            filters.append(Book.id == book_id)
    pattern = f"%{q}%"
    filters.append(
        or_(
            Book.title.ilike(pattern),
            Book.title_en.ilike(pattern),
            Book.author.ilike(pattern),
            Book.publisher.ilike(pattern),
            Book.isbn.ilike(pattern),
            Book.folder_name.ilike(pattern),
            Book.url.ilike(pattern),
            cast(Book.id, String).ilike(pattern),
        )
    )
    return [or_(*filters)] if filters else []


async def _owned_book_ids(db: AsyncSession, user_id: int) -> set[int]:
    result = await db.execute(
        select(OrderItem.book_id)
        .join(Order)
        .where(
            and_(
                Order.user_id == user_id,
                Order.status == OrderStatus.PAID,
            )
        )
    )
    return {row[0] for row in result.all() if row[0] is not None}


async def _user_library(db: AsyncSession, user_id: int) -> list[Book]:
    # Subquery avoids DISTINCT + ORDER BY paid_at (Postgres rejects that combo).
    paid_book_ids = (
        select(OrderItem.book_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            and_(
                Order.user_id == user_id,
                Order.status == OrderStatus.PAID,
            )
        )
        .distinct()
    )
    result = await db.execute(
        select(Book)
        .where(Book.id.in_(paid_book_ids))
        .order_by(desc(Book.id))
    )
    return list(result.scalars().all())


def _ftp_client():
    from app.services.ftp_client import ftp_client

    return ftp_client()


async def _try_migrate_folder_files(
    old_folder: str, new_folder: str, filenames: list[str]
) -> None:
    """Best-effort copy of known files into the new folder (FTP or local)."""
    names = [n for n in filenames if n]
    if not names or old_folder == new_folder:
        return

    if not settings.FTP_ENABLED:
        old_dir = os.path.join(settings.MEDIA_ROOT, old_folder)
        new_dir = os.path.join(settings.MEDIA_ROOT, new_folder)
        if not os.path.isdir(old_dir):
            return
        os.makedirs(new_dir, exist_ok=True)
        for name in names:
            src = os.path.join(old_dir, name)
            dst = os.path.join(new_dir, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.rename(src, dst)
        return

    async with _ftp_client() as client:
        try:
            await client.make_directory(new_folder)
        except Exception:
            pass
        for name in names:
            try:
                await client.rename(f"{old_folder}/{name}", f"{new_folder}/{name}")
            except Exception:
                pass


async def _ensure_storage_folder(book: Book) -> str:
    """
    Return a folder vsftpd can enter. Legacy title-based folders (BOM/Persian)
    are reassigned to book_{id}.
    """
    if _is_ftp_safe_folder(book.folder_name):
        return book.folder_name  # type: ignore[return-value]

    new_folder = Book.storage_folder(book.id)
    old_folder = book.folder_name
    if old_folder and old_folder != new_folder:
        candidates = [
            book.cover_filename or "cover.jpg",
            book.file_filename or "",
            book.pdf_filename,
        ]
        await _try_migrate_folder_files(old_folder, new_folder, candidates)
    book.folder_name = new_folder
    return new_folder


async def _upload_book_file(folder_name: str, filename: str, data: bytes) -> None:
    if not folder_name:
        raise ValueError("book folder_name is required for upload")

    if not settings.FTP_ENABLED:
        dest_dir = os.path.join(settings.MEDIA_ROOT, folder_name)
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        return

    # Match scraper: mkdir → cwd → STOR basename.
    # vsftpd returns 553 on STOR with nested paths (folder/file).
    async with _ftp_client() as client:
        try:
            await client.make_directory(folder_name)
        except Exception:
            pass
        await client.change_directory(folder_name)
        async with client.upload_stream(filename) as stream:
            await stream.write(data)


async def _delete_book_file(folder_name: str, filename: str) -> None:
    if not settings.FTP_ENABLED:
        path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)
        if os.path.exists(path):
            os.remove(path)
        return

    async with _ftp_client() as client:
        remote = f"{folder_name}/{filename}"
        try:
            await client.remove(remote)
        except Exception:
            pass


async def _rename_book_file(folder_name: str, old_name: str, new_name: str) -> bool:
    """Rename stored ebook file. Returns True if rename succeeded."""
    if not old_name or not new_name or old_name == new_name:
        return old_name == new_name

    if not settings.FTP_ENABLED:
        dest_dir = os.path.join(settings.MEDIA_ROOT, folder_name)
        old_path = os.path.join(dest_dir, old_name)
        new_path = os.path.join(dest_dir, new_name)
        if not os.path.exists(old_path):
            return False
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(new_path):
            os.remove(new_path)
        os.rename(old_path, new_path)
        return True

    async with _ftp_client() as client:
        old_remote = f"{folder_name}/{old_name}"
        new_remote = f"{folder_name}/{new_name}"
        try:
            await client.rename(old_remote, new_remote)
            return True
        except Exception:
            return False


async def _resolve_stored_filename(book: Book) -> str | None:
    """Find the actual ebook file on storage (titled name, stored name, or legacy)."""
    if not book.folder_name:
        return None
    candidates: list[str] = []
    if book.file_filename:
        candidates.append(book.file_filename)
    titled = Book.build_legacy_titled_filename(
        book.title_en, book.title, book.file_format
    )
    if titled not in candidates:
        candidates.append(titled)
    legacy = f"book.{book.file_format or 'pdf'}"
    if legacy not in candidates:
        candidates.append(legacy)
    for name in candidates:
        if await check_file_exists(book.folder_name, name):
            return name
    return None


def _parse_price(price: str, original_price: str) -> tuple[Decimal, Decimal] | str:
    from app.utils.price import round_toman

    try:
        price_val = round_toman(Decimal(price.replace(",", "") or "0"))
        orig_val = (
            round_toman(Decimal(original_price.replace(",", "")))
            if original_price.strip()
            else price_val + Decimal("35000")
        )
        if orig_val <= price_val and price_val > 0:
            orig_val = price_val + Decimal("35000")
        return price_val, orig_val
    except InvalidOperation:
        return "قیمت نامعتبر است"


# Dashboard


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    range_key: str = Query("7d", alias="range"),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    books_count = await db.scalar(select(func.count()).select_from(Book)) or 0
    users_count = await db.scalar(select(func.count()).select_from(User)) or 0
    orders_count = await db.scalar(
        select(func.count()).select_from(Order).where(Order.status == OrderStatus.PAID)
    ) or 0
    missing_pdf = await db.scalar(
        select(func.count()).select_from(Book).where(Book.has_pdf == False)  # noqa: E712
    ) or 0

    sales_today = await db.scalar(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(
            and_(Order.status == OrderStatus.PAID, Order.paid_at >= today_start)
        )
    ) or 0
    sales_month = await db.scalar(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(
            and_(Order.status == OrderStatus.PAID, Order.paid_at >= month_start)
        )
    ) or 0

    latest_scraper = (
        await db.execute(select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(1))
    ).scalar_one_or_none()

    start, end, resolved_range = _resolve_chart_range(range_key, date_from, date_to)
    sales_chart = await _sales_series(db, start, end)
    top_books_chart = await _top_books_series(db, start, end, limit=10)

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "admin": admin,
            "stats": {
                "books": books_count,
                "users": users_count,
                "orders": orders_count,
                "missing_pdf": missing_pdf,
                "sales_today": sales_today,
                "sales_month": sales_month,
            },
            "latest_scraper": latest_scraper,
            "range_key": resolved_range,
            "date_from": start.strftime("%Y-%m-%d"),
            "date_to": end.strftime("%Y-%m-%d"),
            "range_presets": CHART_RANGE_PRESETS,
            "range_total_sales": sales_chart["total_sales"],
            "range_total_orders": sales_chart["total_orders"],
            "sales_chart": sales_chart,
            "top_books_chart": top_books_chart,
            "top_books": top_books_chart["books"],
        },
    )


# Books


@router.get("/books", response_class=HTMLResponse)
async def admin_books(
    request: Request,
    q: str = "",
    missing_pdf: int = 0,
    page: int = 1,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    page = max(1, page)
    filters = []
    if missing_pdf:
        filters.append(Book.has_pdf == False)  # noqa: E712
    filters.extend(_book_search_filters(q))

    where = and_(*filters) if filters else True
    total = await db.scalar(select(func.count()).select_from(Book).where(where)) or 0
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 0

    result = await db.execute(
        select(Book)
        .where(where)
        .order_by(desc(Book.created_at))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    books = result.scalars().all()

    return templates.TemplateResponse(
        "admin/books.html",
        {
            "request": request,
            "admin": admin,
            "books": books,
            "q": q,
            "missing_pdf": missing_pdf,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "message": request.query_params.get("msg"),
            "uploaded_id": request.query_params.get("uploaded_id"),
            "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
        },
    )


@router.get("/books/new", response_class=HTMLResponse)
async def admin_book_new_form(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/book_new.html",
        {
            "request": request,
            "admin": admin,
            "error": None,
            "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
            "categories": await _all_categories(db),
            "selected_category_ids": set(),
        },
    )


@router.post("/books/new")
async def admin_book_create(
    request: Request,
    title: str = Form(...),
    author: str = Form(""),
    publisher: str = Form(""),
    description: str = Form(""),
    isbn: str = Form(""),
    title_en: str = Form(""),
    price: str = Form("0"),
    original_price: str = Form(""),
    is_active: str | None = Form(None),
    cover: UploadFile | None = File(None),
    file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    async def _new_ctx(error: str, selected: set[int] | None = None):
        return {
            "request": request,
            "admin": admin,
            "error": error,
            "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
            "categories": await _all_categories(db),
            "selected_category_ids": selected or set(),
        }

    form = await request.form()
    category_ids: list[int] = []
    for raw in form.getlist("category_ids"):
        try:
            category_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    selected_ids = set(category_ids)

    title = title.strip()
    if not title:
        return templates.TemplateResponse(
            "admin/book_new.html",
            await _new_ctx("عنوان الزامی است", selected_ids),
            status_code=400,
        )

    parsed = _parse_price(price, original_price)
    if isinstance(parsed, str):
        return templates.TemplateResponse(
            "admin/book_new.html",
            await _new_ctx(parsed, selected_ids),
            status_code=400,
        )
    price_val, orig_val = parsed

    book = Book(
        url=f"manual://pending-{uuid.uuid4().hex[:12]}",
        title=title,
        title_en=title_en.strip() or None,
        author=author.strip() or None,
        publisher=publisher.strip() or None,
        isbn=isbn.strip() or None,
        description=description.strip() or None,
        price=price_val,
        original_price=orig_val,
        folder_name=None,
        cover_filename="cover.jpg",
        file_format="pdf",
        has_pdf=False,
        is_active=is_active is not None,
    )
    db.add(book)
    await db.flush()
    folder_name = Book.storage_folder(book.id)
    book.folder_name = folder_name
    book.url = f"manual://{folder_name}"
    book.ensure_slug()

    if category_ids:
        cats = list(
            (
                await db.execute(select(Category).where(Category.id.in_(category_ids)))
            )
            .scalars()
            .all()
        )
        book.categories = cats

    if cover and cover.filename:
        ext = _file_ext(cover.filename)
        if ext not in ALLOWED_COVER_EXTS:
            await db.rollback()
            return templates.TemplateResponse(
                "admin/book_new.html",
                await _new_ctx("فرمت کاور مجاز نیست (jpg/png/webp)", selected_ids),
                status_code=400,
            )
        cover_data = await cover.read()
        if cover_data:
            cover_name = f"cover.{ext if ext != 'jpeg' else 'jpg'}"
            await _upload_book_file(folder_name, cover_name, cover_data)
            book.cover_filename = cover_name

    if file and file.filename:
        ext = _file_ext(file.filename)
        if ext not in ALLOWED_BOOK_EXTS:
            await db.rollback()
            return templates.TemplateResponse(
                "admin/book_new.html",
                await _new_ctx(
                    f"فرمت فایل مجاز نیست. مجاز: {', '.join(sorted(ALLOWED_BOOK_EXTS))}",
                    selected_ids,
                ),
                status_code=400,
            )
        data = await file.read()
        if data:
            book.file_format = ext
            filename = Book.build_stored_filename(book.id, ext)
            await _upload_book_file(folder_name, filename, data)
            book.file_filename = filename
            book.file_size = f"{len(data) // 1024} KB"
            book.sync_has_pdf()

    await db.commit()
    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=created",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _all_categories(db: AsyncSession) -> list[Category]:
    return list(
        (
            await db.execute(
                select(Category).order_by(
                    Category.sort_order.asc(), Category.name.asc()
                )
            )
        )
        .scalars()
        .all()
    )


async def _book_edit_context(
    request: Request,
    db: AsyncSession,
    admin: User,
    book: Book,
    *,
    error: str | None = None,
    message: str | None = None,
    generated_link: str | None = None,
) -> dict:
    await db.refresh(book, attribute_names=["categories"])
    file_exists = False
    if book.file_filename and book.folder_name:
        file_exists = (await _resolve_stored_filename(book)) is not None
    selected_ids = {c.id for c in (book.categories or [])}
    return {
        "request": request,
        "admin": admin,
        "book": book,
        "error": error,
        "message": message or request.query_params.get("msg"),
        "file_exists": file_exists,
        "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
        "categories": await _all_categories(db),
        "selected_category_ids": selected_ids,
        "generated_link": generated_link or request.query_params.get("link"),
        "has_external": bool((book.external_file_url or "").strip()),
        "ttl_hours": await get_download_ttl_hours(db),
    }


@router.get("/books/{book_id}", response_class=HTMLResponse)
async def admin_book_edit(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(
            select(Book)
            .options(selectinload(Book.categories))
            .where(Book.id == book_id)
        )
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse(
        "admin/book_edit.html",
        await _book_edit_context(request, db, admin, book),
    )


@router.post("/books/{book_id}")
async def admin_book_save(
    book_id: int,
    request: Request,
    title: str = Form(...),
    author: str = Form(""),
    publisher: str = Form(""),
    description: str = Form(""),
    isbn: str = Form(""),
    title_en: str = Form(""),
    price: str = Form("0"),
    original_price: str = Form(""),
    is_active: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(
            select(Book)
            .options(selectinload(Book.categories))
            .where(Book.id == book_id)
        )
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    form = await request.form()
    category_ids: list[int] = []
    for raw in form.getlist("category_ids"):
        try:
            category_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    parsed = _parse_price(price, original_price)
    if isinstance(parsed, str):
        return templates.TemplateResponse(
            "admin/book_edit.html",
            await _book_edit_context(
                request, db, admin, book, error=parsed
            ),
        )
    price_val, orig_val = parsed

    book.title = title.strip()
    book.title_en = title_en.strip() or None
    book.author = author.strip() or None
    book.publisher = publisher.strip() or None
    book.isbn = isbn.strip() or None
    book.description = description.strip() or None
    book.price = price_val
    book.original_price = orig_val
    book.is_active = is_active is not None
    book.slug = Book.build_slug(book.title_en, book.title, book_id=book.id)

    if category_ids:
        cats = list(
            (
                await db.execute(
                    select(Category).where(Category.id.in_(category_ids))
                )
            )
            .scalars()
            .all()
        )
        book.categories = cats
    else:
        book.categories = []

    await db.commit()
    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _safe_admin_redirect(next_url: str | None, fallback: str) -> str:
    """Allow only relative /admin/... redirects (open-redirect safe)."""
    if not next_url:
        return fallback
    next_url = next_url.strip()
    if (
        next_url.startswith("/admin/")
        and "://" not in next_url
        and not next_url.startswith("//")
        and "\n" not in next_url
        and "\r" not in next_url
    ):
        return next_url
    return fallback


@router.post("/books/{book_id}/upload")
async def admin_upload_file(
    book_id: int,
    file: UploadFile = File(...),
    next: str = Form(""),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    ext = _file_ext(file.filename)
    if ext not in ALLOWED_BOOK_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"فرمت مجاز نیست. مجاز: {', '.join(sorted(ALLOWED_BOOK_EXTS))}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="فایل خالی است")

    if not book.id:
        raise HTTPException(status_code=400, detail="کتاب نامعتبر است")

    folder_name = await _ensure_storage_folder(book)

    # Remove previous stored file (any known name)
    if book.file_filename or book.has_pdf:
        existing = await _resolve_stored_filename(book)
        if existing:
            await _delete_book_file(folder_name, existing)

    filename = Book.build_stored_filename(book.id, ext)
    try:
        await _upload_book_file(folder_name, filename, data)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"خطا در آپلود فایل: {e}",
        ) from e
    book.file_format = ext
    book.file_filename = filename
    book.file_size = f"{len(data) // 1024} KB"
    book.sync_has_pdf()
    await db.commit()

    # List uploads stay on the list; edit-page uploads return to edit
    fallback = f"/admin/books/{book.id}?msg=uploaded"
    redirect_base = _safe_admin_redirect(next, fallback)
    if redirect_base == fallback:
        redirect_url = fallback
    else:
        sep = "&" if "?" in redirect_base else "?"
        redirect_url = f"{redirect_base}{sep}msg=uploaded&uploaded_id={book.id}"

    return RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/books/{book_id}/file/delete")
async def admin_delete_file(
    book_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    if book.folder_name:
        existing = await _resolve_stored_filename(book)
        if existing:
            await _delete_book_file(book.folder_name, existing)

    book.file_filename = None
    book.file_size = None
    book.sync_has_pdf()
    await db.commit()

    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=file_deleted",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/books/{book_id}/external-url")
async def admin_set_external_url(
    book_id: int,
    external_url: str = Form(""),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    url = (external_url or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return RedirectResponse(
            url=f"/admin/books/{book.id}?msg=bad_external_url",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    book.external_file_url = url or None
    if url:
        from app.services.downloads import extension_from_url

        ext = extension_from_url(url)
        if ext:
            book.file_format = ext
        elif not book.file_filename:
            # Unknown remote type — do not pretend it is PDF
            book.file_format = "bin"
    book.sync_has_pdf()
    await db.commit()
    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=external_saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/books/{book_id}/external-url/delete")
async def admin_clear_external_url(
    book_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)
    book.external_file_url = None
    book.sync_has_pdf()
    await db.commit()
    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=external_cleared",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/books/{book_id}/secure-link")
async def admin_create_secure_link(
    book_id: int,
    note: str = Form(""),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)
    if not book.has_file_ready:
        return RedirectResponse(
            url=f"/admin/books/{book.id}?msg=no_file_for_link",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not book.folder_name:
        book.folder_name = Book.storage_folder(book.id)

    ttl_hours = await get_download_ttl_hours(db)
    link, path = await create_managed_download_link(
        db, book, ttl_hours=ttl_hours, note=note
    )
    await db.commit()
    base = (settings.BASE_URL or "").rstrip("/")
    full = f"{base}{path}" if base else path
    from urllib.parse import quote

    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=link_created&link={quote(full, safe='')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/books/{book_id}/delete")
async def admin_delete_book(
    book_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(
            select(Book)
            .options(selectinload(Book.categories))
            .where(Book.id == book_id)
        )
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    # Snapshot titles for any order items still missing them
    items = (
        await db.execute(select(OrderItem).where(OrderItem.book_id == book_id))
    ).scalars().all()
    for item in items:
        if not item.book_title:
            item.book_title = book.display_title

    if book.folder_name:
        existing = await _resolve_stored_filename(book)
        if existing:
            await _delete_book_file(book.folder_name, existing)
        # Remove cover if present
        cover = book.cover_filename or "cover.jpg"
        try:
            await _delete_book_file(book.folder_name, cover)
        except Exception:
            pass

    book.categories = []
    await db.delete(book)
    await db.commit()
    return RedirectResponse(
        url="/admin/books?msg=deleted",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/books/{book_id}/file")
async def admin_download_file(
    book_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book or not book.has_file_ready:
        raise HTTPException(status_code=404, detail="فایل موجود نیست")

    if book.file_filename:
        filename = await _resolve_stored_filename(book)
        if filename and book.file_filename != filename:
            book.file_filename = filename
            await db.commit()

    if not book.folder_name:
        book.folder_name = Book.storage_folder(book.id)
        await db.commit()

    ttl = (await get_download_ttl_hours(db)) * 3600
    token = make_download_token(
        book, user_id=admin.id, order_id=None, ttl_seconds=ttl
    )
    return RedirectResponse(
        url=download_url(book, token),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# Users


@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    q: str = "",
    page: int = 1,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    page = max(1, page)
    filters = []
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                User.phone.ilike(pattern),
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
                User.email.ilike(pattern),
                User.username.ilike(pattern),
            )
        )
    where = and_(*filters) if filters else True
    total = await db.scalar(select(func.count()).select_from(User).where(where)) or 0
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 0

    result = await db.execute(
        select(User)
        .where(where)
        .order_by(desc(User.created_at))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    users = result.scalars().all()

    return templates.TemplateResponse(
        "admin/users.html",
        {
            "request": request,
            "admin": admin,
            "users": users,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "message": request.query_params.get("msg"),
        },
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def admin_user_edit(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404)

    library = await _user_library(db, user.id)
    return templates.TemplateResponse(
        "admin/user_edit.html",
        {
            "request": request,
            "admin": admin,
            "user": user,
            "library": library,
            "roles": [r.value for r in UserRole],
            "error": None,
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/users/{user_id}")
async def admin_user_save(
    user_id: int,
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("USER"),
    is_active: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404)

    async def _err(msg: str):
        library = await _user_library(db, user.id)
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "admin": admin,
                "user": user,
                "library": library,
                "roles": [r.value for r in UserRole],
                "error": msg,
                "message": None,
            },
            status_code=400,
        )

    ok, phone_or_err = validate_iran_phone(phone)
    if not ok:
        return await _err(phone_or_err)
    new_phone = phone_or_err

    try:
        new_role = UserRole(role)
    except ValueError:
        return await _err("نقش نامعتبر است")

    active = is_active is not None
    if user.id == admin.id:
        if new_role != UserRole.ADMIN:
            return await _err("نمی‌توانید نقش خودتان را تغییر دهید")
        if not active:
            return await _err("نمی‌توانید خودتان را غیرفعال کنید")

    # Uniqueness checks
    if new_phone != user.phone:
        clash = (
            await db.execute(select(User.id).where(User.phone == new_phone))
        ).scalar_one_or_none()
        if clash:
            return await _err("این شماره موبایل قبلاً ثبت شده است")

    email_val = email.strip() or None
    if email_val and email_val != user.email:
        clash = (
            await db.execute(select(User.id).where(User.email == email_val))
        ).scalar_one_or_none()
        if clash:
            return await _err("این ایمیل قبلاً ثبت شده است")

    username_val = username.strip() or None
    if username_val and username_val != user.username:
        clash = (
            await db.execute(select(User.id).where(User.username == username_val))
        ).scalar_one_or_none()
        if clash:
            return await _err("این نام کاربری قبلاً ثبت شده است")

    user.first_name = first_name.strip()
    user.last_name = last_name.strip()
    user.phone = new_phone
    user.email = email_val
    user.username = username_val
    user.role = new_role
    user.is_active = active
    if password.strip():
        user.hashed_password = get_password_hash(password.strip())

    await db.commit()
    return RedirectResponse(
        url=f"/admin/users/{user.id}?msg=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/users/{user_id}/books")
async def admin_user_add_books(
    user_id: int,
    book_ids: str = Form(""),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404)

    ids = _parse_book_ids(book_ids)
    if not ids:
        return RedirectResponse(
            url=f"/admin/users/{user.id}?msg=no_books",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    owned = await _owned_book_ids(db, user.id)
    to_add = [i for i in ids if i not in owned]
    if not to_add:
        return RedirectResponse(
            url=f"/admin/users/{user.id}?msg=already_owned",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    result = await db.execute(select(Book).where(Book.id.in_(to_add)))
    books = list(result.scalars().all())
    if not books:
        return RedirectResponse(
            url=f"/admin/users/{user.id}?msg=books_not_found",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    now = datetime.now(timezone.utc)
    order = Order(
        user_id=user.id,
        status=OrderStatus.PAID,
        total_amount=Decimal("0"),
        payment_gateway_ref_id="admin-gift",
        paid_at=now,
    )
    db.add(order)
    await db.flush()

    for book in books:
        db.add(
            OrderItem(
                order_id=order.id,
                book_id=book.id,
                price=Decimal("0"),
                quantity=1,
            )
        )

    await db.commit()
    return RedirectResponse(
        url=f"/admin/users/{user.id}?msg=books_added",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/users/{user_id}/books/remove")
async def admin_user_remove_book(
    user_id: int,
    book_id: int = Form(...),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404)

    result = await db.execute(
        select(OrderItem)
        .options(selectinload(OrderItem.order))
        .join(Order)
        .where(
            and_(
                Order.user_id == user.id,
                Order.status == OrderStatus.PAID,
                OrderItem.book_id == book_id,
            )
        )
    )
    items = list(result.scalars().all())
    if not items:
        return RedirectResponse(
            url=f"/admin/users/{user.id}?msg=book_not_in_library",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    order_ids = {item.order_id for item in items}
    for item in items:
        await db.delete(item)
    await db.flush()

    for oid in order_ids:
        remaining = await db.scalar(
            select(func.count()).select_from(OrderItem).where(OrderItem.order_id == oid)
        )
        if not remaining:
            order = (
                await db.execute(select(Order).where(Order.id == oid))
            ).scalar_one_or_none()
            if order:
                await db.delete(order)

    await db.commit()
    return RedirectResponse(
        url=f"/admin/users/{user.id}?msg=book_removed",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/users/{user_id}/toggle")
async def admin_toggle_user(
    user_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404)
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="نمی‌توانید خودتان را غیرفعال کنید")
    user.is_active = not user.is_active
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)


# Orders / Reports


@router.get("/orders", response_class=HTMLResponse)
async def admin_orders(
    request: Request,
    status_filter: str = "",
    page: int = 1,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    page = max(1, page)
    filters = []
    if status_filter:
        try:
            filters.append(Order.status == OrderStatus(status_filter))
        except ValueError:
            pass
    where = and_(*filters) if filters else True
    total = await db.scalar(select(func.count()).select_from(Order).where(where)) or 0
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 0

    result = await db.execute(
        select(Order)
        .options(
            selectinload(Order.user),
            selectinload(Order.items).selectinload(OrderItem.book),
        )
        .where(where)
        .order_by(desc(Order.created_at))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    orders = result.scalars().all()

    return templates.TemplateResponse(
        "admin/orders.html",
        {
            "request": request,
            "admin": admin,
            "orders": orders,
            "status_filter": status_filter,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "statuses": [
                {"value": s.value, "label": ORDER_STATUS_FA[s.value]}
                for s in OrderStatus
            ],
        },
    )


@router.get("/reports", response_class=HTMLResponse)
async def admin_reports(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    range_key: str = Query("1m", alias="range"),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    start, end, resolved_range = _resolve_chart_range(range_key, date_from, date_to)

    paid_filter = and_(
        Order.status == OrderStatus.PAID,
        Order.paid_at >= start,
        Order.paid_at <= end,
    )

    total_sales = await db.scalar(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(paid_filter)
    ) or 0
    order_count = await db.scalar(
        select(func.count()).select_from(Order).where(paid_filter)
    ) or 0

    monthly = await db.execute(
        select(
            func.date_trunc("month", Order.paid_at).label("month"),
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), 0),
        )
        .where(paid_filter)
        .group_by("month")
        .order_by("month")
    )
    monthly_rows = monthly.all()

    top_books = await db.execute(
        select(
            Book.id,
            Book.title,
            func.count(OrderItem.id).label("sold"),
            func.coalesce(func.sum(OrderItem.price), 0).label("revenue"),
        )
        .join(OrderItem, OrderItem.book_id == Book.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(paid_filter)
        .group_by(Book.id, Book.title)
        .order_by(desc("sold"))
        .limit(10)
    )
    book_rows = top_books.all()

    sales_chart = await _sales_series(db, start, end)
    top_books_chart = await _top_books_series(db, start, end, limit=10)

    return templates.TemplateResponse(
        "admin/reports.html",
        {
            "request": request,
            "admin": admin,
            "range_key": resolved_range,
            "date_from": start.strftime("%Y-%m-%d"),
            "date_to": end.strftime("%Y-%m-%d"),
            "range_presets": CHART_RANGE_PRESETS,
            "total_sales": total_sales,
            "order_count": order_count,
            "monthly_rows": monthly_rows,
            "book_rows": book_rows,
            "sales_chart": sales_chart,
            "top_books_chart": top_books_chart,
        },
    )


# Scraper status


def _scraper_run_dict(run: ScraperRun) -> dict:
    status = run.status.value if run.status else None
    return {
        "id": run.id,
        "status": status,
        "mode": run.mode,
        "pages_total": run.pages_total or 0,
        "pages_done": run.pages_done or 0,
        "books_saved": run.books_saved or 0,
        "books_skipped": run.books_skipped or 0,
        "error_message": run.error_message,
        "pid": run.pid,
        "hostname": run.hostname,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


async def _scraper_status_payload(db: AsyncSession) -> dict:
    result = await db.execute(
        select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(50)
    )
    runs = list(result.scalars().all())
    latest = runs[0] if runs else None
    running = (
        latest if latest and latest.status == ScraperRunStatus.RUNNING else None
    )
    return {
        "running": _scraper_run_dict(running) if running else None,
        "latest": _scraper_run_dict(latest) if latest else None,
        "runs": [_scraper_run_dict(r) for r in runs],
    }


@router.get("/scraper", response_class=HTMLResponse)
async def admin_scraper_status(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    payload = await _scraper_status_payload(db)
    return templates.TemplateResponse(
        "admin/scraper.html",
        {
            "request": request,
            "admin": admin,
            "initial_status": payload,
        },
    )


@router.get("/scraper/status")
async def admin_scraper_status_json(
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    return await _scraper_status_payload(db)


# Landing hero covers


async def _get_hero_carousel_seconds(db: AsyncSession) -> int:
    row = (
        await db.execute(
            select(AppSetting).where(AppSetting.key == HERO_CAROUSEL_SECONDS_KEY)
        )
    ).scalar_one_or_none()
    try:
        seconds = int(row.value) if row else HERO_CAROUSEL_SECONDS_DEFAULT
    except (TypeError, ValueError):
        seconds = HERO_CAROUSEL_SECONDS_DEFAULT
    return max(3, min(seconds, 120))


async def _set_hero_carousel_seconds(db: AsyncSession, seconds: int) -> int:
    seconds = max(3, min(int(seconds), 120))
    row = (
        await db.execute(
            select(AppSetting).where(AppSetting.key == HERO_CAROUSEL_SECONDS_KEY)
        )
    ).scalar_one_or_none()
    if row:
        row.value = str(seconds)
    else:
        db.add(AppSetting(key=HERO_CAROUSEL_SECONDS_KEY, value=str(seconds)))
    return seconds


def _hero_dir() -> str:
    path = os.path.join(settings.MEDIA_ROOT, HERO_FOLDER)
    os.makedirs(path, exist_ok=True)
    return path


@router.get("/hero", response_class=HTMLResponse)
async def admin_hero_list(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    slides = list(
        (
            await db.execute(
                select(HeroSlide).order_by(HeroSlide.sort_order.asc(), HeroSlide.id.asc())
            )
        )
        .scalars()
        .all()
    )
    interval = await _get_hero_carousel_seconds(db)
    return templates.TemplateResponse(
        "admin/hero.html",
        {
            "request": request,
            "admin": admin,
            "slides": slides,
            "interval_seconds": interval,
            "recommended_size": HERO_RECOMMENDED_SIZE,
            "min_size": HERO_MIN_SIZE,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
            "allowed_exts": sorted(ALLOWED_COVER_EXTS),
        },
    )


@router.post("/hero/settings")
async def admin_hero_settings(
    interval_seconds: int = Form(10),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    await _set_hero_carousel_seconds(db, interval_seconds)
    await db.commit()
    return RedirectResponse(
        url="/admin/hero?msg=settings_saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/hero/upload")
async def admin_hero_upload(
    image: UploadFile = File(...),
    title: str = Form(""),
    sort_order: int = Form(0),
    interval_seconds: int = Form(10),
    is_active: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    ext = _file_ext(image.filename)
    if ext not in ALLOWED_COVER_EXTS:
        return RedirectResponse(
            url="/admin/hero?error=bad_ext",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    data = await image.read()
    if not data:
        return RedirectResponse(
            url="/admin/hero?error=empty",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    await _set_hero_carousel_seconds(db, interval_seconds)

    filename = f"slide_{uuid.uuid4().hex[:12]}.{ext if ext != 'jpeg' else 'jpg'}"
    dest = os.path.join(_hero_dir(), filename)
    with open(dest, "wb") as f:
        f.write(data)

    db.add(
        HeroSlide(
            image_filename=filename,
            title=title.strip() or None,
            sort_order=sort_order,
            is_active=is_active is not None,
        )
    )
    await db.commit()
    return RedirectResponse(
        url="/admin/hero?msg=uploaded",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/hero/{slide_id}/edit")
async def admin_hero_edit(
    slide_id: int,
    title: str = Form(""),
    sort_order: int = Form(0),
    interval_seconds: int = Form(10),
    is_active: str | None = Form(None),
    image: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    slide = (
        await db.execute(select(HeroSlide).where(HeroSlide.id == slide_id))
    ).scalar_one_or_none()
    if not slide:
        raise HTTPException(status_code=404)

    await _set_hero_carousel_seconds(db, interval_seconds)

    slide.title = title.strip() or None
    slide.sort_order = sort_order
    slide.is_active = is_active is not None

    if image and image.filename:
        ext = _file_ext(image.filename)
        if ext not in ALLOWED_COVER_EXTS:
            return RedirectResponse(
                url="/admin/hero?error=bad_ext",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        data = await image.read()
        if data:
            # remove old file
            old_path = os.path.join(_hero_dir(), slide.image_filename)
            if os.path.isfile(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            filename = f"slide_{uuid.uuid4().hex[:12]}.{ext if ext != 'jpeg' else 'jpg'}"
            with open(os.path.join(_hero_dir(), filename), "wb") as f:
                f.write(data)
            slide.image_filename = filename

    await db.commit()
    return RedirectResponse(
        url="/admin/hero?msg=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/hero/{slide_id}/delete")
async def admin_hero_delete(
    slide_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    slide = (
        await db.execute(select(HeroSlide).where(HeroSlide.id == slide_id))
    ).scalar_one_or_none()
    if not slide:
        raise HTTPException(status_code=404)

    path = os.path.join(_hero_dir(), slide.image_filename)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    await db.delete(slide)
    await db.commit()
    return RedirectResponse(
        url="/admin/hero?msg=deleted",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# Coupons & download settings


def _parse_admin_dt(raw: str | None) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    # datetime-local: 2026-08-01T14:30
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value)
        else:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
        if dt.tzinfo is None:
            # Treat as Asia/Tehran wall time ≈ UTC+3:30 without zoneinfo dependency
            dt = dt.replace(tzinfo=timezone(timedelta(hours=3, minutes=30)))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _dt_local_value(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=3, minutes=30)))
    return local.strftime("%Y-%m-%dT%H:%M")


@router.get("/coupons", response_class=HTMLResponse)
async def admin_coupons(
    request: Request,
    page: int = 1,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    page = max(1, page)
    total = await db.scalar(select(func.count()).select_from(Coupon)) or 0
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 0
    result = await db.execute(
        select(Coupon)
        .order_by(desc(Coupon.created_at), desc(Coupon.id))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    coupons = result.scalars().all()

    ttl_row = (
        await db.execute(
            select(AppSetting).where(AppSetting.key == DOWNLOAD_LINK_TTL_HOURS_KEY)
        )
    ).scalar_one_or_none()
    ttl_hours = DOWNLOAD_LINK_TTL_HOURS_DEFAULT
    if ttl_row:
        try:
            ttl_hours = int(ttl_row.value)
        except ValueError:
            pass

    pending_row = (
        await db.execute(
            select(AppSetting).where(
                AppSetting.key == PENDING_FILE_CUSTOMER_MESSAGE_KEY
            )
        )
    ).scalar_one_or_none()
    pending_message = (
        pending_row.value
        if pending_row and pending_row.value
        else PENDING_FILE_CUSTOMER_MESSAGE_DEFAULT
    )

    return templates.TemplateResponse(
        "admin/coupons.html",
        {
            "request": request,
            "admin": admin,
            "coupons": coupons,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "ttl_hours": ttl_hours,
            "pending_message": pending_message,
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/settings/download-ttl")
async def admin_save_download_ttl(
    hours: str = Form(...),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    try:
        value = max(1, min(int(hours), 24 * 30))
    except ValueError:
        return RedirectResponse(
            url="/admin/coupons?msg=ttl_invalid",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    row = (
        await db.execute(
            select(AppSetting).where(AppSetting.key == DOWNLOAD_LINK_TTL_HOURS_KEY)
        )
    ).scalar_one_or_none()
    if row:
        row.value = str(value)
    else:
        db.add(AppSetting(key=DOWNLOAD_LINK_TTL_HOURS_KEY, value=str(value)))
    await db.commit()
    return RedirectResponse(
        url="/admin/coupons?msg=ttl_saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/coupons/new", response_class=HTMLResponse)
async def admin_coupon_new(
    request: Request,
    admin: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/coupon_edit.html",
        {
            "request": request,
            "admin": admin,
            "coupon": None,
            "error": None,
            "form": {
                "code": "",
                "discount_type": "PERCENT",
                "amount": "",
                "max_uses": "",
                "min_order_amount": "",
                "starts_at": "",
                "ends_at": "",
                "is_active": True,
            },
        },
    )


@router.get("/coupons/{coupon_id}", response_class=HTMLResponse)
async def admin_coupon_edit(
    coupon_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    coupon = (
        await db.execute(select(Coupon).where(Coupon.id == coupon_id))
    ).scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "admin/coupon_edit.html",
        {
            "request": request,
            "admin": admin,
            "coupon": coupon,
            "error": None,
            "form": {
                "code": coupon.code,
                "discount_type": coupon.discount_type.value,
                "amount": str(coupon.amount),
                "max_uses": "" if coupon.max_uses is None else str(coupon.max_uses),
                "min_order_amount": (
                    ""
                    if coupon.min_order_amount is None
                    else str(int(coupon.min_order_amount))
                ),
                "starts_at": _dt_local_value(coupon.starts_at),
                "ends_at": _dt_local_value(coupon.ends_at),
                "is_active": coupon.is_active,
            },
        },
    )


async def _save_coupon_from_form(
    db: AsyncSession,
    coupon: Coupon | None,
    *,
    code: str,
    discount_type: str,
    amount: str,
    max_uses: str,
    min_order_amount: str,
    starts_at: str,
    ends_at: str,
    is_active: str | None,
) -> tuple[Coupon | None, str | None]:
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return None, "کد تخفیف الزامی است"
    try:
        dtype = DiscountType(discount_type)
    except ValueError:
        return None, "نوع تخفیف نامعتبر است"
    try:
        amt = Decimal(amount)
        if amt <= 0:
            raise InvalidOperation
        if dtype == DiscountType.PERCENT and amt > 100:
            return None, "درصد تخفیف نمی‌تواند بیشتر از ۱۰۰ باشد"
    except (InvalidOperation, ValueError):
        return None, "مقدار تخفیف نامعتبر است"

    max_uses_val = None
    if (max_uses or "").strip():
        try:
            max_uses_val = int(max_uses)
            if max_uses_val < 1:
                return None, "سقف استفاده باید حداقل ۱ باشد"
        except ValueError:
            return None, "سقف استفاده نامعتبر است"

    min_amt = None
    if (min_order_amount or "").strip():
        try:
            min_amt = Decimal(min_order_amount)
            if min_amt < 0:
                return None, "حداقل مبلغ سفارش نامعتبر است"
        except (InvalidOperation, ValueError):
            return None, "حداقل مبلغ سفارش نامعتبر است"

    starts = _parse_admin_dt(starts_at)
    ends = _parse_admin_dt(ends_at)
    if starts and ends and ends <= starts:
        return None, "پایان اعتبار باید بعد از شروع باشد"

    dup = await db.execute(
        select(Coupon).where(
            Coupon.code == code_norm,
            Coupon.id != (coupon.id if coupon else -1),
        )
    )
    if dup.scalar_one_or_none():
        return None, "این کد قبلاً ثبت شده است"

    if coupon is None:
        coupon = Coupon(used_count=0)
        db.add(coupon)

    coupon.code = code_norm
    coupon.discount_type = dtype
    coupon.amount = amt
    coupon.max_uses = max_uses_val
    coupon.min_order_amount = min_amt
    coupon.starts_at = starts
    coupon.ends_at = ends
    coupon.is_active = bool(is_active)
    await db.commit()
    await db.refresh(coupon)
    return coupon, None


@router.post("/coupons/new")
async def admin_coupon_create(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
    code: str = Form(...),
    discount_type: str = Form(...),
    amount: str = Form(...),
    max_uses: str = Form(""),
    min_order_amount: str = Form(""),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    is_active: str | None = Form(None),
):
    coupon, err = await _save_coupon_from_form(
        db,
        None,
        code=code,
        discount_type=discount_type,
        amount=amount,
        max_uses=max_uses,
        min_order_amount=min_order_amount,
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=is_active,
    )
    if err:
        return templates.TemplateResponse(
            "admin/coupon_edit.html",
            {
                "request": request,
                "admin": admin,
                "coupon": None,
                "error": err,
                "form": {
                    "code": code,
                    "discount_type": discount_type,
                    "amount": amount,
                    "max_uses": max_uses,
                    "min_order_amount": min_order_amount,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "is_active": bool(is_active),
                },
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/coupons/{coupon.id}?msg=created",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/coupons/{coupon_id}")
async def admin_coupon_update(
    coupon_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
    code: str = Form(...),
    discount_type: str = Form(...),
    amount: str = Form(...),
    max_uses: str = Form(""),
    min_order_amount: str = Form(""),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    is_active: str | None = Form(None),
):
    coupon = (
        await db.execute(select(Coupon).where(Coupon.id == coupon_id))
    ).scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404)
    saved, err = await _save_coupon_from_form(
        db,
        coupon,
        code=code,
        discount_type=discount_type,
        amount=amount,
        max_uses=max_uses,
        min_order_amount=min_order_amount,
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=is_active,
    )
    if err:
        return templates.TemplateResponse(
            "admin/coupon_edit.html",
            {
                "request": request,
                "admin": admin,
                "coupon": coupon,
                "error": err,
                "form": {
                    "code": code,
                    "discount_type": discount_type,
                    "amount": amount,
                    "max_uses": max_uses,
                    "min_order_amount": min_order_amount,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "is_active": bool(is_active),
                },
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/coupons/{saved.id}?msg=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/coupons/{coupon_id}/delete")
async def admin_coupon_delete(
    coupon_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    coupon = (
        await db.execute(select(Coupon).where(Coupon.id == coupon_id))
    ).scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404)
    # Soft-delete if referenced by orders
    used = await db.scalar(
        select(func.count()).select_from(Order).where(Order.coupon_id == coupon_id)
    )
    if used:
        coupon.is_active = False
        await db.commit()
        return RedirectResponse(
            url="/admin/coupons?msg=deactivated",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    await db.delete(coupon)
    await db.commit()
    return RedirectResponse(
        url="/admin/coupons?msg=deleted",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# Download link management & pending-file message


def _remaining_label(expires_at: datetime | None) -> str:
    if not expires_at:
        return "—"
    now = datetime.now(timezone.utc)
    exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    total_sec = int((exp - now).total_seconds())
    if total_sec <= 0:
        return "منقضی"
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes or not parts:
        parts.append(f"{minutes} دقیقه")
    return " و ".join(parts)


@router.get("/download-links", response_class=HTMLResponse)
async def admin_download_links(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    # Purge expired / revoked from the managed list
    stale = (
        await db.execute(
            select(DownloadLink).where(
                or_(
                    DownloadLink.revoked_at.is_not(None),
                    and_(
                        DownloadLink.expires_at.is_not(None),
                        DownloadLink.expires_at <= now,
                    ),
                )
            )
        )
    ).scalars().all()
    for row in stale:
        await db.delete(row)
    if stale:
        await db.commit()

    links = list(
        (
            await db.execute(
                select(DownloadLink)
                .options(selectinload(DownloadLink.book))
                .order_by(desc(DownloadLink.created_at))
            )
        )
        .scalars()
        .all()
    )
    base = (settings.BASE_URL or "").rstrip("/")
    rows = []
    for link in links:
        book = link.book
        if book and link.token:
            path = download_url(book, link.token)
            full = f"{base}{path}" if base else path
        else:
            full = ""
        rows.append(
            {
                "link": link,
                "book_title": book.display_title if book else "کتاب حذف‌شده",
                "url": full,
                "created_fa": format_jalali(link.created_at),
                "remaining": _remaining_label(link.expires_at),
            }
        )

    return templates.TemplateResponse(
        "admin/download_links.html",
        {
            "request": request,
            "admin": admin,
            "rows": rows,
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/download-links/{link_id}/revoke")
async def admin_revoke_download_link(
    link_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    link = (
        await db.execute(select(DownloadLink).where(DownloadLink.id == link_id))
    ).scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404)
    await db.delete(link)
    await db.commit()
    return RedirectResponse(
        url="/admin/download-links?msg=revoked",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/settings/pending-file-message")
async def admin_save_pending_file_message(
    message: str = Form(...),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    value = (message or "").strip() or PENDING_FILE_CUSTOMER_MESSAGE_DEFAULT
    row = (
        await db.execute(
            select(AppSetting).where(
                AppSetting.key == PENDING_FILE_CUSTOMER_MESSAGE_KEY
            )
        )
    ).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=PENDING_FILE_CUSTOMER_MESSAGE_KEY, value=value))
    await db.commit()
    return RedirectResponse(
        url="/admin/coupons?msg=pending_msg_saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# Categories


async def _unique_category_slug(
    db: AsyncSession,
    name: str,
    slug_hint: str | None = None,
    exclude_id: int | None = None,
) -> str:
    base = Category.slugify(slug_hint.strip() if slug_hint and slug_hint.strip() else name)
    slug = base
    n = 2
    while True:
        q = select(Category).where(Category.slug == slug)
        if exclude_id is not None:
            q = q.where(Category.id != exclude_id)
        exists = (await db.execute(q)).scalar_one_or_none()
        if not exists:
            return slug
        slug = f"{base}-{n}"
        n += 1


@router.get("/categories", response_class=HTMLResponse)
async def admin_categories(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    cats = await _all_categories(db)
    return templates.TemplateResponse(
        "admin/categories.html",
        {
            "request": request,
            "admin": admin,
            "categories": cats,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
            "bulk_books": request.query_params.get("books"),
            "bulk_links": request.query_params.get("links"),
        },
    )


@router.post("/categories/bulk-assign")
async def admin_categories_bulk_assign(
    request: Request,
    book_ids: str = Form(""),
    search_q: str = Form(""),
    mode: str = Form("add"),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    """Add (or replace) one/more categories on a large set of books."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    form = await request.form()
    category_ids: list[int] = []
    seen_c: set[int] = set()
    for raw in form.getlist("category_ids"):
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid not in seen_c:
            seen_c.add(cid)
            category_ids.append(cid)

    if not category_ids:
        return RedirectResponse(
            url="/admin/categories?error=bulk_no_cats",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    cats = list(
        (
            await db.execute(select(Category).where(Category.id.in_(category_ids)))
        )
        .scalars()
        .all()
    )
    if not cats:
        return RedirectResponse(
            url="/admin/categories?error=bulk_no_cats",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    valid_cat_ids = [c.id for c in cats]

    explicit_ids = _parse_book_ids(book_ids)
    search_q = (search_q or "").strip()
    search_ids: list[int] = []
    if search_q:
        filters = _book_search_filters(search_q)
        where = and_(*filters) if filters else True
        search_ids = list(
            (
                await db.execute(
                    select(Book.id)
                    .where(where)
                    .order_by(Book.id.asc())
                    .limit(50_000)
                )
            )
            .scalars()
            .all()
        )

    if explicit_ids and search_ids:
        search_set = set(search_ids)
        ids = [i for i in explicit_ids if i in search_set]
    elif explicit_ids:
        ids = explicit_ids
    else:
        ids = search_ids

    if not ids:
        return RedirectResponse(
            url="/admin/categories?error=bulk_no_books",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    existing = list(
        (
            await db.execute(
                select(Book.id).where(Book.id.in_(ids)).order_by(Book.id)
            )
        )
        .scalars()
        .all()
    )
    if not existing:
        return RedirectResponse(
            url="/admin/categories?error=bulk_books_missing",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    mode = (mode or "add").strip().lower()
    if mode == "replace":
        await db.execute(
            book_categories.delete().where(
                book_categories.c.book_id.in_(existing)
            )
        )

    pairs = [
        {"book_id": bid, "category_id": cid}
        for bid in existing
        for cid in valid_cat_ids
    ]
    chunk = 800
    for i in range(0, len(pairs), chunk):
        batch = pairs[i : i + chunk]
        stmt = (
            pg_insert(book_categories)
            .values(batch)
            .on_conflict_do_nothing(index_elements=["book_id", "category_id"])
        )
        await db.execute(stmt)

    await db.commit()
    return RedirectResponse(
        url=(
            f"/admin/categories?msg=bulk_assigned"
            f"&books={len(existing)}&links={len(valid_cat_ids)}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/categories/new")
async def admin_category_create(
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    sort_order: int = Form(0),
    is_active: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    name = name.strip()
    if not name:
        return RedirectResponse(
            url="/admin/categories?error=empty_name",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    final_slug = await _unique_category_slug(db, name, slug_hint=slug)
    db.add(
        Category(
            name=name,
            slug=final_slug,
            description=description.strip() or None,
            sort_order=sort_order,
            show_on_home=False,
            is_active=is_active is not None,
        )
    )
    await db.commit()
    return RedirectResponse(
        url="/admin/categories?msg=created",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/categories/{category_id}/edit")
async def admin_category_edit(
    category_id: int,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    sort_order: int = Form(0),
    is_active: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    cat = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404)

    name = name.strip()
    if not name:
        return RedirectResponse(
            url="/admin/categories?error=empty_name",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    cat.name = name
    cat.slug = await _unique_category_slug(
        db, name, slug_hint=slug or name, exclude_id=cat.id
    )
    cat.description = description.strip() or None
    cat.sort_order = sort_order
    cat.show_on_home = False
    cat.is_active = is_active is not None

    await db.commit()
    return RedirectResponse(
        url="/admin/categories?msg=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/categories/{category_id}/delete")
async def admin_category_delete(
    category_id: int,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    cat = (
        await db.execute(
            select(Category)
            .options(selectinload(Category.books))
            .where(Category.id == category_id)
        )
    ).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404)
    cat.books = []
    await db.delete(cat)
    await db.commit()
    return RedirectResponse(
        url="/admin/categories?msg=deleted",
        status_code=status.HTTP_303_SEE_OTHER,
    )
