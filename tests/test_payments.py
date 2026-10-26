import asyncio
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from glorybot.database.models import Order, OrderStatus as S, PaymentIntent, User, utcnow
from glorybot.database.session import read, tx
from glorybot.payments.base import PaymentState
from glorybot.payments.mock import MockGateway
from glorybot.services import order_service as osv
from glorybot.services import payment_service as pay
from glorybot.services import wallet_service as ws


class FakeBot:
    def __init__(self):
        self.sent = []

    async def _rec(self, *a, **k):
        self.sent.append((a, k))
        return type("Msg", (), {"message_id": len(self.sent)})()

    send_message = send_photo = edit_message_text = edit_message_caption = delete_message = _rec


class FakeApp:
    def __init__(self, provider):
        self.bot = FakeBot()
        self.bot_data = {"provider": provider, "gateways": {"NPR": MockGateway()}}


async def _user(tg=20, status="ACTIVE", cur="NPR"):
    async with tx() as s:
        u = User(telegram_id=tg, display_name=f"user{tg}", currency=cur, total_spent=Decimal("0"), status=status)
        s.add(u)
        await s.flush()
    return u


async def _intent(user, squads=2, ident="GPTEST000001", cur="NPR", kind="ORDER", unit="375", amount=None):
    async with tx() as s:
        total = Decimal(amount) if amount else Decimal(unit) * squads
        it = PaymentIntent(user_id=user.id, identifier=ident, provider_payment_id="P-" + ident, amount=total,
                           amount_paisa=int(total * 100), unit_price=Decimal(unit),
                           region_code="BR" if kind == "ORDER" else None, guild_uid="12345678" if kind == "ORDER" else None,
                           squads=squads if kind == "ORDER" else 0, currency=cur, kind=kind, is_mock=True,
                           expires_at=utcnow() + timedelta(minutes=15))
        s.add(it)
        await s.flush()
    return it


async def _drain(app):
    tasks = list(app.bot_data.get("tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)


async def _count_orders():
    async with read() as s:
        return (await s.execute(select(func.count(Order.id)))).scalar_one()


async def _wallet(u, cur="NPR"):
    async with read() as s:
        return await ws.get_wallet(s, u.id, cur, lock=False)


def _ok(it):
    return PaymentState("success", it.amount_paisa, it.identifier)


async def test_paid_qr_places_order_and_sends_bot_request_message(db):
    app, u = FakeApp(db), await _user()
    it = await _intent(u, squads=2)
    assert await pay.apply_state(app, it, _ok(it)) == "paid"
    await _drain(app)
    async with read() as s:
        o = (await s.execute(select(Order))).scalar_one()
    assert o.order_number == "BNY-1001" and o.status == S.SUBMITTED_TO_PROVIDER and len(db.calls) == 2
    assert o.guild_name == "Mock Guild 12345678"
    w = await _wallet(u)
    assert w.available == Decimal("0.00") and w.reserved == Decimal("0.00") and w.lifetime_spent == Decimal("750.00")
    texts = " ".join(str(x) for x in app.bot.sent)
    assert "BOT REQUEST HAVE BEEN SEND" in texts and "PLEASE ACCEPT BOT REQUEST IN GUILD" in texts
    assert "Glory added Monday 4:30 AM" in texts


async def test_same_payment_never_creates_two_orders(db):
    app, u = FakeApp(db), await _user()
    it = await _intent(u, squads=1)
    st = _ok(it)
    await asyncio.gather(pay.apply_state(app, it, st), pay.apply_state(app, it, st))
    await pay.apply_state(app, it, st)
    await _drain(app)
    assert await _count_orders() == 1 and len(db.calls) == 1


async def test_wrong_amount_places_no_order(db):
    app, u = FakeApp(db), await _user()
    it = await _intent(u, squads=2)
    assert await pay.apply_state(app, it, PaymentState("success", 100, it.identifier)) == "error"
    assert await _count_orders() == 0 and not db.calls
    async with read() as s:
        assert (await s.get(PaymentIntent, it.id)).status == "MISMATCH"


async def test_paid_but_order_impossible_credits_wallet(db):
    app, u = FakeApp(db), await _user(status="SUSPENDED")
    it = await _intent(u, squads=1)
    await pay.apply_state(app, it, _ok(it))
    await _drain(app)
    async with read() as s:
        assert (await s.get(PaymentIntent, it.id)).status == "CREDITED"
    assert await _count_orders() == 0 and (await _wallet(u)).available == Decimal("375.00") and not db.calls


async def test_topup_credits_wallet_exactly_once(db):
    app, u = FakeApp(db), await _user()
    it = await _intent(u, kind="TOPUP", amount="500")
    st = _ok(it)
    await asyncio.gather(pay.apply_state(app, it, st), pay.apply_state(app, it, st))
    await pay.apply_state(app, it, st)
    w = await _wallet(u)
    assert w.available == Decimal("500.00") and await _count_orders() == 0


async def test_refund_exactly_once(db):
    app, u = FakeApp(db), await _user()
    it = await _intent(u, squads=1)
    await pay.apply_state(app, it, _ok(it))
    await _drain(app)
    async with read() as s:
        oid = (await s.execute(select(Order.id))).scalar_one()
    async with tx() as s:
        assert await osv.refund_order(s, oid, 1)
    async with tx() as s:
        assert await osv.refund_order(s, oid, 1) is None
    assert (await _wallet(u)).available == Decimal("375.00")
