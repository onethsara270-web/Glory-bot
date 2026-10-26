import asyncio
import logging
import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardMarkup as M
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from glorybot import keyboards as kb
from glorybot import texts
from glorybot.config import get_settings
from glorybot.database.models import (Order, OrderStatus, PaymentIntent, ProviderRequest, ProviderRequestStatus, Region,
                                      SupportMessage, SupportTicket, User)
from glorybot.database.session import read, tx
from glorybot.handlers.common import HTML, esc, notify_admins, reply
from glorybot.payments.base import GatewayError
from glorybot.services import glory_service, runner, stock_service
from glorybot.services import order_service as osv
from glorybot.services import payment_service as pay
from glorybot.services import wallet_service as ws
from glorybot.texts import em
from glorybot.utils.money import fmt_money, order_total
from glorybot.utils.tasks import spawn
from glorybot.utils.timefmt import fmt_dt

log = logging.getLogger(__name__)
btn, GREEN, BLUE, RED = kb.btn, kb.GREEN, kb.BLUE, kb.RED
PAGE = 5
LIVE_REFRESHES, LIVE_EVERY = 8, 25  # live guild view auto-updates for ~3 minutes
_seen: dict[tuple[int, str], int] = {}  # last glory shown per (user, guild): gives the "+N" change

COUNTRIES = [("🇳🇵", "Nepal"), ("🇮🇳", "India"), ("🇧🇩", "Bangladesh"), ("🇵🇰", "Pakistan"), ("🇧🇷", "Brazil"),
             ("🇮🇩", "Indonesia"), ("🇹🇭", "Thailand"), ("🇸🇦", "Saudi Arabia"), ("🇦🇪", "UAE"), ("🌍", "Other…")]
TX_LABEL = {"DEPOSIT": "➕ Added", "RELEASE": "↩️ Returned", "SPEND": "🛒 Spent", "REFUND": "↩️ Refund",
            "ADJUSTMENT": "✏️ Adjusted", "RESERVE": "🔒 Held", "WHEEL_PRIZE": "🎁 Bonus"}

WARNING = ("⚠️ <b>Check before you order</b>\n"
           "• Auto Approval must be <b>OFF</b>\n• All ranks must stay <b>default</b>\n\n"
           "❌ Wrong ID or settings = no refund.")


def _flow(ctx) -> dict:
    return ctx.user_data.setdefault("flow", {})


def _set(ctx, **kw) -> None:
    ctx.user_data["flow"] = kw


def _clear(ctx) -> None:
    ctx.user_data.pop("flow", None)


def _contact() -> str:
    return "@" + get_settings().support_username.lstrip("@")


def _cur(u: User) -> str:
    return "NPR"  # Nepali payments only


def _name(u: User) -> str:
    return u.display_name or u.login_username or "friend"


async def get_user(tg_id: int) -> User | None:
    async with read() as s:
        return (await s.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()


async def _need_user(update, ctx) -> User | None:
    u = await get_user(update.effective_user.id)
    if u is None:
        await reply(update, "👋 Send /start to create your profile first.")
    return u


# ------------------------------------------------------------------ start / profile setup
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _clear(ctx)
    user = await get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(f"{em('fire', '🔥')} Welcome back, <b>{esc(_name(user))}</b>!",
                                        parse_mode=HTML, reply_markup=kb.main_menu())
        await show_home(update, ctx, user)
        return
    _set(ctx, kind="onboard", step="ob_name")
    await update.message.reply_text(
        f"{em('trophy', '🏆')} <b>Welcome to Glory Push!</b>\n\nLet's set up your profile — it takes 10 seconds.\n\n"
        "👤 <b>What's your name?</b>", parse_mode=HTML, reply_markup=kb.main_menu())


async def cmd_weblogin(update: Update, ctx) -> None:
    """One-time code that logs the same account (wallet + orders) into the BNY SHOP website."""
    st = get_settings()
    user = await _need_user(update, ctx)
    if user is None:
        return
    if not st.web_enabled:
        await update.message.reply_text("The website is not enabled right now.")
        return
    from glorybot.web import auth
    code = await auth.make_login_code(user.id)
    site = st.public_url or "your BNY SHOP website"
    await update.message.reply_text(
        f"🌐 <b>BNY SHOP website login</b>\n\nYour one-time code: <code>{code}</code>\n⏳ Valid for {auth.CODE_MINUTES} minutes, works once.\n\n"
        f"Open {esc(site)}, choose <b>Login with Telegram code</b> and enter it. "
        "Your wallet and orders are the same on the bot and the website.", parse_mode=HTML)


async def cmd_help(update: Update, ctx) -> None:
    await update.message.reply_text(
        "🔵 <b>Help</b>\n\n🛒 SHOP NOW — order a glory push\n📦 Squad Stock — what's available now\n"
        "⚔️ My Guilds — watch glory grow live\n🧾 My Orders — all your orders\n💰 Wallet — add balance\n"
        f"/cancel — stop what you're doing\n\n🛟 Need help? {_contact()}", parse_mode=HTML, reply_markup=kb.main_menu())


async def cmd_cancel(update: Update, ctx) -> None:
    _clear(ctx)
    await update.message.reply_text("✖️ Cancelled.", reply_markup=kb.main_menu())
    u = await get_user(update.effective_user.id)
    if u:
        await show_home(update, ctx, u)


def _country_kb(prefix_cancel: str | None) -> M:
    rows, row = [], []
    for i, (flag, name) in enumerate(COUNTRIES):
        row.append(btn(f"{flag} {name}", f"u:co:{i}", style=BLUE if name != "Other…" else None))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if prefix_cancel:
        rows.append(kb.cancel_row(prefix_cancel))
    return M(rows)


async def _ask_country(update, ctx, onboarding: bool) -> None:
    await reply(update, "🌍 <b>Which country are you from?</b>", _country_kb(None if onboarding else "u:prof"))


async def _after_country(update, ctx, msg_update) -> None:
    f = _flow(ctx)
    if f.get("kind") == "onboard":
        await _finish_onboarding(msg_update, ctx, "NPR")
    else:
        async with tx() as s:
            u = (await s.execute(select(User).where(User.telegram_id == msg_update.effective_user.id))).scalar_one()
            u.country = f["country"]
        _clear(ctx)
        await show_profile(msg_update, ctx, await get_user(msg_update.effective_user.id), "✅ Country updated.")


async def _send_or_edit(update, text: str, markup=None) -> None:
    if update.callback_query is not None:
        await reply(update, text, markup)
    else:
        await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=markup)


