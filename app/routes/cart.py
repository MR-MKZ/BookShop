import os
import uuid

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Book, Cart, User

router = APIRouter()

# Templates path
templates_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "kabana", "templates"
)
templates = Jinja2Templates(directory=templates_path)


def get_or_create_session_id(session_id: str | None = Cookie(None)) -> str:
    """Get or create session ID for anonymous users"""
    if not session_id:
        return str(uuid.uuid4())
    return session_id


@router.get("/", response_class=HTMLResponse)
async def cart_page(
    request: Request,
    session_id: str | None = Cookie(None),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cart page"""
    if current_user:
        cart_items = db.query(Cart).filter(Cart.user_id == current_user.id).all()
    else:
        session_id = get_or_create_session_id(session_id)
        cart_items = db.query(Cart).filter(Cart.session_id == session_id).all()

    books = []
    total = 0
    for item in cart_items:
        book = db.query(Book).filter(Book.id == item.book_id).first()
        if book:
            books.append({"book": book, "quantity": item.quantity})
            try:
                price = float(book.price.replace(",", "")) if book.price else 0
                total += price * item.quantity
            except:
                pass

    response = templates.TemplateResponse(
        "cart.html", {"request": request, "cart_items": books, "total": total}
    )

    if not current_user and session_id:
        response.set_cookie(key="session_id", value=session_id)

    return response


@router.post("/add/{book_id}")
async def add_to_cart(
    book_id: int,
    quantity: int = 1,
    session_id: str | None = Cookie(None),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add book to cart"""
    book = db.query(Book).filter(Book.id == book_id, Book.is_active == True).first()
    if not book:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Book not found")

    session_id = get_or_create_session_id(session_id)

    # Check if item already in cart
    if current_user:
        cart_item = (
            db.query(Cart)
            .filter(Cart.user_id == current_user.id, Cart.book_id == book_id)
            .first()
        )
    else:
        cart_item = (
            db.query(Cart)
            .filter(Cart.session_id == session_id, Cart.book_id == book_id)
            .first()
        )

    if cart_item:
        cart_item.quantity += quantity
    else:
        cart_item = Cart(
            user_id=current_user.id if current_user else None,
            session_id=session_id if not current_user else None,
            book_id=book_id,
            quantity=quantity,
        )
        db.add(cart_item)

    db.commit()

    response = RedirectResponse(url="/cart/", status_code=302)
    if not current_user:
        response.set_cookie(key="session_id", value=session_id)
    return response


@router.post("/remove/{cart_item_id}")
async def remove_from_cart(
    cart_item_id: int,
    session_id: str | None = Cookie(None),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove item from cart"""
    if current_user:
        cart_item = (
            db.query(Cart)
            .filter(Cart.id == cart_item_id, Cart.user_id == current_user.id)
            .first()
        )
    else:
        session_id = get_or_create_session_id(session_id)
        cart_item = (
            db.query(Cart)
            .filter(Cart.id == cart_item_id, Cart.session_id == session_id)
            .first()
        )

    if cart_item:
        db.delete(cart_item)
        db.commit()

    response = RedirectResponse(url="/cart/", status_code=302)
    if not current_user and session_id:
        response.set_cookie(key="session_id", value=session_id)
    return response
