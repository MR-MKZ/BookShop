from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import Book, User
from app.routers.media import signer  # Import signer for token generation
from app.auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Helper to get user optionally
async def get_current_user_optional(request: Request, db: AsyncSession = Depends(get_async_db)) -> User | None:
    try:
        return await get_current_user(request=request, db=db)
    except HTTPException:
        return None

@router.get("/")
async def home(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional)
):
    result = await db.execute(
        select(Book)
        .where(Book.is_active == True)
        .order_by(desc(Book.created_at))
        .limit(6)
    )
    new_books = result.scalars().all()

    return templates.TemplateResponse(
        "index.html", {
            "request": request,
            "books": new_books,
            "query": "",
            "current_user": current_user
        }
    )


@router.get("/search")
async def search(
    request: Request,
    q: str = "",
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional)
):
    if not q:
        return templates.TemplateResponse(
            "search_results.html", {
                "request": request,
                "books": [],
                "query": "",
                "current_user": current_user
            }
        )

    query = (
        select(Book)
        .where(
            or_(
                Book.title.ilike(f"%{q}%"),
                Book.author.ilike(f"%{q}%"),
                Book.publisher.ilike(f"%{q}%"),
                Book.isbn.ilike(f"%{q}%"),
            )
        )
        .where(Book.is_active == True)
    )

    result = await db.execute(query)
    books = result.scalars().all()

    return templates.TemplateResponse(
        "search_results.html", {
            "request": request,
            "books": books,
            "query": q,
            "current_user": current_user
        }
    )


@router.get("/book/{book_id}")
async def book_detail(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional)
):
    result = await db.execute(select(Book).where(Book.id == book_id))
    book = result.scalar_one_or_none()

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Generate Secure Token for Download Link
    filename = f"{book.folder_name}.{book.file_format}"
    token = signer.dumps(
        {"folder": book.folder_name, "filename": filename},
        salt="pdf-download"
    )

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "book": book,
            "token": token,
            "filename": filename,
            "query": "",
            "current_user": current_user
        }
    )

@router.get("/profile")
async def profile(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """User Profile Page"""
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "current_user": current_user
        }
    )