async def _finish_onboarding(update, ctx, currency: str) -> None:
    f, tg = _flow(ctx), update.effective_user
    try:
        async with tx() as s:
            user = User(telegram_id=tg.id, telegram_username=tg.username, display_name=f.get("name") or tg.first_name,
                        country=f.get("country"), currency=currency, referral_code=secrets.token_hex(4).upper(),
                        total_spent=0)
            s.add(user)
            await s.flush()
            await ws.get_wallet(s, user.id, currency, lock=False)
    except IntegrityError:
        pass
    _clear(ctx)
    user = await get_user(tg.id)
    await _send_or_edit(update, f"🎉 <b>You're all set, {esc(_name(user))}!</b>\n🌍 {esc(user.country or '—')}")
    await show_home(update, ctx, user)


# ------------------------------------------------------------------ home
async def show_home(update, ctx, user: User) -> None:
    provider = ctx.application.bot_data["provider"]
    async with tx() as s:
        w = await ws.get_wallet(s, user.id, _cur(user), lock=False)
    lines = [f"{em('trophy', '🏆')} <b>Glory Push</b>", "",
             f"👋 <b>{esc(_name(user))}</b>", f"💰 Wallet: <b>{fmt_money(w.available, _cur(user))}</b>"]
    try:
        snap = await stock_service.snapshot(provider)
        if snap["paused"]:
            lines.append("⏸ <b>Orders are paused for a moment.</b>")
        elif not snap["window_open"]:
            lines.append("🌙 <b>Service closed</b> — opens Monday 12 AM (Nepal time).")
        elif snap["credits"] is not None:
            total = sum(r["stock"] for r in snap["rows"] if r["available"])
            lines.append("🟢 <b>Squads available</b> — tap 🛒 SHOP NOW" if total else "⛔ <b>Out of stock right now.</b>")
    except Exception:  # noqa: BLE001
        pass
    lines += ["", "Choose what you need 👇"]
    await reply(update, "\n".join(lines), kb.home_kb())


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _clear(ctx)
    user = await _need_user(update, ctx)
    if user:
        await show_home(update, ctx, user)


async def cmd_orders(update, ctx):
    u = await _need_user(update, ctx)
    if u:
        await show_orders(update, u, 0)


async def cmd_wallet(update, ctx):
    u = await _need_user(update, ctx)
    if u:
        await show_wallet(update, u)


async def cmd_profile(update, ctx):
    u = await _need_user(update, ctx)
    if u:
        await show_profile(update, ctx, u)


async def cmd_support(update, ctx):
    u = await _need_user(update, ctx)
    if u:
        await show_support(update)


async def cmd_stock(update, ctx):
    u = await _need_user(update, ctx)
    if u:
        await show_stock(update, ctx, u)


# ------------------------------------------------------------------ stock
async def show_stock(update, ctx, user: User, force: bool = False) -> None:
    provider = ctx.application.bot_data["provider"]
    snap = await stock_service.snapshot(provider, force=force)
    async with read() as s:
        price = await osv.current_price(s, None, "NPR")
    stamp = datetime.now(get_settings().tz).strftime("%I:%M:%S %p")
    lines = [f"{em('stock', '🔍')} <b>Squads available now</b>", ""]
    if snap["credits"] is None:
        lines.append("🟡 Stock can't be loaded right now. Please refresh in a moment.")
    else:
        c = snap["credits"]
        lines += [f"🟢 Normal regions: <b>{c['basic']}</b> squads", f"💎 Premium regions: <b>{c['premium']}</b> squads", "",
                  "<b>Regions</b>"]
        for r in snap["rows"]:
            reg = r["region"]
            mark = " 💎" if reg.tier == "premium" else ""
            lines.append(f"{reg.emoji} {esc(reg.display_name)}{mark} — " + ("✅ available" if r["available"] else "⛔ sold out"))
    if snap["paused"]:
        lines += ["", "⏸ Orders are paused for a moment."]
    elif not snap["window_open"]:
        lines += ["", "🌙 Service closed until Monday 12 AM (Nepal time)."]
    lines += ["", f"💵 Price: <b>{fmt_money(price, 'NPR')}</b> per squad", f"🕐 Updated {stamp}"]
    await reply(update, "\n".join(lines), M([[btn("🔄 Refresh", "u:stockr", style=BLUE, icon="refresh"),
                                              btn("🛒 SHOP NOW", "u:ord", style=GREEN, icon="buy")],
                                             [btn("🏠 Menu", "u:home", style=BLUE, icon="home")]]))


