"""Shared FTP client helpers (remote download-host aware)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aioftp

from app.config import settings


async def enter_ftp_base(client: aioftp.Client) -> None:
    """Change into FTP_BASE_DIR when set (e.g. ``www`` on DL hosts)."""
    base = (settings.FTP_BASE_DIR or "").strip().strip("/")
    if not base:
        return
    for part in base.split("/"):
        if not part or part in (".", ".."):
            continue
        try:
            await client.change_directory(part)
        except aioftp.StatusCodeError:
            await client.make_directory(part)
            await client.change_directory(part)


@asynccontextmanager
async def ftp_client() -> AsyncIterator[aioftp.Client]:
    """Connected FTP client already inside ``FTP_BASE_DIR``."""
    async with aioftp.Client.context(
        host=settings.FTP_HOST,
        port=settings.FTP_PORT,
        user=settings.FTP_USER,
        password=settings.FTP_PASS,
        socket_timeout=30,
    ) as client:
        await enter_ftp_base(client)
        yield client
