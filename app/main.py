import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import Base, engine
from app.routes import auth, books, cart, media_proxy, store

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="BookShop", description="Digital Book Store", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
static_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(static_path, "templates"))

# Include routers
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(books.router, tags=["books"])
app.include_router(cart.router, prefix="/cart", tags=["cart"])
app.include_router(store.router, prefix="/store", tags=["store"])
app.include_router(media_proxy.router, prefix="/media", tags=["media"])
# app.include_router(checkout.router, prefix="/checkout", tags=["checkout"])
# app.include_router(admin.router, prefix="/admin", tags=["admin"])
# app.include_router(downloads.router, prefix="/download", tags=["downloads"])


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}
