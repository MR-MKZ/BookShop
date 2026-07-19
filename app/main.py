import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routers import admin, auth, media, payment, store
from app.routers.admin import AdminAuthRedirect

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
