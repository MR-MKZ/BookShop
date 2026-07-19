"""Payment flow with Zibal sandbox."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import get_async_db
from app.models import Book, Order, OrderItem, OrderStatus, User
from app.services import zibal

router = APIRouter(prefix="/payment", tags=["payment"])


@router.post("/buy/{book_id}")
async def buy_book(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Book).where(Book.id == book_id, Book.is_active == True))
    book = result.scalar_one_or_none()
    if not book:
        raise HTTPException(status_code=404, detail="کتاب یافت نشد")

    if not book.has_pdf:
        raise HTTPException(status_code=400, detail="فایل این کتاب هنوز آماده فروش نیست")

    # Already purchased?
    owned = await db.execute(
        select(OrderItem)
        .join(Order)
        .where(
            and_(
                Order.user_id == current_user.id,
                Order.status == OrderStatus.PAID,
                OrderItem.book_id == book_id,
            )
        )
    )
    if owned.scalar_one_or_none():
        return RedirectResponse(url="/profile", status_code=status.HTTP_303_SEE_OTHER)

    amount = int(book.price or 0)
    if amount <= 0:
        # Free book — grant immediately
        order = Order(
            user_id=current_user.id,
            status=OrderStatus.PAID,
            total_amount=0,
            paid_at=datetime.now(timezone.utc),
        )
        db.add(order)
        await db.flush()
        db.add(OrderItem(order_id=order.id, book_id=book.id, price=0, quantity=1))
        await db.commit()
        return RedirectResponse(url="/profile", status_code=status.HTTP_303_SEE_OTHER)

    order = Order(
        user_id=current_user.id,
        status=OrderStatus.PENDING,
        total_amount=amount,
    )
    db.add(order)
    await db.flush()
    db.add(OrderItem(order_id=order.id, book_id=book.id, price=amount, quantity=1))
    await db.commit()
    await db.refresh(order)

    callback = str(request.base_url).rstrip("/") + "/payment/callback"
    try:
        data = await zibal.request_payment(
            amount_toman=amount,
            order_id=order.id,
            description=f"خرید کتاب: {book.title}",
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


@router.get("/callback")
@router.post("/callback")
async def payment_callback(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """Zibal redirects here after payment (GET or POST)."""
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            params.update({k: form.get(k) for k in form})
        except Exception:
            pass

    success = str(params.get("success", ""))
    track_id = params.get("trackId") or params.get("track_id")
    order_id = params.get("orderId") or params.get("order_id")

    if not track_id:
        return RedirectResponse(url="/profile?pay=error", status_code=status.HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.payment_gateway_transaction_id == str(track_id))
    )
    order = result.scalar_one_or_none()

    if not order and order_id:
        result = await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == int(order_id))
        )
        order = result.scalar_one_or_none()

    if not order:
        return RedirectResponse(url="/profile?pay=error", status_code=status.HTTP_303_SEE_OTHER)

    if order.status == OrderStatus.PAID:
        return RedirectResponse(url="/profile?pay=ok", status_code=status.HTTP_303_SEE_OTHER)

    if success not in ("1", "true", "True"):
        order.status = OrderStatus.FAILED
        await db.commit()
        return RedirectResponse(url="/profile?pay=failed", status_code=status.HTTP_303_SEE_OTHER)

    try:
        verify = await zibal.verify_payment(track_id)
    except zibal.ZibalError:
        order.status = OrderStatus.FAILED
        await db.commit()
        return RedirectResponse(url="/profile?pay=failed", status_code=status.HTTP_303_SEE_OTHER)

    order.status = OrderStatus.PAID
    order.paid_at = datetime.now(timezone.utc)
    order.payment_gateway_ref_id = str(verify.get("refNumber") or "")
    await db.commit()

    return RedirectResponse(url="/profile?pay=ok", status_code=status.HTTP_303_SEE_OTHER)
