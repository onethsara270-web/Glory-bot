"""Payments: create a QR / Binance Pay checkout, watch it (polling), and act once the money is verified.
Nepali rupees only, through APINepal QR. Two kinds of payment:
  ORDER  - pay for an order; the order is placed automatically (no admin step)
  TOPUP  - add balance to the wallet
Safety rules: the paid amount must equal what we asked for; a payment is claimed by an atomic PENDING->PAID
update (exactly once); if the order can't be placed after payment the money goes to the customer's wallet instead
of being lost; payments that arrive after the on-screen timer are still honored."""
import asyncio
import base64
import html
import io
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from telegram import InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.error import TelegramError

from glorybot import keyboards as kb
from glorybot import texts
from glorybot.config import get_settings
from glorybot.database.models import PaymentIntent, User, utcnow
from glorybot.database.session import read, tx
from glorybot.payments.base import GatewayError
from glorybot.services import order_service as osv
from glorybot.services import runner, stock_service
from glorybot.services import wallet_service as ws
from glorybot.utils.money import fmt_money, order_total, to_minor, to_money
from glorybot.utils.tasks import spawn
from glorybot.utils.timefmt import fmt_dt

log = logging.getLogger(__name__)
HTML = ParseMode.HTML

TOPUP_PRESETS = {"NPR": [100, 200, 500, 1000, 2000]}
TOPUP_MIN = {"NPR": Decimal("50")}
TOPUP_MAX = {"NPR": Decimal("100000")}


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite returns naive datetimes


def _esc(x) -> str:
    return html.escape(str(x))


def gateway_for(app, currency: str):
    gw = app.bot_data.get("gateways", {}).get(currency)
    if gw is None:
        raise osv.OrderError("Payments are not available right now.")
    return gw


