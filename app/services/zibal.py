"""Zibal payment gateway client (sandbox-ready)."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings

ZIBAL_REQUEST_URL = "https://gateway.zibal.ir/v1/request"
ZIBAL_VERIFY_URL = "https://gateway.zibal.ir/v1/verify"
ZIBAL_START_URL = "https://gateway.zibal.ir/start/{track_id}"


class ZibalError(Exception):
    def __init__(self, message: str, result_code: int | None = None):
        super().__init__(message)
        self.result_code = result_code


async def request_payment(
    amount_toman: int,
    order_id: int,
    description: str,
    mobile: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """
    Register a payment with Zibal.
    amount_toman is converted to Rials (*10) for the API.
    """
    payload: dict[str, Any] = {
        "merchant": settings.ZIBAL_MERCHANT,
        "amount": int(amount_toman) * 10,
        "callbackUrl": callback_url or settings.ZIBAL_CALLBACK_URL,
        "description": description,
        "orderId": str(order_id),
    }
    if mobile:
        payload["mobile"] = mobile

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(ZIBAL_REQUEST_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    if data.get("result") != 100:
        raise ZibalError(
            f"Zibal request failed: {data.get('message', data)}",
            result_code=data.get("result"),
        )
    return data


async def verify_payment(track_id: int | str) -> dict[str, Any]:
    payload = {
        "merchant": settings.ZIBAL_MERCHANT,
        "trackId": int(track_id),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(ZIBAL_VERIFY_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # 100 = success, 201 = already verified
    if data.get("result") not in (100, 201):
        raise ZibalError(
            f"Zibal verify failed: {data.get('message', data)}",
            result_code=data.get("result"),
        )
    return data


def payment_start_url(track_id: int | str) -> str:
    return ZIBAL_START_URL.format(track_id=track_id)
