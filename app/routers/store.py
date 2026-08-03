from math import ceil
import json
import re
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user, get_current_user_optional
from app.database import get_async_db
from app.models import (
    HERO_CAROUSEL_SECONDS_DEFAULT,
    HERO_CAROUSEL_SECONDS_KEY,
    HOME_CATEGORY_BOOKS_LIMIT_DEFAULT,
    HOME_CATEGORY_BOOKS_LIMIT_KEY,
    AppSetting,
    Book,
    Category,
    HeroSlide,
    Order,
    OrderItem,
    OrderStatus,
    User,
    book_categories,
)
from app.services.checkout_helpers import get_download_ttl_seconds
from app.services.downloads import download_url, make_download_token
from app.utils.price import format_price, round_toman

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 24


def _cover_url(book: Book) -> str:
    cover = book.cover_filename or "cover.jpg"
    return f"/media/proxy/cover/{book.folder_name}/{cover}"


async def _download_token_for(
    db, book: Book, user_id: int | None, order_id: int | None = None
) -> str:
    ttl = await get_download_ttl_seconds(db)
    return make_download_token(
        book, user_id=user_id, order_id=order_id, ttl_seconds=ttl
    )


def _tojson(value) -> Markup:
    def _default(obj):
        if isinstance(obj, Decimal):
            # Prefer int for whole toman amounts in JSON-LD
            if obj == obj.to_integral_value():
                return int(obj)
            return float(obj)
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    return Markup(json.dumps(value, ensure_ascii=False, default=_default))


templates.env.globals["cover_url"] = _cover_url
templates.env.globals["format_price"] = format_price
templates.env.globals["round_toman"] = round_toman
templates.env.filters["tojson"] = _tojson
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

    limit_row = (
        await db.execute(
            select(AppSetting).where(AppSetting.key == HOME_CATEGORY_BOOKS_LIMIT_KEY)
        )
    ).scalar_one_or_none()
    try:
        cat_limit = (
            int(limit_row.value) if limit_row else HOME_CATEGORY_BOOKS_LIMIT_DEFAULT
        )
    except (TypeError, ValueError):
        cat_limit = HOME_CATEGORY_BOOKS_LIMIT_DEFAULT
    cat_limit = max(1, min(cat_limit, 48))

    home_cats = list(
        (
            await db.execute(
                select(Category)
                .where(
                    and_(
                        Category.is_active == True,  # noqa: E712
                        Category.show_on_home == True,  # noqa: E712
                    )
                )
                .order_by(Category.sort_order.asc(), Category.name.asc())
            )
        )
        .scalars()
        .all()
    )
    home_category_sections = []
    for cat in home_cats:
        books_result = await db.execute(
            select(Book)
            .join(book_categories, book_categories.c.book_id == Book.id)
            .where(
                and_(
                    book_categories.c.category_id == cat.id,
                    Book.is_active == True,  # noqa: E712
                )
            )
            .order_by(desc(Book.created_at))
            .limit(cat_limit)
        )
        cat_books = list(books_result.scalars().all())
        if cat_books:
            home_category_sections.append({"category": cat, "books": cat_books})

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "books": new_books,
            "hero_slides": hero_slides,
            "hero_interval_ms": seconds * 1000,
            "home_categories": home_cats,
            "home_category_sections": home_category_sections,
            "query": "",
            "current_user": current_user,
        },
    )


@router.get("/categories")
async def categories_index(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    cats = list(
        (
            await db.execute(
                select(Category)
                .where(Category.is_active == True)  # noqa: E712
                .order_by(Category.sort_order.asc(), Category.name.asc())
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "categories.html",
        {
            "request": request,
            "categories": cats,
            "query": "",
            "current_user": current_user,
        },
    )


@router.get("/category/{slug}")
async def category_detail(
    slug: str,
    request: Request,
    page: int = 1,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    page = max(1, page)
    cat = (
        await db.execute(
            select(Category).where(
                and_(Category.slug == slug, Category.is_active == True)  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="دسته یافت نشد")

    count_q = (
        select(func.count())
        .select_from(Book)
        .join(book_categories, book_categories.c.book_id == Book.id)
        .where(
            and_(
                book_categories.c.category_id == cat.id,
                Book.is_active == True,  # noqa: E712
            )
        )
    )
    total = await db.scalar(count_q) or 0
    total_pages = max(1, ceil(total / PAGE_SIZE)) if total else 0

    books = list(
        (
            await db.execute(
                select(Book)
                .join(book_categories, book_categories.c.book_id == Book.id)
                .where(
                    and_(
                        book_categories.c.category_id == cat.id,
                        Book.is_active == True,  # noqa: E712
                    )
                )
                .order_by(desc(Book.created_at))
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        )
        .scalars()
        .all()
    )

    return templates.TemplateResponse(
        "category.html",
        {
            "request": request,
            "category": cat,
            "books": books,
            "page": page,
            "total_pages": total_pages,
            "total": total,
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


@router.get("/book/{book_ref}")
async def book_detail(
    book_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    book = None
    # Legacy numeric URLs → permanent redirect to English-name slug
    if book_ref.isdigit():
        result = await db.execute(select(Book).where(Book.id == int(book_ref)))
        book = result.scalar_one_or_none()
        if not book:
            raise HTTPException(status_code=404, detail="کتاب یافت نشد")
        book.ensure_slug()
        await db.commit()
        if book.slug and book.slug != book_ref:
            return RedirectResponse(
                url=book.path,
                status_code=status.HTTP_301_MOVED_PERMANENTLY,
            )
    else:
        result = await db.execute(select(Book).where(Book.slug == book_ref))
        book = result.scalar_one_or_none()
        if not book:
            # Fallback: …_123 at end of path before slug column is filled
            m = re.search(r"_(\d+)$", book_ref)
            if m:
                result = await db.execute(select(Book).where(Book.id == int(m.group(1))))
                book = result.scalar_one_or_none()
                if book:
                    book.ensure_slug()
                    await db.commit()
                    if book.slug and book.slug != book_ref:
                        return RedirectResponse(
                            url=book.path,
                            status_code=status.HTTP_301_MOVED_PERMANENTLY,
                        )
        if not book:
            raise HTTPException(status_code=404, detail="کتاب یافت نشد")

    owned = False
    download_token = None
    download_href = None
    if current_user:
        owned = await _user_owns_book(db, current_user.id, book.id)
        if owned and book.has_file_ready:
            download_token = await _download_token_for(db, book, current_user.id)
            download_href = download_url(book, download_token)

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "book": book,
            "owned": owned,
            "token": download_token,
            "download_href": download_href,
            "filename": None,
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
            href = None
            if item.book.has_file_ready:
                token = await _download_token_for(db, item.book, current_user.id)
                href = download_url(item.book, token)
            library.append(
                {
                    "book": item.book,
                    "token": token,
                    "download_href": href,
                    "order": order,
                }
            )

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
    if not book or not book.has_file_ready:
        raise HTTPException(status_code=404, detail="فایل موجود نیست")

    if not await _user_owns_book(db, current_user.id, book.id):
        raise HTTPException(status_code=403, detail="ابتدا کتاب را خریداری کنید")

    token = await _download_token_for(db, book, current_user.id)
    url = download_url(book, token)
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