def qr_caption(it: PaymentIntent) -> str:
    st = get_settings()
    left = max(0, int((aware(it.expires_at) - utcnow()).total_seconds() // 60) + 1)
    until = aware(it.expires_at).astimezone(st.tz).strftime("%I:%M %p")
    amount = fmt_money(it.amount, it.currency)
    test = "\n🧪 <i>Test mode: confirms by itself in a few seconds.</i>" if it.is_mock else ""
    how = "1️⃣ Open your bank or wallet app\n2️⃣ Scan this QR and pay the exact amount\n"
    after = "Your order starts by itself ✅" if it.kind == "ORDER" else "Your wallet is topped up by itself ✅"
    return (f"{texts.em('pay', '💳')} <b>Pay {amount}</b>\n⏳ <b>{left} min left</b> (until {until})\n\n"
            f"{how}3️⃣ {after}{test}")


def pay_kb(it: PaymentIntent) -> M:
    rows = []
    rows.append([kb.btn("🔄 I've paid — check", f"pay:chk:{it.id}", style=kb.GREEN, icon="refresh")])
    rows.append([kb.btn("✖️ Cancel", f"pay:x:{it.id}", style=kb.RED, icon="cancel")])
    return M(rows)


async def _edit(bot, it: PaymentIntent, text: str, markup=None) -> None:
    if not it.qr_chat_id or not it.qr_message_id:
        return
    try:
        if it.qr_is_photo:
            await bot.edit_message_caption(it.qr_chat_id, it.qr_message_id, caption=text, parse_mode=HTML, reply_markup=markup)
        else:
            await bot.edit_message_text(text, it.qr_chat_id, it.qr_message_id, parse_mode=HTML, reply_markup=markup)
    except TelegramError as e:
        log.debug("QR edit skipped: %s", e)


async def _remove(bot, it: PaymentIntent) -> None:
    """Once paid, the QR disappears from the chat (it is no longer needed)."""
    if not it.qr_chat_id or not it.qr_message_id:
        return
    try:
        await bot.delete_message(it.qr_chat_id, it.qr_message_id)
    except TelegramError as e:
        log.debug("QR delete skipped: %s", e)
        await _edit(bot, it, "✅ Paid", None)


async def send_qr(bot, it: PaymentIntent, chat_id: int) -> None:
    caption, markup = qr_caption(it), pay_kb(it)
    msg, is_photo = None, False
    photo = None
    if it.qr_data and it.qr_data.startswith("data:image") and "," in it.qr_data:
        try:
            photo = io.BytesIO(base64.b64decode(it.qr_data.split(",", 1)[1]))
            photo.name = "qr.png"
        except Exception:  # noqa: BLE001
            photo = None
    elif it.qr_url:
        photo = it.qr_url
    if photo is not None:
        try:
            msg = await bot.send_photo(chat_id, photo, caption=caption, parse_mode=HTML, reply_markup=markup)
            is_photo = True
        except Exception as e:  # noqa: BLE001
            log.warning("QR image send failed: %s", type(e).__name__)
    if msg is None:
        extra = f"\n\n<code>{_esc(it.qr_string)}</code>" if it.qr_string else ""
        rows = list(markup.inline_keyboard)
        if it.checkout_url:
            rows = [[kb.btn("🔗 Open payment page", url=it.checkout_url, style=kb.GREEN)]] + rows
        msg = await bot.send_message(chat_id, caption + extra, parse_mode=HTML, reply_markup=M(rows))
    async with tx() as s:
        row = await s.get(PaymentIntent, it.id)
        row.qr_chat_id, row.qr_message_id, row.qr_is_photo = chat_id, msg.message_id, is_photo


async def _create_intent(app, user: User, *, kind: str, currency: str, amount: Decimal, unit: Decimal,
                         region: str | None, uid: str | None, squads: int, reuse_key: tuple | None,
                         deliver: bool = True) -> PaymentIntent:
    st, provider = get_settings(), app.bot_data["provider"]
    gw = gateway_for(app, currency)
    now, reuse = utcnow(), None
    async with tx() as s:
        if user.status != "ACTIVE":
            raise osv.OrderError("Your account is suspended. Please contact support.")
        open_rows = list((await s.execute(select(PaymentIntent).where(
            PaymentIntent.user_id == user.id, PaymentIntent.status == "PENDING", PaymentIntent.ui_open.is_(True),
            PaymentIntent.provider_payment_id.is_not(None)).order_by(PaymentIntent.id.desc()))).scalars())
        open_rows = [x for x in open_rows if aware(x.expires_at) > now]
        reuse = next((x for x in open_rows if (x.kind, x.currency, x.region_code or None, x.guild_uid or None, x.squads,
                                                 to_money(x.amount)) == reuse_key), None)
        it = reuse
        if reuse is None:
            if len(open_rows) >= 3:
                raise osv.OrderError("You already have open payments. Finish or cancel one first.")
            it = PaymentIntent(user_id=user.id, identifier="GP" + secrets.token_hex(6).upper(), amount=amount,
                               amount_paisa=to_minor(amount), unit_price=unit, region_code=region or "", guild_uid=uid or "",
                               squads=squads, currency=currency, kind=kind, is_mock=provider.is_mock,
                               expires_at=now + timedelta(minutes=st.payment_ui_minutes))
            s.add(it)
            await s.flush()
    if reuse is not None:
        if deliver:
            await send_qr(app.bot, reuse, user.telegram_id)
        return reuse
    desc = f"Glory Push {squads} squad(s)" if kind == "ORDER" else "Wallet top-up"
    try:
        qr = await gw.create_qr(identifier=it.identifier, amount_paisa=it.amount_paisa, description=desc,
                                customer_name=user.display_name or user.login_username or "customer",
                                ttl_minutes=st.payment_watch_minutes)
    except GatewayError as e:
        async with tx() as s:
            row = await s.get(PaymentIntent, it.id)
            row.status, row.note = "FAILED", f"create failed: {e}"[:200]
        log.warning("payment create failed (%s): %s (HTTP %s)", currency, e, e.status)
        from glorybot.handlers.common import notify_admins
        hint = "Check APINEPAL_SECRET_KEY and that FoneBiz/Fonepay is connected in your APINepal dashboard."
        await notify_admins(app.bot, f"🔴 <b>{currency} payment could not be created</b>: {_esc(e)}\n{hint}")
        raise
    async with tx() as s:
        row = await s.get(PaymentIntent, it.id, populate_existing=True)
        row.provider_payment_id, row.qr_data, row.qr_string, row.checkout_url, row.qr_url = (
            qr.id, qr.qr_png_base64, qr.qr_string, qr.checkout_url, qr.qr_url)
    if deliver:
        await send_qr(app.bot, row, user.telegram_id)
    return row


async def start_payment(app, user: User, region: str, uid: str, squads: int, deliver: bool = True) -> PaymentIntent:
    """Pay for an order (in the customer's currency). Raises osv.OrderError / stock_service.StockError / GatewayError."""
    currency = user.currency or "NPR"
    await stock_service.check_orderable(app.bot_data["provider"], region, squads, force=True)
    async with read() as s:
        unit = await osv.current_price(s, region, currency)
    return await _create_intent(app, user, kind="ORDER", currency=currency, amount=order_total(unit, squads), unit=unit,
                                region=region, uid=uid, squads=squads, deliver=deliver,
                                reuse_key=("ORDER", currency, region, uid, squads, order_total(unit, squads)))


async def start_topup(app, user: User, amount: Decimal, deliver: bool = True) -> PaymentIntent:
    currency = user.currency or "NPR"
    amount = to_money(amount)
    if not TOPUP_MIN[currency] <= amount <= TOPUP_MAX[currency]:
        raise osv.OrderError(f"Amount must be between {fmt_money(TOPUP_MIN[currency], currency)} and "
                             f"{fmt_money(TOPUP_MAX[currency], currency)}.")
    return await _create_intent(app, user, kind="TOPUP", currency=currency, amount=amount, unit=amount, region=None,
                                uid=None, squads=0, deliver=deliver,
                                reuse_key=("TOPUP", currency, None, None, 0, amount))


def parse_amount(text: str) -> Decimal | None:
    try:
        v = Decimal(text.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return to_money(v) if v > 0 else None


async def cancel_ui(app, pid: int, user_id: int) -> None:
    async with tx() as s:
        it = await s.get(PaymentIntent, pid, populate_existing=True)
        if not it or it.user_id != user_id or it.status != "PENDING":
            return
        it.ui_open = False
    await _edit(app.bot, it, "❌ Cancelled.\nIf you already paid, we'll still find it and finish it for you.")


async def check_now(app, pid: int) -> str:
    """Poll one payment immediately. Returns pending|paid|failed|error."""
    async with read() as s:
        it = await s.get(PaymentIntent, pid)
    if not it or not it.provider_payment_id:
        return "error"
    if it.status in ("PAID", "CREDITED"):
        return "paid"
    if it.status != "PENDING":
        return "failed"
    try:
        state = await gateway_for(app, it.currency).get_payment(it.provider_payment_id)
    except (GatewayError, osv.OrderError) as e:
        log.warning("payment check failed: %s", e)
        return "error"
    return await apply_state(app, it, state)


async def apply_state(app, it: PaymentIntent, state) -> str:
    if state.status == "pending":
        return "pending"
    if state.status == "failed":
        async with tx() as s:
            res = await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status == "PENDING")
                                  .values(status="FAILED").execution_options(synchronize_session=False))
        if res.rowcount == 1:
            await _edit(app.bot, it, "❌ This payment expired or failed. Please start again.")
        return "failed"
    return await _process_paid(app, it, state)


async def _process_paid(app, it: PaymentIntent, state) -> str:
    from glorybot.handlers.common import notify_admins, safe_send
    if (state.amount_paisa is not None and state.amount_paisa != it.amount_paisa) or \
            (state.identifier and state.identifier != it.identifier):
        async with tx() as s:
            await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status == "PENDING")
                            .values(status="MISMATCH", note=f"paid {state.amount_paisa} expected {it.amount_paisa}")
                            .execution_options(synchronize_session=False))
        await notify_admins(app.bot, f"🔴 <b>Payment mismatch</b> {_esc(it.identifier)} ({it.currency}): expected "
                                     f"{it.amount_paisa / 100:.2f}, provider says {state.amount_paisa}. "
                                     "Nothing was placed or credited — please check manually.")
        return "error"
    if it.kind == "TOPUP":
        return await _process_topup(app, it)
    try:
        async with tx() as s:
            res = await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status == "PENDING")
                                  .values(status="PAID", paid_at=utcnow()).execution_options(synchronize_session=False))
            if res.rowcount != 1:
                return "paid"  # already handled elsewhere
            user = await s.get(User, it.user_id)
            order = await osv.create_order(s, user, it.region_code, it.guild_uid, it.squads, it.is_mock,
                                           payment={"method": "QR", "remark": it.identifier},
                                           unit_price=Decimal(str(it.unit_price)), currency=it.currency)
            r = await osv.approve_order(s, order.id, 0, it.is_mock)  # 0 = automatic: nobody has to approve
            if r != "ok":
                raise osv.OrderError(f"auto-approve: {r}")
            await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id).values(order_id=order.id)
                            .execution_options(synchronize_session=False))
            number, oid, tg, name = order.order_number, order.id, user.telegram_id, user.display_name
    except Exception as e:  # noqa: BLE001  -> never lose the customer's money
        log.warning("auto-order failed after payment %s: %s", it.identifier, e)
        await _credit_wallet(app, it, str(e))
        return "paid"
    await _remove(app.bot, it)
    await safe_send(app.bot, tg, f"✅ <b>Payment received — {fmt_money(it.amount, it.currency)}</b>\n"
                                 f"🧾 Order <code>{number}</code> — sending your bot request now…")
    await notify_admins(app.bot, f"🆕 <b>NEW ORDER PLACED</b> (QR paid){' 🧪 MOCK' if it.is_mock else ''}\n\n🧾 <code>{number}</code>\n"
                                 f"👤 {_esc(name)} (<code>{tg}</code>)\n🌍 {_esc(it.region_code)} · UID <code>{_esc(it.guild_uid)}</code>\n"
                                 f"👥 {it.squads} squad(s) · 💳 {fmt_money(it.amount, it.currency)}\n🕐 {fmt_dt(utcnow())}\n"
                                 "✅ Placed automatically — nothing for you to confirm.",
                        kb.admin_auto_order_kb(oid, tg))
    spawn(app, runner.run_order(app, oid))
    return "paid"


