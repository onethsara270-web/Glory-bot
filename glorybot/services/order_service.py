import asyncio
import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select, update

from glorybot.config import get_settings
from glorybot.database.models import (Order, OrderStatus, OrderStatusHistory, PricingConfiguration,
                                      ProviderRequest, ProviderRequestStatus as PR, RefundRecord, Region, User, utcnow)
from glorybot.database.session import read, tx
from glorybot.providers.base import CreateOutcome, CreateResult, ProviderAdapter
from glorybot.services import wallet_service as ws
from glorybot.services.audit import audit
from glorybot.services.state_machine import check_transition
from glorybot.utils.money import order_total

log = logging.getLogger(__name__)
S = OrderStatus

PAID_METHODS = ("QR",)          # orders funded by a verified gateway payment (money is credited, then reserved)
RETRY_CODES = {"busy", "rate_limited", "network"}   # provider said "not created" -> safe to try again
RETRY_WAITS = (2, 4, 6)         # seconds between retries
VERIFY_WAITS = (3, 5)           # seconds before each "did it get created anyway?" check


class OrderError(Exception):
    pass


def order_number_for(order_id: int) -> str:
    return f"BNY-{1000 + order_id}"


# ---------------------------------------------------------------- prices
def default_price(currency: str) -> Decimal:
    st = get_settings()
    return st.default_price_per_squad


async def current_price(s, region_code: str | None = None, currency: str = "NPR") -> Decimal:
    base = select(PricingConfiguration).where(PricingConfiguration.currency == currency)
    if region_code:
        r = (await s.execute(base.where(PricingConfiguration.region_code == region_code)
                             .order_by(PricingConfiguration.id.desc()).limit(1))).scalar_one_or_none()
        if r:
            return Decimal(str(r.price_per_squad))
    r = (await s.execute(base.where(PricingConfiguration.region_code.is_(None))
                         .order_by(PricingConfiguration.id.desc()).limit(1))).scalar_one_or_none()
    return Decimal(str(r.price_per_squad)) if r else default_price(currency)


async def set_price(s, price: Decimal, admin_id: int, currency: str = "NPR") -> None:
    s.add(PricingConfiguration(region_code=None, price_per_squad=price, currency=currency, set_by_admin=admin_id))
    await audit(s, admin_id, "SET_PRICE", currency, f"new price {price}")


