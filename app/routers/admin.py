"""Admin panel: books, users, orders, reports, PDF upload."""

from __future__ import annotations

import os
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
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user_optional
from app.config import settings
from app.database import get_async_db
from app.models import Book, Order, OrderItem, OrderStatus, User, UserRole


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

PAGE_SIZE = 30


def _parse_date(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            dt = dt + timedelta(days=1) - timedelta(microseconds=1)
        return dt
    except ValueError:
        return None


async def _upload_pdf_to_ftp(folder_name: str, filename: str, data: bytes) -> None:
    if not settings.FTP_ENABLED:
        dest_dir = os.path.join(settings.MEDIA_ROOT, folder_name)
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        return

    async with aioftp.Client.context(
        host=settings.FTP_HOST,
        port=settings.FTP_PORT,
        user=settings.FTP_USER,
        password=settings.FTP_PASS,
        socket_timeout=30,
    ) as client:
        try:
            await client.make_directory(folder_name)
        except Exception:
            pass
        remote = f"{folder_name}/{filename}"
        async with client.upload_stream(remote) as stream:
            await stream.write(data)


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
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
        },
    )


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
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                Book.title.ilike(pattern),
                Book.author.ilike(pattern),
                Book.isbn.ilike(pattern),
            )
        )

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
        },
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
    return templates.TemplateResponse(
        "admin/book_edit.html",
        {"request": request, "admin": admin, "book": book, "error": None},
    )


@router.post("/books/{book_id}")
async def admin_book_save(
    book_id: int,
    request: Request,
    title: str = Form(...),
    author: str = Form(""),
    publisher: str = Form(""),
    description: str = Form(""),
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

    try:
        price_val = Decimal(price.replace(",", "") or "0")
        orig_val = (
            Decimal(original_price.replace(",", ""))
            if original_price.strip()
            else price_val + Decimal("35000")
        )
    except InvalidOperation:
        return templates.TemplateResponse(
            "admin/book_edit.html",
            {
                "request": request,
                "admin": admin,
                "book": book,
                "error": "قیمت نامعتبر است",
            },
        )

    book.title = title.strip()
    book.author = author.strip() or None
    book.publisher = publisher.strip() or None
    book.description = description.strip() or None
    book.price = price_val
    book.original_price = orig_val
    book.is_active = is_active is not None
    await db.commit()
    return RedirectResponse(
        url=f"/admin/books?msg=saved&q={book_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/books/{book_id}/upload")
async def admin_upload_pdf(
    book_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    book = (
        await db.execute(select(Book).where(Book.id == book_id))
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="فقط فایل PDF مجاز است")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="فایل خالی است")

    filename = book.pdf_filename
    await _upload_pdf_to_ftp(book.folder_name, filename, data)
    book.has_pdf = True
    book.file_format = "pdf"
    book.file_size = f"{len(data) // 1024} KB"
    await db.commit()

    return RedirectResponse(
        url="/admin/books?missing_pdf=1&msg=uploaded",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
        },
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
        .options(selectinload(Order.user), selectinload(Order.items).selectinload(OrderItem.book))
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
            "statuses": [s.value for s in OrderStatus],
        },
    )


@router.get("/reports", response_class=HTMLResponse)
async def admin_reports(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    db: AsyncSession = Depends(get_async_db),
    admin: User = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    start = _parse_date(date_from) or (now - timedelta(days=30))
    end = _parse_date(date_to, end_of_day=True) or now

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

    # Monthly breakdown
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

    # Top books by sales count
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
        .limit(50)
    )
    book_rows = top_books.all()

    return templates.TemplateResponse(
        "admin/reports.html",
        {
            "request": request,
            "admin": admin,
            "date_from": date_from or start.strftime("%Y-%m-%d"),
            "date_to": date_to or end.strftime("%Y-%m-%d"),
            "total_sales": total_sales,
            "order_count": order_count,
            "monthly_rows": monthly_rows,
            "book_rows": book_rows,
        },
    )
