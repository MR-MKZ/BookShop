from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import Book
from app.routers.media import signer  # Import signer for token generation

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/")
async def home(request: Request, db: AsyncSession = Depends(get_async_db)):
    result = await db.execute(
        select(Book)
        .where(Book.is_active == True)
        .order_by(desc(Book.created_at))
        .limit(6)
    )
    new_books = result.scalars().all()

    return templates.TemplateResponse(
        "index.html", {"request": request, "books": new_books, "query": ""}
    )


@router.get("/search")
async def search(request: Request, q: str = "", db: AsyncSession = Depends(get_async_db)):
    if not q:
        return templates.TemplateResponse(
            "search_results.html", {"request": request, "books": [], "query": ""}
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
        "search_results.html", {"request": request, "books": books, "query": q}
    )


@router.get("/book/{book_id}")
async def book_detail(
    book_id: int, request: Request, db: AsyncSession = Depends(get_async_db)
):
    result = await db.execute(select(Book).where(Book.id == book_id))
    book = result.scalar_one_or_none()

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Generate Secure Token for Download Link
    # We pass 'folder' and 'filename' to be validated later
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
            "query": ""
        }
    )
