from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user, get_current_user_optional
from app.database import get_async_db
from app.models import (
    HERO_CAROUSEL_SECONDS_DEFAULT,
    HERO_CAROUSEL_SECONDS_KEY,
    AppSetting,
    Book,
    HeroSlide,
    Order,
    OrderItem,
    OrderStatus,
    User,
)
from app.routers.media import signer

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 24


def _cover_url(book: Book) -> str:
    cover = book.cover_filename or "cover.jpg"
    return f"/media/proxy/cover/{book.folder_name}/{cover}"


def _download_token(book: Book, user_id: int) -> str:
    return signer.dumps(
        {
            "folder": book.folder_name,
            "filename": book.pdf_filename,
            "download_name": book.download_filename,
            "user_id": user_id,
            "book_id": book.id,
        },
        salt="pdf-download",
    )


def _format_price(value) -> str:
    if value is None:
        return "۰"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


templates.env.globals["cover_url"] = _cover_url
templates.env.globals["format_price"] = _format_price


@router.get("/")
async def home(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    result = await db.execute(
        select(Book)
        .where(Book.is_active == True)  # noqa: E712
        .order_by(desc(Book.created_at))
        .limit(8)
    )
    new_books = result.scalars().all()

    slides_result = await db.execute(
        select(HeroSlide)
        .where(HeroSlide.is_active == True)  # noqa: E712
        .order_by(HeroSlide.sort_order.asc(), HeroSlide.id.asc())
    )
    hero_slides = list(slides_result.scalars().all())

    setting = (
        await db.execute(
            select(AppSetting).where(AppSetting.key == HERO_CAROUSEL_SECONDS_KEY)
        )
    ).scalar_one_or_none()
    try:
        seconds = int(setting.value) if setting else HERO_CAROUSEL_SECONDS_DEFAULT
    except (TypeError, ValueError):
        seconds = HERO_CAROUSEL_SECONDS_DEFAULT
    seconds = max(3, min(seconds, 120))

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "books": new_books,
            "hero_slides": hero_slides,
            "hero_interval_ms": seconds * 1000,
            "query": "",
            "current_user": current_user,
        },
    )


@router.get("/search")
async def search(
    request: Request,
    q: str = "",
    page: int = 1,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    page = max(1, page)
    q = (q or "").strip()

    if q:
        pattern = f"%{q}%"
        filters = and_(
            Book.is_active == True,  # noqa: E712
            or_(
                Book.title.ilike(pattern),
                Book.author.ilike(pattern),
                Book.publisher.ilike(pattern),
                Book.isbn.ilike(pattern),
                Book.title_en.ilike(pattern),
            ),
        )
    else:
        filters = Book.is_active == True  # noqa: E712

    total = await db.scalar(select(func.count()).select_from(Book).where(filters)) or 0
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 0

    result = await db.execute(
        select(Book)
        .where(filters)
        .order_by(desc(Book.created_at))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    books = result.scalars().all()

    return templates.TemplateResponse(
        "search_results.html",
        {
            "request": request,
            "books": books,
            "query": q,
            "current_user": current_user,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


async def _user_owns_book(db: AsyncSession, user_id: int, book_id: int) -> bool:
    result = await db.execute(
        select(OrderItem.id)
        .join(Order)
        .where(
            and_(
                Order.user_id == user_id,
                Order.status == OrderStatus.PAID,
                OrderItem.book_id == book_id,
            )
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


@router.get("/book/{book_id}")
async def book_detail(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    result = await db.execute(select(Book).where(Book.id == book_id))
    book = result.scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404, detail="کتاب یافت نشد")

    owned = False
    download_token = None
    if current_user:
        owned = await _user_owns_book(db, current_user.id, book.id)
        if owned and book.has_pdf:
            download_token = _download_token(book, current_user.id)

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "book": book,
            "owned": owned,
            "token": download_token,
            "filename": book.pdf_filename if book.has_pdf else None,
            "query": "",
            "current_user": current_user,
        },
    )


@router.get("/profile")
async def profile(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
    pay: str | None = None,
):
    if not current_user:
        return RedirectResponse(
            url="/auth/login?next=/profile",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.book))
        .where(
            and_(Order.user_id == current_user.id, Order.status == OrderStatus.PAID)
        )
        .order_by(desc(Order.paid_at), desc(Order.created_at))
    )
    orders = result.scalars().all()

    library = []
    seen = set()
    for order in orders:
        for item in order.items:
            if item.book_id in seen or not item.book:
                continue
            seen.add(item.book_id)
            token = None
            if item.book.has_pdf:
                token = _download_token(item.book, current_user.id)
            library.append({"book": item.book, "token": token, "order": order})

    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "current_user": current_user,
            "library": library,
            "pay": pay,
            "query": "",
        },
    )


@router.get("/download/{book_id}")
async def download_book(
    book_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """Issue a timed download URL only if the user has purchased the book."""
    result = await db.execute(select(Book).where(Book.id == book_id))
    book = result.scalar_one_or_none()
    if not book or not book.has_pdf:
        raise HTTPException(status_code=404, detail="فایل موجود نیست")

    if not await _user_owns_book(db, current_user.id, book.id):
        raise HTTPException(status_code=403, detail="ابتدا کتاب را خریداری کنید")

    token = _download_token(book, current_user.id)
    url = f"/media/proxy/book/{book.folder_name}/{book.pdf_filename}?token={token}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
