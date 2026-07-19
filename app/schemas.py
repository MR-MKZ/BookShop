from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.utils.phone import validate_iran_phone


class UserCreate(BaseModel):
    first_name: str = Field(min_length=2, max_length=50)
    last_name: str = Field(min_length=2, max_length=50)
    phone: str
    password: str = Field(min_length=6, max_length=128)

    @field_validator("phone")
    @classmethod
    def phone_must_be_iranian(cls, v: str) -> str:
        ok, result = validate_iran_phone(v)
        if not ok:
            raise ValueError(result)
        return result

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_names(cls, v: str) -> str:
        return v.strip()


class UserLogin(BaseModel):
    phone: str
    password: str

    @field_validator("phone")
    @classmethod
    def phone_must_be_iranian(cls, v: str) -> str:
        ok, result = validate_iran_phone(v)
        if not ok:
            raise ValueError(result)
        return result


class UserResponse(BaseModel):
    id: int
    phone: str
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
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
    original_price: Decimal | None = None
    description: str | None
    file_format: str | None
    has_pdf: bool = False
    is_active: bool

    class Config:
        from_attributes = True


class BookDetail(BookResponse):
    isbn: str | None
    folder_name: str | None
    cover_filename: str | None = None


class BookAdminUpdate(BaseModel):
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    description: str | None = None
    price: Decimal | None = None
    original_price: Decimal | None = None
    is_active: bool | None = None


class OrderCreate(BaseModel):
    book_id: int


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
