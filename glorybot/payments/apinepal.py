"""APINepal v2 gateway, built strictly from https://apinepal.com/docs/api.
Auth: `Authorization: Bearer sk_...`. QR: POST /v2/qr/{fonebiz|fonepay}. Status: GET /v2/payments/{id}
(reading a pending payment triggers a fresh provider check, so polling is the documented fallback to webhooks).
Idempotency-Key = our identifier, so retrying a timed-out create can never produce two payments."""
import logging
from decimal import Decimal

import httpx

from glorybot.payments.base import GatewayError, PaymentGateway, PaymentState, QRPayment

log = logging.getLogger(__name__)


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


class ApiNepalGateway(PaymentGateway):
    is_mock = False

    def __init__(self, base_url: str, secret_key: str, qr_provider: str = "fonebiz", timeout: float = 15.0):
        self._provider = qr_provider
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {secret_key}"},
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=5.0))

    async def create_qr(self, *, identifier: str, amount_paisa: int, description: str,
                        customer_name: str, ttl_minutes: int = 30) -> QRPayment:
        body = {"amount_paisa": amount_paisa, "currency": "NPR", "identifier": identifier,
                "description": description, "customer": {"first_name": customer_name[:40]}}
        last: GatewayError | None = None
        for _ in range(2):  # safe: the same Idempotency-Key replays the first result
            try:
                r = await self._client.post(f"/v2/qr/{self._provider}", json=body,
                                            headers={"Idempotency-Key": identifier})
            except httpx.HTTPError as e:
                last = GatewayError(f"network: {type(e).__name__}")
                continue
            data = _json(r)
            if r.status_code >= 500:
                last = GatewayError(f"HTTP {r.status_code}", r.status_code)
                continue
            if r.status_code >= 400 or data.get("success") is False:
                raise GatewayError(str(data.get("message") or data.get("code") or f"HTTP {r.status_code}")[:200],
                                   r.status_code)
            d = data.get("data", data)
            if not d.get("id"):
                raise GatewayError("provider returned no payment id")
            return QRPayment(str(d["id"]), d.get("qr_png_base64"), d.get("qr_string"), d.get("checkout_url"))
        raise last or GatewayError("unknown error")

    async def get_payment(self, payment_id: str) -> PaymentState:
        try:
            r = await self._client.get(f"/v2/payments/{payment_id}")
        except httpx.HTTPError as e:
            raise GatewayError(f"network: {type(e).__name__}")
        data = _json(r)
        if r.status_code >= 400 or data.get("success") is False:
            raise GatewayError(str(data.get("message") or f"HTTP {r.status_code}")[:200], r.status_code)
        d = data.get("data", data)
        raw = str(d.get("status", "")).lower()
        status = "success" if raw in ("success", "succeeded") else "failed" if raw == "failed" else "pending"
        paisa = d.get("amount_paisa")
        if paisa is None and d.get("amount") is not None:
            paisa = int((Decimal(str(d["amount"])) * 100).to_integral_value())
        return PaymentState(status, int(paisa) if paisa is not None else None, d.get("identifier"))

    async def ping(self) -> None:
        r = await self._client.get("/v2/payments", params={"limit": 1})
        if r.status_code >= 400:
            raise GatewayError(f"HTTP {r.status_code}", r.status_code)

    async def aclose(self) -> None:
        await self._client.aclose()
