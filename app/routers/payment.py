"""Payment callbacks and legacy buy redirect."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_async_db
from app.models import Order, OrderStatus
from app.routers.cart import CART_COOKIE
from app.routers.checkout import finalize_paid_order
from app.services.checkout_helpers import ORDER_ACCESS_COOKIE, ORDER_ACCESS_COOKIE_MAX_AGE
from app.services.payments import PaymentError, get_gateway
from app.utils.security import cookie_kwargs

router = APIRouter(prefix="/payment", tags=["payment"])


def _thanks_redirect(order: Order) -> RedirectResponse:
    token = order.access_token or ""
    url = f"/orders/{order.id}/thanks"
    if token:
        url += f"?t={token}"
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if token:
        response.set_cookie(
            ORDER_ACCESS_COOKIE,
            token,
            **cookie_kwargs(max_age=ORDER_ACCESS_COOKIE_MAX_AGE),
        )
    return response


@router.post("/buy/{book_id}")
async def buy_book(book_id: int):
    """Legacy instant-buy → checkout (guest-friendly)."""
    return RedirectResponse(
        url=f"/checkout/buy-now/{book_id}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


def _verify_matches_order(order: Order, verify: dict) -> bool:
    """Bind gateway verify payload to the DB order (amount + order id)."""
    expected_rial = int(Decimal(str(order.total_amount))) * 10
    amount = verify.get("amount_rial")
    if amount is not None:
        try:
            if int(amount) != expected_rial:
                return False
        except (TypeError, ValueError):
            return False

    raw_oid = verify.get("order_id")
    if raw_oid is not None and str(raw_oid).strip() != "":
        if str(raw_oid).strip() != str(order.id):
            return False
    return True


def _callback_success(params: dict, gateway_id: str | None) -> bool:
    """Gateway-specific success signal from callback query/form params."""
    if gateway_id == "torobpay" or str(params.get("state", "")).upper() in (
        "OK",
        "FAILED",
    ):
        return str(params.get("state", "")).upper() == "OK"
    success = str(params.get("success", ""))
    return success in ("1", "true", "True")


async def _lookup_order(
    db: AsyncSession,
    params: dict,
    gateway_id: str | None,
) -> Order | None:
    track_id = (
        params.get("trackId")
        or params.get("track_id")
        or params.get("paymentToken")
        or params.get("payment_token")
    )
    if track_id:
        result = await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.payment_gateway_transaction_id == str(track_id))
        )
        order = result.scalar_one_or_none()
        if order:
            return order

    # Torob Pay returnURL sends merchant transactionId (= order.id)
    if gateway_id == "torobpay" or params.get("state") is not None:
        txn = params.get("transactionId") or params.get("transaction_id")
        if txn and str(txn).strip().isdigit():
            result = await db.execute(
                select(Order)
                .options(selectinload(Order.items))
                .where(
                    Order.id == int(str(txn).strip()),
                    Order.payment_gateway == "torobpay",
                )
            )
            return result.scalar_one_or_none()
    return None


async def _handle_callback(
    request: Request,
    db: AsyncSession,
    gateway_id: str | None = None,
):
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            params.update({k: form.get(k) for k in form})
        except Exception:
            pass

    cart_sid = request.cookies.get(CART_COOKIE)
    order = await _lookup_order(db, params, gateway_id)
    if not order:
        return RedirectResponse(url="/orders/recover", status_code=status.HTTP_303_SEE_OTHER)

    gw_id = gateway_id or order.payment_gateway
    try:
        gateway = get_gateway(gw_id)
    except PaymentError:
        return _thanks_redirect(order)

    if order.status == OrderStatus.PAID:
        return _thanks_redirect(order)

    if not _callback_success(params, gw_id):
        order.status = OrderStatus.FAILED
        await db.commit()
        return _thanks_redirect(order)

    track_id = order.payment_gateway_transaction_id
    if not track_id:
        order.status = OrderStatus.FAILED
        await db.commit()
        return _thanks_redirect(order)

    try:
        verify = await gateway.verify_payment(track_id)
    except PaymentError:
        order.status = OrderStatus.FAILED
        await db.commit()
        return _thanks_redirect(order)

    if not _verify_matches_order(order, verify):
        order.status = OrderStatus.FAILED
        await db.commit()
        return _thanks_redirect(order)

    await finalize_paid_order(
        db,
        order,
        ref_id=str(verify.get("ref_id") or ""),
        cart_session_id=cart_sid,
    )
    await db.commit()
    return _thanks_redirect(order)


@router.get("/callback")
@router.post("/callback")
async def payment_callback(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    return await _handle_callback(request, db)


@router.get("/callback/{gateway_id}")
@router.post("/callback/{gateway_id}")
async def payment_callback_gateway(
    gateway_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    return await _handle_callback(request, db, gateway_id=gateway_id)
