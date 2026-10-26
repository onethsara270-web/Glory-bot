import asyncio
import csv
import io
import logging
import time
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from telegram import InlineKeyboardMarkup as M
from telegram import InputFile, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from glorybot import keyboards as kb
from glorybot.config import get_settings
from glorybot.database.models import (AuditLog, Broadcast, BroadcastDelivery, Order, PaymentIntent, OrderStatus as S,
                                      Region, SupportMessage, SupportTicket, User)
from glorybot.database.session import read, tx
from glorybot.handlers.common import HTML, esc, is_admin, reply, safe_send, status_label
from glorybot.seed import sync_regions
from glorybot.services import glory_service, runner, stock_service
from glorybot.services import order_service as osv
from glorybot.services import wallet_service as ws
from glorybot.services.audit import audit
from glorybot.texts import website_url
from glorybot.utils.money import fmt_money, to_money
from glorybot.utils.tasks import spawn
from glorybot.utils.timefmt import fmt_dt

log = logging.getLogger(__name__)
btn, GREEN, BLUE, RED = kb.btn, kb.GREEN, kb.BLUE, kb.RED
PRICE_STEPS = {"NPR": ["100", "25"]}
PRICE_MIN = {"NPR": Decimal("1")}
PRICE_MAX = {"NPR": Decimal("100000")}
_redeem_hits: list[float] = []   # provider allows 5 redeem attempts per 60 s


def _flow(ctx) -> dict:
    return ctx.user_data.setdefault("flow", {})


def _set(ctx, **kw) -> None:
    ctx.user_data["flow"] = kw


def _uname(u: User | None) -> str:
    return esc((u.display_name or u.login_username or "?") if u else "?")


# ------------------------------------------------------------------ dashboard
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("flow", None)
    await dashboard(update, ctx)


