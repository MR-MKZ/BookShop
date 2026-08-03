"""Signed download link helpers."""

from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Book, DownloadLink
from app.routers.media import signer

# Ceiling for TimedSerializer; payload ``exp`` is the real deadline.
DOWNLOAD_SIGNER_MAX_AGE = 60 * 60 * 24 * 31

# Path segment for books that only have an external URL (never a real stored file).
EXTERNAL_PROXY_FILENAME = "external.bin"


def extension_from_url(url: str | None) -> str | None:
    """Best-effort file extension from an external download URL path."""
    if not url:
        return None
    try:
        path = unquote(urlparse(url).path or "")
    except Exception:
        return None
    _, ext = os.path.splitext(path)
    ext = (ext or "").lstrip(".").lower()
    if not ext or len(ext) > 12:
        return None
    if not re.fullmatch(r"[a-z0-9]+", ext):
        return None
    return ext


def proxy_filename(book: Book) -> str:
    """Filename used in the public proxy path (not the external host URL)."""
    if book.file_filename:
        return book.pdf_filename
    if book.external_file_url and str(book.external_file_url).strip():
        return EXTERNAL_PROXY_FILENAME
    return book.pdf_filename


def client_download_name(book: Book) -> str:
    """Human download name using real format (from upload or external URL)."""
    ext = None
    if book.file_filename:
        _, file_ext = os.path.splitext(book.file_filename)
        ext = (file_ext or "").lstrip(".").lower() or None
    if not ext and book.external_file_url:
        ext = extension_from_url(book.external_file_url)
    if not ext:
        ext = (book.file_format or "bin").lstrip(".").lower() or "bin"
    return Book.build_download_filename(
        book.id, book.title_en, book.title, ext
    )


def make_download_token(
    book: Book,
    *,
    user_id: int | None,
    order_id: int | None,
    ttl_seconds: int,
    link_id: int | None = None,
) -> str:
    folder = book.folder_name or Book.storage_folder(book.id)
    filename = proxy_filename(book)
    payload = {
        "folder": folder,
        "filename": filename,
        "download_name": client_download_name(book),
        "user_id": user_id,
        "book_id": book.id,
        "order_id": order_id,
        "exp": int(time.time()) + int(ttl_seconds),
    }
    if link_id is not None:
        payload["link_id"] = int(link_id)
    return signer.dumps(payload, salt="pdf-download")


def download_url(book: Book, token: str) -> str:
    folder = book.folder_name or Book.storage_folder(book.id)
    filename = proxy_filename(book)
    return (
        f"/media/proxy/book/{quote(folder, safe='')}/"
        f"{quote(filename, safe='')}?token={quote(token, safe='')}"
    )


def token_expired(payload: dict) -> bool:
    exp = payload.get("exp")
    if exp is None:
        return False
    try:
        return int(time.time()) > int(exp)
    except (TypeError, ValueError):
        return True


async def create_managed_download_link(
    db: AsyncSession,
    book: Book,
    *,
    ttl_hours: float,
    user_id: int | None = None,
    order_id: int | None = None,
    note: str | None = None,
) -> tuple[DownloadLink, str]:
    """Persist a DownloadLink row and return (row, absolute-path URL)."""
    if not book.has_file_ready:
        raise ValueError("file_not_ready")

    ttl_seconds = max(60, int(float(ttl_hours) * 3600))
    now = datetime.now(timezone.utc)
    link = DownloadLink(
        token=secrets.token_urlsafe(24),
        book_id=book.id,
        user_id=user_id,
        order_id=order_id,
        expires_at=now + timedelta(seconds=ttl_seconds),
        note=(note or "").strip() or None,
        download_count=0,
    )
    db.add(link)
    await db.flush()

    signed = make_download_token(
        book,
        user_id=user_id,
        order_id=order_id,
        ttl_seconds=ttl_seconds,
        link_id=link.id,
    )
    # Store the signed token so admin can re-copy the same URL until expiry
    link.token = signed
    await db.flush()
    return link, download_url(book, signed)
