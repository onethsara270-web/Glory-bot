"""Places an order at the provider in the background and tells the customer and admins what happened."""
import logging

from telegram import InlineKeyboardMarkup as M

from glorybot import keyboards as kb
from glorybot import texts
from glorybot.database.models import Order, OrderStatus as S, ProviderRequest, ProviderRequestStatus as PR, Region, User
from glorybot.database.session import read
from glorybot.handlers.common import notify_admins, safe_send
from glorybot.services import glory_service, stock_service
from glorybot.services import order_service as osv
from glorybot.texts import esc
from glorybot.utils.money import fmt_money

from sqlalchemy import select

log = logging.getLogger(__name__)
LOW_STOCK = 5


async def load(oid: int):
    async with read() as s:
        o = await s.get(Order, oid)
        if o is None:
            return None, None, None, []
        u = await s.get(User, o.user_id)
        r = await s.get(Region, o.region_code)
        reqs = list((await s.execute(select(ProviderRequest).where(ProviderRequest.order_id == oid)
                                     .order_by(ProviderRequest.squad_index))).scalars())
    return o, u, r, reqs


async def run_order(app, oid: int) -> None:
    """Safe to call again after a restart: execute_order only continues what is still open."""
    provider = app.bot_data["provider"]
    try:
        status = await osv.execute_order(oid, provider)
    except Exception:  # noqa: BLE001
        log.exception("execute_order crashed for %s", oid)
        await notify_admins(app.bot, f"🔴 Order id {oid}: unexpected error while placing. Check logs and the order status.")
        return
    if status is None:
        return
    stock_service.invalidate()
    o, u, r, reqs = await load(oid)
    bot = app.bot
    if status == S.SUBMITTED_TO_PROVIDER:
        gid = next((x.provider_group_id for x in reqs if x.status == PR.CREATED and x.provider_group_id), None)
        name = o.guild_name
        if not name and gid:
            name = await glory_service.remember_guild_name(provider, o.guild_uid, gid)
        ordered = o.ordered_squads or o.squads
        note = ""
        if o.squads < ordered:
            note = (f"\n🟡 Only <b>{o.squads} of {ordered}</b> squads could be started (stock ran out). "
                    f"The rest, {fmt_money(osv.order_total(o.unit_price, ordered - o.squads), o.currency)}, "
                    "is back in your 💰 Wallet.\n")
        markup = M([[kb.btn("📈 Live glory", f"u:gv:{o.region_code}:{o.guild_uid}", style=kb.BLUE, icon="guild")],
                    [kb.btn("🏠 Menu", "u:home", icon="home")]])
        await safe_send(bot, u.telegram_id, texts.bot_sent_text(o.order_number, o.guild_uid, name, o.squads, note)
                        + ("\n\n🧪 <i>Test mode — no real bot was sent.</i>" if o.is_mock else ""), markup)
        await notify_admins(bot, f"🤖 <b>{o.order_number}</b> — bot request sent to guild{' 🧪' if o.is_mock else ''}\n"
                                 f"🏷 {esc(name or '—')} · UID <code>{esc(o.guild_uid)}</code> · {o.squads} squad(s)"
                                 + (f"\n🟡 Partial: asked {ordered}, started {o.squads}." if note else ""),
                            kb.admin_auto_order_kb(o.id, u.telegram_id))
        await _low_stock_alert(app)
    elif status == S.FAILED:
        why = esc(next((x.error for x in reqs if x.error), ""))
        await safe_send(bot, u.telegram_id,
                        f"🔴 <b>Order {o.order_number} could not be started.</b>\n"
                        f"Your {fmt_money(osv.order_total(o.unit_price, o.ordered_squads or o.squads), o.currency)} "
                        "is back in your 💰 Wallet — you can try again.", kb.nav_home())
        await notify_admins(bot, f"🔴 {o.order_number} FAILED at provider; funds returned to wallet.\n{why}",
                            kb.admin_auto_order_kb(o.id, u.telegram_id))
        await _low_stock_alert(app)
    else:
        from glorybot.handlers import admin as admin_h
        await safe_send(bot, u.telegram_id, f"🟡 <b>Order {o.order_number} is being checked.</b>\n"
                                            "We'll update you very soon.", kb.nav_home())
        await notify_admins(bot, "🟡 <b>NEEDS A LOOK</b>\n\n" + admin_h.order_text(o, u, r, reqs), admin_h.order_kb(o))


async def _low_stock_alert(app) -> None:
    try:
        cr = await stock_service.credits(app.bot_data["provider"], force=True)
    except Exception:  # noqa: BLE001
        return
    low = [f"{k}: {v}" for k, v in cr.items() if v < LOW_STOCK]
    if low:
        await notify_admins(app.bot, "🟠 <b>Credits running low</b> (" + ", ".join(low) + ")\n"
                                     "Add more from ⚙️ Admin → 🎟 Redeem Code.")