async def dashboard(update, ctx) -> None:
    provider = ctx.application.bot_data["provider"]
    async with read() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar_one()
        by = {r[0]: r[1] for r in (await s.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all()}
        sales = (await s.execute(select(Order.currency, func.coalesce(func.sum(Order.total_amount), 0)).where(
            Order.status.in_([S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS, S.COMPLETED]), Order.is_mock.is_(False))
            .group_by(Order.currency))).all()
        qr_bad = (await s.execute(select(func.count(PaymentIntent.id)).where(
            PaymentIntent.status.in_(["REVIEW", "MISMATCH"])))).scalar_one()
    g = lambda st: by.get(st, 0)  # noqa: E731
    paused = await stock_service.is_paused()
    try:
        cr = await stock_service.credits(provider, force=True)
        credits_line = f"💳 Website credits: basic <b>{cr['basic']}</b> · premium <b>{cr['premium']}</b>"
    except Exception:  # noqa: BLE001
        credits_line = "💳 Website credits: 🔴 can't reach provider"
    mode = "🧪 MOCK" if provider.is_mock else "🟢 LIVE"
    sales_line = " · ".join(fmt_money(v, c) for c, v in sales) or "—"
    await reply(update,
                f"👑 <b>ADMIN DASHBOARD</b>   {mode}{'   ⏸ PAUSED' if paused else ''}\n\n{credits_line}\n"
                f"👥 Users: {users}\n🔴 Live orders: <b>{g(S.APPROVED) + g(S.PROCESSING) + g(S.SUBMITTED_TO_PROVIDER) + g(S.IN_PROGRESS)}</b>\n"
                f"🏁 Completed: {g(S.COMPLETED)} · ❌ Failed: {g(S.FAILED)}\n"
                f"🟡 Needs a look: <b>{g(S.REQUIRES_RECONCILIATION)}</b> orders · <b>{qr_bad}</b> payments\n"
                f"💵 Sales: {sales_line}", kb.admin_home_kb(paused))


# ------------------------------------------------------------------ order helpers
async def _load(oid: int):
    return await runner.load(oid)


def order_text(o, u, r, reqs) -> str:
    req = "\n".join(f"  #{x.squad_index}: {x.status.value}"
                    f"{' · ' + esc(x.provider_group_id) if x.provider_group_id else ''}"
                    f"{' · ' + esc(x.error) if x.error else ''}" for x in reqs) or "  —"
    ordered = o.ordered_squads or o.squads
    return (f"🧾 <b>{o.order_number}</b>{' 🧪 MOCK' if o.is_mock else ''}\n🕐 {fmt_dt(o.created_at)}\n\n"
            f"👤 {_uname(u)} (<code>{u.telegram_id}</code>)\n🌍 {esc(r.display_name)}\n"
            f"🏷 {esc(o.guild_name or '—')} · UID: <code>{esc(o.guild_uid)}</code>\n"
            f"👥 {o.squads}{'/' + str(ordered) if ordered != o.squads else ''} × {fmt_money(o.unit_price, o.currency)} = "
            f"<b>{fmt_money(o.total_amount, o.currency)}</b>\n💳 Paid via: {'wallet' if o.payment_method == 'WALLET' else 'QR'}"
            f"{' · ref <code>' + esc(o.payment_remark) + '</code>' if o.payment_remark else ''}\n"
            f"Status: {status_label(o.status)}\nProvider requests:\n{req}")


def order_kb(o) -> M:
    rows = []
    if o.status == S.REQUIRES_RECONCILIATION:
        rows.append([btn("🔍 Check provider groups", f"adm:ck:{o.id}", style=BLUE)])
        rows.append([btn("✅ Work was created → charge", f"adm:rs:{o.id}:spent", style=GREEN)])
        rows.append([btn("↩️ Nothing created → release funds", f"adm:rs:{o.id}:release", style=RED)])
    if o.status in (S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS, S.COMPLETED):
        rows.append([btn("🏆 Glory", f"adm:og:{o.id}", style=BLUE)])
    if o.status in (S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS):
        rows.append([btn("🏁 Mark Completed", f"adm:cm:{o.id}", style=GREEN)])
    if o.status in (S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS, S.COMPLETED):
        rows.append([btn("↩️ Refund to wallet", f"adm:rf:{o.id}", style=RED)])
    rows.append([btn("🔴 Live orders", "adm:lo"), btn("⬅️ Dashboard", "adm:home")])
    return M(rows)


async def _show_order(update, oid: int) -> None:
    o, u, r, reqs = await _load(oid)
    if o is None:
        await reply(update, "🔴 Order not found.", kb.back_admin())
        return
    await reply(update, order_text(o, u, r, reqs), order_kb(o))


async def run_order(app, oid: int) -> None:  # kept for callers that still import it from here
    await runner.run_order(app, oid)


# ------------------------------------------------------------------ callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("Not authorized", show_alert=True)
        log.warning("Unauthorized admin callback from %s: %s", uid, q.data)
        return
    await q.answer()
    p = q.data.split(":")
    act = p[1]
    provider = ctx.application.bot_data["provider"]

    if act == "home":
        ctx.user_data.pop("flow", None)
        await dashboard(update, ctx)
    # ---- live orders
    elif act == "lo":
        await _live_orders(update, provider)
    elif act == "vw":
        await _show_order(update, int(p[2]))
    elif act == "ck":
        o, u, r, _ = await _load(int(p[2]))
        try:
            groups = [g for g in await provider.list_groups() if g.clan_id == o.guild_uid]
            txt = (f"🔍 Provider groups for UID <code>{esc(o.guild_uid)}</code>: <b>{len(groups)}</b>\n"
                   + "\n".join(f"• {esc(g.group_id)} · {esc(g.status)} · {esc(g.created_at)}" for g in groups[:15])
                   + f"\n\nOrder expects {o.ordered_squads or o.squads} squad(s).")
        except Exception as e:  # noqa: BLE001
            txt = f"🔴 Could not query provider: {esc(type(e).__name__)}"
        await reply(update, txt, order_kb(o))
    elif act == "rs":
        async with tx() as s:
            o = await osv.resolve_reconciliation(s, int(p[2]), uid, p[3])
        if o is None:
            await reply(update, "🟡 Order is no longer awaiting reconciliation.", kb.back_admin())
            return
        o2, u, _, _ = await _load(o.id)
        if o2.status == S.FAILED:
            await safe_send(ctx.bot, u.telegram_id, f"🔴 Order {o.order_number} could not be placed. Funds returned to your wallet.")
        else:
            await safe_send(ctx.bot, u.telegram_id, f"🟢 Order {o.order_number} was confirmed. Please accept the bot request in your guild.")
        await reply(update, f"✅ {o.order_number} reconciled ({p[3]}).", kb.back_admin())
    elif act == "cm":
        async with tx() as s:
            o = await osv.complete_order(s, int(p[2]), uid)
        if o is None:
            await reply(update, "🟡 Order can't be completed from its current status.", kb.back_admin())
            return
        _, u, _, _ = await _load(o.id)
        await safe_send(ctx.bot, u.telegram_id, f"🏁 <b>Order {o.order_number} completed!</b> Thank you 💛")
        await reply(update, f"🏁 {o.order_number} marked completed.", kb.back_admin())
    elif act == "se":
        _set(ctx, step="adm_search_o" if p[2] == "o" else "adm_search_u")
        await reply(update, "🔎 Send an order number (BNY-1001), guild UID, name or Telegram ID:" if p[2] == "o"
                    else "🔎 Send a name, @username or Telegram ID:", M([[btn("⬅️ Back", "adm:home")]]))
    elif act == "og":
        o, u, r, reqs = await _load(int(p[2]))
        try:
            data = (await glory_service.order_glory(provider, [o.id]))[o.id]
            txt = (f"🏆 <b>{o.order_number}</b> — <b>{data['glory']:,}</b> glory\n"
                   + "\n".join(f"• {esc(g['id'])}: {g['glory']:,} · {esc(g['status'])}" for g in data["groups"]))
            gbtns = [[btn(f"⚙️ Group {g['id'][:10]}", f"adm:g:{g['id']}")] for g in data["groups"][:10]]
        except Exception as e:  # noqa: BLE001
            txt, gbtns = f"🔴 Could not fetch glory: {esc(type(e).__name__)}", []
        await reply(update, txt, M(gbtns + list(order_kb(o).inline_keyboard)))
    elif act == "rf":
        o, u, _, _ = await _load(int(p[2]))
        await reply(update, f"↩️ Refund <b>{fmt_money(o.total_amount, o.currency)}</b> of <code>{o.order_number}</code> to the customer's wallet?\n"
                            "The provider groups keep running — stop them from 🏆 Groups if needed.",
                    M([[btn("✅ Yes, refund", f"adm:rfy:{o.id}", style=RED), btn("Cancel", f"adm:vw:{o.id}")]]))
    elif act == "rfy":
        async with tx() as s:
            o = await osv.refund_order(s, int(p[2]), uid)
        if o is None:
            await reply(update, "🟡 Can't refund this order (already refunded or wrong status).", kb.back_admin())
            return
        _, u, _, _ = await _load(o.id)
        await safe_send(ctx.bot, u.telegram_id, f"↩️ <b>Refund:</b> {fmt_money(o.total_amount, o.currency)} for order "
                                                f"{o.order_number} was added to your 💰 Wallet.")
        await reply(update, f"↩️ {o.order_number} refunded to wallet (once).", kb.back_admin())
    # ---- stock
    elif act in ("st", "rt", "rsy", "pz"):
        if act == "pz":
            now = not await stock_service.is_paused()
            await stock_service.set_conf("paused", "1" if now else "0")
            async with tx() as s:
                await audit(s, uid, "PAUSE_ORDERS" if now else "RESUME_ORDERS")
        if act == "rt":
            async with tx() as s:
                r = await s.get(Region, p[2])
                r.enabled = not r.enabled
                await audit(s, uid, "REGION_OUT_OF_STOCK" if not r.enabled else "REGION_IN_STOCK", r.code)
        if act == "rsy" and not provider.is_mock:
            await sync_regions(provider)
        if act == "pz" and len(p) < 3:
            await dashboard(update, ctx)
        else:
            await _stock_screen(update, provider)
    # ---- prices
    elif act in ("pr", "pp"):
        note = ""
        if act == "pp":
            cur, delta = p[2], Decimal(p[3])
            async with tx() as s:
                new = await osv.current_price(s, None, cur) + delta
                if not PRICE_MIN[cur] <= new <= PRICE_MAX[cur]:
                    note = f"⚠️ {cur} price must stay between {PRICE_MIN[cur]} and {PRICE_MAX[cur]}.\n\n"
                else:
                    await osv.set_price(s, new, uid, cur)
        await _price_screen(update, note)
    elif act == "prs":
        _set(ctx, step="adm_price", cur=p[2])
        await reply(update, f"✏️ Send the new price per squad in {p[2]}:", M([[btn("⬅️ Back", "adm:pr")]]))
    # ---- redeem / credits
    elif act == "rd":
        _set(ctx, step="adm_redeem")
        try:
            cr = await stock_service.credits(provider, force=True)
            have = f"Now: basic <b>{cr['basic']}</b> · premium <b>{cr['premium']}</b>\n\n"
        except Exception:  # noqa: BLE001
            have = ""
        await reply(update, f"🎟 <b>Add credits</b>\n{have}Send a coupon code and I'll redeem it on the website account "
                            "(max 5 tries per minute).\nNeed a code? Buy credits on the website first.",
                    M([[btn("🌐 Open website", url=website_url())], [btn("⬅️ Dashboard", "adm:home")]]))
    # ---- users
    elif act == "us":
        _set(ctx, step="adm_search_u")
        await reply(update, "👥 Send a name, @username or Telegram ID to look up:", kb.back_admin())
    elif act in ("ust", "uad", "uac", "uor"):
        target = int(p[2])
        if act == "ust":
            async with tx() as s:
                u = await s.get(User, target)
                u.status = "SUSPENDED" if u.status == "ACTIVE" else "ACTIVE"
                await audit(s, uid, "SUSPEND_USER" if u.status == "SUSPENDED" else "REACTIVATE_USER", f"user:{target}")
            await _user_card(update, target)
        elif act == "uad":
            _set(ctx, step="adm_adj_amount", target=target, cur="NPR")
            await reply(update, "💰 Send the adjustment in NPR (minus to deduct), e.g. <code>100</code> or <code>-50</code>:")
        else:
            async with read() as s:
                rows = list((await s.execute(select(Order).where(Order.user_id == target)
                                             .order_by(Order.id.desc()).limit(10))).scalars())
            btns = [[btn(f"{o.order_number} · {status_label(o.status)}", f"adm:vw:{o.id}")] for o in rows]
            await reply(update, "🧾 Orders", M(btns + [[btn("⬅️ Back", f"adm:uc:{target}")]]))
    elif act == "uc":
        await _user_card(update, int(p[2]))
    # ---- broadcast
    elif act == "bc":
        await reply(update, "📢 <b>Broadcast</b>\nChoose the audience:", M([
            [btn("👥 All users", "adm:bca:all", style=BLUE), btn("🟢 Active users", "adm:bca:active", style=GREEN)],
            [btn("🧾 Users with orders", "adm:bca:orders")], [btn("⬅️ Back", "adm:home")]]))
    elif act == "bca":
        _set(ctx, step="adm_bc_msg", audience=p[2])
        await reply(update, "📝 Now send the message to broadcast (text, photo, video, document, sticker…).\n"
                            "You'll get a preview first.")
    elif act == "bcgo":
        f = _flow(ctx)
        if f.get("step") != "adm_bc_ready":
            return
        async with tx() as s:
            bc = Broadcast(admin_id=uid, audience=f["audience"], source_chat_id=f["chat"],
                           source_message_id=f["mid"], status="SENDING")
            s.add(bc)
            await s.flush()
            await audit(s, uid, "BROADCAST", f"bc:{bc.id}", f["audience"])
            bid = bc.id
        ctx.user_data.pop("flow", None)
        await reply(update, f"📢 Broadcast #{bid} started. You'll get a summary.", kb.back_admin())
        spawn(ctx.application, run_broadcast(ctx.application, bid))
    # ---- reports / settings
    elif act == "rp":
        async with read() as s:
            rows = (await s.execute(select(Order.region_code, func.count(Order.id), func.coalesce(func.sum(Order.squads), 0))
                                    .group_by(Order.region_code))).all()
            money = (await s.execute(select(Order.currency, func.coalesce(func.sum(Order.total_amount), 0)).where(
                Order.status.in_([S.SUBMITTED_TO_PROVIDER, S.IN_PROGRESS, S.COMPLETED]), Order.is_mock.is_(False))
                .group_by(Order.currency))).all()
            topups = (await s.execute(select(PaymentIntent.currency, func.coalesce(func.sum(PaymentIntent.amount), 0)).where(
                PaymentIntent.kind == "TOPUP", PaymentIntent.status == "PAID", PaymentIntent.is_mock.is_(False))
                .group_by(PaymentIntent.currency))).all()
        text = ("📊 <b>Reports</b>\n\n<b>Sales</b>: " + (" · ".join(fmt_money(v, c) for c, v in money) or "—")
                + "\n<b>Wallet top-ups</b>: " + (" · ".join(fmt_money(v, c) for c, v in topups) or "—")
                + "\n\n<b>Orders per region</b>\n" + ("\n".join(f"• {r[0]}: {r[1]} orders, {r[2]} squads" for r in rows) or "No orders yet"))
        await reply(update, text, M([[btn("📥 Export orders CSV", "adm:csv", style=BLUE)], [btn("⬅️ Back", "adm:home")]]))
    elif act == "csv":
        async with read() as s:
            orders = list((await s.execute(select(Order).order_by(Order.id))).scalars())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["order", "time_nepal", "user_id", "region", "guild_uid", "guild_name", "squads", "unit_price",
                    "total", "currency", "status", "mock"])
        for o in orders:
            w.writerow([o.order_number, fmt_dt(o.created_at), o.user_id, o.region_code, o.guild_uid, o.guild_name or "",
                        o.squads, o.unit_price, o.total_amount, o.currency, o.status.value, o.is_mock])
        await ctx.bot.send_document(uid, InputFile(io.BytesIO(buf.getvalue().encode()), filename="orders.csv"))
        async with tx() as s:
            await audit(s, uid, "EXPORT_CSV")
    elif act in ("set", "sw"):
        if act == "sw":
            now = not await stock_service.window_enabled()
            await stock_service.set_conf("service_window", "1" if now else "0")
            async with tx() as s:
                await audit(s, uid, "SERVICE_WINDOW", None, str(now))
        win = await stock_service.window_enabled()
        paused = await stock_service.is_paused()
        await reply(update, "⚙️ <b>Settings</b>\n\n"
                            f"🌙 Service window (closed Sun 9 PM → Mon 12 AM): <b>{'ON' if win else 'OFF'}</b>\n"
                            f"⏸ Orders paused: <b>{'YES' if paused else 'no'}</b>\n"
                            f"🎨 Colored buttons: {'on' if get_settings().button_colors else 'off'} · "
                            f"✨ Premium emoji: {'on' if get_settings().premium_emoji_buttons else 'off'} (set in .env)",
                    M([[btn(f"🌙 Service window: {'turn OFF' if win else 'turn ON'}", "adm:sw", style=RED if win else GREEN)],
                       [btn("🔌 API status", "adm:api", style=BLUE), btn("🔔 Provider notices", "adm:nt")],
                       [btn("📜 Edit rules", "adm:ru", style=GREEN), btn("✨ Get premium emoji IDs", "adm:em")],
                       [btn("🧾 Audit log", "adm:au")],
                       [btn("🌍 Regions / re-sync", "adm:rgs"), btn("🌐 Website", url=website_url())],
                       [btn("⬅️ Dashboard", "adm:home")]]))
    elif act == "rgs":
        async with read() as s:
            regs = list((await s.execute(select(Region).order_by(Region.sort_order))).scalars())
        lines = [f"{r.emoji} {esc(r.display_name)} → {esc(r.provider_region_id or 'unmapped')} · {r.tier}" for r in regs]
        await reply(update, "🌍 <b>Regions</b>\n" + "\n".join(lines), M([[btn("🔄 Re-sync from provider", "adm:rsy", style=BLUE)],
                                                                         [btn("⬅️ Settings", "adm:set")]]))
    elif act in ("ru", "rur"):
        if act == "rur":
            await stock_service.set_conf("rules", "")
            async with tx() as s:
                await audit(s, uid, "RESET_RULES")
        from glorybot import texts as _t
        _set(ctx, step="adm_rules")
        await reply(update, "📜 <b>Current rules</b> (what customers see):\n\n" + await _t.get_rules()
                    + "\n\n✏️ <b>Send the new rules as one message</b> to replace them (plain text; use new lines).",
                    M([[btn("↩️ Reset to default", "adm:rur", style=RED)], [btn("⬅️ Settings", "adm:set")]]))
    elif act == "em":
        _set(ctx, step="adm_emoji")
        await reply(update, "✨ Send me a message containing the premium emoji you want to use. I'll reply with their IDs "
                            "for <code>PREMIUM_EMOJI_IDS</code> (format <code>name=ID</code>, names: buy, stock, guild, orders, "
                            "wallet, profile, support, refresh, cancel, pay, ok, home, fire, trophy).", M([[btn("⬅️ Settings", "adm:set")]]))
    elif act == "au":
        async with read() as s:
            rows = list((await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(15))).scalars())
        await reply(update, "🧾 <b>Audit log</b>\n\n" + ("\n".join(
            f"{fmt_dt(a.at)} · <code>{a.actor_id}</code> · {esc(a.action)} {esc(a.target or '')}" for a in rows) or "Empty"),
                    M([[btn("⬅️ Settings", "adm:set")]]))
    elif act in ("pm", "pmv"):
        await _payments(update, p)
    elif act in ("gl", "g", "ga", "gdc"):
        await _groups(update, ctx, p, provider, uid)
    elif act == "nt":
        try:
            notes = await provider.notifications()
            txt = "🔔 <b>Provider notices</b>\n\n" + ("\n\n".join(
                f"<b>{esc(n.get('title', n.get('type', '')))}</b>\n{esc(n.get('message', ''))}" for n in notes[:10]) or "No new notices.")
        except Exception as e:  # noqa: BLE001
            txt = f"🔴 Could not fetch notices: {esc(type(e).__name__)}"
        await reply(update, txt, M([[btn("⬅️ Settings", "adm:set")]]))
    elif act == "api":
        await _api_status(update, ctx, provider)
    elif act == "tr":
        _set(ctx, step="adm_reply", tid=int(p[2]))
        await reply(update, "💬 Send your reply to the customer:")


