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
    AppSetting,
    Book,
    HeroSlide,
    Order,
    OrderItem,
    OrderStatus,
    ScraperRun,
    ScraperRunStatus,
    User,
    UserRole,
)
from app.routers.media import check_file_exists, signer
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


def _parse_book_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in re.split(r"[\s,;]+", (raw or "").strip()):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    # Preserve order, unique
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
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
    return aioftp.Client.context(
        host=settings.FTP_HOST,
        port=settings.FTP_PORT,
        user=settings.FTP_USER,
        password=settings.FTP_PASS,
        socket_timeout=30,
    )


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
    try:
        price_val = Decimal(price.replace(",", "") or "0")
        orig_val = (
            Decimal(original_price.replace(",", ""))
            if original_price.strip()
            else price_val + Decimal("35000")
        )
        return price_val, orig_val
    except InvalidOperation:
        return "قیمت نامعتبر است"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


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
    admin: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/book_new.html",
        {
            "request": request,
            "admin": admin,
            "error": None,
            "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
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
    title = title.strip()
    if not title:
        return templates.TemplateResponse(
            "admin/book_new.html",
            {
                "request": request,
                "admin": admin,
                "error": "عنوان الزامی است",
                "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
            },
            status_code=400,
        )

    parsed = _parse_price(price, original_price)
    if isinstance(parsed, str):
        return templates.TemplateResponse(
            "admin/book_new.html",
            {
                "request": request,
                "admin": admin,
                "error": parsed,
                "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
            },
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

    if cover and cover.filename:
        ext = _file_ext(cover.filename)
        if ext not in ALLOWED_COVER_EXTS:
            await db.rollback()
            return templates.TemplateResponse(
                "admin/book_new.html",
                {
                    "request": request,
                    "admin": admin,
                    "error": "فرمت کاور مجاز نیست (jpg/png/webp)",
                    "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
                },
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
                {
                    "request": request,
                    "admin": admin,
                    "error": f"فرمت فایل مجاز نیست. مجاز: {', '.join(sorted(ALLOWED_BOOK_EXTS))}",
                    "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
                },
                status_code=400,
            )
        data = await file.read()
        if data:
            book.file_format = ext
            filename = Book.build_stored_filename(book.id, ext)
            await _upload_book_file(folder_name, filename, data)
            book.has_pdf = True
            book.file_filename = filename
            book.file_size = f"{len(data) // 1024} KB"

    await db.commit()
    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=created",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/books/{book_id}", response_class=HTMLResponse)
async def admin_book_edit(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    file_exists = False
    if book.has_pdf and book.folder_name:
        file_exists = (await _resolve_stored_filename(book)) is not None

    return templates.TemplateResponse(
        "admin/book_edit.html",
        {
            "request": request,
            "admin": admin,
            "book": book,
            "error": None,
            "message": request.query_params.get("msg"),
            "file_exists": file_exists,
            "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
        },
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
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    parsed = _parse_price(price, original_price)
    if isinstance(parsed, str):
        file_exists = False
        if book.has_pdf and book.folder_name:
            file_exists = (await _resolve_stored_filename(book)) is not None
        return templates.TemplateResponse(
            "admin/book_edit.html",
            {
                "request": request,
                "admin": admin,
                "book": book,
                "error": parsed,
                "message": None,
                "file_exists": file_exists,
                "allowed_exts": sorted(ALLOWED_BOOK_EXTS),
            },
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
    if book.has_pdf:
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
    book.has_pdf = True
    book.file_format = ext
    book.file_filename = filename
    book.file_size = f"{len(data) // 1024} KB"
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

    if book.has_pdf and book.folder_name:
        existing = await _resolve_stored_filename(book)
        if existing:
            await _delete_book_file(book.folder_name, existing)

    book.has_pdf = False
    book.file_filename = None
    book.file_size = None
    await db.commit()

    return RedirectResponse(
        url=f"/admin/books/{book.id}?msg=file_deleted",
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
    if not book or not book.has_pdf:
        raise HTTPException(status_code=404, detail="فایل موجود نیست")

    filename = await _resolve_stored_filename(book)
    if not filename:
        raise HTTPException(status_code=404, detail="فایل روی دیسک/FTP یافت نشد")

    # Sync DB if we recovered a legacy/titled name
    if book.file_filename != filename:
        book.file_filename = filename
        await db.commit()

    token = signer.dumps(
        {
            "folder": book.folder_name,
            "filename": filename,
            "download_name": book.download_filename,
            "user_id": admin.id,
            "book_id": book.id,
            "admin": True,
        },
        salt="pdf-download",
    )
    url = f"/media/proxy/book/{book.folder_name}/{filename}?token={token}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Orders / Reports
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Scraper status
# ---------------------------------------------------------------------------


@router.get("/scraper", response_class=HTMLResponse)
async def admin_scraper_status(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(
        select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(50)
    )
    runs = list(result.scalars().all())
    latest = runs[0] if runs else None
    running = latest if latest and latest.status == ScraperRunStatus.RUNNING else None

    return templates.TemplateResponse(
        "admin/scraper.html",
        {
            "request": request,
            "admin": admin,
            "runs": runs,
            "latest": latest,
            "running": running,
        },
    )


# ---------------------------------------------------------------------------
# Landing hero covers
# ---------------------------------------------------------------------------


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
