"""Guest / logged-in checkout, order thanks, and order recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import (
    create_access_token,
    get_current_user_optional,
    get_password_hash,
)
from app.config import settings
from app.database import get_async_db
from app.models import (
    Book,
    Cart,
    Coupon,
    CouponRedemption,
    Order,
    OrderItem,
    OrderStatus,
    User,
    UserRole,
)
from app.routers.cart import (
    _cart_session_id,
    _clear_cart,
    _load_cart_items,
    _owns_book,
    _owner_filter,
)
from app.services.checkout_helpers import (
    COUPON_COOKIE,
    ORDER_ACCESS_COOKIE,
    ORDER_ACCESS_COOKIE_MAX_AGE,
    compute_discount,
    find_valid_coupon,

    get_download_ttl_hours,
    get_download_ttl_seconds,
    new_order_access_token,
    validate_email,
)
from app.services.downloads import download_url, make_download_token
from app.services.payments import PaymentError, get_gateway, list_gateways
from app.utils.phone import normalize_iran_phone, validate_iran_phone
from app.utils.price import format_price, round_toman
from app.utils.security import cookie_kwargs

router = APIRouter(tags=["checkout"])
templates = Jinja2Templates(directory="app/templates")

templates.env.globals["format_price"] = format_price
templates.env.globals["round_toman"] = round_toman


def _set_order_access_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        ORDER_ACCESS_COOKIE,
        token,
        **cookie_kwargs(max_age=ORDER_ACCESS_COOKIE_MAX_AGE),
    )


def _thanks_url(order: Order) -> str:
    return f"/orders/{order.id}/thanks?t={order.access_token}"


async def _books_for_checkout(
    db: AsyncSession, user: User | None, sid: str | None
) -> list[Book]:
    items = await _load_cart_items(db, user, sid)
    to_buy: list[Book] = []
    seen: set[int] = set()
    for cart_row, book in items:
        if book.id in seen:
            continue
        if user and await _owns_book(db, user.id, book.id):
            await db.delete(cart_row)
            continue
        if not book.has_pdf or not book.is_active:
            continue
        seen.add(book.id)
        to_buy.append(book)
    await db.flush()
    return to_buy


async def _checkout_context(
    request: Request,
    db: AsyncSession,
    current_user: User | None,
    *,
    error: str | None = None,
    form_data: dict | None = None,
):
    sid = _cart_session_id(request)
    books = await _books_for_checkout(db, current_user, sid)
    subtotal = sum(round_toman(b.price) for b in books)

    coupon_code = request.cookies.get(COUPON_COOKIE) or ""
    if form_data and form_data.get("coupon_code") is not None:
        coupon_code = form_data.get("coupon_code") or ""

    coupon, coupon_error = await find_valid_coupon(db, coupon_code, subtotal)
    # Invalid / exhausted codes are ignored for pricing; only surface error after Apply
    # (explicit non-empty code in form_data), never as a checkout blocker.
    show_coupon_error = bool(coupon_error and (coupon_code or "").strip() and form_data is not None)
    if coupon_error:
        coupon = None

    discount = compute_discount(coupon, subtotal) if coupon else Decimal("0")
    total = subtotal - discount
    if total < 0:
        total = Decimal("0")

    fd = form_data or {}
    if current_user and not form_data:
        fd = {
            "first_name": current_user.first_name or "",
            "last_name": current_user.last_name or "",
            "phone": current_user.phone or "",
            "email": current_user.email or "",
        }

    return {
        "request": request,
        "current_user": current_user,
        "books": books,
        "subtotal": subtotal,
        "discount": discount,
        "total": total,
        "coupon": coupon,
        # Hidden field / applied code: only a *valid* coupon
        "coupon_code": coupon.code if coupon else "",
        # Text field after failed apply
        "coupon_input": (coupon_code if show_coupon_error else "") or "",
        "coupon_error": coupon_error if show_coupon_error else None,
        "clear_coupon_cookie": bool(coupon_error and coupon_code.strip()),
        "gateways": list_gateways(),
        "error": error,
        "form": fd,
        "query": "",
        "cart_count": len(books),
    }


@router.get("/checkout")
async def checkout_page(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    ctx = await _checkout_context(request, db, current_user)
    if not ctx["books"]:
        await db.commit()
        return RedirectResponse(url="/cart?msg=empty", status_code=status.HTTP_303_SEE_OTHER)
    await db.commit()
    response = templates.TemplateResponse("checkout.html", ctx)
    if ctx.get("clear_coupon_cookie"):
        response.delete_cookie(COUPON_COOKIE)
    return response


@router.post("/checkout/apply-coupon")
async def apply_coupon(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
    coupon_code: str = Form(""),
    action: str = Form("apply"),
):
    if action == "remove":
        response = RedirectResponse(url="/checkout", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(COUPON_COOKIE)
        return response

    sid = _cart_session_id(request)
    books = await _books_for_checkout(db, current_user, sid)
    subtotal = sum(round_toman(b.price) for b in books)
    coupon, err = await find_valid_coupon(db, coupon_code, subtotal)
    await db.commit()

    response = RedirectResponse(url="/checkout", status_code=status.HTTP_303_SEE_OTHER)
    if coupon and not err:
        response.set_cookie(
            COUPON_COOKIE,
            coupon.code,
            **cookie_kwargs(max_age=60 * 60 * 6),
        )
        return response

    ctx = await _checkout_context(
        request,
        db,
        current_user,
        form_data={"coupon_code": coupon_code},
    )
    if not ctx["books"]:
        bad = RedirectResponse(url="/cart?msg=empty", status_code=status.HTTP_303_SEE_OTHER)
        bad.delete_cookie(COUPON_COOKIE)
        return bad
    # Keep coupon_error visible in the coupon card (not as a page-blocking error)
    ctx["coupon_error"] = err or "کد تخفیف معتبر نیست"
    ctx["coupon_input"] = coupon_code or ""
    ctx["coupon_code"] = ""
    response = templates.TemplateResponse("checkout.html", ctx, status_code=400)
    response.delete_cookie(COUPON_COOKIE)
    return response


@router.post("/checkout/place-order")
async def place_order(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
    first_name: str = Form(...),
    last_name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    customer_note: str = Form(""),
    create_account: str | None = Form(None),
    password: str = Form(""),
    password_confirm: str = Form(""),
    payment_gateway: str = Form(""),
    coupon_code: str = Form(""),
):
    form_data = {
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "email": email,
        "customer_note": customer_note,
        "create_account": bool(create_account),
        "coupon_code": "",
        "payment_gateway": payment_gateway,
    }

    sid = _cart_session_id(request)
    books_early = await _books_for_checkout(db, current_user, sid)
    subtotal_early = sum(round_toman(b.price) for b in books_early)
    code = (coupon_code or request.cookies.get(COUPON_COOKIE) or "").strip()
    coupon, coupon_err = await find_valid_coupon(db, code, subtotal_early)
    # Stale / exhausted / invalid codes must not block checkout — ignore until a new valid code is applied
    clear_bad_coupon = bool(code and coupon_err)
    if coupon_err:
        coupon = None
    form_data["coupon_code"] = coupon.code if coupon else ""

    async def rerender(error: str):
        ctx = await _checkout_context(
            request, db, current_user, error=error, form_data=form_data
        )
        if not ctx["books"]:
            resp = RedirectResponse(
                url="/cart?msg=empty", status_code=status.HTTP_303_SEE_OTHER
            )
        else:
            resp = templates.TemplateResponse("checkout.html", ctx, status_code=400)
        if clear_bad_coupon:
            resp.delete_cookie(COUPON_COOKIE)
        return resp

    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    if not first_name:
        return await rerender("نام الزامی است")
    if not last_name:
        return await rerender("نام خانوادگی الزامی است")

    ok_phone, phone_or_err = validate_iran_phone(phone)
    if not ok_phone:
        return await rerender(phone_or_err)
    phone_norm = phone_or_err

    ok_email, email_or_err = validate_email(email)
    if not ok_email:
        return await rerender(email_or_err)
    email_norm = email_or_err

    want_account = bool(create_account) and not current_user
    if want_account:
        if len(password or "") < 6:
            return await rerender("رمز عبور باید حداقل ۶ کاراکتر باشد")
        if password != password_confirm:
            return await rerender("تکرار رمز عبور مطابقت ندارد")
        existing_phone = (
            await db.execute(select(User.id).where(User.phone == phone_norm))
        ).scalar_one_or_none()
        if existing_phone:
            return await rerender(
                "این شماره تلفن قبلاً ثبت شده است. لطفاً وارد شوید."
            )
        existing_email = (
            await db.execute(select(User.id).where(User.email == email_norm))
        ).scalar_one_or_none()
        if existing_email:
            return await rerender("این ایمیل قبلاً ثبت شده است. لطفاً وارد شوید.")

    books = books_early
    if not books:
        await db.commit()
        resp = RedirectResponse(url="/cart?msg=empty", status_code=status.HTTP_303_SEE_OTHER)
        if clear_bad_coupon:
            resp.delete_cookie(COUPON_COOKIE)
        return resp

    subtotal = subtotal_early
    discount = compute_discount(coupon, subtotal) if coupon else Decimal("0")
    total = subtotal - discount
    if total < 0:
        total = Decimal("0")

    gateway_id = (payment_gateway or "").strip()
    try:
        gateway = get_gateway(gateway_id or None)
    except PaymentError as e:
        return await rerender(str(e))
    user = current_user
    login_token: str | None = None
    if want_account:
        user = User(
            first_name=first_name,
            last_name=last_name,
            phone=phone_norm,
            email=email_norm,
            hashed_password=get_password_hash(password),
            role=UserRole.USER,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        login_token = create_access_token(
            data={"sub": user.phone, "role": user.role.value},
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )

    access_token = new_order_access_token()
    order = Order(
        user_id=user.id if user else None,
        status=OrderStatus.PENDING,
        subtotal_amount=subtotal,
        discount_amount=discount,
        total_amount=total,
        coupon_id=coupon.id if coupon else None,
        billing_first_name=first_name,
        billing_last_name=last_name,
        billing_phone=phone_norm,
        billing_email=email_norm,
        customer_note=(customer_note or "").strip() or None,
        access_token=access_token,
        payment_gateway=gateway.id,
    )
    db.add(order)
    await db.flush()
    for book in books:
        db.add(
            OrderItem(
                order_id=order.id,
                book_id=book.id,
                price=round_toman(book.price),
                quantity=1,
            )
        )

    # Free / fully discounted
    if total <= 0:
        order.status = OrderStatus.PAID
        order.paid_at = datetime.now(timezone.utc)
        if coupon:
            await _redeem_coupon(db, coupon, order)
        await _clear_cart(db, user, sid)
        await db.commit()
        response = RedirectResponse(
            url=_thanks_url(order), status_code=status.HTTP_303_SEE_OTHER
        )
        _set_order_access_cookie(response, access_token)
        response.delete_cookie(COUPON_COOKIE)
        if login_token:
            response.set_cookie(
                key="access_token",
                value=f"Bearer {login_token}",
                **cookie_kwargs(max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60),
            )
        return response

    await db.commit()
    await db.refresh(order)

    callback = (
        str(request.base_url).rstrip("/") + f"/payment/callback/{gateway.id}"
    )
    titles = "، ".join((b.title or "")[:40] for b in books[:3])
    try:
        data = await gateway.request_payment(
            amount_toman=int(total),
            order_id=order.id,
            description=f"سفارش #{order.id}: {titles}",
            mobile=phone_norm,
            callback_url=callback,
        )
    except PaymentError as e:
        order.status = OrderStatus.FAILED
        await db.commit()
        return await rerender(f"خطا در اتصال به درگاه: {e}")

    track_id = data.get("track_id")
    order.payment_gateway_transaction_id = str(track_id)
    await db.commit()

    response = RedirectResponse(
        url=gateway.start_url(track_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_order_access_cookie(response, access_token)
    response.delete_cookie(COUPON_COOKIE)
    if login_token:
        response.set_cookie(
            key="access_token",
            value=f"Bearer {login_token}",
            **cookie_kwargs(max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60),
        )
    return response


async def _redeem_coupon(db: AsyncSession, coupon: Coupon, order: Order) -> None:
    # Soft check again under same transaction
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        return
    existing = (
        await db.execute(
            select(CouponRedemption.id).where(CouponRedemption.order_id == order.id)
        )
    ).scalar_one_or_none()
    if existing:
        return
    coupon.used_count = int(coupon.used_count or 0) + 1
    db.add(CouponRedemption(coupon_id=coupon.id, order_id=order.id))


async def finalize_paid_order(
    db: AsyncSession,
    order: Order,
    ref_id: str = "",
    *,
    cart_session_id: str | None = None,
) -> None:
    if order.status == OrderStatus.PAID:
        return
    order.status = OrderStatus.PAID
    order.paid_at = datetime.now(timezone.utc)
    if ref_id:
        order.payment_gateway_ref_id = ref_id

    if order.coupon_id:
        coupon = (
            await db.execute(select(Coupon).where(Coupon.id == order.coupon_id))
        ).scalar_one_or_none()
        if coupon:
            await _redeem_coupon(db, coupon, order)

    book_ids = [item.book_id for item in order.items]
    if book_ids:
        owner_parts = []
        if order.user_id:
            owner_parts.append(Cart.user_id == order.user_id)
        if cart_session_id:
            owner_parts.append(Cart.session_id == cart_session_id)
        if owner_parts:
            await db.execute(
                delete(Cart).where(
                    and_(Cart.book_id.in_(book_ids), or_(*owner_parts))
                )
            )


@router.get("/orders/{order_id}/thanks")
async def order_thanks(
    order_id: int,
    request: Request,
    t: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.book))
        .where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        return RedirectResponse(url="/orders/recover", status_code=status.HTTP_303_SEE_OTHER)

    token = t or request.cookies.get(ORDER_ACCESS_COOKIE) or ""
    allowed = False
    if token and order.access_token and secrets_compare(token, order.access_token):
        allowed = True
    elif current_user and order.user_id and order.user_id == current_user.id:
        allowed = True

    if not allowed:
        return RedirectResponse(url="/orders/recover", status_code=status.HTTP_303_SEE_OTHER)

    downloads = []
    ttl_hours = await get_download_ttl_hours(db)
    if order.status == OrderStatus.PAID:
        ttl = await get_download_ttl_seconds(db)
        for item in order.items:
            if not item.book or not item.book.has_pdf:
                continue
            tok = make_download_token(
                item.book,
                user_id=order.user_id,
                order_id=order.id,
                ttl_seconds=ttl,
            )
            downloads.append(
                {
                    "book": item.book,
                    "url": download_url(item.book, tok),
                }
            )

    response = templates.TemplateResponse(
        "order_thanks.html",
        {
            "request": request,
            "current_user": current_user,
            "order": order,
            "downloads": downloads,
            "ttl_hours": ttl_hours,
            "query": "",
            "paid": order.status == OrderStatus.PAID,
            "pending": order.status == OrderStatus.PENDING,
            "failed": order.status
            in (OrderStatus.FAILED, OrderStatus.CANCELLED),
        },
    )
    if order.access_token:
        response.set_cookie(
            ORDER_ACCESS_COOKIE,
            order.access_token,
            **cookie_kwargs(max_age=ORDER_ACCESS_COOKIE_MAX_AGE),
        )
    return response


def secrets_compare(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a or "", b or "")


@router.get("/orders/recover")
async def recover_page(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    return templates.TemplateResponse(
        "order_recover.html",
        {
            "request": request,
            "current_user": current_user,
            "error": None,
            "order_id": "",
            "contact": "",
            "query": "",
        },
    )


@router.post("/orders/recover")
async def recover_order(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
    order_id: str = Form(...),
    contact: str = Form(...),
):
    def fail(msg: str):
        return templates.TemplateResponse(
            "order_recover.html",
            {
                "request": request,
                "current_user": current_user,
                "error": msg,
                "order_id": order_id,
                "contact": contact,
                "query": "",
            },
            status_code=400,
        )

    try:
        oid = int(str(order_id).strip())
    except (TypeError, ValueError):
        return fail("شماره سفارش معتبر نیست")

    contact_raw = (contact or "").strip()
    if not contact_raw:
        return fail("ایمیل یا شماره تلفن الزامی است")

    order = (
        await db.execute(select(Order).where(Order.id == oid))
    ).scalar_one_or_none()
    if not order:
        return fail("سفارشی با این مشخصات یافت نشد")

    phone_ok, phone_norm = validate_iran_phone(contact_raw)
    email_ok, email_norm = validate_email(contact_raw)

    matched = False
    if phone_ok and order.billing_phone == phone_norm:
        matched = True
    elif email_ok and (order.billing_email or "").lower() == email_norm:
        matched = True
    elif order.billing_phone and normalize_iran_phone(contact_raw) == order.billing_phone:
        matched = True
    elif order.billing_email and contact_raw.lower() == order.billing_email.lower():
        matched = True

    if not matched:
        return fail("سفارشی با این مشخصات یافت نشد")

    if not order.access_token:
        order.access_token = new_order_access_token()
        await db.commit()

    response = RedirectResponse(
        url=_thanks_url(order), status_code=status.HTTP_303_SEE_OTHER
    )
    _set_order_access_cookie(response, order.access_token)
    return response


@router.post("/checkout/buy-now/{book_id}")
async def buy_now(
    book_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """Add book to cart (if needed) and go to checkout."""
    book = (
        await db.execute(
            select(Book).where(Book.id == book_id, Book.is_active == True)  # noqa: E712
        )
    ).scalar_one_or_none()
    if not book or not book.has_pdf:
        return RedirectResponse(
            url=book.path if book else "/search",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if current_user and await _owns_book(db, current_user.id, book_id):
        return RedirectResponse(url="/profile", status_code=status.HTTP_303_SEE_OTHER)

    sid = _cart_session_id(request)
    response = RedirectResponse(url="/checkout", status_code=status.HTTP_303_SEE_OTHER)
    if not sid:
        from app.routers.cart import _new_session_cookie

        sid = _new_session_cookie(response)

    filt = _owner_filter(current_user, sid)
    existing = None
    if filt is not None:
        existing = (
            await db.execute(select(Cart).where(and_(Cart.book_id == book_id, filt)))
        ).scalar_one_or_none()
    if not existing:
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