async def _live_orders(update, provider) -> None:
    orders = await osv.live_orders(10)
    if not orders:
        await reply(update, "🔴 <b>Live orders</b>\n\nNothing running right now.",
                    M([[btn("🔄 Refresh", "adm:lo", style=BLUE), btn("⬅️ Dashboard", "adm:home")]]))
        return
    glory: dict = {}
    try:
        glory = await glory_service.order_glory(provider, [o.id for o in orders])
    except Exception:  # noqa: BLE001
        pass
    async with read() as s:
        users = {u.id: u for u in (await s.execute(select(User).where(User.id.in_({o.user_id for o in orders})))).scalars()}
    lines = ["🔴 <b>Live orders</b>", ""]
    for o in orders:
        gl = glory.get(o.id, {}).get("glory")
        lines.append(f"<b>{o.order_number}</b> · {_uname(users.get(o.user_id))} · {o.squads} sq · {esc(o.region_code)}\n"
                     f"   🏷 {esc(o.guild_name or o.guild_uid)} · {status_label(o.status)}"
                     f"{' · 🏆 ' + format(gl, ',') if gl is not None else ''}\n   🕐 {fmt_dt(o.created_at)}")
    btns = [btn(o.order_number, f"adm:vw:{o.id}", style=BLUE) for o in orders]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([btn("🔄 Refresh", "adm:lo", style=BLUE), btn("🔎 Search", "adm:se:o"), btn("⬅️ Dashboard", "adm:home")])
    await reply(update, "\n".join(lines), M(rows))


