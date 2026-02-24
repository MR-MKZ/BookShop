import os

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Book
from app.schemas import BookDetail, BookResponse

router = APIRouter()

# Templates path
templates_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "kabana", "templates"
)
templates = Jinja2Templates(directory=templates_path)


@router.get("/books", response_class=HTMLResponse)
async def books_list(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    """List all books"""
    query = db.query(Book).filter(Book.is_active == True)

    if category:
        query = query.filter(Book.category == category)

    if search:
        query = query.filter(
            (Book.title_fa.contains(search))
            | (Book.title_en.contains(search))
            | (Book.author.contains(search))
        )

    total = query.count()
    books = query.offset((page - 1) * limit).limit(limit).all()

    return templates.TemplateResponse(
        "categories.html",
        {
            "request": request,
            "books": books,
            "page": page,
            "limit": limit,
            "total": total,
            "category": category,
            "search": search,
        },
    )


@router.get("/book/{book_id}", response_class=HTMLResponse)
async def book_detail(request: Request, book_id: int, db: Session = Depends(get_db)):
    """Book detail page"""
    book = db.query(Book).filter(Book.id == book_id, Book.is_active == True).first()
    if not book:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Book not found")

    return templates.TemplateResponse(
        "book-details.html", {"request": request, "book": book}
    )


@router.get("/api/books", response_model=list[BookResponse])
async def api_books_list(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    """API endpoint for books list"""
    query = db.query(Book).filter(Book.is_active == True)

    if category:
        query = query.filter(Book.category == category)

    if search:
        query = query.filter(
            (Book.title_fa.contains(search))
            | (Book.title_en.contains(search))
            | (Book.author.contains(search))
        )

    books = query.offset((page - 1) * limit).limit(limit).all()
    return books


@router.get("/api/book/{book_id}", response_model=BookDetail)
async def api_book_detail(book_id: int, db: Session = Depends(get_db)):
    """API endpoint for book detail"""
    book = db.query(Book).filter(Book.id == book_id, Book.is_active == True).first()
    if not book:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Book not found")
    return book
