import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_async_db
from app.models import Book, DownloadLink

router = APIRouter(prefix="/media/proxy", tags=["media"])
logger = logging.getLogger(__name__)

# Use SECRET_KEY for signing and validating links
signer = URLSafeTimedSerializer(settings.SECRET_KEY)

DEFAULT_COVER_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "book" / "default.png"

# Covers change rarely; allow long browser/CDN cache (proxy still used — never direct FTP).
COVER_CACHE_CONTROL = "public, max-age=604800, stale-while-revalidate=86400"
HERO_CACHE_CONTROL = "public, max-age=86400, stale-while-revalidate=3600"
DEFAULT_COVER_CACHE_CONTROL = "public, max-age=86400"


def _cover_etag(folder_name: str, filename: str) -> str:
    digest = hashlib.sha1(f"{folder_name}/{filename}".encode()).hexdigest()[:16]
    return f'W/"{digest}"'


def _attachment_headers(
    storage_filename: str, download_name: str | None, book_id: int | None
) -> dict[str, str]:
    name = download_name or storage_filename
    return {
        "Content-Disposition": Book.content_disposition(name, book_id),
    }


def is_safe_path(path_part: str) -> bool:
    """
    Sanitize filename/folder_name to prevent path traversal.
    Allow alphanumeric, underscore, dash, dot. No slashes, no '..'.
    """
    if not path_part:
        return False
    # Check for path traversal attempts
    if ".." in path_part or "/" in path_part or "\\" in path_part:
        return False
    return True


async def stream_media(folder_name: str, filename: str):
    """
    Smart generator for file streaming:
    - If FTP_ENABLED is False, read directly from disk.
    - If FTP_ENABLED is True, stream from FTP server.
    """
    # Security: Validate paths to prevent traversal
    if not is_safe_path(folder_name) or not is_safe_path(filename):
        logger.warning(
            "Path traversal attempt blocked: %s/%s", folder_name, filename
        )
        yield b""
        return

    file_path = f"{folder_name}/{filename}"

    # Local Disk Mode
    if not settings.FTP_ENABLED:
        full_path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)
        # Extra check for local path
        if not os.path.abspath(full_path).startswith(os.path.abspath(settings.MEDIA_ROOT)):
            yield b""
            return

        if not os.path.exists(full_path):
            # Signal caller that file is missing by yielding nothing or handling exception
            # Generators can't easily raise HTTP exceptions that propagate cleanly to response status
            # if headers are already sent, but for StreamingResponse, if we yield nothing, it sends 200 OK empty.
            # We need to check existence BEFORE calling this in the route for 404.
            yield b""
            return

        async with aiofiles.open(full_path, "rb") as f:
            while True:
                chunk = await f.read(8192)
                if not chunk:
                    break
                yield chunk

    # FTP Server Mode
    else:
        try:
            from app.services.ftp_client import ftp_client

            async with ftp_client() as client:
                async with client.download_stream(file_path) as stream:
                    async for block in stream.iter_by_block(8192):
                        yield block
        except Exception as e:
            logger.error("FTP stream error (%s): %s", settings.FTP_HOST, e)
            yield b""


async def check_file_exists(folder_name: str, filename: str) -> bool:
    """Check if file exists (Local or FTP)"""
    if not settings.FTP_ENABLED:
        full_path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)
        return os.path.exists(full_path)
    else:
        try:
            from app.services.ftp_client import ftp_client

            async with ftp_client() as client:
                file_path = f"{folder_name}/{filename}"
                try:
                    await client.stat(file_path)
                    return True
                except Exception:
                    return False
        except Exception:
            return False


BOOK_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
    ".azw": "application/vnd.amazon.ebook",
    ".azw3": "application/vnd.amazon.ebook",
    ".fb2": "application/x-fictionbook+xml",
    ".djvu": "image/vnd.djvu",
    ".txt": "text/plain",
    ".rar": "application/vnd.rar",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
}


def book_media_type(filename: str) -> str:
    _, ext = os.path.splitext(filename.lower())
    return BOOK_CONTENT_TYPES.get(ext, "application/octet-stream")


@router.get("/cover/{folder_name}/{filename}")
async def proxy_cover(folder_name: str, filename: str, request: Request):
    """
    Public access to book covers (proxied). Browser-cacheable.
    """
    # Security: Validate file extension
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp"}
    _, ext = os.path.splitext(filename.lower())
    if ext not in allowed_exts:
        raise HTTPException(status_code=403, detail="Invalid media type")

    if not is_safe_path(folder_name) or not is_safe_path(filename):
        raise HTTPException(status_code=403, detail="Invalid path")

    etag = _cover_etag(folder_name, filename)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": COVER_CACHE_CONTROL,
            },
        )

    # Missing covers: serve a static default instead of 404 (avoids client retry spam)
    if not await check_file_exists(folder_name, filename):
        if DEFAULT_COVER_PATH.is_file():
            return FileResponse(
                DEFAULT_COVER_PATH,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": DEFAULT_COVER_CACHE_CONTROL,
                    "ETag": etag,
                },
            )
        return Response(status_code=404)

    media_type = "image/jpeg"
    if ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"

    return StreamingResponse(
        stream_media(folder_name, filename),
        media_type=media_type,
        headers={
            "Cache-Control": COVER_CACHE_CONTROL,
            "ETag": etag,
            "Accept-Ranges": "none",
        },
    )


