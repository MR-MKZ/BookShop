import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_async_db
from app.models import Book, DownloadLink, User

router = APIRouter(prefix="/media/proxy", tags=["media"])
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")

EXTERNAL_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Encoding": "identity",
}

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
    storage_filename: str,
    download_name: str | None,
    book_id: int | None,
    *,
    content_length: int | None = None,
    accept_ranges: bool = True,
    content_range: str | None = None,
) -> dict[str, str]:
    name = download_name or storage_filename
    headers = {
        "Content-Disposition": Book.content_disposition(name, book_id),
        "Cache-Control": "private, no-store",
    }
    if accept_ranges:
        headers["Accept-Ranges"] = "bytes"
    if content_length is not None and content_length >= 0:
        headers["Content-Length"] = str(int(content_length))
    if content_range:
        headers["Content-Range"] = content_range
    return headers


def _parse_range_header(range_header: str | None, size: int) -> tuple[int, int] | None:
    """Return inclusive (start, end) for a single bytes range, or None for full file."""
    if not range_header or size <= 0:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        # suffix: last N bytes
        length = int(end_s)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start >= size or start < 0:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


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
    ".bin": "application/octet-stream",
}


def book_media_type(filename: str) -> str:
    _, ext = os.path.splitext(filename.lower())
    return BOOK_CONTENT_TYPES.get(ext, "application/octet-stream")


async def media_file_size(folder_name: str, filename: str) -> int | None:
    """Return byte size for local/FTP stored file, or None if unknown."""
    if not is_safe_path(folder_name) or not is_safe_path(filename):
        return None
    if not settings.FTP_ENABLED:
        full_path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)
        try:
            return os.path.getsize(full_path)
        except OSError:
            return None
    try:
        from app.services.ftp_client import ftp_client

        async with ftp_client() as client:
            st = await client.stat(f"{folder_name}/{filename}")
            size = getattr(st, "size", None)
            return int(size) if size is not None else None
    except Exception:
        return None


async def stream_media_range(
    folder_name: str, filename: str, start: int = 0, end: int | None = None
):
    """Stream a byte range from local disk or FTP (inclusive end)."""
    if not is_safe_path(folder_name) or not is_safe_path(filename):
        return
    length = None if end is None else (end - start + 1)

    if not settings.FTP_ENABLED:
        full_path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)
        async with aiofiles.open(full_path, "rb") as f:
            await f.seek(start)
            remaining = length
            while True:
                chunk_size = 8192
                if remaining is not None:
                    if remaining <= 0:
                        break
                    chunk_size = min(chunk_size, remaining)
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
        return

    # FTP: download stream then skip (aioftp has no reliable seek on all servers)
    from app.services.ftp_client import ftp_client

    skipped = 0
    remaining = length
    try:
        async with ftp_client() as client:
            async with client.download_stream(f"{folder_name}/{filename}") as stream:
                async for block in stream.iter_by_block(8192):
                    if skipped < start:
                        need = start - skipped
                        if len(block) <= need:
                            skipped += len(block)
                            continue
                        block = block[need:]
                        skipped = start
                    if remaining is not None:
                        if remaining <= 0:
                            break
                        if len(block) > remaining:
                            block = block[:remaining]
                        remaining -= len(block)
                    if block:
                        yield block
    except Exception as e:
        logger.error("FTP ranged stream error: %s", e)


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


@dataclass
class ExternalOpenResult:
    status_code: int
    media_type: str
    content_length: int | None
    content_range: str | None
    accept_ranges: bool
    stream: object  # async generator


