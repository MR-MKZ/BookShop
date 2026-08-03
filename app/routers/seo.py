"""Public SEO endpoints: robots.txt and XML sitemaps."""

from __future__ import annotations

from datetime import timezone
from math import ceil
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_async_db
from app.models import Book

router = APIRouter(tags=["seo"])

SITEMAP_PAGE_SIZE = 10000


def _site_origin() -> str:
    return (settings.BASE_URL or "").rstrip("/") or "https://lamabook.ir"


def _xml_response(body: str) -> Response:
    return Response(content=body, media_type="application/xml; charset=utf-8")


@router.get("/robots.txt")
async def robots_txt():
    origin = _site_origin()
    body = f"""User-agent: *
Allow: /
Allow: /book/
Allow: /search
Allow: /static/
Allow: /media/proxy/cover/

Disallow: /admin/
Disallow: /auth/
Disallow: /cart
Disallow: /checkout
Disallow: /profile
Disallow: /payment/
Disallow: /download/
Disallow: /media/proxy/book/
Disallow: /health
Disallow: /docs
Disallow: /redoc
Disallow: /openapi.json

Sitemap: {origin}/sitemap.xml
"""
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get("/sitemap.xml")
async def sitemap_index(db: AsyncSession = Depends(get_async_db)):
    origin = _site_origin()
    total = (
        await db.scalar(
            select(func.count()).select_from(Book).where(Book.is_active == True)  # noqa: E712
        )
        or 0
    )
    pages = max(1, ceil(total / SITEMAP_PAGE_SIZE)) if total else 0

    urls = [
        f"  <sitemap>\n    <loc>{escape(origin)}/sitemap-static.xml</loc>\n  </sitemap>"
    ]
    for page in range(1, pages + 1):
        urls.append(
            f"  <sitemap>\n    <loc>{escape(origin)}/sitemap-books-{page}.xml</loc>\n  </sitemap>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</sitemapindex>\n"
    )
    return _xml_response(body)


@router.get("/sitemap-static.xml")
async def sitemap_static():
    origin = _site_origin()
    entries = [
        ("/", "daily", "1.0"),
        ("/search", "daily", "0.8"),
    ]
    parts = []
    for path, freq, priority in entries:
        parts.append(
            "  <url>\n"
            f"    <loc>{escape(origin + path)}</loc>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(parts)
        + "\n</urlset>\n"
    )
    return _xml_response(body)


@router.get("/sitemap-books-{page}.xml")
async def sitemap_books(page: int, db: AsyncSession = Depends(get_async_db)):
    if page < 1:
        raise HTTPException(status_code=404, detail="Sitemap not found")

    origin = _site_origin()
    total = (
        await db.scalar(
            select(func.count()).select_from(Book).where(Book.is_active == True)  # noqa: E712
        )
        or 0
    )
    pages = max(1, ceil(total / SITEMAP_PAGE_SIZE)) if total else 0
    if not pages or page > pages:
        raise HTTPException(status_code=404, detail="Sitemap not found")

    result = await db.execute(
        select(Book.id, Book.slug, Book.title_en, Book.title, Book.updated_at, Book.created_at)
        .where(Book.is_active == True)  # noqa: E712
        .order_by(Book.id.asc())
        .offset((page - 1) * SITEMAP_PAGE_SIZE)
        .limit(SITEMAP_PAGE_SIZE)
    )
    rows = result.all()

    parts = []
    for row in rows:
        if row.slug:
            path = f"/book/{row.slug}"
        else:
            path = (
                f"/book/{Book.build_slug(row.title_en, row.title, book_id=row.id)}"
            )
        loc = origin + path
        lastmod_dt = row.updated_at or row.created_at
        lastmod = ""
        if lastmod_dt is not None:
            if lastmod_dt.tzinfo is None:
                lastmod_dt = lastmod_dt.replace(tzinfo=timezone.utc)
            lastmod = (
                f"\n    <lastmod>{lastmod_dt.astimezone(timezone.utc).date().isoformat()}</lastmod>"
            )
        parts.append(
            "  <url>\n"
            f"    <loc>{escape(loc)}</loc>{lastmod}\n"
            "    <changefreq>weekly</changefreq>\n"
            "    <priority>0.7</priority>\n"
            "  </url>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(parts)
        + "\n</urlset>\n"
    )
    return _xml_response(body)