@router.get("/hero/{filename}")
async def proxy_hero(filename: str, request: Request):
    """Public landing-page hero images (always from local MEDIA_ROOT/hero)."""
    if not is_safe_path(filename):
        raise HTTPException(status_code=403, detail="Invalid path")
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp"}
    _, ext = os.path.splitext(filename.lower())
    if ext not in allowed_exts:
        raise HTTPException(status_code=403, detail="Invalid media type")

    path = Path(settings.MEDIA_ROOT) / "hero" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Hero image not found")

    etag = _cover_etag("hero", filename)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": HERO_CACHE_CONTROL,
            },
        )

    media_type = "image/jpeg"
    if ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": HERO_CACHE_CONTROL,
            "ETag": etag,
        },
    )


async def stream_external_url(url: str):
    """Stream a remote file through our server so the client never sees the URL."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    logger.error("External download upstream status %s", resp.status)
                    return
                async for chunk in resp.content.iter_chunked(8192):
                    if chunk:
                        yield chunk
    except Exception as e:
        logger.error("External download stream error: %s", type(e).__name__)
        return


async def external_url_reachable(url: str) -> bool:
    """Lightweight check before streaming; never exposes the URL to clients."""
    timeout = aiohttp.ClientTimeout(total=20, sock_connect=10, sock_read=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=True) as resp:
                if resp.status < 400:
                    return True
                # Some hosts reject HEAD; try a ranged GET
            async with session.get(
                url, allow_redirects=True, headers={"Range": "bytes=0-0"}
            ) as resp:
                return resp.status < 400
    except Exception as e:
        logger.error("External download probe failed: %s", type(e).__name__)
        return False


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@router.get("/category/{filename}")
async def proxy_category_image(filename: str, request: Request):
    """Public category images (local MEDIA_ROOT/categories)."""
    if not is_safe_path(filename):
        raise HTTPException(status_code=403, detail="Invalid path")
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp"}
    _, ext = os.path.splitext(filename.lower())
    if ext not in allowed_exts:
        raise HTTPException(status_code=403, detail="Invalid media type")

    path = Path(settings.MEDIA_ROOT) / "categories" / filename
    if not path.is_file():
        if DEFAULT_COVER_PATH.is_file():
            return FileResponse(
                DEFAULT_COVER_PATH,
                media_type="image/jpeg",
                headers={"Cache-Control": DEFAULT_COVER_CACHE_CONTROL},
            )
        raise HTTPException(status_code=404, detail="Category image not found")

    etag = _cover_etag("categories", filename)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": HERO_CACHE_CONTROL},
        )

    media_type = "image/jpeg"
    if ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": HERO_CACHE_CONTROL, "ETag": etag},
    )


@router.get("/book/{folder_name}/{filename}")
async def proxy_book(
    folder_name: str,
    filename: str,
    token: str = Query(...),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Protected access to book files with timed token.
    External host URLs are resolved server-side from the DB and never appear
    in the token, query string, or redirect Location header.
    """
    from app.services.downloads import (
        DOWNLOAD_SIGNER_MAX_AGE,
        EXTERNAL_PROXY_FILENAME,
        token_expired,
    )

    try:
        data = signer.loads(token, salt="pdf-download", max_age=DOWNLOAD_SIGNER_MAX_AGE)

        if data.get("filename") != filename or data.get("folder") != folder_name:
            raise BadSignature("Token mismatch")

        if isinstance(data, dict) and token_expired(data):
            raise SignatureExpired("Embedded expiry passed")

    except SignatureExpired:
        raise HTTPException(status_code=403, detail="لینک دانلود منقضی شده است")
    except BadSignature:
        raise HTTPException(status_code=403, detail="توکن نامعتبر است")

    download_name = data.get("download_name") if isinstance(data, dict) else None
    book_id = data.get("book_id") if isinstance(data, dict) else None
    link_id = data.get("link_id") if isinstance(data, dict) else None

    if link_id is not None:
        link = (
            await db.execute(
                select(DownloadLink).where(DownloadLink.id == int(link_id))
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if (
            not link
            or link.revoked_at is not None
            or (link.expires_at and link.expires_at <= now)
        ):
            raise HTTPException(status_code=403, detail="لینک دانلود منقضی شده است")
        if book_id is not None and link.book_id and int(link.book_id) != int(book_id):
            raise HTTPException(status_code=403, detail="توکن نامعتبر است")
        link.download_count = int(link.download_count or 0) + 1
        link.used_at = now
        link.is_used = True
        await db.commit()

    book: Book | None = None
    if book_id is not None:
        book = (
            await db.execute(select(Book).where(Book.id == int(book_id)))
        ).scalar_one_or_none()

    # Prefer local/FTP file when present
    local_name = filename
    if book and book.file_filename:
        local_name = book.pdf_filename
        if await check_file_exists(folder_name, local_name):
            return StreamingResponse(
                stream_media(folder_name, local_name),
                media_type=book_media_type(local_name),
                headers=_attachment_headers(
                    local_name, download_name or book.download_filename, book.id
                ),
            )

    if filename != EXTERNAL_PROXY_FILENAME and await check_file_exists(
        folder_name, filename
    ):
        return StreamingResponse(
            stream_media(folder_name, filename),
            media_type=book_media_type(filename),
            headers=_attachment_headers(filename, download_name, book_id),
        )

    # External URL: load only from DB — never from token / redirects / HTML
    external = (book.external_file_url or "").strip() if book else ""
    if external and _is_http_url(external):
        if not await external_url_reachable(external):
            raise HTTPException(status_code=404, detail="فایل یافت نشد")
        out_name = download_name or (
            book.download_filename if book else filename
        )
        media = book_media_type(out_name)
        return StreamingResponse(
            stream_external_url(external),
            media_type=media,
            headers=_attachment_headers(out_name, out_name, book_id),
        )

    raise HTTPException(status_code=404, detail="فایل یافت نشد")
