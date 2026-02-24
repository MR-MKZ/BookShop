import os

import aiofiles
import aioftp
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

router = APIRouter(prefix="/media/proxy", tags=["media"])

# استفاده از SECRET_KEY برای امضا و اعتبارسنجی لینک‌ها
signer = URLSafeTimedSerializer(settings.SECRET_KEY)


async def stream_media(folder_name: str, filename: str):
    """
    ژنراتور هوشمند برای استریم فایل:
    - اگر FTP_ENABLED غیرفعال باشد، فایل را مستقیم از دیسک سرور می‌خواند.
    - اگر FTP_ENABLED فعال باشد، فایل را از سرور FTP (لوکال یا سرور مجزای خارجی) استریم می‌کند.
    """
    file_path = f"{folder_name}/{filename}"

    # حالت Local Disk
    if not settings.FTP_ENABLED:
        full_path = os.path.join(settings.MEDIA_ROOT, folder_name, filename)

        if not os.path.exists(full_path):
            yield b""
            return

        async with aiofiles.open(full_path, "rb") as f:
            while True:
                chunk = await f.read(8192)  # خواندن تکه تکه
                if not chunk:
                    break
                yield chunk

    # حالت FTP Server (لوکال یا خارجی در پروداکشن)
    else:
        try:
            # socket_timeout برای جلوگیری از هنگ کردن پراکسی در صورت کندی سرور FTP خارجی اضافه شده است
            async with aioftp.Client.context(
                host=settings.FTP_HOST,
                port=settings.FTP_PORT,
                user=settings.FTP_USER,
                password=settings.FTP_PASS,
                socket_timeout=15,
            ) as client:
                # استریم مستقیم از FTP سرور به کاربر بدون ذخیره موقت روی دیسک کانتینر شما
                async with client.download_stream(file_path) as stream:
                    async for block in stream.iter_by_block(8192):
                        yield block
        except Exception as e:
            print(f"FTP Stream Error ({settings.FTP_HOST}): {e}")
            yield b""


@router.get("/cover/{folder_name}/{filename}")
async def proxy_cover(folder_name: str, filename: str):
    """
    دسترسی عمومی به کاور کتاب‌ها.
    """
    media_type = "image/jpeg"
    if filename.lower().endswith(".png"):
        media_type = "image/png"
    elif filename.lower().endswith(".webp"):
        media_type = "image/webp"

    return StreamingResponse(stream_media(folder_name, filename), media_type=media_type)


@router.get("/book/{folder_name}/{filename}")
async def proxy_book(folder_name: str, filename: str, token: str = Query(...)):
    """
    دسترسی محافظت شده به فایل کتاب با توکن زمان‌دار.
    """
    try:
        # اعتبارسنجی توکن (اعتبار: 1 ساعت = 3600 ثانیه)
        data = signer.loads(token, salt="pdf-download", max_age=3600)

        # امنیت بیشتر: چک کردن اینکه توکن متعلق به همین فایل است
        if data.get("filename") != filename or data.get("folder") != folder_name:
            raise BadSignature("Token mismatch")

    except SignatureExpired:
        raise HTTPException(status_code=403, detail="لینک دانلود منقضی شده است.")
    except BadSignature:
        raise HTTPException(status_code=403, detail="توکن نامعتبر است.")

    return StreamingResponse(
        stream_media(folder_name, filename),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
