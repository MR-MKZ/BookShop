"""Torob Pay (credit) payment gateway adapter."""

from __future__ import annotations

from typing import Any

from app.services import torobpay
from app.services.payments.base import PaymentError, register_gateway


class TorobPayGateway:
    id = "torobpay"
    title = "ترب‌پی (خرید اعتباری)"
    description = "پرداخت اقساطی از طریق درگاه ترب‌پی"
    is_default = False

    async def request_payment(
        self,
        amount_toman: int,
        order_id: int,
        description: str,
        mobile: str | None = None,
        callback_url: str | None = None,
        *,
        cart_items: list[dict[str, Any]] | None = None,
        customer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del description  # Torob uses cart item names instead
        if not cart_items:
            raise PaymentError("سبد خرید برای ترب‌پی الزامی است")
        if not customer:
            raise PaymentError("اطلاعات آدرس برای ترب‌پی الزامی است")

        amount_rial = int(amount_toman) * 10
        if amount_rial < torobpay.MIN_AMOUNT_RIAL:
            raise PaymentError(
                f"حداقل مبلغ ترب‌پی {torobpay.MIN_AMOUNT_RIAL // 10:,} تومان است"
            )

        items_payload = []
        for item in cart_items:
            items_payload.append(
                {
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item.get("title") or "کتاب"),
                    "count": int(item.get("count") or item.get("quantity") or 1),
                    "amount": int(item["amount"]),  # Rials
                    "category": str(item.get("category") or "کتاب"),
                }
            )

        cart_total = sum(i["amount"] * i["count"] for i in items_payload)
        # Prefer order total (after discount) as the charged amount
        payload: dict[str, Any] = {
            "amount": amount_rial,
            "paymentMethodTypeDto": "ONLINE_CREDIT",
            "returnURL": callback_url or "",
            "transactionId": str(order_id),
            "cartList": [
                {
                    "cartId": str(order_id),
                    "totalAmount": amount_rial,
                    "taxAmount": 0,
                    "shippingAmount": 0,
                    "isTaxIncluded": False,
                    "isShipmentIncluded": False,
                    "cartItems": items_payload,
                }
            ],
            "address": str(customer.get("address") or ""),
            "postalCode": str(customer.get("postal_code") or ""),
            "customer_full_name": str(customer.get("full_name") or ""),
            "city": str(customer.get("city") or ""),
            "province": str(customer.get("province") or ""),
            "registration_phone_number": str(
                customer.get("registration_phone") or mobile or ""
            ),
        }
        if mobile:
            payload["mobile"] = mobile

        discount_rial = max(0, cart_total - amount_rial)
        if discount_rial:
            payload["discountAmount"] = discount_rial

        try:
            data = await torobpay.create_payment_token(payload)
        except torobpay.TorobPayError as e:
            raise PaymentError(str(e), result_code=e.code) from e

        return {
            "track_id": data["payment_token"],
            "redirect_url": data["payment_page_url"],
            "raw": data.get("raw"),
        }

    async def verify_payment(self, track_id: int | str) -> dict[str, Any]:
        """Verify then settle (explicit settle per CPG docs)."""
        try:
            verified = await torobpay.verify_payment(str(track_id))
            settled = await torobpay.settle_payment(str(track_id))
        except torobpay.TorobPayError as e:
            raise PaymentError(str(e), result_code=e.code) from e

        ref = settled.get("transaction_id") or verified.get("transaction_id") or ""
        return {
            "ref_id": str(ref),
            "raw": {"verify": verified.get("raw"), "settle": settled.get("raw")},
        }

    def start_url(self, track_id: int | str) -> str:
        # paymentPageUrl is returned from request_payment as redirect_url.
        del track_id
        return ""


def _maybe_register() -> None:
    if torobpay.torobpay_configured():
        register_gateway(TorobPayGateway())


_maybe_register()
