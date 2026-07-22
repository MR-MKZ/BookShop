from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
import hashlib
import base64

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from fastapi.security.utils import get_authorization_scheme_param
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_async_db
from app.models import User, UserRole
from app.utils.phone import normalize_iran_phone

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt.
    Pre-hashes with SHA-256 to safely bypass the 72-byte limit without truncating.
    """
    if not password:
        raise ValueError("Password cannot be empty")

    b64_hash = base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(b64_hash, salt)
    return hashed_bytes.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    b64_hash = base64.b64encode(hashlib.sha256(plain_password.encode("utf-8")).digest())
    return bcrypt.checkpw(b64_hash, hashed_password.encode("utf-8"))


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def authenticate_user(db: AsyncSession, phone: str, password: str) -> User | None:
    """Authenticate a user by Iranian phone number."""
    phone = normalize_iran_phone(phone)
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()

    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


async def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_async_db),
) -> User:
    """Get current authenticated user from Header or Cookie"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        cookie_authorization: str = request.cookies.get("access_token")
        if cookie_authorization:
            scheme, param = get_authorization_scheme_param(cookie_authorization)
            if scheme.lower() == "bearer":
                token = param
            else:
                token = cookie_authorization

    if not token:
        raise credentials_exception

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        subject: str = payload.get("sub")
        if subject is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Prefer phone (new tokens); fall back to username for old tokens
    result = await db.execute(
        select(User).where((User.phone == subject) | (User.username == subject))
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception
    return user


async def get_current_user_optional(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_async_db),
) -> User | None:
    try:
        return await get_current_user(request=request, token=token, db=db)
    except HTTPException:
        return None


async def get_current_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Get current admin user"""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user
