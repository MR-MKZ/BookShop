"""Shared HTTP security helpers (redirects, cookies)."""

from __future__ import annotations

from urllib.parse import urlparse

from app.config import settings


def safe_next_url(next_url: str | None, fallback: str = "/") -> str:
    """Allow only same-origin relative paths (block open redirects)."""
    if not next_url:
        return fallback
    candidate = str(next_url).strip()
    if not candidate.startswith("/"):
        return fallback
    if candidate.startswith("//") or "://" in candidate:
        return fallback
    if "\n" in candidate or "\r" in candidate or "\\" in candidate:
        return fallback
    return candidate


def cookie_secure() -> bool:
    """Set Secure flag when the public site is HTTPS."""
    if settings.COOKIE_SECURE is not None:
        return settings.COOKIE_SECURE
    base = (settings.BASE_URL or "").strip().lower()
    return base.startswith("https://")


def cookie_kwargs(*, max_age: int | None = None, httponly: bool = True) -> dict:
    kwargs: dict = {
        "httponly": httponly,
        "samesite": "lax",
        "secure": cookie_secure(),
    }
    if max_age is not None:
        kwargs["max_age"] = max_age
    return kwargs


def public_origin_allowed(origin: str) -> bool:
    """True if Origin matches configured BASE_URL / DOMAIN_NAME."""
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    allowed_hosts = {
        urlparse(settings.BASE_URL).hostname or "",
        settings.DOMAIN_NAME.split(":")[0].lower(),
        "localhost",
        "127.0.0.1",
    }
    allowed_hosts = {h.lower() for h in allowed_hosts if h}
    return host in allowed_hosts
