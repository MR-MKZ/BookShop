"""Torob API V3 product feed (JWT EdDSA auth)."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_async_db
from app.models import Book
from app.routers.seo import site_origin
from app.utils.price import round_toman

router = APIRouter(prefix="/torob_api/v3", tags=["torob-api"])

PAGE_SIZE = 100
API_VERSION = "torob_api_v3"

TOROB_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAt6Mu4T0pBORY11W+QeM35UsmLO3vsf+6yKpFDEImFk0=
-----END PUBLIC KEY-----"""

_SORT_VALUES = frozenset({"date_added_desc", "date_updated_desc"})


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _expected_audience(request: Request) -> str:
    host = (request.headers.get("host") or "").strip()
    if host:
        return host
    parsed = urlparse(settings.BASE_URL or "")
    if parsed.netloc:
        return parsed.netloc
    return (settings.DOMAIN_NAME or "").strip()


def _verify_torob_token(token: str, audience: str) -> None:
    jwt.decode(
        token,
        key=TOROB_PUBLIC_KEY,
        algorithms=["EdDSA"],
        audience=audience,
        options={"require": ["exp", "aud"]},
    )


def _authenticate(
    request: Request,
    x_torob_token: str | None,
    token_version: str | None,
) -> JSONResponse | None:
    """Return an error response if auth fails, else None."""
    if not x_torob_token:
        return _error("X-Torob-Token header is missing", 401)
    if token_version != "1":
        return _error("X-Torob-Token-Version must be 1", 401)
    audience = _expected_audience(request)
    try:
        _verify_torob_token(x_torob_token, audience)
    except jwt.ExpiredSignatureError:
        return _error("token expired", 401)
    except jwt.InvalidAudienceError:
        return _error("invalid token audience", 401)
    except jwt.PyJWTError:
        return _error("invalid torob token", 401)
    return None