async def _stock_screen(update, provider) -> None:
    snap = await stock_service.snapshot(provider, force=True)
    lines = ["📦 <b>Stock & Credits</b>", ""]
    if snap["credits"] is None:
        lines.append("🔴 Can't reach the provider right now.")
    else:
        c = snap["credits"]
        lines += [f"💳 Website credits: basic <b>{c['basic']}</b> · premium <b>{c['premium']}</b>",
                  "<i>Customers see exactly these numbers.</i>"]
    if snap["paused"]:
        lines.append("\n⏸ <b>Orders are paused.</b>")
    lines += ["", "Tap a region to switch it out of stock / back in:"]
    btns = []
    for r in snap["rows"]:
        reg = r["region"]
        label = (f"✅ {reg.display_name} · {r['stock']}" if r["enabled"] else f"⛔ {reg.display_name} · OFF")
        btns.append(btn(label, f"adm:rt:{reg.code}", style=GREEN if r["enabled"] else RED))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([btn("➕ Add credits (redeem code)", "adm:rd", style=GREEN), btn("🌐 Website", url=website_url())])
    rows.append([btn("▶️ Resume orders" if snap["paused"] else "⏸ Pause all orders", "adm:pz:s",
                     style=GREEN if snap["paused"] else RED), btn("🔄 Refresh", "adm:st", style=BLUE)])
    rows.append([btn("⬅️ Dashboard", "adm:home")])
    await reply(update, "\n".join(lines), M(rows))


