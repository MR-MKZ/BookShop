"""Shopping cart: add/remove books and checkout multiple items."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_current_user_optional
from app.database import get_async_db
from app.models import Book, Cart, Order, OrderItem, OrderStatus, User
from app.services import zibal

router = APIRouter(tags=["cart"])
templates = Jinja2Templates(directory="app/templates")

CART_COOKIE = "cart_sid"


def _cart_session_id(request: Request) -> str | None:
    return request.cookies.get(CART_COOKIE)


def _new_session_cookie(response: RedirectResponse) -> str:
    sid = secrets.token_urlsafe(24)
    response.set_cookie(
        CART_COOKIE, sid, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax"
    )
    return sid


def _owner_filter(user: User | None, sid: str | None):
    parts = []
    if user:
        parts.append(Cart.user_id == user.id)
    if sid:
        parts.append(Cart.session_id == sid)
    if not parts:
        return None
    return or_(*parts)


async def cart_count_for_request(
    db: AsyncSession,
    request: Request,
    user: User | None,
) -> int:
    filt = _owner_filter(user, _cart_session_id(request))
    if filt is None:
        return 0
    return (await db.scalar(select(func.count()).select_from(Cart).where(filt))) or 0


async def _load_cart_items(
    db: AsyncSession, user: User | None, sid: str | None
) -> list[tuple[Cart, Book]]:
    filt = _owner_filter(user, sid)
    if filt is None:
        return []
    result = await db.execute(
        select(Cart, Book)
        .join(Book, Book.id == Cart.book_id)
        .where(filt)
        .order_by(Cart.id.desc())
    )
    return [(row[0], row[1]) for row in result.all()]


async def _owns_book(db: AsyncSession, user_id: int, book_id: int) -> bool:
    result = await db.execute(
        select(OrderItem.id)
        .join(Order)
        .where(
            and_(
                Order.user_id == user_id,
                Order.status == OrderStatus.PAID,
                OrderItem.book_id == book_id,
            )
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _clear_cart(db: AsyncSession, user: User | None, sid: str | None) -> None:
    filt = _owner_filter(user, sid)
    if filt is not None:
        await db.execute(delete(Cart).where(filt))


@router.get("/cart")
async def view_cart(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    sid = _cart_session_id(request)
    items = await _load_cart_items(db, current_user, sid)
    total = sum(float(book.price or 0) for _, book in items)

    return templates.TemplateResponse(
        "cart.html",
        {
            "request": request,
            "current_user": current_user,
            "items": items,
            "total": total,
            "cart_count": len(items),
            "query": "",
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/cart/add/{book_id}")
async def add_to_cart(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    book = (
        await db.execute(
            select(Book).where(Book.id == book_id, Book.is_active == True)  # noqa: E712
        )
    ).scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404, detail="کتاب یافت نشد")
    if not book.has_pdf:
        raise HTTPException(status_code=400, detail="فایل این کتاب آماده فروش نیست")

    if current_user and await _owns_book(db, current_user.id, book_id):
        return RedirectResponse(url="/profile", status_code=status.HTTP_303_SEE_OTHER)

    sid = _cart_session_id(request)
    filt = _owner_filter(current_user, sid)
    if filt is not None:
        existing = (
            await db.execute(
                select(Cart).where(and_(Cart.book_id == book_id, filt))
            )
        ).scalar_one_or_none()
        if existing:
            return RedirectResponse(
                url="/cart?msg=exists", status_code=status.HTTP_303_SEE_OTHER
            )

    response = RedirectResponse(url="/cart?msg=added", status_code=status.HTTP_303_SEE_OTHER)
    if not sid:
        sid = _new_session_cookie(response)

    db.add(
        Cart(
            user_id=current_user.id if current_user else None,
            session_id=sid,
            book_id=book_id,
            quantity=1,
        )
    )
    await db.commit()
    return response


@router.post("/cart/remove/{cart_id}")
async def remove_from_cart(
    cart_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    sid = _cart_session_id(request)
    filt = _owner_filter(current_user, sid)
    if filt is None:
        return RedirectResponse(url="/cart", status_code=status.HTTP_303_SEE_OTHER)

    item = (
        await db.execute(select(Cart).where(and_(Cart.id == cart_id, filt)))
    ).scalar_one_or_none()
    if item:
        await db.delete(item)
        await db.commit()

    return RedirectResponse(url="/cart?msg=removed", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cart/checkout")
async def checkout_cart(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    sid = _cart_session_id(request)
    items = await _load_cart_items(db, current_user, sid)
    if not items:
        return RedirectResponse(url="/cart?msg=empty", status_code=status.HTTP_303_SEE_OTHER)

    to_buy: list[Book] = []
    seen: set[int] = set()
    for cart_row, book in items:
        if book.id in seen:
            await db.delete(cart_row)
            continue
        if await _owns_book(db, current_user.id, book.id):
            await db.delete(cart_row)
            continue
        if not book.has_pdf or not book.is_active:
            continue
        seen.add(book.id)
        to_buy.append(book)

    await db.flush()

    if not to_buy:
        await db.commit()
        return RedirectResponse(url="/profile", status_code=status.HTTP_303_SEE_OTHER)

    total = sum(int(book.price or 0) for book in to_buy)

    if total <= 0:
        order = Order(
            user_id=current_user.id,
            status=OrderStatus.PAID,
            total_amount=Decimal("0"),
            paid_at=datetime.now(timezone.utc),
        )
        db.add(order)
        await db.flush()
        for book in to_buy:
            db.add(OrderItem(order_id=order.id, book_id=book.id, price=0, quantity=1))
        await _clear_cart(db, current_user, sid)
        await db.commit()
        return RedirectResponse(url="/profile?pay=ok", status_code=status.HTTP_303_SEE_OTHER)

    order = Order(
        user_id=current_user.id,
        status=OrderStatus.PENDING,
        total_amount=total,
    )
    db.add(order)
    await db.flush()
    for book in to_buy:
        db.add(
            OrderItem(
                order_id=order.id,
                book_id=book.id,
                price=book.price or 0,
                quantity=1,
            )
        )
    await db.commit()
    await db.refresh(order)

    callback = str(request.base_url).rstrip("/") + "/payment/callback"
    titles = "، ".join((b.title or "")[:40] for b in to_buy[:3])
    try:
        data = await zibal.request_payment(
            amount_toman=total,
            order_id=order.id,
            description=f"خرید سبد: {titles}",
            mobile=current_user.phone,
            callback_url=callback,
        )
    except zibal.ZibalError as e:
        order.status = OrderStatus.FAILED
        await db.commit()
        raise HTTPException(status_code=502, detail=str(e)) from e

    track_id = data.get("trackId")
    order.payment_gateway_transaction_id = str(track_id)
    await db.commit()

    return RedirectResponse(
        url=zibal.payment_start_url(track_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )
