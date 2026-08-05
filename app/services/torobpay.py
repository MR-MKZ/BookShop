"""Torob Pay CPG HTTP client (OAuth + payment token / verify / settle)."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

OAUTH_PATH = "/api/online/v1/oauth/token"
ELIGIBLE_PATH = "/api/online/offer/v1/eligible"
TOKEN_PATH = "/api/online/payment/v1/token"
VERIFY_PATH = "/api/online/payment/v1/verify"
SETTLE_PATH = "/api/online/payment/v1/settle"
REVERT_PATH = "/api/online/payment/v1/revert"

# Docs: minimum 200,000 Rials
MIN_AMOUNT_RIAL = 200_000
TOKEN_CACHE_TTL_SEC = 55 * 60

_oauth_token: str | None = None
_oauth_expires_at: float = 0.0


class TorobPayError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def torobpay_configured() -> bool:
    if not settings.TOROBPAY_ENABLED:
        return False
    return bool(
        (settings.TOROBPAY_CLIENT_ID or "").strip()
        and (settings.TOROBPAY_CLIENT_SECRET or "").strip()
        and (settings.TOROBPAY_USERNAME or "").strip()
        and (settings.TOROBPAY_PASSWORD or "").strip()
    )


def _base_url() -> str:
    return (settings.TOROBPAY_API_URL or "https://cpg.torobpay.com").rstrip("/")


def _basic_auth_header() -> str:
    raw = f"{settings.TOROBPAY_CLIENT_ID}:{settings.TOROBPAY_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def _error_message(body: dict[str, Any] | None, fallback: str) -> str:
    if not body:
        return fallback
    err = body.get("error")
    if isinstance(err, dict):
        return (
            str(err.get("user_message") or err.get("message") or fallback).strip()
            or fallback
        )
    if isinstance(err, str) and err.strip():
        return err.strip()
    return fallback


async def get_oauth_token(*, force: bool = False) -> str:
    global _oauth_token, _oauth_expires_at
    now = time.monotonic()
    if not force and _oauth_token and now < _oauth_expires_at:
        return _oauth_token

    url = _base_url() + OAUTH_PATH
    payload = {
        "username": settings.TOROBPAY_USERNAME,
        "password": settings.TOROBPAY_PASSWORD,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
        )
        try:
            body = resp.json()
        except Exception:
            body = None

    if resp.status_code != 200:
        raise TorobPayError(
            _error_message(body if isinstance(body, dict) else None, "خطا در احراز هویت ترب‌پی"),
            code=resp.status_code,
        )

    token = None
    if isinstance(body, dict):
        token = body.get("access_token")
        if not token and isinstance(body.get("response"), dict):
            token = body["response"].get("access_token")
    if not token:
        raise TorobPayError("توکن دسترسی ترب‌پی دریافت نشد")

    _oauth_token = str(token)
    _oauth_expires_at = now + TOKEN_CACHE_TTL_SEC
    return _oauth_token


async def _auth_headers() -> dict[str, str]:
    token = await get_oauth_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = _base_url() + path
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method, url, headers=headers, json=json_body, params=params
        )
        try:
            body = resp.json()
        except Exception:
            body = {}

    if resp.status_code == 401:
        # Refresh oauth once and retry
        await get_oauth_token(force=True)
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )
            try:
                body = resp.json()
            except Exception:
                body = {}

    if not isinstance(body, dict):
        body = {}

    if resp.status_code >= 400 or body.get("successful") is False:
        raise TorobPayError(
            _error_message(body, f"خطای ترب‌پی (HTTP {resp.status_code})"),
            code=resp.status_code,
        )
    return body


async def is_eligible(amount_rial: int) -> bool:
    """Return True if Torob Pay may be shown for this amount (fail closed)."""
    if amount_rial < MIN_AMOUNT_RIAL:
        return False
    if not torobpay_configured():
        return False
    try:
        body = await _request(
            "GET",
            ELIGIBLE_PATH,
            params={"amount": int(amount_rial)},
        )
        response = body.get("response") or {}
        return bool(response.get("eligible"))
    except TorobPayError as e:
        logger.warning("Torob Pay eligibility check failed: %s", e)
        return False
    except Exception:
        logger.exception("Torob Pay eligibility check error")
        return False


async def create_payment_token(payload: dict[str, Any]) -> dict[str, Any]:
    body = await _request("POST", TOKEN_PATH, json_body=payload)
    response = body.get("response") or {}
    payment_token = response.get("paymentToken")
    payment_page_url = response.get("paymentPageUrl")
    if not payment_token or not payment_page_url:
        raise TorobPayError("پاسخ صدور توکن پرداخت ناقص است")
    return {
        "payment_token": str(payment_token),
        "payment_page_url": str(payment_page_url),
        "raw": body,
    }


async def verify_payment(payment_token: str) -> dict[str, Any]:
    body = await _request(
        "POST", VERIFY_PATH, json_body={"paymentToken": str(payment_token)}
    )
    response = body.get("response") or {}
    return {
        "transaction_id": str(response.get("transactionId") or ""),
        "raw": body,
    }


async def settle_payment(payment_token: str) -> dict[str, Any]:
    body = await _request(
        "POST", SETTLE_PATH, json_body={"paymentToken": str(payment_token)}
    )
    response = body.get("response") or {}
    return {
        "transaction_id": str(response.get("transactionId") or ""),
        "raw": body,
    }


async def revert_payment(payment_token: str) -> dict[str, Any]:
    body = await _request(
        "POST", REVERT_PATH, json_body={"paymentToken": str(payment_token)}
    )
    response = body.get("response") or {}
    return {
        "transaction_id": str(response.get("transactionId") or ""),
        "raw": body,
    }
