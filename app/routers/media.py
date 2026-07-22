import os
import re
from pathlib import Path

import aiofiles
import aioftp
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.models import Book

router = APIRouter(prefix="/media/proxy", tags=["media"])

# Use SECRET_KEY for signing and validating links
signer = URLSafeTimedSerializer(settings.SECRET_KEY)

DEFAULT_COVER_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "book" / "default.jpg"


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
        print(f"Security Alert: Path traversal attempt detected: {folder_name}/{filename}")
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
            async with aioftp.Client.context(
                host=settings.FTP_HOST,
                port=settings.FTP_PORT,
                user=settings.FTP_USER,
                password=settings.FTP_PASS,
                socket_timeout=15,
            ) as client:
                async with client.download_stream(file_path) as stream:
                    async for block in stream.iter_by_block(8192):
                        yield block
        except Exception as e:
            print(f"FTP Stream Error ({settings.FTP_HOST}): {e}")
            yield b""


async def check_file_exists(folder_name: str, filename: str) -> bool:
    """Check if file exists (Local or FTP)"""
    if not settings.FTP_ENABLED:
        full_path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)
        return os.path.exists(full_path)
    else:
        try:
            async with aioftp.Client.context(
                host=settings.FTP_HOST,
                port=settings.FTP_PORT,
                user=settings.FTP_USER,
                password=settings.FTP_PASS,
                socket_timeout=5,
            ) as client:
                file_path = f"{folder_name}/{filename}"
                # aioftp doesn't have a simple exists(), try stat or listing
                try:
                    await client.stat(file_path)
                    return True
                except:
                    return False
        except:
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
async def proxy_cover(folder_name: str, filename: str):
    """
    Public access to book covers.
    """
    # Security: Validate file extension
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp"}
    _, ext = os.path.splitext(filename.lower())
    if ext not in allowed_exts:
        raise HTTPException(status_code=403, detail="Invalid media type")

    # Missing covers: serve a static default instead of 404 (avoids client retry spam)
    if not await check_file_exists(folder_name, filename):
        if DEFAULT_COVER_PATH.is_file():
            return FileResponse(
                DEFAULT_COVER_PATH,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return Response(status_code=404)

    media_type = "image/jpeg"
    if ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"

    return StreamingResponse(stream_media(folder_name, filename), media_type=media_type)


@router.get("/hero/{filename}")
async def proxy_hero(filename: str):
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

    media_type = "image/jpeg"
    if ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/book/{folder_name}/{filename}")
async def proxy_book(folder_name: str, filename: str, token: str = Query(...)):
    """
    Protected access to book files with timed token.
    Token must include matching folder/filename and optionally user_id/book_id.
    """
    try:
        data = signer.loads(token, salt="pdf-download", max_age=3600)

        if data.get("filename") != filename or data.get("folder") != folder_name:
            raise BadSignature("Token mismatch")

    except SignatureExpired:
        raise HTTPException(status_code=403, detail="لینک دانلود منقضی شده است")
    except BadSignature:
        raise HTTPException(status_code=403, detail="توکن نامعتبر است")

    if not await check_file_exists(folder_name, filename):
        raise HTTPException(status_code=404, detail="فایل یافت نشد")

    download_name = data.get("download_name") if isinstance(data, dict) else None
    book_id = data.get("book_id") if isinstance(data, dict) else None

    return StreamingResponse(
        stream_media(folder_name, filename),
        media_type=book_media_type(filename),
        headers=_attachment_headers(filename, download_name, book_id),
    )