async def _price_screen(update, note: str = "") -> None:
    async with read() as s:
        npr = await osv.current_price(s, None, "NPR")
    big, small = PRICE_STEPS["NPR"]
    rows = [[btn(f"➖ {big}", f"adm:pp:NPR:-{big}", style=RED), btn(f"➖ {small}", f"adm:pp:NPR:-{small}", style=RED),
             btn(f"➕ {small}", f"adm:pp:NPR:{small}", style=GREEN), btn(f"➕ {big}", f"adm:pp:NPR:{big}", style=GREEN)],
            [btn("✏️ Set exact price", "adm:prs:NPR", style=GREEN)], [btn("⬅️ Dashboard", "adm:home", style=BLUE)]]
    await reply(update, note + "💰 <b>Price per squad</b>\n\n"
                        f"🇳🇵 <b>{fmt_money(npr, 'NPR')}</b>\n\n"
                        "<i>Changes apply to new orders at once; existing orders keep their price.</i>", M(rows))


async def _payments(update, p) -> None:
    if p[1] == "pmv":
        async with read() as s:
            it = await s.get(PaymentIntent, int(p[2]))
            u = await s.get(User, it.user_id) if it else None
        if not it:
            await reply(update, "🔴 Not found.", kb.back_admin())
            return
        await reply(update, f"💳 <b>{esc(it.identifier)}</b> — {it.status}{' 🧪' if it.is_mock else ''}\n👤 {_uname(u)} (<code>{u.telegram_id}</code>)\n"
                            f"{it.kind} · {fmt_money(it.amount, it.currency)} · {it.squads} squad(s) · UID <code>{esc(it.guild_uid or '-')}</code>\n"
                            f"🕐 {fmt_dt(it.created_at)}\nProvider id: <code>{esc(it.provider_payment_id)}</code>\nNote: {esc(it.note or '-')}",
                    M([[btn("⬅️ Payments", "adm:pm")]]))
        return
    async with read() as s:
        rows = list((await s.execute(select(PaymentIntent).order_by(PaymentIntent.id.desc()).limit(15))).scalars())
    icon = {"PAID": "🟢", "PENDING": "🟡", "CREDITED": "🟡", "FAILED": "🔴", "MISMATCH": "🔴", "REVIEW": "🟠", "CLOSED": "⚪"}
    btns = [[btn(f"{icon.get(i.status, '⚪')} {fmt_money(i.amount, i.currency)} · {i.kind.lower()} · {i.status}", f"adm:pmv:{i.id}")]
            for i in rows] + [[btn("⬅️ Back", "adm:home")]]
    await reply(update, "💳 <b>Latest payments</b>\n🟠 REVIEW / 🔴 MISMATCH need a manual look in your APINepal dashboard.", M(btns))


