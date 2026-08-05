"""Payment gateways package. Import adapters so they self-register."""

from app.services.payments import torobpay_gateway as _torobpay  # noqa: F401
from app.services.payments import zibal_gateway as _zibal  # noqa: F401
from app.services.payments.base import (
    GatewayInfo,
    PaymentError,
    PaymentGateway,
    default_gateway_id,
    get_gateway,
    list_gateways,
    register_gateway,
)

__all__ = [
    "GatewayInfo",
    "PaymentError",
    "PaymentGateway",
    "default_gateway_id",
    "get_gateway",
    "list_gateways",
    "register_gateway",
]