# ------------------------------------------------------------------ ordering
async def start_order(update, ctx, user: User) -> None:
    if user.status != "ACTIVE":
        await reply(update, f"🔴 Your account is suspended. Contact {_contact()}.", kb.nav_home())
        return
    provider = ctx.application.bot_data["provider"]
    snap = await stock_service.snapshot(provider, force=True)
    if snap["paused"]:
        await reply(update, "⏸ <b>Orders are paused for a moment.</b>\nPlease try again soon.", kb.nav_home())
        return
    if not snap["window_open"]:
        await reply(update, "🌙 <b>Service is closed.</b>\nIt opens again Monday 12 AM (Nepal time).", kb.nav_home())
        return
    if snap["credits"] is None:
        await reply(update, "🟡 Stock can't be checked right now. Please try again in a minute.", kb.nav_home())
        return
    rows, row = [], []
    for r in snap["rows"]:
        reg = r["region"]
        if r["available"]:
            row.append(btn(f"{reg.emoji} {reg.display_name}", f"u:rg:{reg.code}", style=GREEN))
        else:
            row.append(btn(f"⛔ {reg.display_name} · sold out", "u:so", style=RED))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not any(r["available"] for r in snap["rows"]):
        await reply(update, "⛔ <b>Everything is out of stock right now.</b>\nCheck back soon!", kb.nav_home())
        return
    rows.append([btn("🔍 Check SQD available", "u:stock", style=BLUE, icon="stock")])
    rows.append(kb.cancel_row())
    _set(ctx, kind="order", step="region")
    await reply(update, f"{em('buy', '🛒')} <b>SHOP NOW</b>\n1 squad = 1 glory push.\n\n🌍 Pick your region "
                        "(⛔ = sold out):", M(rows))


async def _ask_uid(update, ctx, prefix: str = "") -> None:
    _flow(ctx)["step"] = "ord_uid"
    await reply(update, f"{prefix}🛡 Send your <b>Guild ID</b> (numbers only):", M([kb.cancel_row()]))


async def _show_warn(update, ctx) -> None:
    f = _flow(ctx)
    async with read() as s:
        region = await s.get(Region, f["region"])
    f["step"] = "warn"
    await _send_or_edit(update, f"🛡 Guild ID: <code>{esc(f['uid'])}</code>\n🌍 Region: {region.emoji} {esc(region.display_name)}\n\n{WARNING}",
                        M([[btn("✅ I checked — continue", "u:ok", style=GREEN, icon="ok")],
                           [btn("✏️ Change ID", "u:uid"), btn("🌍 Change region", "u:reg")], kb.cancel_row()]))