async def open_external_download(
    url: str, *, range_header: str | None = None
) -> ExternalOpenResult | None:
    """
    Open upstream GET (optionally with Range). Never redirects the browser
    to the external URL. Rejects HTML interstitial pages.
    """
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
    session = aiohttp.ClientSession(timeout=timeout)
    headers = dict(EXTERNAL_FETCH_HEADERS)
    if range_header:
        headers["Range"] = range_header
    try:
        resp = await session.get(url, allow_redirects=True, headers=headers)
    except Exception as e:
        await session.close()
        logger.error("External download open failed: %s", type(e).__name__)
        return None

    if resp.status >= 400 and resp.status != 416:
        logger.error("External download upstream status %s", resp.status)
        resp.release()
        await session.close()
        return None

    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    # Download hosts sometimes return an HTML landing/ads page instead of the file
    if ctype.startswith("text/html") or ctype == "application/xhtml+xml":
        logger.error("External URL returned HTML interstitial, not a file")
        resp.release()
        await session.close()
        return None

    cl_raw = resp.headers.get("Content-Length")
    content_length = None
    if cl_raw and str(cl_raw).isdigit():
        content_length = int(cl_raw)
    content_range = resp.headers.get("Content-Range")
    accept_ranges = True

    media_type = ctype or "application/octet-stream"
    if media_type in {"application/force-download", "binary/octet-stream"}:
        media_type = "application/octet-stream"

    status_code = resp.status if resp.status in (200, 206) else 200

    async def _gen():
        try:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                if chunk:
                    yield chunk
        except Exception as e:
            logger.error("External download stream error: %s", type(e).__name__)
        finally:
            resp.release()
            await session.close()

    return ExternalOpenResult(
        status_code=status_code,
        media_type=media_type,
        content_length=content_length,
        content_range=content_range,
        accept_ranges=accept_ranges,
        stream=_gen(),
    )


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def download_error_response(
    request: Request,
    *,
    status_code: int,
    page_title: str,
    message: str,
    icon: str = "exclamation-circle",
    current_user: User | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "download_error.html",
        {
            "request": request,
            "page_title": page_title,
            "message": message,
            "icon": icon,
            "current_user": current_user,
            "query": "",
        },
        status_code=status_code,
    )


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


async def _mark_link_download(db: AsyncSession, link: DownloadLink | None) -> None:
    if not link:
        return
    now = datetime.now(timezone.utc)
    link.download_count = int(link.download_count or 0) + 1
    link.used_at = now
    link.is_used = True
    await db.commit()


