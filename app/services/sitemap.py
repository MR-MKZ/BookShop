"""fastapi-sitemap integration with per-request rebuild (catalog is dynamic)."""

from __future__ import annotations

from datetime import timezone

from fastapi import Response
from fastapi_sitemap import SiteMap, URLInfo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import SessionLocal
from app.models import Book, Category
from app.routers.seo import site_origin


class DynamicSiteMap(SiteMap):
    """Like SiteMap.attach, but rebuild XML on each request."""

    def attach(self, route: str = "/sitemap.xml") -> None:
        async def _serve():
            xml = self._build_xml(list(self._collect_urls()))
            return Response(xml, media_type="application/xml")

        self.app.add_api_route(
            route, _serve, methods=["GET"], include_in_schema=False
        )


def _sync_session():
    if SessionLocal is not None:
        return SessionLocal()
    url = settings.DATABASE_URL
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif url.startswith("sqlite+aiosqlite://"):
        url = url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    eng = create_engine(url)
    return sessionmaker(autocommit=False, autoflush=False, bind=eng)()


def attach_sitemap(app) -> DynamicSiteMap:
    origin = site_origin()
    sitemap = DynamicSiteMap(
        app=app,
        base_url=origin,
        exclude_patterns=[
            r"^/api/",
            r"^/docs",
            r"^/redoc",
            r"^/openapi\.json$",
            r"^/admin",
            r"^/auth",
            r"^/cart",
            r"^/checkout",
            r"^/profile",
            r"^/payment",
            r"^/download",
            r"^/media",
            r"^/health",
            r"^/robots\.txt$",
            r"^/favicon",
            r"^/static",
            r"^/sitemap",
            r"^/google",
            r"^/\d+\.txt$",
        ],
        include_dynamic=False,
        changefreq="weekly",
        priority_map={
            "/": 1.0,
            "/search": 0.8,
            "/categories": 0.8,
        },
        gzip=False,
    )

    @sitemap.source
    def catalog_urls():
        yield URLInfo(f"{origin}/", changefreq="daily", priority=1.0)
        yield URLInfo(f"{origin}/search", changefreq="daily", priority=0.8)
        yield URLInfo(f"{origin}/categories", changefreq="weekly", priority=0.8)

        db = None
        try:
            db = _sync_session()
            books = (
                db.execute(
                    select(Book)
                    .where(Book.is_active == True)  # noqa: E712
                    .order_by(Book.id.asc())
                )
                .scalars()
                .all()
            )
            for book in books:
                path = book.path
                lastmod = None
                dt = book.updated_at or book.created_at
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    lastmod = dt.astimezone(timezone.utc).date().isoformat()
                yield URLInfo(
                    f"{origin}{path}",
                    changefreq="weekly",
                    priority=0.7,
                    lastmod=lastmod,
                )

            cats = (
                db.execute(
                    select(Category)
                    .where(Category.is_active == True)  # noqa: E712
                    .order_by(Category.id.asc())
                )
                .scalars()
                .all()
            )
            for cat in cats:
                yield URLInfo(
                    f"{origin}{cat.path}",
                    changefreq="weekly",
                    priority=0.6,
                )
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()

    sitemap.attach()
    return sitemap