async def _show_squads(update, ctx, user: User) -> None:
    f = _flow(ctx)
    provider = ctx.application.bot_data["provider"]
    try:
        have = await stock_service.check_orderable(provider, f["region"], 1, force=True)
    except stock_service.StockError as e:
        await reply(update, str(e), kb.nav_home())
        return
    cur = _cur(user)
    async with read() as s:
        price = await osv.current_price(s, f["region"], cur)
    top = min(get_settings().max_squads_per_order, have)
    btns = [btn(f"{n} · {fmt_money(price * n, cur)}", f"u:sq:{n}", style=GREEN) for n in range(1, top + 1)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [kb.cancel_row()]
    f["step"] = "squads"
    await reply(update, f"👥 <b>How many squads?</b>\n💵 {fmt_money(price, cur)} each · 📦 up to {top} now", M(rows))


async def _show_summary(update, ctx, user: User) -> None:
    f = _flow(ctx)
    cur = _cur(user)
    async with read() as s:
        region = await s.get(Region, f["region"])
        price = await osv.current_price(s, f["region"], cur)
    async with tx() as s:
        w = await ws.get_wallet(s, user.id, cur, lock=False)
    total = order_total(price, f["squads"])
    f["step"] = "summary"
    pay_label = f"📲 Pay {fmt_money(total, cur)} by QR"
    rows = [[btn(pay_label, "u:pq", style=GREEN, icon="pay")]]
    if w.available >= total:
        rows.append([btn(f"💰 Pay from wallet ({fmt_money(w.available, cur)})", "u:pw", style=GREEN, icon="wallet")])
    else:
        rows.append([btn(f"➕ Add balance (wallet {fmt_money(w.available, cur)})", "u:top", style=GREEN, icon="wallet")])
    rows.append([btn("✏️ Change squads", "u:ok", style=RED), btn("✖️ Cancel", "u:home", style=RED, icon="cancel")])
    await reply(update, f"🏆 <b>Your order</b>\n\n🌍 {region.emoji} {esc(region.display_name)}\n"
                        f"🛡 Guild ID: <code>{esc(f['uid'])}</code>\n👥 Squads: <b>{f['squads']}</b>\n"
                        f"💳 <b>Total: {fmt_money(total, cur)}</b>\n\nPay and your bot request is sent automatically ✅", M(rows))


async def _pay_qr(update, ctx, user: User) -> None:
    f = _flow(ctx)
    if f.get("step") not in ("summary", "paying_qr"):
        return
    f["step"] = "paying_qr"
    try:
        await pay.start_payment(ctx.application, user, f["region"], f["uid"], f["squads"])
    except (osv.OrderError, stock_service.StockError) as e:
        f["step"] = "summary"
        await reply(update, f"🔴 {esc(e)}", kb.nav_home())
    except GatewayError:
        f["step"] = "summary"
        await reply(update, f"🔴 Payment is unavailable right now.\nTry again in a minute or contact {_contact()}.",
                    M([[btn("🔁 Try again", "u:ok", style=GREEN)], [btn("🏠 Menu", "u:home")]]))
    else:
        _clear(ctx)  # the QR is its own message with its own buttons
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except TelegramError:
            pass


async def _pay_wallet(update, ctx, user: User) -> None:
    f = _flow(ctx)
    if f.get("step") != "summary":
        return
    f["step"] = "paying_wallet"
    provider = ctx.application.bot_data["provider"]
    try:
        await stock_service.check_orderable(provider, f["region"], f["squads"], force=True)
        async with tx() as s:
            order = await osv.create_order(s, user, f["region"], f["uid"], f["squads"], provider.is_mock)
            if await osv.approve_order(s, order.id, 0, provider.is_mock) != "ok":
                raise osv.OrderError("Could not start your order. Please try again.")
            number, oid, total, uid_, rc, squads, cur = (order.order_number, order.id, order.total_amount,
                                                         order.guild_uid, order.region_code, order.squads, order.currency)
    except ws.InsufficientFunds as e:
        f["step"] = "summary"
        await reply(update, f"🔴 Wallet is short by <b>{fmt_money(e.shortfall, _cur(user))}</b>.",
                    M([[btn("➕ Add balance", "u:top", style=GREEN)], [btn("🏠 Menu", "u:home", style=RED)]]))
        return
    except (osv.OrderError, stock_service.StockError) as e:
        f["step"] = "summary"
        await reply(update, f"🔴 {esc(e)}", kb.nav_home())
        return
    _clear(ctx)
    await reply(update, f"✅ <b>Paid {fmt_money(total, cur)} from wallet</b>\n🧾 Order <code>{number}</code> — sending your bot request now…")
    await notify_admins(ctx.bot, f"🆕 <b>NEW ORDER PLACED</b> (wallet){' 🧪 MOCK' if provider.is_mock else ''}\n\n🧾 <code>{number}</code>\n"
                                 f"👤 {esc(_name(user))} (<code>{user.telegram_id}</code>)\n🌍 {esc(rc)} · UID <code>{esc(uid_)}</code>\n"
                                 f"👥 {squads} squad(s) · 💳 {fmt_money(total, cur)}\n🕐 {fmt_dt(datetime.now(get_settings().tz))}\n"
                                 "✅ Placed automatically — nothing for you to confirm.", kb.admin_auto_order_kb(oid, user.telegram_id))
    spawn(ctx.application, runner.run_order(ctx.application, oid))


# ------------------------------------------------------------------ guilds (live glory)
def _guild_text(user_id: int, info: dict, region_name: str, live: bool = False) -> str:
    stamp = datetime.now(get_settings().tz).strftime("%I:%M:%S %p")
    uid = info["uid"]
    title = info.get("name") or f"Guild {uid}"
    head = f"{em('guild', '⚔️')} <b>{esc(title)}</b>\n🆔 <code>{esc(uid)}</code> · 🌍 {esc(region_name)}\n\n"
    if not info["squads"]:
        return head + "No running squads yet."
    prev = _seen.get((user_id, uid))
    _seen[(user_id, uid)] = info["glory"]
    delta = ""
    if prev is not None:
        d = info["glory"] - prev
        delta = f"  <b>▲ +{d:,}</b>" if d > 0 else "  <i>(no change yet)</i>"
    bots = f"{info['bots_running']}/{info['bots']}" if info["bots"] else "—"
    return (head + f"🏆 Glory: <b>{info['glory']:,}</b>{delta}\n👥 Squads: {info['squads']} ({info['running']} running)\n"
            f"🤖 Bots online: {bots}\n{texts.clock_line(info.get('start'))}\n🧾 Orders: {info['orders']}\n\n"
            f"🕐 Updated {stamp}{' · 🔴 live' if live else ''}")


def _guild_kb(region: str, uid: str) -> M:
    return M([[btn("🔄 Refresh", f"u:gr:{region}:{uid}", style=BLUE, icon="refresh"),
               btn("ℹ️ Glory info", f"u:gi:{region}:{uid}", style=BLUE)],
              [btn("➕ Order more", f"u:gm:{region}:{uid}", style=GREEN, icon="buy")],
              [btn("⬅️ My Guilds", "u:gl", style=BLUE), btn("🏠 Menu", "u:home", style=BLUE, icon="home")]])


async def _region_name(code: str) -> str:
    async with read() as s:
        r = await s.get(Region, code)
    return f"{r.emoji} {r.display_name}" if r else code


async def show_glory_info(update, ctx, user: User, region: str, uid: str) -> None:
    provider = ctx.application.bot_data["provider"]
    try:
        info = await glory_service.guild_info(provider, user.id, region, uid)
    except Exception:  # noqa: BLE001
        await reply(update, "🟡 Live data is unavailable right now. Try again in a minute.", _guild_kb(region, uid))
        return
    title = info.get("name") or f"Guild {uid}"
    lines = [f"ℹ️ <b>Glory info — {esc(title)}</b>", f"🆔 <code>{esc(uid)}</code>", "",
             f"🏆 Total glory so far: <b>{info['glory']:,}</b>", ""]
    for i, g in enumerate(info["groups"], 1):
        lines.append(f"<b>Squad {i}</b> · 🏆 {g['glory']:,} · {esc(g['status'])}")
        if g.get("start"):
            lines.append("   " + texts.clock_line(g["start"]))
    if not info["groups"]:
        lines.append("No running squads yet.")
    lines += ["", texts.DETAILS]
    await reply(update, "\n".join(lines), M([[btn("🔄 Refresh", f"u:gi:{region}:{uid}", style=BLUE, icon="refresh"),
                                              btn("⬅️ Back", f"u:gv:{region}:{uid}", style=BLUE)]]))


async def show_guilds(update, user: User) -> None:
    guilds = await glory_service.user_guilds(user.id)
    if not guilds:
        await reply(update, "⚔️ <b>My Guilds</b>\n\nNo guilds yet. Tap 🛒 SHOP NOW to start!",
                    M([[btn("🛒 SHOP NOW", "u:ord", style=GREEN)], [btn("🏠 Menu", "u:home")]]))
        return
    rows = [[btn(f"⚔️ {n or 'Guild'} · {u}", f"u:gv:{r}:{u}", style=BLUE)] for r, u, n in guilds[:12]]
    rows.append([btn("🏠 Menu", "u:home", icon="home")])
    await reply(update, "⚔️ <b>My Guilds</b>\nTap a guild to watch glory grow live:", M(rows))


async def _live_loop(app, chat_id: int, message_id: int, user_id: int, region: str, uid: str) -> None:
    provider = app.bot_data["provider"]
    rname = await _region_name(region)
    last = None
    for _ in range(LIVE_REFRESHES):
        await asyncio.sleep(LIVE_EVERY)
        try:
            info = await glory_service.guild_info(provider, user_id, region, uid)
            text = _guild_text(user_id, info, rname, live=True)
            await app.bot.edit_message_text(text, chat_id, message_id, parse_mode=HTML, reply_markup=_guild_kb(region, uid))
            last = text
        except TelegramError as e:
            if "not modified" in str(e).lower():
                continue
            return
        except Exception:  # noqa: BLE001
            return
    if last:
        try:
            await app.bot.edit_message_text(last.replace(" · 🔴 live", ""), chat_id, message_id, parse_mode=HTML,
                                            reply_markup=_guild_kb(region, uid))
        except TelegramError:
            pass


async def show_guild(update, ctx, user: User, region: str, uid: str, force: bool = False) -> None:
    provider = ctx.application.bot_data["provider"]
    try:
        info = await glory_service.guild_info(provider, user.id, region, uid, force=force)
    except Exception as e:  # noqa: BLE001
        log.warning("guild fetch failed: %s", type(e).__name__)
        await reply(update, "🟡 Live data is unavailable right now. Try again in a minute.", _guild_kb(region, uid))
        return
    rname = await _region_name(region)
    await reply(update, _guild_text(user.id, info, rname, live=bool(info["squads"])), _guild_kb(region, uid))
    q = update.callback_query
    if q and q.message and info["squads"]:
        key = ("live", q.message.chat_id)
        old = ctx.application.bot_data.get(key)
        if old and not old.done():
            old.cancel()
        ctx.application.bot_data[key] = spawn(ctx.application, _live_loop(
            ctx.application, q.message.chat_id, q.message.message_id, user.id, region, uid))


# ------------------------------------------------------------------ orders
async def show_orders(update, user: User, page: int) -> None:
    async with read() as s:
        total = (await s.execute(select(func.count(Order.id)).where(Order.user_id == user.id))).scalar_one()
        rows = list((await s.execute(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc())
                                     .offset(page * PAGE).limit(PAGE))).scalars())
    if not rows:
        await reply(update, "🧾 <b>My Orders</b>\n\nNo orders yet.", M([[btn("🛒 SHOP NOW", "u:ord", style=GREEN)],
                                                                      [btn("🏠 Menu", "u:home")]]))
        return
    lines = [f"🧾 <b>My Orders</b> ({total})", ""]
    for i, o in enumerate(rows, start=page * PAGE + 1):
        lines.append(f"<b>{i}. {o.order_number}</b> · {esc(o.guild_name or 'Guild ' + o.guild_uid)} · {o.squads} sq\n"
                     f"    🕐 {fmt_dt(o.created_at)}\n    {texts.user_status(o.status)}")
    btns = [btn(o.order_number, f"u:ov:{o.id}", style=BLUE) for o in rows]
    buttons = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"u:os:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(btn("Next ➡️", f"u:os:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([btn("🏠 Menu", "u:home", icon="home")])
    await reply(update, "\n".join(lines), M(buttons))


