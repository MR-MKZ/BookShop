import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import AsyncSessionLocal
from app.routers import admin, auth, cart, media, payment, store
from app.routers.admin import AdminAuthRedirect
from app.routers.cart import cart_count_for_request

app = FastAPI(
    title="Kabana Book Store",
    description="Digital Book Store",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CartCountMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.cart_count = 0
        try:
            async with AsyncSessionLocal() as db:
                user = None
                try:
                    from jose import jwt
                    from sqlalchemy import select
                    from app.config import settings
                    from app.models import User

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
                request.state.cart_count = await cart_count_for_request(db, request, user)
        except Exception:
            request.state.cart_count = 0
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

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(store.router, tags=["store"])
app.include_router(cart.router, tags=["cart"])
app.include_router(payment.router, tags=["payment"])
app.include_router(media.router, tags=["media"])
app.include_router(admin.router, tags=["admin"])


@app.exception_handler(AdminAuthRedirect)
async def admin_auth_redirect_handler(request: Request, exc: AdminAuthRedirect):
    return RedirectResponse(
        url=f"/auth/login?next={exc.next_path}",
        status_code=303,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
