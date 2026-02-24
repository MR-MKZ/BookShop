import os
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, or_
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
from app.schemas import UserCreate, UserResponse

router = APIRouter()

# Templates path
templates_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app", "templates"
)
templates = Jinja2Templates(directory=templates_path)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(
    request: Request,
    username: str = None, # Make optional to handle form data correctly if needed, or stick to form params
    password: str = None,
    db: AsyncSession = Depends(get_async_db)
):
    """Login endpoint"""
    # If using Form data directly (from HTML form), we might need Form(...) dependency
    # But sticking to previous signature for now, assuming JSON or query params,
    # BUT standard login usually uses Form data.
    # Let's assume the previous implementation was expecting query/body params matching these names.
    # To be safe for HTML forms, we should use Form.

    # However, to keep it simple and consistent with previous code (which didn't import Form):
    if not username or not password:
         # Try to parse form data if not provided
         form = await request.form()
         username = form.get("username")
         password = form.get("password")

    user = await authenticate_user(db, username, password)
    if not user:
        # For HTML response we might want to return template with error
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})

    if not user.is_active:
         return templates.TemplateResponse("login.html", {"request": request, "error": "Account disabled"})

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role.value},
        expires_delta=access_token_expires,
    )

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Register page"""
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
async def register(request: Request, db: AsyncSession = Depends(get_async_db)):
    """Register new user"""
    # Handle Form Data manually to support HTML form submission
    form = await request.form()
    email = form.get("email")
    username = form.get("username")
    password = form.get("password")
    full_name = form.get("full_name")

    if not email or not username or not password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Missing fields"})

    # Check if user exists
    result = await db.execute(
        select(User).where(or_(User.email == email, User.username == username))
    )
    existing_user = result.scalar_one_or_none()

    if existing_user:
        return templates.TemplateResponse("register.html", {"request": request, "error": "User already exists"})

    # Create new user
    hashed_password = get_password_hash(password)
    db_user = User(
        email=email,
        username=username,
        hashed_password=hashed_password,
        full_name=full_name,
        role=UserRole.USER,
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    # Auto login or redirect to login
    return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
async def logout():
    """Logout endpoint"""
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="access_token")
    return response


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    return current_user