async def show_order(update, user: User, oid: int) -> None:
    async with read() as s:
        o = await s.get(Order, oid)
        region = await s.get(Region, o.region_code) if o else None
    if not o or o.user_id != user.id:
        await reply(update, "🔴 Order not found.", kb.nav_home())
        return
    rows = [[btn("⬅️ My Orders", "u:os:0"), btn("🏠 Menu", "u:home", icon="home")]]
    if o.status in (OrderStatus.SUBMITTED_TO_PROVIDER, OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED):
        rows.insert(0, [btn("📈 Live glory", f"u:gv:{o.region_code}:{o.guild_uid}", style=BLUE, icon="guild")])
    extra = ""
    clock = ""
    if o.status in (OrderStatus.SUBMITTED_TO_PROVIDER, OrderStatus.IN_PROGRESS):
        async with read() as s:
            start = (await s.execute(select(func.max(ProviderRequest.sent_at)).where(
                ProviderRequest.order_id == o.id, ProviderRequest.status == ProviderRequestStatus.CREATED))).scalar_one_or_none()
        clock = ("\n" + texts.clock_line(start)) if start else ""
    if o.ordered_squads and o.ordered_squads != o.squads:
        extra = f"\n🟡 Asked {o.ordered_squads}, started {o.squads} (rest returned to wallet)"
    await reply(update, f"🧾 <b>{o.order_number}</b>{' 🧪 test' if o.is_mock else ''}\n\n"
                        f"🏷 {esc(o.guild_name or '—')}\n🛡 Guild ID: <code>{esc(o.guild_uid)}</code>\n"
                        f"🌍 {region.emoji} {esc(region.display_name)}\n👥 Squads: {o.squads}{extra}\n"
                        f"💳 <b>{fmt_money(o.total_amount, o.currency)}</b> · {'wallet' if o.payment_method == 'WALLET' else 'QR payment'}\n"
                        f"🕐 {fmt_dt(o.created_at)}\n{texts.user_status(o.status)}{clock}", M(rows))


