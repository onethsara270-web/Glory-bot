from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from glorybot.database.models import OrderStatus as S
from glorybot.services import glory_service, stock_service
from glorybot.services.state_machine import InvalidTransition, check_transition
from glorybot.utils.money import fmt_money, fmt_npr, order_total, to_minor

NP = ZoneInfo("Asia/Kathmandu")


def test_totals_and_formats():
    assert order_total(Decimal("375"), 10) == Decimal("3750.00")
    assert fmt_npr(order_total(Decimal("375"), 3)) == "1,125"
    assert fmt_money(Decimal("1125"), "NPR") == "NPR 1,125" and fmt_money(Decimal("3.5"), "USDT") == "3.50 USDT"
    assert to_minor(Decimal("3.50")) == 350 and to_minor(Decimal("375")) == 37500


def test_state_machine():
    check_transition(S.PENDING_ADMIN_APPROVAL, S.APPROVED)
    for old, new in [(S.COMPLETED, S.PROCESSING), (S.REFUNDED, S.APPROVED), (S.APPROVED, S.APPROVED)]:
        with pytest.raises(InvalidTransition):
            check_transition(old, new)


def test_service_window_sunday_9pm_to_monday():
    assert stock_service.window_is_open(datetime(2026, 10, 11, 20, 59, tzinfo=NP))      # Sunday 8:59 PM
    assert not stock_service.window_is_open(datetime(2026, 10, 11, 21, 0, tzinfo=NP))   # Sunday 9:00 PM
    assert not stock_service.window_is_open(datetime(2026, 10, 11, 23, 59, tzinfo=NP))
    assert stock_service.window_is_open(datetime(2026, 10, 12, 0, 0, tzinfo=NP))        # Monday 12:00 AM
    assert stock_service.window_is_open(datetime(2026, 10, 14, 15, 0, tzinfo=NP))


def test_guild_name_parsing():
    assert glory_service.extract_name({"clan_name": " Bny Squad "}) == "Bny Squad"
    assert glory_service.extract_name({"success": True, "data": {"guild_name": "Alpha"}}) == "Alpha"
    assert glory_service.extract_name({"clan": {"info": {"name": "Deep"}}}) == "Deep"
    assert glory_service.extract_name({"success": True}) is None


def test_run_clock():
    from datetime import timedelta, timezone
    from glorybot.texts import clock_line
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert "7:40" in clock_line(now - timedelta(minutes=20), now) and "0:20" in clock_line(now - timedelta(minutes=20), now)
    assert "finished" in clock_line(now - timedelta(hours=9), now)


async def test_stock_rules(db):
    from glorybot.database.models import Region
    from glorybot.database.session import tx
    assert await stock_service.check_orderable(db, "BR", 1) >= 1
    db.credits["basic"] = 2
    stock_service.invalidate()
    with pytest.raises(stock_service.StockError):
        await stock_service.check_orderable(db, "BR", 3)
    db.credits["basic"] = 20
    stock_service.invalidate()
    snap = await stock_service.snapshot(db)
    assert {r["region"].code: r["stock"] for r in snap["rows"]}["BR"] == 20
    async with tx() as s:
        (await s.get(Region, "BR")).enabled = False
    with pytest.raises(stock_service.StockError):
        await stock_service.check_orderable(db, "BR", 1)
    await stock_service.set_conf("paused", "1")
    with pytest.raises(stock_service.StockError):
        await stock_service.check_orderable(db, "IN", 1)
