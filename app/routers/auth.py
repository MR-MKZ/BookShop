import os
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    get_password_hash,
)
from app.config import settings
from app.database import get_async_db
from app.models import User, UserRole
from app.schemas import UserResponse
from app.utils.phone import validate_iran_phone

router = APIRouter()

templates_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_path)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "phone": "",
            "next": request.query_params.get("next", "/"),
        },
    )


@router.post("/login")
async def login(request: Request, db: AsyncSession = Depends(get_async_db)):
    form = await request.form()
    phone = form.get("phone") or form.get("username")
    password = form.get("password")

    ok, phone_or_err = validate_iran_phone(phone)
    if not ok:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": phone_or_err,
                "phone": phone or "",
                "next": form.get("next") or "/",
            },
        )

    user = await authenticate_user(db, phone_or_err, password or "")
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "شماره تلفن یا رمز عبور اشتباه است",
                "phone": phone_or_err,
                "next": form.get("next") or "/",
            },
        )

    if not user.is_active:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "حساب کاربری غیرفعال است",
                "phone": phone_or_err,
                "next": form.get("next") or "/",
            },
        )

    access_token = create_access_token(
        data={"sub": user.phone, "role": user.role.value},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    next_url = form.get("next") or "/"
    if not str(next_url).startswith("/"):
        next_url = "/"

    response = RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(
        "register.html", {"request": request, "error": None, "form": {}}
    )


@router.post("/register")
async def register(request: Request, db: AsyncSession = Depends(get_async_db)):
    form = await request.form()
    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    phone = form.get("phone")
    password = form.get("password") or ""
    form_data = {
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone or "",
    }

    if len(first_name) < 2 or len(last_name) < 2:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "نام و نام خانوادگی باید حداقل ۲ کاراکتر باشند",
                "form": form_data,
            },
        )

    ok, phone_or_err = validate_iran_phone(phone)
    if not ok:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": phone_or_err, "form": form_data},
        )

    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "رمز عبور باید حداقل ۶ کاراکتر باشد",
                "form": form_data,
            },
        )

    existing = await db.execute(select(User).where(User.phone == phone_or_err))
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": "این شماره تلفن قبلاً ثبت شده است",
                "form": form_data,
            },
        )

    db_user = User(
        phone=phone_or_err,
        first_name=first_name,
        last_name=last_name,
        full_name=f"{first_name} {last_name}",
        hashed_password=get_password_hash(password),
        username=phone_or_err,
        email=None,
        role=UserRole.USER,
        is_active=True,
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    access_token = create_access_token(
        data={"sub": db_user.phone, "role": db_user.role.value},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="access_token")
    return response


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return current_user