# ------------------------------------------------------------------ wallet
async def show_wallet(update, user: User) -> None:
    cur = _cur(user)
    async with tx() as s:
        w = await ws.get_wallet(s, user.id, cur, lock=False)
        hist = [t for t in await ws.recent(s, user.id, 12, cur) if t.type.value != "RESERVE"][:6]
    lines = [f"{em('wallet', '💰')} <b>Wallet</b>", "", f"🟢 Balance: <b>{fmt_money(w.available, cur)}</b>"]
    lines += ["", "<b>Recent</b>"] + ([f"• {TX_LABEL.get(t.type.value, t.type.value.title())} {fmt_money(t.amount, cur)} · {fmt_dt(t.created_at)}"
                                      for t in hist] or ["• Nothing yet"])
    await reply(update, "\n".join(lines), M([[btn("➕ Add balance", "u:top", style=GREEN, icon="pay")],
                                            [btn("🏠 Menu", "u:home", icon="home")]]))


async def show_topup(update, ctx, user: User) -> None:
    cur = _cur(user)
    amounts = pay.TOPUP_PRESETS[cur]
    btns = [btn(fmt_money(a, cur), f"u:tp:{a}", style=GREEN) for a in amounts]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([btn("✏️ Other amount", "u:tpc", style=GREEN)])
    rows.append([btn("⬅️ Wallet", "u:wal", style=RED), btn("🏠 Menu", "u:home", style=RED, icon="home")])
    how = "Pay by QR with any Nepali bank/wallet app."
    await reply(update, f"➕ <b>Add balance</b>\n{how}\n\nChoose an amount:", M(rows))


async def _topup(update, ctx, user: User, amount: Decimal) -> None:
    try:
        await pay.start_topup(ctx.application, user, amount)
    except osv.OrderError as e:
        await _send_or_edit(update, f"🔴 {esc(e)}", M([[btn("⬅️ Back", "u:top")]]))
    except GatewayError:
        await _send_or_edit(update, f"🔴 Payment is unavailable right now. Try again soon or contact {_contact()}.",
                            M([[btn("⬅️ Back", "u:top")]]))
    else:
        _clear(ctx)
        if update.callback_query:
            try:
                await update.callback_query.edit_message_reply_markup(None)
            except TelegramError:
                pass


# ------------------------------------------------------------------ profile
async def show_profile(update, ctx, user: User, note: str = "") -> None:
    async with read() as s:
        n = (await s.execute(select(func.count(Order.id)).where(Order.user_id == user.id))).scalar_one()
    text = (f"{note}\n\n" if note else "") + (
        f"{em('profile', '👤')} <b>{esc(_name(user))}</b>\n\n🌍 Country: {esc(user.country or 'Not set')}\n"
        f"💱 Currency: <b>NPR</b> (Nepali Rupee)\n🧾 Orders: {n} · 👥 Squads: {user.total_squads}\n"
        f"🆔 <code>{user.telegram_id}</code>")
    rows = [[btn("✏️ Edit name", "u:pn", style=BLUE), btn("🌍 Country", "u:pc", style=BLUE)],
            [btn("📸 Profile photo", "u:pp", style=GREEN)],
            [btn("🏠 Menu", "u:home", icon="home")]]
    await reply(update, text.strip(), M(rows), photo=user.avatar_file_id)