def _iso(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _book_available(book: Book) -> bool:
    avail = (book.availability or "").strip().lower()
    if avail in {"outofstock", "out_of_stock", "out-of-stock", "unavailable"}:
        return False
    return bool(book.is_active)


def _cover_url(book: Book, origin: str) -> str:
    folder = book.folder_name or Book.storage_folder(book.id)
    cover = book.cover_filename or "cover.jpg"
    return f"{origin}/media/proxy/cover/{folder}/{cover}"


def _product_url(book: Book, origin: str) -> str:
    return f"{origin}{book.path}"


def _book_to_product(book: Book, origin: str) -> dict[str, Any]:
    title_fa = (book.title or "").strip()
    title_en = (book.title_en or "").strip()
    title = title_fa or title_en or "بدون عنوان"
    subtitle = title_en if title_en and title_en != title else None

    current = int(round_toman(book.price))
    old: int | None = None
    if book.original_price is not None:
        old_val = int(round_toman(book.original_price))
        if old_val > current:
            old = old_val

    cats = getattr(book, "categories", None) or []
    category_name = None
    if cats:
        category_name = (cats[0].name or "").strip()[:200] or None

    spec: dict[str, str | int] = {}
    if book.author:
        spec["نویسنده"] = str(book.author)[:200]
    if book.publisher:
        spec["ناشر"] = str(book.publisher)[:200]
    if book.isbn:
        spec["ISBN"] = str(book.isbn)[:64]
    if book.pages:
        spec["صفحات"] = str(book.pages)[:64]
    if book.language:
        spec["زبان"] = str(book.language)[:64]
    if book.publish_year:
        spec["سال انتشار"] = str(book.publish_year)[:32]
    if book.edition:
        spec["ویرایش"] = str(book.edition)[:64]
    if book.file_format:
        spec["فرمت"] = str(book.file_format)[:32]

    short_desc = None
    if book.description:
        text = re.sub(r"\s+", " ", book.description).strip()
        if text:
            short_desc = text[:500]

    date_added = _iso(book.created_at)
    date_updated = _iso(book.updated_at or book.created_at)

    product: dict[str, Any] = {
        "page_unique": str(book.id),
        "page_url": _product_url(book, origin),
        "title": title[:500],
        "current_price": current,
        "availability": _book_available(book),
        "image_links": [_cover_url(book, origin)],
        "date_added": date_added,
        "date_updated": date_updated,
    }
    if subtitle:
        product["subtitle"] = subtitle[:500]
    if old is not None:
        product["old_price"] = old
    if category_name:
        product["category_name"] = category_name
    if short_desc:
        product["short_desc"] = short_desc
    if spec:
        product["spec"] = spec
    return product


def _envelope(
    *,
    products: list[dict[str, Any]],
    current_page: int,
    total: int,
) -> dict[str, Any]:
    max_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    return {
        "api_version": API_VERSION,
        "current_page": current_page,
        "total": total,
        "max_pages": max_pages,
        "products": products,
    }


def _parse_book_id_from_url(url: str) -> int | None:
    """Extract trailing numeric id from /book/{slug}_{id} or /book/{id}."""
    try:
        path = urlparse(url).path.rstrip("/")
    except Exception:
        return None
    m = re.search(r"/book/(?:.+_)?(\d+)$", path)
    if m:
        return int(m.group(1))
    return None


@router.post("/products")
async def torob_products(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    x_torob_token: str | None = Header(default=None, alias="X-Torob-Token"),
    x_torob_token_version: str | None = Header(
        default=None, alias="X-Torob-Token-Version"
    ),
    c_torob_token_version: str | None = Header(
        default=None, alias="C-Torob-Token-Version"
    ),
):
    auth_err = _authenticate(
        request,
        x_torob_token,
        x_torob_token_version or c_torob_token_version,
    )
    if auth_err is not None:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return _error("invalid JSON body")

    if not isinstance(body, dict) or not body:
        return _error("request body must be a non-empty JSON object")

    keys = set(body.keys())
    origin = site_origin()

    # Mode: list by page + sort
    if "page" in keys or "sort" in keys:
        if keys != {"page", "sort"}:
            return _error("page listing requires exactly page and sort parameters")
        if "page" not in body:
            return _error("page parameter is not provided")
        if "sort" not in body:
            return _error("sort parameter is not provided")
        try:
            page = int(body["page"])
        except (TypeError, ValueError):
            return _error("page must be an integer starting from 1")
        if page < 1:
            return _error("page must be an integer starting from 1")
        sort = body["sort"]
        if sort not in _SORT_VALUES:
            return _error("sort must be date_added_desc or date_updated_desc")

        count_q = select(func.count()).select_from(Book).where(Book.is_active.is_(True))
        total = int((await db.execute(count_q)).scalar_one() or 0)

        if sort == "date_updated_desc":
            order_col = func.coalesce(Book.updated_at, Book.created_at).desc()
        else:
            order_col = Book.created_at.desc()

        result = await db.execute(
            select(Book)
            .where(Book.is_active.is_(True))
            .options(selectinload(Book.categories))
            .order_by(order_col, Book.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        books = list(result.scalars().unique().all())
        products = [_book_to_product(b, origin) for b in books]
        return _envelope(products=products, current_page=page, total=total)

    # Mode: by page_uniques
    if "page_uniques" in keys:
        if keys != {"page_uniques"}:
            return _error("page_uniques must be the only parameter")
        uniques = body.get("page_uniques")
        if not isinstance(uniques, list) or not uniques:
            return _error("page_uniques must be a non-empty list")
        ids: list[int] = []
        for u in uniques:
            try:
                ids.append(int(str(u).strip()))
            except (TypeError, ValueError):
                continue
        if not ids:
            return _envelope(products=[], current_page=1, total=0)

        result = await db.execute(
            select(Book)
            .options(selectinload(Book.categories))
            .where(Book.id.in_(ids), Book.is_active.is_(True))
        )
        books = list(result.scalars().unique().all())
        by_id = {b.id: b for b in books}
        ordered = [by_id[i] for i in ids if i in by_id]
        products = [_book_to_product(b, origin) for b in ordered]
        return _envelope(products=products, current_page=1, total=len(products))

    # Mode: by page_urls
    if "page_urls" in keys:
        if keys != {"page_urls"}:
            return _error("page_urls must be the only parameter")
        urls = body.get("page_urls")
        if not isinstance(urls, list) or not urls:
            return _error("page_urls must be a non-empty list")

        ids: list[int] = []
        for u in urls:
            if not isinstance(u, str):
                continue
            bid = _parse_book_id_from_url(u)
            if bid is not None:
                ids.append(bid)

        if not ids:
            return _envelope(products=[], current_page=1, total=0)

        result = await db.execute(
            select(Book)
            .options(selectinload(Book.categories))
            .where(Book.id.in_(ids), Book.is_active.is_(True))
        )
        books = list(result.scalars().unique().all())
        by_id = {b.id: b for b in books}
        url_to_book: dict[str, Book] = {}
        for b in books:
            abs_url = _product_url(b, origin)
            url_to_book[abs_url] = b
            url_to_book[abs_url.rstrip("/")] = b

        ordered: list[Book] = []
        seen: set[int] = set()
        for u in urls:
            if not isinstance(u, str):
                continue
            book = url_to_book.get(u) or url_to_book.get(u.rstrip("/"))
            if book is None:
                bid = _parse_book_id_from_url(u)
                book = by_id.get(bid) if bid is not None else None
            if book and book.id not in seen:
                seen.add(book.id)
                ordered.append(book)

        products = [_book_to_product(b, origin) for b in ordered]
        return _envelope(products=products, current_page=1, total=len(products))

    return _error("unsupported request parameters")
