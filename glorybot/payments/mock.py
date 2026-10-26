import itertools
import time

from glorybot.payments.base import PaymentGateway, PaymentState, QRPayment


class MockGateway(PaymentGateway):
    """Dev/test gateway: a payment 'succeeds' `delay` seconds after creation (or when forced)."""
    is_mock = True

    def __init__(self, delay: float = 6.0):
        self.delay = delay
        self._ids = itertools.count(1)
        self.created: dict[str, dict] = {}
        self.force: dict[str, str] = {}

    async def create_qr(self, *, identifier, amount_paisa, description, customer_name, ttl_minutes=30) -> QRPayment:
        pid = f"MOCKPAY{next(self._ids)}"
        self.created[pid] = {"t": time.monotonic(), "paisa": amount_paisa, "identifier": identifier}
        return QRPayment(pid, None, f"MOCK-QR-{identifier}", None)

    async def get_payment(self, payment_id) -> PaymentState:
        c = self.created.get(payment_id)
        if c is None:
            return PaymentState("failed")
        status = self.force.get(payment_id) or ("success" if time.monotonic() - c["t"] >= self.delay else "pending")
        return PaymentState(status, c["paisa"], c["identifier"])