async def _api_status(update, ctx, provider) -> None:
    txt = f"🔌 <b>API status</b>\nMode: {'🧪 MOCK' if provider.is_mock else '🟢 LIVE'}\n"
    try:
        bal = await provider.get_account_balance()
        groups = await provider.list_groups()
        running = sum(1 for g in groups if g.status == "running")
        txt += (f"💳 Credits — basic: <b>{bal.get('basic_credits')}</b>, premium: <b>{bal.get('premium_credits')}</b>\n"
                f"👥 Groups: {len(groups)} (running {running}) of max {bal.get('max_groups')}")
    except Exception as e:  # noqa: BLE001
        txt += f"🔴 Provider check failed: {esc(type(e).__name__)}"
    for cur, gw in ctx.application.bot_data.get("gateways", {}).items():
        try:
            await gw.ping()
            txt += "\n✅ Nepali payments connected" + (" (test)" if gw.is_mock else "")
        except Exception as e:  # noqa: BLE001
            txt += f"\n🔴 Payments: {esc(type(e).__name__)} {esc(getattr(e, 'status', '') or '')}"
    await reply(update, txt, M([[btn("⬅️ Settings", "adm:set")]]))


async def _user_card(update, uid_: int) -> None:
    async with read() as s:
        u = await s.get(User, uid_)
        n = (await s.execute(select(func.count(Order.id)).where(Order.user_id == uid_))).scalar_one()
    if not u:
        await reply(update, "🔴 User not found.", kb.back_admin())
        return
    async with tx() as s:
        wn = await ws.get_wallet(s, uid_, "NPR", lock=False)
    await reply(update,
                f"👤 <b>{_uname(u)}</b> (<code>{u.telegram_id}</code>) @{esc(u.telegram_username or '-')}\n"
                f"🌍 {esc(u.country or '-')}\nStatus: {'🟢 Active' if u.status == 'ACTIVE' else '🔴 Suspended'}\n"
                f"Wallet: {fmt_money(wn.available, 'NPR')}\n"
                f"Orders: {n} · Squads: {u.total_squads}",
                M([[btn("🔴 Suspend / 🟢 Reactivate", f"adm:ust:{u.id}", style=RED)],
                   [btn("💰 Adjust wallet", f"adm:uad:{u.id}", style=GREEN), btn("🧾 Orders", f"adm:uor:{u.id}")],
                   [btn("💬 Contact", url=f"tg://user?id={u.telegram_id}"), btn("⬅️ Back", "adm:home")]]))