async def _process_topup(app, it: PaymentIntent) -> str:
    from glorybot.handlers.common import notify_admins, safe_send
    async with tx() as s:
        res = await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status == "PENDING")
                              .values(status="PAID", paid_at=utcnow()).execution_options(synchronize_session=False))
        if res.rowcount != 1:
            return "paid"
        user = await s.get(User, it.user_id)
        try:
            await ws.deposit(s, it.user_id, it.amount, f"intent-{it.id}", "system", currency=it.currency,
                             reason="Wallet top-up")
        except ws.AlreadyApplied:
            pass
        w = await ws.get_wallet(s, it.user_id, it.currency, lock=False)
        tg, name, bal = user.telegram_id, user.display_name, w.available
    await _remove(app.bot, it)
    await safe_send(app.bot, tg, f"✅ <b>Balance added: {fmt_money(it.amount, it.currency)}</b>\n"
                                 f"💰 New balance: <b>{fmt_money(bal, it.currency)}</b>",
                    M([[kb.btn("🛒 SHOP NOW", "u:ord", style=kb.GREEN, icon="buy"), kb.btn("🏠 Menu", "u:home", icon="home")]]))
    await notify_admins(app.bot, f"💰 Wallet top-up: {fmt_money(it.amount, it.currency)} from {_esc(name)} (<code>{tg}</code>)"
                                 f"{' 🧪' if it.is_mock else ''}")
    return "paid"


