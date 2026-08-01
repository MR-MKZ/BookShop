"""Signed download link helpers."""

from __future__ import annotations

import time
from urllib.parse import quote

from app.models import Book
from app.routers.media import signer

# Ceiling for TimedSerializer; payload ``exp`` is the real deadline.
DOWNLOAD_SIGNER_MAX_AGE = 60 * 60 * 24 * 31


def make_download_token(
    book: Book, *, user_id: int | None, order_id: int | None, ttl_seconds: int
) -> str:
    return signer.dumps(
        {
            "folder": book.folder_name,
            "filename": book.pdf_filename,
            "download_name": book.download_filename,
            "user_id": user_id,
            "book_id": book.id,
            "order_id": order_id,
            "exp": int(time.time()) + int(ttl_seconds),
        },
        salt="pdf-download",
    )


def download_url(book: Book, token: str) -> str:
    return (
        f"/media/proxy/book/{quote(book.folder_name, safe='')}/"
        f"{quote(book.pdf_filename, safe='')}?token={quote(token, safe='')}"
    )


def token_expired(payload: dict) -> bool:
    exp = payload.get("exp")
    if exp is None:
        return False
    try:
        return int(time.time()) > int(exp)
    except (TypeError, ValueError):
        return True
