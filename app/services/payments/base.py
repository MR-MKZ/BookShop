"""Payment gateway protocol and registry.

Add a new gateway by implementing PaymentGateway and registering it in GATEWAYS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class PaymentError(Exception):
    def __init__(self, message: str, result_code: int | None = None):
        super().__init__(message)
        self.result_code = result_code


@dataclass(frozen=True)
class GatewayInfo:
    id: str
    title: str
    description: str = ""
    is_default: bool = False


@runtime_checkable
class PaymentGateway(Protocol):
    id: str
    title: str
    description: str
    is_default: bool

    async def request_payment(
        self,
        amount_toman: int,
        order_id: int,
        description: str,
        mobile: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Register payment; return dict with at least track_id."""

    async def verify_payment(self, track_id: int | str) -> dict[str, Any]:
        """Verify payment; return dict with optional ref_id."""

    def start_url(self, track_id: int | str) -> str:
        """Redirect URL for the user to pay."""


_REGISTRY: dict[str, PaymentGateway] = {}


def register_gateway(gateway: PaymentGateway) -> None:
    _REGISTRY[gateway.id] = gateway


def get_gateway(gateway_id: str | None) -> PaymentGateway:
    if gateway_id and gateway_id in _REGISTRY:
        return _REGISTRY[gateway_id]
    for g in _REGISTRY.values():
        if g.is_default:
            return g
    if _REGISTRY:
        return next(iter(_REGISTRY.values()))
    raise PaymentError("هیچ درگاه پرداختی پیکربندی نشده است")


def list_gateways() -> list[GatewayInfo]:
    items = [
        GatewayInfo(
            id=g.id,
            title=g.title,
            description=getattr(g, "description", "") or "",
            is_default=bool(g.is_default),
        )
        for g in _REGISTRY.values()
    ]
    items.sort(key=lambda x: (not x.is_default, x.title))
    return items


def default_gateway_id() -> str:
    return get_gateway(None).id