# ------------------------------------------------------------------ text flows
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    f = _flow(ctx)
    step = f.get("step", "")
    msg = update.message
    text = (msg.text or "").strip()
    provider = ctx.application.bot_data["provider"]

    if step == "adm_bc_msg":
        f.update(step="adm_bc_ready", chat=msg.chat_id, mid=msg.message_id)
        await ctx.bot.copy_message(uid, msg.chat_id, msg.message_id)
        await msg.reply_text("👆 Preview. Send to everyone in the chosen audience?", reply_markup=M([
            [btn("✅ Send broadcast", "adm:bcgo", style=GREEN)], [btn("✖️ Cancel", "adm:home", style=RED)]]))
        return
    if step == "adm_emoji":
        ents = [e for e in (msg.entities or []) if e.type == "custom_emoji"]
        if not ents:
            await msg.reply_text("I don't see any premium emoji in that message. Send one that has premium emoji in it.")
            return
        lines = [f"<code>name={e.custom_emoji_id}</code>  ← {esc(msg.parse_entity(e))}" for e in ents]
        ctx.user_data.pop("flow", None)
        await msg.reply_text("✨ Replace <code>name</code> with a button name and put the pairs in PREMIUM_EMOJI_IDS, "
                             "then set PREMIUM_EMOJI_BUTTONS=true:\n\n" + "\n".join(lines), parse_mode=HTML,
                             reply_markup=M([[btn("⬅️ Settings", "adm:set")]]))
        return
    if not text:
        return
    if step == "adm_rules":
        body = text.strip()
        if len(body) > 3500:
            await msg.reply_text("🔴 Too long — keep it under 3500 characters.")
            return
        await stock_service.set_conf("rules", "<b>📜 Rules</b>\n\n" + esc(body))
        async with tx() as s:
            await audit(s, uid, "EDIT_RULES")
        ctx.user_data.pop("flow", None)
        await msg.reply_text("✅ Rules updated. Customers see them under 📜 Rules.",
                             reply_markup=M([[btn("⚙️ Settings", "adm:set"), btn("⬅️ Dashboard", "adm:home")]]))
    elif step == "adm_price":
        cur = f["cur"]
        try:
            price = to_money(Decimal(text.replace(",", "")))
        except (InvalidOperation, ValueError):
            price = Decimal("0")
        if not PRICE_MIN[cur] <= price <= PRICE_MAX[cur]:
            await msg.reply_text(f"🔴 Enter a price between {PRICE_MIN[cur]} and {PRICE_MAX[cur]}:")
            return
        async with tx() as s:
            await osv.set_price(s, price, uid, cur)
        ctx.user_data.pop("flow", None)
        await msg.reply_text(f"🟢 {cur} price is now <b>{fmt_money(price, cur)}</b> per squad.", parse_mode=HTML,
                             reply_markup=M([[btn("💰 Prices", "adm:pr", style=GREEN), btn("⬅️ Dashboard", "adm:home")]]))
    elif step == "adm_redeem":
        now = time.monotonic()
        _redeem_hits[:] = [t for t in _redeem_hits if now - t < 60]
        if len(_redeem_hits) >= 5:
            await msg.reply_text("🟡 The provider allows 5 tries per minute. Wait a moment and send the code again.")
            return
        _redeem_hits.append(now)
        code = text.strip()
        try:
            res = await provider.redeem_coupon(code)
        except NotImplementedError:
            res = {"_ok": False, "error": "redeem is not available in this mode"}
        except Exception as e:  # noqa: BLE001
            res = {"_ok": False, "error": f"request failed ({type(e).__name__})"}
        try:
            await msg.delete()  # don't leave the code lying in the chat
        except TelegramError:
            pass
        async with tx() as s:
            await audit(s, uid, "REDEEM_COUPON" if res.get("_ok") else "REDEEM_COUPON_FAILED", None, code[:3] + "***")
        stock_service.invalidate()
        detail = esc(str(res.get("message") or res.get("error") or res.get("detail") or "")[:200])
        try:
            cr = await stock_service.credits(provider, force=True)
            now_line = f"\n💳 Credits now: basic <b>{cr['basic']}</b> · premium <b>{cr['premium']}</b>"
        except Exception:  # noqa: BLE001
            now_line = ""
        ok = bool(res.get("_ok"))
        if ok:
            ctx.user_data.pop("flow", None)
        await ctx.bot.send_message(uid, (f"✅ <b>Code redeemed!</b> {detail}" if ok else f"🔴 <b>Code not accepted.</b> {detail}\nSend another code or go back.")
                                   + now_line, parse_mode=HTML,
                                   reply_markup=M([[btn("🎟 Redeem another", "adm:rd", style=GREEN), btn("📦 Stock", "adm:st", style=BLUE)],
                                                   [btn("⬅️ Dashboard", "adm:home")]]))
    elif step in ("adm_search_o", "adm_search_u"):
        ctx.user_data.pop("flow", None)
        async with read() as s:
            if step == "adm_search_o":
                cond = [Order.order_number.ilike(f"%{text}%"), Order.guild_uid == text, Order.guild_name.ilike(f"%{text}%")]
                ucond = [User.display_name.ilike(f"%{text}%")]
                if text.isdigit():
                    ucond.append(User.telegram_id == int(text))
                cond.append(Order.user_id.in_(select(User.id).where(or_(*ucond))))
                rows = list((await s.execute(select(Order).where(or_(*cond)).order_by(Order.id.desc()).limit(10))).scalars())
                btns = [[btn(f"{o.order_number} · {status_label(o.status)}", f"adm:vw:{o.id}")] for o in rows]
            else:
                t = text.lstrip("@")
                cond = [User.display_name.ilike(f"%{t}%"), User.telegram_username.ilike(f"%{t}%"), User.login_username_lower == t.lower()]
                if text.isdigit():
                    cond.append(User.telegram_id == int(text))
                rows = list((await s.execute(select(User).where(or_(*cond)).limit(10))).scalars())
                btns = [[btn(f"{x.display_name or x.login_username} ({x.telegram_id})", f"adm:uc:{x.id}")] for x in rows]
        await msg.reply_text(f"🔎 {len(rows)} result(s)", reply_markup=M(btns + [[btn("⬅️ Back", "adm:home")]]))
    elif step == "adm_adj_amount":
        try:
            amt = to_money(Decimal(text))
            assert amt != 0
        except (InvalidOperation, ValueError, AssertionError):
            await msg.reply_text("🔴 Send a non-zero number, e.g. 100 or -50:")
            return
        f.update(step="adm_adj_reason", amount=str(amt))
        await msg.reply_text("✍️ Now send the <b>reason</b> (required, audited):", parse_mode=HTML)
    elif step == "adm_adj_reason":
        cur = f.get("cur", "NPR")
        try:
            async with tx() as s:
                await ws.adjust(s, f["target"], Decimal(f["amount"]), text[:200], f"admin:{uid}", currency=cur)
                await audit(s, uid, "WALLET_ADJUST", f"user:{f['target']}", f"{f['amount']} {cur} | {text[:200]}")
        except ws.InsufficientFunds:
            await msg.reply_text("🔴 Would make the balance negative. Cancelled.")
        else:
            await msg.reply_text(f"🟢 Wallet adjusted by {fmt_money(Decimal(f['amount']), cur)}.", reply_markup=kb.back_admin())
        ctx.user_data.pop("flow", None)
    elif step == "adm_reply":
        async with tx() as s:
            t = await s.get(SupportTicket, f["tid"])
            u = await s.get(User, t.user_id)
            s.add(SupportMessage(ticket_id=t.id, sender="admin", text=text))
            no = t.ticket_no
        await safe_send(ctx.bot, u.telegram_id, f"🛟 <b>Support reply</b> ({no})\n\n{esc(text)}")
        ctx.user_data.pop("flow", None)
        await msg.reply_text("🟢 Reply sent.", reply_markup=kb.back_admin())
    else:
        await msg.reply_text("Use /admin to open the dashboard.")


