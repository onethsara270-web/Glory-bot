from glorybot.config import Settings
from glorybot.payments.apinepal import ApiNepalGateway
from glorybot.payments.base import PaymentGateway
from glorybot.payments.mock import MockGateway


def build_gateways(s: Settings) -> dict[str, PaymentGateway]:
    """Only Nepali payments (APINepal QR). Kept as a dict keyed by currency."""
    if s.payment_mode != "live":
        return {"NPR": MockGateway()}
    return {"NPR": ApiNepalGateway(s.apinepal_base_url, s.apinepal_secret_key.get_secret_value(), s.apinepal_qr_provider)}
