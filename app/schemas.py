from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: str | None = None
    phone: str | None = None


class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    full_name: str | None
    role: str
    is_active: bool

    class Config:
        from_attributes = True


class BookResponse(BaseModel):
    id: int
    title: str | None
    author: str | None
    publisher: str | None
    price: Decimal | None
    description: str | None
    file_format: str | None
    is_active: bool

    class Config:
        from_attributes = True


class BookDetail(BookResponse):
    isbn: str | None
    folder_name: str | None


class OrderCreate(BaseModel):
    items: list[dict]  # [{"book_id": 1, "quantity": 1}]


class OrderResponse(BaseModel):
    id: int
    status: str
    total_amount: Decimal
    created_at: datetime

    class Config:
        from_attributes = True


class DownloadLinkResponse(BaseModel):
    token: str
    download_url: str
    expires_at: datetime
    is_used: bool