# ------------------------------------------------------------------ broadcast worker
async def run_broadcast(app, bid: int) -> None:
    async with read() as s:
        bc = await s.get(Broadcast, bid)
        q = select(User.telegram_id)
        if bc.audience == "active":
            q = q.where(User.status == "ACTIVE")
        elif bc.audience == "orders":
            q = q.where(User.id.in_(select(Order.user_id)))
        ids = list((await s.execute(q)).scalars())
    sent = failed = blocked = 0
    fails: list[tuple[int, str, str]] = []
    for tid in ids:
        for attempt in range(2):
            try:
                await app.bot.copy_message(tid, bc.source_chat_id, bc.source_message_id)
                sent += 1
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except Forbidden:
                blocked += 1
                fails.append((tid, "BLOCKED", "bot blocked"))
            except TelegramError as e:
                failed += 1
                fails.append((tid, "FAILED", type(e).__name__))
            break
        await asyncio.sleep(0.06)  # stay far below Telegram's ~30 msg/s limit
    async with tx() as s:
        b = await s.get(Broadcast, bid)
        b.sent, b.failed, b.blocked, b.status = sent, failed, blocked, "DONE"
        for tid, st, err in fails:
            s.add(BroadcastDelivery(broadcast_id=bid, telegram_id=tid, status=st, error=err))
    await safe_send(app.bot, bc.admin_id, f"📢 <b>Broadcast #{bid} finished</b>\n🟢 Sent: {sent}\n"
                                          f"🔴 Failed: {failed}\n🚫 Blocked: {blocked}")


async def _groups(update, ctx, p, provider, uid) -> None:
    act = p[1]
    try:
        if act == "gl":
            groups = await provider.list_groups()
            hist = {str(h.get("group_id")): h for h in await provider.group_history()}
            page = int(p[2]) if len(p) > 2 else 0
            chunk = groups[page * 8:(page + 1) * 8]
            btns = [[btn(f"{'🟢' if g.status == 'running' else '⚪'} {g.clan_id} · {g.region} · "
                       f"{int((hist.get(g.group_id) or {}).get('glory_farmed') or 0):,}🏆", callback_data=f"adm:g:{g.group_id}")]
                    for g in chunk]
            nav = []
            if page > 0:
                nav.append(btn("⬅️", callback_data=f"adm:gl:{page - 1}"))
            if (page + 1) * 8 < len(groups):
                nav.append(btn("➡️", callback_data=f"adm:gl:{page + 1}"))
            await reply(update, f"🏆 <b>Provider groups</b> ({len(groups)})\nTap a group to manage it.",
                        M(btns + ([nav] if nav else []) + [[btn("⬅️ Back", callback_data="adm:home")]]))
            return
        gid = p[2]
        if act == "ga":
            action = p[3]
            if action not in ("restart", "stop"):
                return
            res = await provider.group_action(gid, action)
            async with tx() as s:
                await audit(s, uid, f"GROUP_{action.upper()}", gid)
            await reply(update, f"✅ {action} sent for <code>{esc(gid)}</code>\n<code>{esc(str(res)[:300])}</code>",
                        M([[btn("⬅️ Group", callback_data=f"adm:g:{gid}")]]))
        elif act == "gdc":
            if len(p) > 3 and p[3] == "yes":
                res = await provider.group_action(gid, "delete")
                async with tx() as s:
                    await audit(s, uid, "GROUP_DELETE", gid)
                await reply(update, f"🗑 Deleted <code>{esc(gid)}</code>\n<code>{esc(str(res)[:300])}</code>",
                            M([[btn("⬅️ Groups", callback_data="adm:gl")]]))
            else:
                await reply(update, f"⚠️ Permanently delete group <code>{esc(gid)}</code>? This cannot be undone.",
                            M([[btn("🗑 Yes, delete", callback_data=f"adm:gdc:{gid}:yes"), btn("Cancel", callback_data=f"adm:g:{gid}")]]))
        else:  # card
            hist = next((h for h in await provider.group_history() if str(h.get("group_id")) == gid), {})
            g = next((x for x in await provider.list_groups() if x.group_id == gid), None)
            try:
                live = await provider.group_action(gid, "get-glory") if g and g.status == "running" else None
            except Exception:  # noqa: BLE001
                live = None
            try:
                cinfo = await provider.clan_info(gid)
            except Exception:  # noqa: BLE001
                cinfo = None
            gname = glory_service.extract_name(cinfo) if cinfo else (g.name if g else None)
            txt = (f"🏆 <b>Group</b> <code>{esc(gid)}</code>\nGuild name: <b>{esc(gname or '—')}</b>\nClan/UID: <code>{esc(g.clan_id if g else hist.get('clan_id'))}</code>\n"
                   f"Region: {esc(g.region if g else hist.get('region'))}\nStatus: {esc(g.status if g else hist.get('status', 'gone'))}\n"
                   f"Glory farmed: <b>{int(hist.get('glory_farmed') or 0):,}</b>"
                   + (f"\nLive: <code>{esc(str(live)[:200])}</code>" if live else "")
                   + (f"\nClan info: <code>{esc(str(cinfo)[:300])}</code>" if cinfo else ""))
            await reply(update, txt, M([
                [btn("🔄 Restart", callback_data=f"adm:ga:{gid}:restart"), btn("⏹ Stop", callback_data=f"adm:ga:{gid}:stop")],
                [btn("🗑 Delete", callback_data=f"adm:gdc:{gid}"), btn("🔄 Refresh", callback_data=f"adm:g:{gid}")],
                [btn("⬅️ Groups", callback_data="adm:gl")]]))
    except Exception as e:  # noqa: BLE001
        log.warning("group action failed: %s", type(e).__name__)
        await reply(update, f"🔴 Provider request failed: {esc(type(e).__name__)}", kb.back_admin())
