"""Zibal payment gateway adapter."""

from __future__ import annotations

from typing import Any

from app.services import zibal
from app.services.payments.base import PaymentError, register_gateway


class ZibalGateway:
    id = "zibal"
    title = "زیبال"
    description = "پرداخت آنلاین از طریق درگاه زیبال"
    is_default = True

    async def request_payment(
        self,
        amount_toman: int,
        order_id: int,
        description: str,
        mobile: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        try:
            data = await zibal.request_payment(
                amount_toman=amount_toman,
                order_id=order_id,
                description=description,
                mobile=mobile,
                callback_url=callback_url,
            )
        except zibal.ZibalError as e:
            raise PaymentError(str(e), result_code=e.result_code) from e
        return {
            "track_id": data.get("trackId"),
            "raw": data,
        }

    async def verify_payment(self, track_id: int | str) -> dict[str, Any]:
        try:
            data = await zibal.verify_payment(track_id)
        except zibal.ZibalError as e:
            raise PaymentError(str(e), result_code=e.result_code) from e
        # amount is in Rials from Zibal; orderId echoes what we sent at request time
        return {
            "ref_id": str(data.get("refNumber") or ""),
            "amount_rial": data.get("amount"),
            "order_id": data.get("orderId"),
            "raw": data,
        }

    def start_url(self, track_id: int | str) -> str:
        return zibal.payment_start_url(track_id)


register_gateway(ZibalGateway())