# ------------------------------------------------------------------ support
async def show_support(update) -> None:
    uname = get_settings().support_username.lstrip("@")
    await reply(update, f"🛟 <b>Support</b>\n\nChat with our admin: <b>@{esc(uname)}</b>\nOr send us a message here 👇", M([
        [btn("💬 Chat on Telegram", url=f"https://t.me/{uname}", style=GREEN)],
        [btn("🧾 Order issue", "u:sc:order"), btn("💳 Payment issue", "u:sc:payment")],
        [btn("❓ Other question", "u:sc:general")], [btn("🏠 Menu", "u:home", icon="home")]]))


async def _create_ticket(update, ctx, user: User, category: str, text: str) -> None:
    async with tx() as s:
        count = (await s.execute(select(func.count(SupportTicket.id)))).scalar_one()
        t = SupportTicket(ticket_no=f"TK-{count + 1:05d}-{secrets.token_hex(2).upper()}", user_id=user.id, category=category)
        s.add(t)
        await s.flush()
        s.add(SupportMessage(ticket_id=t.id, sender="user", text=text))
        tid, no = t.id, t.ticket_no
    _clear(ctx)
    await update.message.reply_text(f"🟢 Ticket <code>{no}</code> sent. We'll reply here soon.", parse_mode=HTML,
                                    reply_markup=kb.nav_home())
    await notify_admins(ctx.bot, f"🛟 <b>SUPPORT {no}</b> ({esc(category)})\n👤 {esc(_name(user))} "
                                 f"(<code>{user.telegram_id}</code>)\n\n{esc(text[:1500])}",
                        M([[btn("💬 Reply", f"adm:tr:{tid}", style=GREEN)]]))


# ------------------------------------------------------------------ message router
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    f = _flow(ctx)
    step = f.get("step")
    msg = update.message
    text = (msg.text or "").strip()
    if step == "ob_name" and text:
        if not 2 <= len(text) <= 24 or any(ord(c) < 32 for c in text):
            await msg.reply_text("🔴 Please send a name with 2–24 characters:")
            return
        f["name"] = text
        f["step"] = "ob_country"
        await msg.reply_text("🌍 <b>Which country are you from?</b>", parse_mode=HTML, reply_markup=_country_kb(None))
        return
    if step == "ob_country_txt" and text:
        f["country"] = text[:32]
        await _after_country(update, ctx, update)
        return
    user = await get_user(update.effective_user.id)
    if user is None:
        await msg.reply_text("👋 Send /start to create your profile.")
        return
    if step == "ord_uid" and text:
        st = get_settings()
        if not (text.isdigit() and st.guild_uid_min_len <= len(text) <= st.guild_uid_max_len):
            await msg.reply_text(f"🔴 Guild ID must be {st.guild_uid_min_len}–{st.guild_uid_max_len} digits. Try again:")
            return
        f["uid"] = text
        await _show_warn(update, ctx)
    elif step == "top_amount" and text:
        amt = pay.parse_amount(text)
        if amt is None:
            await msg.reply_text("🔴 Send a number, like 500:")
            return
        await _topup(update, ctx, user, amt)
    elif step == "prof_name" and text:
        if not 2 <= len(text) <= 24:
            await msg.reply_text("🔴 Please send a name with 2–24 characters:")
            return
        async with tx() as s:
            u = await s.get(User, user.id)
            u.display_name = text
        _clear(ctx)
        await show_profile(update, ctx, await get_user(user.id), "✅ Name updated.")
    elif step == "prof_country_txt" and text:
        async with tx() as s:
            u = await s.get(User, user.id)
            u.country = text[:32]
        _clear(ctx)
        await show_profile(update, ctx, await get_user(user.id), "✅ Country updated.")
    elif step == "prof_photo":
        if not msg.photo:
            await msg.reply_text("📸 Please send a photo (not a file).")
            return
        async with tx() as s:
            u = await s.get(User, user.id)
            u.avatar_file_id = msg.photo[-1].file_id
        _clear(ctx)
        await show_profile(update, ctx, await get_user(user.id), "✅ Photo updated.")
    elif step == "sup_text" and text:
        await _create_ticket(update, ctx, user, f.get("category", "general"), text)
    else:
        await msg.reply_text("Tap 🏠 Menu to continue 👇", reply_markup=kb.main_menu())


# ------------------------------------------------------------------ callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    p = q.data.split(":")
    head, act = p[0], (p[1] if len(p) > 1 else "")
    f = _flow(ctx)
    # profile setup works before the user exists
    if head == "u" and act == "co" and f.get("kind") == "onboard":
        await q.answer()
        flag_name = COUNTRIES[int(p[2])]
        if flag_name[1] == "Other…":
            f["step"] = "ob_country_txt"
            await reply(update, "🌍 Type your country name:")
            return
        f["country"] = flag_name[1]
        await _after_country(update, ctx, update)
        return
    user = await get_user(q.from_user.id)
    if user is None:
        await q.answer()
        await reply(update, "👋 Send /start to create your profile first.")
        return
    if head == "pay":
        await _pay_cb(update, ctx, user, p)
        return
    if act == "so":
        await q.answer("⛔ Sold out right now. Check 📦 Squad Stock.", show_alert=True)
        return
    await q.answer()
    try:
        await _route(update, ctx, user, f, p, act)
    except stock_service.StockError as e:
        await reply(update, str(e), kb.nav_home())