async def _credit_wallet(app, it: PaymentIntent, reason: str) -> None:
    from glorybot.handlers.common import notify_admins, safe_send
    async with tx() as s:
        res = await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status.in_(["PENDING", "PAID"]))
                              .values(status="CREDITED", paid_at=utcnow(), note=reason[:200]).execution_options(synchronize_session=False))
        if res.rowcount != 1:
            return
        user = await s.get(User, it.user_id)
        await ws.deposit(s, it.user_id, it.amount, f"intent-{it.id}", "system", currency=it.currency,
                         reason="Payment kept in wallet")
        tg = user.telegram_id
    await safe_send(app.bot, tg, f"🟡 We received {fmt_money(it.amount, it.currency)} but couldn't start the order.\n"
                                 "Your money is safe in 💰 Wallet. Please order again or contact support.", kb.nav_home())
    await notify_admins(app.bot, f"🟡 Paid {_esc(it.identifier)} ({fmt_money(it.amount, it.currency)}) but the order failed: "
                                 f"{_esc(reason[:150])}\nCredited to the customer's wallet.")


# ------------------------------------------------------------------ background watcher
async def watcher(app) -> None:
    last_poll: dict[int, float] = {}
    last_edit: dict[int, float] = {}
    loop = asyncio.get_running_loop()
    while True:
        try:
            await _tick(app, last_poll, last_edit, loop.time())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("payment watcher tick failed")
        await asyncio.sleep(4)