@router.get("/book/{folder_name}/{filename}")
async def proxy_book(
    folder_name: str,
    filename: str,
    request: Request,
    token: str | None = Query(None),
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
        client_download_name,
        extension_from_url,
        token_expired,
    )

    def err(
        status_code: int,
        page_title: str,
        message: str,
        icon: str = "exclamation-circle",
    ) -> HTMLResponse:
        return download_error_response(
            request,
            status_code=status_code,
            page_title=page_title,
            message=message,
            icon=icon,
            current_user=None,
        )

    if not token or not str(token).strip():
        return err(
            403,
            "لینک دانلود ناقص است",
            "این آدرس به‌تنهایی معتبر نیست. لطفاً از لینک کامل دانلود "
            "(همراه با توکن) که پس از خرید یا از پشتیبانی دریافت کرده‌اید استفاده کنید.",
            icon="link",
        )

    try:
        data = signer.loads(token, salt="pdf-download", max_age=DOWNLOAD_SIGNER_MAX_AGE)

        if data.get("filename") != filename or data.get("folder") != folder_name:
            raise BadSignature("Token mismatch")

        if isinstance(data, dict) and token_expired(data):
            raise SignatureExpired("Embedded expiry passed")

    except SignatureExpired:
        return err(
            403,
            "لینک دانلود منقضی شده است",
            "مهلت این لینک به پایان رسیده است. از بازیابی سفارش یا کتابخانه من "
            "لینک تازه بگیرید، یا با پشتیبانی تماس بگیرید.",
            icon="clock",
        )
    except BadSignature:
        return err(
            403,
            "لینک نامعتبر است",
            "توکن این لینک معتبر نیست. مطمئن شوید لینک را کامل کپی کرده‌اید.",
            icon="ban",
        )

    download_name = data.get("download_name") if isinstance(data, dict) else None
    book_id = data.get("book_id") if isinstance(data, dict) else None
    link_id = data.get("link_id") if isinstance(data, dict) else None

    managed_link: DownloadLink | None = None
    if link_id is not None:
        managed_link = (
            await db.execute(
                select(DownloadLink).where(DownloadLink.id == int(link_id))
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if (
            not managed_link
            or managed_link.revoked_at is not None
            or (managed_link.expires_at and managed_link.expires_at <= now)
        ):
            return err(
                403,
                "لینک دانلود منقضی شده است",
                "این لینک باطل یا منقضی شده است. از پشتیبانی لینک جدید بخواهید.",
                icon="clock",
            )
        if (
            book_id is not None
            and managed_link.book_id
            and int(managed_link.book_id) != int(book_id)
        ):
            return err(
                403,
                "لینک نامعتبر است",
                "توکن این لینک با کتاب مطابقت ندارد.",
                icon="ban",
            )

    book: Book | None = None
    if book_id is not None:
        book = (
            await db.execute(select(Book).where(Book.id == int(book_id)))
        ).scalar_one_or_none()

    not_found = err(
        404,
        "فایل یافت نشد",
        "در حال حاضر امکان دانلود این فایل وجود ندارد. کمی بعد دوباره تلاش کنید "
        "یا با پشتیبانی تماس بگیرید.",
        icon="file",
    )

    range_header = request.headers.get("range") or request.headers.get("Range")
    # Count only first/full downloads, not mid-file resume probes
    count_download = not (
        range_header and not range_header.strip().lower().startswith("bytes=0-")
    )

    async def _serve_stored(local_name: str, out_name: str, bid: int | None):
        size = await media_file_size(folder_name, local_name)
        byte_range = _parse_range_header(range_header, size) if size else None
        if range_header and size and byte_range is None:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{size}",
                    "Accept-Ranges": "bytes",
                },
            )
        if count_download:
            await _mark_link_download(db, managed_link)
        if byte_range is not None:
            start, end = byte_range
            length = end - start + 1
            return StreamingResponse(
                stream_media_range(folder_name, local_name, start, end),
                status_code=206,
                media_type=book_media_type(out_name),
                headers=_attachment_headers(
                    local_name,
                    out_name,
                    bid,
                    content_length=length,
                    content_range=f"bytes {start}-{end}/{size}",
                ),
            )
        return StreamingResponse(
            stream_media_range(folder_name, local_name, 0, (size - 1) if size else None)
            if size
            else stream_media(folder_name, local_name),
            media_type=book_media_type(out_name),
            headers=_attachment_headers(
                local_name,
                out_name,
                bid,
                content_length=size,
            ),
        )

    # Prefer local/FTP file when present
    if book and book.file_filename:
        local_name = book.pdf_filename
        if await check_file_exists(folder_name, local_name):
            out_name = download_name or client_download_name(book)
            return await _serve_stored(local_name, out_name, book.id)

    if filename != EXTERNAL_PROXY_FILENAME and await check_file_exists(
        folder_name, filename
    ):
        out_name = download_name or filename
        return await _serve_stored(filename, out_name, book_id)

    # External URL: load only from DB — never from token / redirects / HTML
    external = (book.external_file_url or "").strip() if book else ""
    if external and _is_http_url(external):
        opened = await open_external_download(external, range_header=range_header)
        if opened is None:
            return not_found

        # Prefer real extension from remote URL over stale token ".pdf"
        ext = extension_from_url(external)
        if book:
            out_name = client_download_name(book)
            # Keep DB format in sync for next token generation
            if ext and (book.file_format or "").lower() != ext:
                book.file_format = ext
                await db.commit()
        else:
            out_name = download_name or filename
            if ext and out_name.lower().endswith(".pdf") and ext != "pdf":
                out_name = out_name[: -4] + f".{ext}"

        # Never claim PDF when remote file is not PDF
        media = opened.media_type
        if ext and ext != "pdf" and media == "application/pdf":
            media = book_media_type(f"x.{ext}")
        elif ext:
            guessed = book_media_type(f"x.{ext}")
            if media in {"", "application/octet-stream"} or not media:
                media = guessed

        if count_download:
            await _mark_link_download(db, managed_link)

        headers = _attachment_headers(
            out_name,
            out_name,
            book.id if book else book_id,
            content_length=opened.content_length,
            accept_ranges=opened.accept_ranges,
            content_range=opened.content_range,
        )
        return StreamingResponse(
            opened.stream,
            status_code=opened.status_code,
            media_type=media or book_media_type(out_name),
            headers=headers,
        )

    return not_found