async def _route(update, ctx, user: User, f: dict, p: list[str], act: str) -> None:
    if act == "home":
        _clear(ctx)
        await show_home(update, ctx, user)
    elif act == "stock":
        await show_stock(update, ctx, user)
    elif act == "stockr":
        await show_stock(update, ctx, user, force=True)
    elif act == "ord":
        await start_order(update, ctx, user)
    elif act == "gl":
        await show_guilds(update, user)
    elif act == "gi":
        await show_glory_info(update, ctx, user, p[2], p[3])
    elif act == "rules":
        await reply(update, await texts.get_rules(), M([[btn("🏠 Menu", "u:home", style=BLUE, icon="home")]]))
    elif act in ("gv", "gr"):
        await show_guild(update, ctx, user, p[2], p[3], force=act == "gr")
    elif act == "gm":
        _set(ctx, kind="order", region=p[2], uid=p[3])
        await _show_squads(update, ctx, user)
    elif act == "os":
        await show_orders(update, user, int(p[2]))
    elif act == "ov":
        await show_order(update, user, int(p[2]))
    elif act == "wal":
        await show_wallet(update, user)
    elif act == "top":
        await show_topup(update, ctx, user)
    elif act == "tp":
        await _topup(update, ctx, user, Decimal(p[2]))
    elif act == "tpc":
        _set(ctx, kind="topup", step="top_amount")
        lo, hi = pay.TOPUP_MIN[_cur(user)], pay.TOPUP_MAX[_cur(user)]
        await reply(update, f"✏️ Send the amount ({fmt_money(lo, _cur(user))} – {fmt_money(hi, _cur(user))}):",
                    M([[btn("⬅️ Back", "u:top")]]))
    elif act == "prof":
        _clear(ctx)
        await show_profile(update, ctx, user)
    elif act == "pn":
        _set(ctx, kind="profile", step="prof_name")
        await reply(update, "✏️ Send your new name:", M([kb.cancel_row("u:prof")]))
    elif act == "pc":
        _set(ctx, kind="profile", step="prof_country")
        await _ask_country(update, ctx, onboarding=False)
    elif act == "co" and f.get("kind") == "profile":
        flag_name = COUNTRIES[int(p[2])]
        if flag_name[1] == "Other…":
            f["step"] = "prof_country_txt"
            await reply(update, "🌍 Type your country name:")
            return
        f["country"] = flag_name[1]
        await _after_country(update, ctx, update)
    elif act == "pp":
        _set(ctx, kind="profile", step="prof_photo")
        await reply(update, "📸 Send me a photo to use as your profile picture:", M([kb.cancel_row("u:prof")]))
    elif act == "sup":
        await show_support(update)
    elif act == "sc":
        _set(ctx, kind="support", step="sup_text", category=p[2])
        await reply(update, "💬 Tell us the problem in one message:", M([kb.cancel_row()]))
    elif act in ("rg", "ok", "uid", "reg", "sq", "pq", "pw"):
        if f.get("kind") != "order":
            await reply(update, "⌛ Session ended. Tap 🛒 SHOP NOW to start again.", kb.nav_home())
            return
        await _order_cb(update, ctx, user, f, p, act)


async def _pay_cb(update, ctx, user: User, p: list[str]) -> None:
    q = update.callback_query
    pid = int(p[2])
    async with read() as s:
        it = await s.get(PaymentIntent, pid)
    if not it or it.user_id != user.id:
        await q.answer("Not found", show_alert=True)
        return
    if p[1] == "x":
        await q.answer("Cancelled")
        await pay.cancel_ui(ctx.application, pid, user.id)
        return
    res = await pay.check_now(ctx.application, pid)
    msgs = {"pending": "⏳ Not received yet. Pay, then tap again.", "paid": "✅ Payment received!",
            "failed": "❌ This payment expired. Please start again.", "error": "⚠️ Couldn't check now. Try again in a moment."}
    await q.answer(msgs.get(res, "…"), show_alert=res != "paid")


async def _order_cb(update, ctx, user: User, f: dict, p: list[str], act: str) -> None:
    provider = ctx.application.bot_data["provider"]
    if act == "rg":
        code = p[2]
        await stock_service.check_orderable(provider, code, 1)
        f["region"] = code
        if f.get("uid"):
            await _show_warn(update, ctx)
        else:
            await _ask_uid(update, ctx, f"🌍 {await _region_name(code)}\n\n")
    elif act == "sq":
        n = int(p[2])
        if not 1 <= n <= get_settings().max_squads_per_order or not f.get("uid") or not f.get("region"):
            return
        await stock_service.check_orderable(provider, f["region"], n, force=True)
        f["squads"] = n
        await _show_summary(update, ctx, user)
    elif act == "uid":
        await _ask_uid(update, ctx)
    elif act == "reg":
        await start_order(update, ctx, user)
    elif act == "ok" and f.get("uid") and f.get("region"):
        await _show_squads(update, ctx, user)
    elif act == "pq" and f.get("squads"):
        await _pay_qr(update, ctx, user)
    elif act == "pw" and f.get("squads"):
        await _pay_wallet(update, ctx, user)
