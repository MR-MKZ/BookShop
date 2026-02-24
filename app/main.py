import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import engine, Base
from app.routers import auth, media, store, admin

# Create database tables (Sync for now, or rely on Alembic)
# Base.metadata.create_all(bind=engine) # Better to use Alembic

app = FastAPI(title="Kabana Book Store", description="Digital Book Store", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
# Ensure static directory exists
static_path = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_path):
    os.makedirs(static_path)

app.mount("/static", StaticFiles(directory=static_path), name="static")

# Templates
templates_path = os.path.join(os.path.dirname(__file__), "templates")
if not os.path.exists(templates_path):
    os.makedirs(templates_path)
templates = Jinja2Templates(directory=templates_path)

# Include routers
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(store.router, tags=["store"]) # No prefix for store to handle root /
app.include_router(media.router, tags=["media"])
app.include_router(admin.router, tags=["admin"])


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}
