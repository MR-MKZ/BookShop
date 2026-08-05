"""Public SEO: robots.txt. Sitemap is served via fastapi-sitemap in main.py."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.config import settings

router = APIRouter(tags=["seo"])


def site_origin() -> str:
    return (settings.BASE_URL or "").rstrip("/") or "https://lamabook.ir"


@router.get("/robots.txt")
async def robots_txt():
    origin = site_origin()
    body = f"""User-agent: *
Allow: /
Allow: /book/
Allow: /search
Allow: /categories
Allow: /category/
Allow: /static/
Allow: /media/proxy/cover/
Allow: /media/proxy/category/

Disallow: /admin/
Disallow: /auth/
Disallow: /cart
Disallow: /checkout
Disallow: /profile
Disallow: /payment/
Disallow: /download/
Disallow: /torob_api/
Disallow: /media/proxy/book/
Disallow: /health
Disallow: /docs
Disallow: /redoc
Disallow: /openapi.json

Sitemap: {origin}/sitemap.xml
"""
    return Response(content=body, media_type="text/plain; charset=utf-8")
