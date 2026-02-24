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
    title_fa: str | None
    title_en: str | None
    author: str | None
    publisher: str | None
    price: str | None
    image_url: str | None
    description: str | None
    is_active: bool

    class Config:
        from_attributes = True


class BookDetail(BookResponse):
    isbn: str | None
    publish_year: str | None
    language: str | None
    pages: str | None
    file_format: str | None
    file_size: str | None
    edition: str | None
    availability: str | None
    category: str | None


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
