from abc import ABC, abstractmethod
from dataclasses import dataclass


class GatewayError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class QRPayment:
    id: str
    qr_png_base64: str | None
    qr_string: str | None
    checkout_url: str | None
    qr_url: str | None = None  # hosted QR image (Binance Pay)


@dataclass
class PaymentState:
    status: str  # pending | success | failed
    amount_paisa: int | None = None
    identifier: str | None = None


class PaymentGateway(ABC):
    is_mock = False

    @abstractmethod
    async def create_qr(self, *, identifier: str, amount_paisa: int, description: str,
                        customer_name: str, ttl_minutes: int = 30) -> QRPayment:
        """amount_paisa = amount in minor units (paisa for NPR, cents for USDT)."""

    @abstractmethod
    async def get_payment(self, payment_id: str) -> PaymentState: ...

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None
