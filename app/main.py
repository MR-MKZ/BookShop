import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import settings
from app.database import AsyncSessionLocal
from app.routers import admin, auth, cart, checkout, media, payment, seo, store
from app.routers.admin import AdminAuthRedirect
from app.routers.cart import cart_count_for_request
from app.utils.security import safe_next_url

_docs = None if settings.is_production else "/docs"
_redoc = None if settings.is_production else "/redoc"
_openapi = None if settings.is_production else "/openapi.json"

app = FastAPI(
    title="لامابوک",
    description="مرجع دانلود کتاب‌های الکترونیکی خارجی",
    version="1.0.0",
    docs_url=_docs,
    redoc_url=_redoc,
    openapi_url=_openapi,
)

# Trust X-Forwarded-Proto/Host from nginx so url_for() emits https:// (no mixed content)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

_cors_origins = [
    settings.BASE_URL.rstrip("/"),
    f"https://{settings.DOMAIN_NAME}",
    f"http://{settings.DOMAIN_NAME}",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted({o for o in _cors_origins if o}),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class CartCountMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.cart_count = 0
        request.state.nav_categories = []
        try:
            async with AsyncSessionLocal() as db:
                user = None
                try:
                    from jose import jwt
                    from sqlalchemy import select

                    from app.models import Category, User

                    token = None
                    cookie = request.cookies.get("access_token")
                    if cookie and cookie.startswith("Bearer "):
                        token = cookie[7:]
                    if token:
                        payload = jwt.decode(
                            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
                        )
                        phone = payload.get("sub")
                        if phone:
                            result = await db.execute(select(User).where(User.phone == phone))
                            user = result.scalar_one_or_none()
                except Exception:
                    user = None
                try:
                    cats = (
                        await db.execute(
                            select(Category)
                            .where(Category.is_active == True)  # noqa: E712
                            .order_by(Category.sort_order.asc(), Category.name.asc())
                        )
                    ).scalars().all()
                    request.state.nav_categories = list(cats)
                except Exception:
                    request.state.nav_categories = []
                request.state.cart_count = await cart_count_for_request(db, request, user)
        except Exception:
            request.state.cart_count = 0
            request.state.nav_categories = []
        return await call_next(request)


app.add_middleware(CartCountMiddleware)

static_path = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_path):
    os.makedirs(static_path)

app.mount("/static", StaticFiles(directory=static_path), name="static")

templates_path = os.path.join(os.path.dirname(__file__), "templates")
if not os.path.exists(templates_path):
    os.makedirs(templates_path)
templates = Jinja2Templates(directory=templates_path)
# Relative static helper (optional); templates mostly use /static/... paths directly
templates.env.globals["static_url"] = lambda path: f"/static/{str(path).lstrip('/')}"

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(store.router, tags=["store"])
app.include_router(seo.router, tags=["seo"])
app.include_router(cart.router, tags=["cart"])
app.include_router(checkout.router, tags=["checkout"])
app.include_router(payment.router, tags=["payment"])
app.include_router(media.router, tags=["media"])
app.include_router(admin.router, tags=["admin"])

# Sitemap via fastapi-sitemap (dynamic rebuild; see app.services.sitemap)
from app.services.sitemap import attach_sitemap  # noqa: E402

attach_sitemap(app)


@app.exception_handler(AdminAuthRedirect)
async def admin_auth_redirect_handler(request: Request, exc: AdminAuthRedirect):
    next_path = safe_next_url(exc.next_path, "/admin/")
    return RedirectResponse(
        url=f"/auth/login?next={next_path}",
        status_code=303,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/29498328.txt")
async def domain_verification_file():
    path = os.path.join(static_path, "29498328.txt")
    return FileResponse(path, media_type="text/plain")


@app.get("/googlead705c5e6dd77f35.html")
async def google_site_verification_file():
    path = os.path.join(static_path, "googlead705c5e6dd77f35.html")
    return FileResponse(path, media_type="text/html")