# ---------------------------------------------------------------- state changes
async def transition(s, order_id: int, old: S, new: S, actor: str, reason: str | None = None) -> bool:
    """Atomic compare-and-set status change; False if someone else already moved the order."""
    check_transition(old, new)
    res = await s.execute(update(Order).where(Order.id == order_id, Order.status == old)
                          .values(status=new, updated_at=utcnow())
                          .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        return False
    s.add(OrderStatusHistory(order_id=order_id, old_status=old.value, new_status=new.value,
                             actor=actor, reason=reason))
    return True


async def create_order(s, user: User, region_code: str, guild_uid: str, squads: int, is_mock: bool,
                       payment: dict | None = None, unit_price: Decimal | None = None,
                       currency: str | None = None) -> Order:
    st = get_settings()
    currency = currency or user.currency or "NPR"
    if user.status != "ACTIVE":
        raise OrderError("Your account is suspended. Please contact support.")
    if not 1 <= squads <= st.max_squads_per_order:
        raise OrderError("Invalid squad quantity.")
    region = await s.get(Region, region_code)
    if not region or not region.enabled or not region.provider_region_id:
        raise OrderError("This region is out of stock right now.")
    unit = unit_price if unit_price is not None else await current_price(s, region_code, currency)
    total = order_total(unit, squads)
    order = Order(order_number="TMP-" + uuid.uuid4().hex[:16], user_id=user.id, region_code=region_code,
                  guild_uid=guild_uid, squads=squads, ordered_squads=squads, unit_price=unit, total_amount=total,
                  currency=currency, status=S.PENDING_ADMIN_APPROVAL, is_mock=is_mock,
                  settings_confirmed=True, submitted_at=utcnow(),
                  payment_method=(payment or {}).get("method", "WALLET"),
                  payment_remark=(payment or {}).get("remark"))
    s.add(order)
    await s.flush()
    order.order_number = order_number_for(order.id)
    if not payment:  # wallet payment: reserve now. Gateway-paid orders are funded in approve_order.
        await ws.reserve(s, user.id, total, order.order_number, currency=currency)  # may raise InsufficientFunds
    s.add(OrderStatusHistory(order_id=order.id, old_status=None, new_status=S.PENDING_ADMIN_APPROVAL.value,
                             actor=f"user:{user.telegram_id}", reason="submitted"))
    return order


async def approve_order(s, order_id: int, admin_id: int, provider_is_mock: bool) -> str:
    """Moves a funded order to APPROVED. admin_id 0 = automatic (nobody has to approve paid orders)."""
    order = await s.get(Order, order_id, populate_existing=True)
    if order is None:
        return "missing"
    if order.status != S.PENDING_ADMIN_APPROVAL:
        return "state"
    if order.is_mock != provider_is_mock:
        return "mode"
    region = await s.get(Region, order.region_code)
    if not region or not region.provider_region_id:
        return "region"
    paid = order.payment_method in PAID_METHODS
    if not paid:
        w = await ws.get_wallet(s, order.user_id, order.currency)
        if Decimal(str(w.reserved)) < Decimal(str(order.total_amount)):
            return "funds"
    actor = "system" if admin_id == 0 else f"admin:{admin_id}"
    if not await transition(s, order_id, S.PENDING_ADMIN_APPROVAL, S.APPROVED, actor):
        return "state"
    if paid:  # verified gateway payment: credit exactly once, then reserve for this order
        try:
            await ws.deposit(s, order.user_id, order.total_amount, f"order-{order.order_number}", actor,
                             currency=order.currency, reason="Payment received")
        except ws.AlreadyApplied:
            pass
        try:
            await ws.reserve(s, order.user_id, order.total_amount, order.order_number, actor, currency=order.currency)
        except ws.AlreadyApplied:
            pass
    await s.execute(update(Order).where(Order.id == order_id).values(admin_id=admin_id or None))
    if admin_id:
        await audit(s, admin_id, "APPROVE_ORDER", order.order_number)
    return "ok"


# ---------------------------------------------------------------- provider execution
async def _group_ids(provider: ProviderAdapter, uid: str) -> set[str] | None:
    try:
        return {g.group_id for g in await provider.list_groups() if g.clan_id == uid}
    except Exception:  # noqa: BLE001
        return None


async def _verify(provider: ProviderAdapter, uid: str, before: set[str] | None) -> tuple[str, str | None]:
    """After an unclear answer: did the provider create a new group for this guild anyway?
    Returns ('created', id) | ('absent', None) | ('unknown', None)."""
    if before is None:
        return "unknown", None
    listed_ok = False
    for wait in VERIFY_WAITS:
        await asyncio.sleep(wait)
        try:
            groups = await provider.list_groups()
        except Exception:  # noqa: BLE001
            listed_ok = False
            continue
        listed_ok = True
        async with read() as s:
            claimed = {g for (g,) in (await s.execute(select(ProviderRequest.provider_group_id).where(
                ProviderRequest.provider_group_id.is_not(None)))).all()}
        new = [g.group_id for g in groups if g.clan_id == uid and g.group_id not in before and g.group_id not in claimed]
        if new:
            return "created", new[0]
    return ("absent", None) if listed_ok else ("unknown", None)


async def execute_order(order_id: int, provider: ProviderAdapter) -> S | None:
    """Place one provider request per squad. Each request row is committed with sent_at BEFORE the HTTP call,
    so a crash can never lead to a blind re-send. Unclear answers are checked against the provider's group
    list (before/after) instead of being guessed; 'busy' answers are retried because they mean 'not created'."""
    async with tx() as s:
        order = await s.get(Order, order_id, populate_existing=True)
        if order is None or order.is_mock != provider.is_mock:
            return None
        if order.status == S.APPROVED:
            if not await transition(s, order_id, S.APPROVED, S.PROCESSING, "system"):
                return None
        elif order.status != S.PROCESSING:
            return None
        if order.ordered_squads is None:
            order.ordered_squads = order.squads
        region = await s.get(Region, order.region_code)
        prov_region, uid, squads = region.provider_region_id, order.guild_uid, order.ordered_squads
        have = {r.squad_index for r in (await s.execute(
            select(ProviderRequest).where(ProviderRequest.order_id == order_id))).scalars()}
        for i in range(1, squads + 1):
            if i not in have:
                s.add(ProviderRequest(order_id=order_id, squad_index=i))

    while True:
        async with tx() as s:
            row = (await s.execute(select(ProviderRequest).where(
                ProviderRequest.order_id == order_id, ProviderRequest.status == PR.PENDING,
                ProviderRequest.sent_at.is_(None)).order_by(ProviderRequest.squad_index).limit(1))
            ).scalar_one_or_none()
            if row is None:
                break
            row.sent_at = utcnow()
            row_id = row.id
        before = await _group_ids(provider, uid)
        res: CreateResult | None = None
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                res = await provider.create_order(provider_region=prov_region, guild_uid=uid)
            except Exception as e:  # bug or unexpected failure after sending: outcome unknown
                res = CreateResult(CreateOutcome.UNCERTAIN, error_code="exception", message=type(e).__name__)
            if res.outcome == CreateOutcome.REJECTED and res.error_code in RETRY_CODES and attempt < len(RETRY_WAITS):
                await asyncio.sleep(RETRY_WAITS[attempt])
                continue
            break
        if res.outcome == CreateOutcome.UNCERTAIN:
            state, gid = await _verify(provider, uid, before)
            if state == "created":
                res = CreateResult(CreateOutcome.CREATED, group_id=gid, http_status=res.http_status,
                                   error_code="verified", message="found in group list after unclear answer", raw=res.raw)
            elif state == "absent":
                res = CreateResult(CreateOutcome.REJECTED, http_status=res.http_status, error_code="verified_absent",
                                   message="checked group list: nothing was created", raw=res.raw)
        async with tx() as s:
            row = await s.get(ProviderRequest, row_id)
            row.status = PR[res.outcome.value]
            row.provider_group_id = res.group_id
            row.http_status = res.http_status
            row.sanitized_response = res.raw
            row.error = f"{res.error_code}: {res.message}" if res.error_code and res.outcome != CreateOutcome.CREATED else None
            if res.outcome != CreateOutcome.CREATED:
                await _halt_remaining(s, order_id)
        if res.outcome != CreateOutcome.CREATED:
            break
    return await finalize_order(order_id)


async def _halt_remaining(s, order_id: int) -> None:
    rows = (await s.execute(select(ProviderRequest).where(
        ProviderRequest.order_id == order_id, ProviderRequest.status == PR.PENDING,
        ProviderRequest.sent_at.is_(None)))).scalars()
    for r in rows:
        r.status = PR.REJECTED
        r.error = "skipped: earlier squad did not succeed"


async def _counts(s, order_id: int) -> tuple[int, int, int]:
    rows = list((await s.execute(select(ProviderRequest.status).where(ProviderRequest.order_id == order_id))).scalars())
    return (sum(1 for x in rows if x == PR.CREATED), sum(1 for x in rows if x == PR.UNCERTAIN),
            sum(1 for x in rows if x == PR.PENDING))


async def _settle(s, order: Order, placed: int, actor: str, reason: str | None = None) -> S:
    """Money side of a finished order. placed = squads really started. Charges those, releases the rest."""
    ordered = order.ordered_squads or order.squads
    full = order_total(order.unit_price, ordered)
    if placed <= 0:
        await transition(s, order.id, order_status(order), S.FAILED, actor, reason or "nothing was created")
        try:
            await ws.release(s, order.user_id, full, order.order_number, "Order failed — returned to wallet",
                             currency=order.currency)
        except ws.AlreadyApplied:
            pass
        return S.FAILED
    await transition(s, order.id, order_status(order), S.SUBMITTED_TO_PROVIDER, actor, reason)
    charged = order_total(order.unit_price, placed)
    order.placed_squads = placed
    if placed < ordered:  # partial fill: keep only what was delivered
        order.squads, order.total_amount = placed, charged
    await _spend(s, order, partial=placed < ordered)
    if placed < ordered:
        try:
            await ws.release(s, order.user_id, full - charged, order.order_number,
                             "Unfilled squads returned to wallet", currency=order.currency,
                             ref_id=f"{order.order_number}-rest")
        except ws.AlreadyApplied:
            pass
    return S.SUBMITTED_TO_PROVIDER


def order_status(order: Order) -> S:
    return order.status


async def finalize_order(order_id: int) -> S:
    async with tx() as s:
        order = await s.get(Order, order_id, populate_existing=True)
        if order.status != S.PROCESSING:
            return order.status
        created, uncertain, pending = await _counts(s, order_id)
        if uncertain:
            await transition(s, order_id, S.PROCESSING, S.REQUIRES_RECONCILIATION, "system", "unclear provider answer")
            return S.REQUIRES_RECONCILIATION
        if pending:  # should not happen; keep for recovery
            await transition(s, order_id, S.PROCESSING, S.REQUIRES_RECONCILIATION, "system", "unfinished requests")
            return S.REQUIRES_RECONCILIATION
        return await _settle(s, order, created, "system")


async def _spend(s, order: Order, partial: bool = False) -> None:
    try:
        await ws.spend(s, order.user_id, order.total_amount, order.order_number, currency=order.currency,
                       partial=partial)
    except ws.AlreadyApplied:
        return
    user = await s.get(User, order.user_id)
    user.total_squads += order.squads
    user.total_spent = Decimal(str(user.total_spent)) + Decimal(str(order.total_amount))


async def resolve_reconciliation(s, order_id: int, admin_id: int, action: str) -> Order | None:
    """action: 'spent' (unclear squads WERE created -> charge) or 'release' (they were not)."""
    order = await s.get(Order, order_id, populate_existing=True)
    if order is None or order.status != S.REQUIRES_RECONCILIATION:
        return None
    created, uncertain, _ = await _counts(s, order_id)
    placed = created + (uncertain if action == "spent" else 0)
    placed = min(placed, order.ordered_squads or order.squads)
    res = await _settle(s, order, placed, f"admin:{admin_id}", f"reconciled: {action}")
    if res == S.FAILED and action == "spent":
        return None
    await audit(s, admin_id, f"RECONCILE_{action.upper()}", order.order_number)
    return order


async def set_guild_name(order_id: int, name: str | None) -> None:
    if not name:
        return
    async with tx() as s:
        await s.execute(update(Order).where(Order.id == order_id, Order.guild_name.is_(None))
                        .values(guild_name=name[:80]).execution_options(synchronize_session=False))


async def complete_order(s, order_id: int, admin_id: int) -> Order | None:
    order = await s.get(Order, order_id, populate_existing=True)
    if order is None or order.status not in (S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS):
        return None
    if not await transition(s, order_id, order.status, S.COMPLETED, f"admin:{admin_id}"):
        return None
    user = await s.get(User, order.user_id)
    user.total_successful_orders += 1
    await audit(s, admin_id, "COMPLETE_ORDER", order.order_number)
    return order


async def recover() -> list[int]:
    """Startup recovery. Returns order ids whose execution must continue."""
    ids: list[int] = []
    async with tx() as s:
        ids += list((await s.execute(select(Order.id).where(Order.status == S.APPROVED))).scalars())
        for o in (await s.execute(select(Order).where(Order.status == S.PROCESSING))).scalars():
            rows = list((await s.execute(select(ProviderRequest).where(ProviderRequest.order_id == o.id))).scalars())
            bad = False
            for r in rows:
                if r.status == PR.PENDING and r.sent_at is not None:
                    r.status = PR.UNCERTAIN
                    r.error = "interrupted by restart; outcome unknown"
                if r.status in (PR.UNCERTAIN, PR.REJECTED):
                    bad = True
            if bad:
                await _halt_remaining(s, o.id)
            ids.append(o.id)
    return ids


async def refund_order(s, order_id: int, admin_id: int) -> Order | None:
    """Admin refund of a delivered order to the customer's wallet. Exactly once (state + ledger + unique record)."""
    order = await s.get(Order, order_id, populate_existing=True)
    if order is None or order.status not in (S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS, S.COMPLETED):
        return None
    if not await transition(s, order_id, order.status, S.REFUNDED, f"admin:{admin_id}", "admin refund"):
        return None
    try:
        await ws.refund(s, order.user_id, order.total_amount, order.order_number, f"admin:{admin_id}",
                        currency=order.currency)
    except ws.AlreadyApplied:
        return None
    s.add(RefundRecord(order_id=order.id, amount=order.total_amount, reason="admin refund", admin_id=admin_id))
    await audit(s, admin_id, "REFUND_ORDER", order.order_number)
    return order


async def live_orders(limit: int = 12) -> list[Order]:
    async with read() as s:
        return list((await s.execute(select(Order).where(Order.status.in_(
            [S.APPROVED, S.PROCESSING, S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS, S.REQUIRES_RECONCILIATION]))
            .order_by(Order.id.desc()).limit(limit))).scalars())


async def count_orders() -> int:
    async with read() as s:
        return (await s.execute(select(func.count(Order.id)))).scalar_one()
