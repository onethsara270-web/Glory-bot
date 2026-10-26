"""Wallet ledger, one wallet per user per currency. Every function must be called inside `tx()`.
Each money movement is guarded twice against repeats: an explicit existence check and the unique
constraint (type, ref_type, ref_id) on wallet_transactions."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from glorybot.database.models import TxType, Wallet, WalletTransaction
from glorybot.utils.money import to_money

ZERO = Decimal("0.00")


class InsufficientFunds(Exception):
    def __init__(self, shortfall: Decimal):
        self.shortfall = shortfall
        super().__init__(f"short by {shortfall}")


class AlreadyApplied(Exception):
    pass


async def get_wallet(s, user_id: int, currency: str = "NPR", lock: bool = True) -> Wallet:
    q = select(Wallet).where(Wallet.user_id == user_id, Wallet.currency == currency)
    if lock:
        q = q.with_for_update()
    w = (await s.execute(q)).scalar_one_or_none()
    if w is None:
        w = Wallet(user_id=user_id, currency=currency, available=ZERO, reserved=ZERO,
                   lifetime_deposits=ZERO, lifetime_spent=ZERO)
        s.add(w)
        await s.flush()
    return w


async def _exists(s, type_: TxType, ref_type: str, ref_id) -> bool:
    q = select(WalletTransaction.id).where(
        WalletTransaction.type == type_, WalletTransaction.ref_type == ref_type,
        WalletTransaction.ref_id == str(ref_id)).limit(1)
    return (await s.execute(q)).first() is not None


async def _post(s, w, type_, amount, ref_type, ref_id, reason, actor):
    s.add(WalletTransaction(
        txn_id="T" + uuid.uuid4().hex[:15].upper(), user_id=w.user_id, type=type_, currency=w.currency,
        amount=to_money(amount), available_after=to_money(w.available),
        reserved_after=to_money(w.reserved), ref_type=ref_type, ref_id=str(ref_id),
        reason=reason, actor=actor))
    await s.flush()


async def reserve(s, user_id, amount, order_number, actor="system", currency="NPR"):
    amount = to_money(amount)
    w = await get_wallet(s, user_id, currency)
    if await _exists(s, TxType.RESERVE, "order", order_number):
        raise AlreadyApplied()
    if to_money(w.available) < amount:
        raise InsufficientFunds(amount - to_money(w.available))
    w.available = to_money(w.available) - amount
    w.reserved = to_money(w.reserved) + amount
    await _post(s, w, TxType.RESERVE, amount, "order", order_number, "Funds reserved for order", actor)


async def spend(s, user_id, amount, order_number, actor="system", currency="NPR", partial=False):
    """Charge reserved money. partial=True is used once, when only some squads were placed."""
    amount = to_money(amount)
    w = await get_wallet(s, user_id, currency)
    if await _exists(s, TxType.SPEND, "order", order_number):
        raise AlreadyApplied()
    if not partial and await _exists(s, TxType.RELEASE, "order", order_number):
        raise AlreadyApplied()
    if to_money(w.reserved) < amount:
        raise ValueError("reserved balance lower than order amount")
    w.reserved = to_money(w.reserved) - amount
    w.lifetime_spent = to_money(w.lifetime_spent) + amount
    await _post(s, w, TxType.SPEND, amount, "order", order_number, "Order paid", actor)


async def release(s, user_id, amount, order_number, reason="Funds released", actor="system", currency="NPR",
                  ref_id=None):
    """Give reserved money back. `ref_id` lets a partial order release its unused part separately."""
    amount = to_money(amount)
    w = await get_wallet(s, user_id, currency)
    ref = ref_id or order_number
    if await _exists(s, TxType.RELEASE, "order", ref):
        raise AlreadyApplied()
    if ref_id is None and await _exists(s, TxType.SPEND, "order", order_number):
        raise AlreadyApplied()
    if to_money(w.reserved) < amount:
        raise ValueError("reserved balance lower than order amount")
    w.reserved = to_money(w.reserved) - amount
    w.available = to_money(w.available) + amount
    await _post(s, w, TxType.RELEASE, amount, "order", ref, reason, actor)


async def deposit(s, user_id, amount, deposit_id, actor, currency="NPR", reason="Deposit verified"):
    amount = to_money(amount)
    w = await get_wallet(s, user_id, currency)
    if await _exists(s, TxType.DEPOSIT, "deposit", deposit_id):
        raise AlreadyApplied()
    w.available = to_money(w.available) + amount
    w.lifetime_deposits = to_money(w.lifetime_deposits) + amount
    await _post(s, w, TxType.DEPOSIT, amount, "deposit", deposit_id, reason, actor)


async def adjust(s, user_id, delta, reason, actor, currency="NPR"):
    delta = to_money(delta)
    w = await get_wallet(s, user_id, currency)
    if delta < 0 and to_money(w.available) < -delta:
        raise InsufficientFunds(-delta - to_money(w.available))
    w.available = to_money(w.available) + delta
    sign = "+" if delta >= 0 else "-"
    await _post(s, w, TxType.ADJUSTMENT, abs(delta), "adjust", uuid.uuid4().hex[:16], f"[{sign}] {reason}", actor)


async def refund(s, user_id, amount, order_number, actor, currency="NPR"):
    amount = to_money(amount)
    w = await get_wallet(s, user_id, currency)
    if await _exists(s, TxType.REFUND, "order", order_number):
        raise AlreadyApplied()
    w.available = to_money(w.available) + amount
    await _post(s, w, TxType.REFUND, amount, "order", order_number, "Order refunded by admin", actor)


async def recent(s, user_id, limit=5, currency="NPR"):
    q = (select(WalletTransaction).where(WalletTransaction.user_id == user_id,
                                         WalletTransaction.currency == currency)
         .order_by(WalletTransaction.id.desc()).limit(limit))
    return list((await s.execute(q)).scalars())
