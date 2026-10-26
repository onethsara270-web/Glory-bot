from decimal import Decimal

import pytest

from glorybot.database.models import Order, OrderStatus as S, User
from glorybot.database.session import read, tx
from glorybot.providers.base import CreateOutcome as C
from glorybot.providers.mock_provider import MockProvider
from glorybot.services import order_service as osv
from glorybot.services import wallet_service as ws


async def _user(tg=10, money="1000", cur="NPR"):
    async with tx() as s:
        u = User(telegram_id=tg, display_name=f"user{tg}", currency=cur, total_spent=Decimal("0"))
        s.add(u)
        await s.flush()
        await ws.get_wallet(s, u.id, cur, lock=False)
        if money != "0":
            await ws.deposit(s, u.id, Decimal(money), f"seed{tg}", "test", currency=cur)
    return u


async def _order(u, squads=2):
    async with tx() as s:
        o = await osv.create_order(s, u, "BR", "12345678", squads, True)
    async with tx() as s:
        assert await osv.approve_order(s, o.id, 0, True) == "ok"
    return o


async def _wallet(u, cur="NPR"):
    async with read() as s:
        return await ws.get_wallet(s, u.id, cur, lock=False)


async def test_deposit_exactly_once(db):
    u = await _user(money="0")
    async with tx() as s:
        await ws.deposit(s, u.id, Decimal("100"), 1, "admin:1")
    with pytest.raises(ws.AlreadyApplied):
        async with tx() as s:
            await ws.deposit(s, u.id, Decimal("100"), 1, "admin:1")


async def test_insufficient_funds(db):
    u = await _user(money="300")
    with pytest.raises(ws.InsufficientFunds):
        async with tx() as s:
            await osv.create_order(s, u, "BR", "12345678", 1, True)  # 375 > 300


async def test_order_numbers_start_at_bny_1001_and_price_snapshot(db):
    u = await _user()
    o = await _order(u, 2)
    assert o.order_number == "BNY-1001" and o.total_amount == Decimal("750.00")
    async with tx() as s:
        await osv.set_price(s, Decimal("400"), 1, "NPR")
    async with tx() as s:
        assert await osv.approve_order(s, o.id, 0, True) == "state"  # double approval blocked
    assert await osv.execute_order(o.id, db) == S.SUBMITTED_TO_PROVIDER
    assert len(db.calls) == 2
    w = await _wallet(u)
    assert w.reserved == Decimal("0.00") and w.available == Decimal("250.00")
    async with read() as s:
        assert (await s.get(Order, o.id)).unit_price == Decimal("375.00")  # historical price unchanged
    async with tx() as s:
        await ws.deposit(s, u.id, Decimal("500"), "more", "test")
    o2 = await _order(u, 1)
    assert o2.order_number == "BNY-1002" and o2.unit_price == Decimal("400.00")


async def test_unclear_answer_that_did_create_is_detected(db):
    u = await _user()
    o = await _order(u, 1)
    prov = MockProvider(script=[C.UNCERTAIN])
    prov.create_on_uncertain = True
    assert await osv.execute_order(o.id, prov) == S.SUBMITTED_TO_PROVIDER
    assert len(prov.calls) == 1 and len(prov.groups) == 1   # never re-sent, found by group list
    assert (await _wallet(u)).available == Decimal("625.00")


async def test_unclear_answer_with_nothing_created_refunds_without_resend(db):
    u = await _user()
    o = await _order(u, 2)
    prov = MockProvider(script=[C.UNCERTAIN])
    assert await osv.execute_order(o.id, prov) == S.FAILED
    assert len(prov.calls) == 1
    assert await osv.execute_order(o.id, prov) is None and len(prov.calls) == 1
    w = await _wallet(u)
    assert w.available == Decimal("1000.00") and w.reserved == Decimal("0.00")


async def test_unclear_and_unverifiable_goes_to_reconciliation_once(db):
    u = await _user()
    o = await _order(u, 2)
    prov = MockProvider(script=[C.UNCERTAIN])
    prov.fail_listing = True
    assert await osv.execute_order(o.id, prov) == S.REQUIRES_RECONCILIATION
    assert len(prov.calls) == 1
    async with tx() as s:
        assert await osv.resolve_reconciliation(s, o.id, 1, "release")
    async with tx() as s:
        assert not await osv.resolve_reconciliation(s, o.id, 1, "release")  # second time is a no-op
    assert (await _wallet(u)).available == Decimal("1000.00")  # returned exactly once


async def test_rejected_provider_releases_funds(db):
    u = await _user()
    o = await _order(u, 1)
    prov = MockProvider(script=[C.REJECTED])
    assert await osv.execute_order(o.id, prov) == S.FAILED
    w = await _wallet(u)
    assert w.available == Decimal("1000.00") and w.reserved == Decimal("0.00")


async def test_busy_answers_are_retried(db):
    u = await _user()
    o = await _order(u, 1)
    prov = MockProvider(script=[(C.REJECTED, "busy"), (C.REJECTED, "busy")])
    assert await osv.execute_order(o.id, prov) == S.SUBMITTED_TO_PROVIDER
    assert len(prov.calls) == 3 and len(prov.groups) == 1


async def test_partial_fill_charges_only_started_squads(db):
    u = await _user(money="2000")
    o = await _order(u, 3)
    prov = MockProvider(script=[C.CREATED, (C.REJECTED, "insufficient_credits")])
    assert await osv.execute_order(o.id, prov) == S.SUBMITTED_TO_PROVIDER
    async with read() as s:
        row = await s.get(Order, o.id)
    assert row.squads == 1 and row.ordered_squads == 3 and row.total_amount == Decimal("375.00")
    w = await _wallet(u)
    assert w.available == Decimal("1625.00") and w.reserved == Decimal("0.00") and w.lifetime_spent == Decimal("375.00")


async def test_admin_price_change(db):
    async with tx() as s:
        await osv.set_price(s, Decimal("400"), 1, "NPR")
    async with read() as s:
        assert await osv.current_price(s, None, "NPR") == Decimal("400.00")