async def _tick(app, last_poll: dict, last_edit: dict, now_m: float) -> None:
    st, now = get_settings(), utcnow()
    async with read() as s:
        intents = list((await s.execute(select(PaymentIntent).where(
            PaymentIntent.status == "PENDING", PaymentIntent.provider_payment_id.is_not(None)))).scalars())
    for it in intents:
        gw = app.bot_data.get("gateways", {}).get(it.currency)
        if gw is None:
            continue
        age = (now - aware(it.created_at)).total_seconds()
        if it.ui_open and now >= aware(it.expires_at):
            async with tx() as s:
                row = await s.get(PaymentIntent, it.id)
                row.ui_open = False
            await _edit(app.bot, it, "⌛ <b>This QR expired.</b>\nIf you already paid, don't worry — we'll still find it.\n"
                                     "Otherwise start again.")
        elif it.ui_open and now_m - last_edit.get(it.id, 0) >= 60:
            last_edit[it.id] = now_m
            await _edit(app.bot, it, qr_caption(it), pay_kb(it))
        interval = 5 if age < st.payment_ui_minutes * 60 else 20
        final = age > st.payment_watch_minutes * 60 + 60
        if not final and now_m - last_poll.get(it.id, 0) < interval:
            continue
        last_poll[it.id] = now_m
        try:
            state = await gw.get_payment(it.provider_payment_id)
        except GatewayError as e:
            log.warning("poll %s failed: %s", it.identifier, e)
            if final:  # can't confirm: never silently drop a possibly-paid QR
                async with tx() as s:
                    await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status == "PENDING")
                                    .values(status="REVIEW", note="could not verify at close").execution_options(synchronize_session=False))
                from glorybot.handlers.common import notify_admins
                await notify_admins(app.bot, f"🟡 Could not verify {it.currency} payment <code>{_esc(it.provider_payment_id)}</code> "
                                             f"({_esc(it.identifier)}). Check it in your APINepal dashboard.")
            continue
        result = await apply_state(app, it, state)
        if final and result == "pending":
            async with tx() as s:
                await s.execute(update(PaymentIntent).where(PaymentIntent.id == it.id, PaymentIntent.status == "PENDING")
                                .values(status="CLOSED").execution_options(synchronize_session=False))
