import logging
import sys
import time

import httpx
from sqlalchemy import select
from collections import defaultdict, deque

from telegram import BotCommand, Update
from telegram.error import Conflict, NetworkError
from telegram.ext import (ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

from glorybot import keyboards as kb
from glorybot.config import get_settings, setup_logging
from glorybot.database.models import Region
from glorybot.database.session import dispose, init_models, migrate_data, read
from glorybot.handlers import admin, user
from glorybot.handlers.common import is_admin, notify_admins
from glorybot.payments import build_gateways
from glorybot.providers import build_provider
from glorybot.seed import seed
from glorybot.services import order_service as osv
from glorybot.services import payment_service, runner
from glorybot.utils.tasks import spawn

log = logging.getLogger("glorybot")
_hits: dict[int, deque] = defaultdict(deque)


async def throttle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u is None:
        return
    dq, now = _hits[u.id], time.monotonic()
    while dq and now - dq[0] > 10:
        dq.popleft()
    dq.append(now)
    if len(dq) > 20:  # >20 updates in 10s from one user: ignore
        raise ApplicationHandlerStop


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    step = ctx.user_data.get("flow", {}).get("step", "")
    if step.startswith("adm_"):
        if is_admin(update.effective_user.id):
            await admin.on_message(update, ctx)
            return
        ctx.user_data.pop("flow", None)
    await user.on_message(update, ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("Another instance of this bot is polling with the same token. Stop the other one.")
        return
    if isinstance(err, NetworkError):
        log.warning("Network problem: %s", err)
        return
    log.error("Unhandled error", exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await ctx.bot.send_message(update.effective_chat.id, "⚠️ Something went wrong. Please try again.")
    except Exception:  # noqa: BLE001
        pass


async def selfcheck(provider, gateways) -> str:
    """Real startup test: provider connection + region mapping. Result is sent to the admins."""
    lines = []
    try:
        bal = await provider.get_account_balance()
        lines.append(f"✅ Provider connected — credits: basic {bal.get('basic_credits')}, premium {bal.get('premium_credits')}")
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        hint = " (keys rejected — check GLORY_API_KEY / GLORY_API_MASTER_KEY)" if code in (401, 403) else ""
        lines.append(f"🔴 Provider error: HTTP {code}{hint}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"🔴 Provider unreachable: {type(e).__name__}")
    for cur, gw in gateways.items():
        try:
            await gw.ping()
            lines.append(f"✅ {cur} payments connected" if not gw.is_mock else f"🧪 {cur} payments: test mode")
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "status", None)
            hint = " (key rejected)" if code in (401, 403) else ""
            lines.append(f"🔴 {cur} payments error: {type(e).__name__} {str(e)[:80]}{hint}")
    async with read() as s:
        regs = list((await s.execute(select(Region))).scalars())
    bad = [r.code for r in regs if not r.provider_region_id]
    lines.append(f"✅ All {len(regs)} regions mapped" if not bad else f"🟡 Unmapped regions (can't take orders): {', '.join(bad)}")
    return "\n".join(lines)


async def post_init(app) -> None:
    settings = get_settings()
    await init_models()
    await migrate_data()
    provider = build_provider(settings)
    app.bot_data["provider"] = provider
    gateways = build_gateways(settings)
    app.bot_data["gateways"] = gateways
    await seed(settings, provider)
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"), BotCommand("stock", "Squad stock"), BotCommand("orders", "My orders"),
        BotCommand("wallet", "My wallet"), BotCommand("profile", "My profile"), BotCommand("support", "Support"), BotCommand("weblogin", "Website login code"),
        BotCommand("help", "Help"), BotCommand("cancel", "Cancel current action")])
    for oid in await osv.recover():
        spawn(app, runner.run_order(app, oid))
    me = await app.bot.get_me()
    mode = "MOCK (no real orders/payments)" if provider.is_mock else "LIVE"
    log.info("Bot @%s started — provider mode: %s", me.username, mode)
    report = await selfcheck(provider, gateways)
    spawn(app, payment_service.watcher(app))
    if settings.web_enabled:
        from glorybot.web import app as webapp
        await webapp.start(app)
    log.info("Self-check:\n%s", report)
    await notify_admins(app.bot, f"🟢 Bot started — provider mode: <b>{mode}</b>\n\n{report}\n\nSend /admin to open the dashboard.")


async def post_shutdown(app) -> None:
    if get_settings().web_enabled:
        from glorybot.web import app as webapp
        await webapp.stop(app)
    for t in list(app.bot_data.get("tasks", ())):
        t.cancel()
    for obj in [app.bot_data.get("provider"), *app.bot_data.get("gateways", {}).values()]:
        if obj:
            await obj.aclose()
    await dispose()


def run() -> None:
    try:
        settings = get_settings()
    except Exception as e:  # noqa: BLE001
        from pydantic import ValidationError
        print("❌ CONFIGURATION ERROR — fix your .env / panel variables:")
        if isinstance(e, ValidationError):
            for er in e.errors():
                print(f"  • {'.'.join(str(x) for x in er['loc']) or 'settings'}: {er['msg']}")
        else:
            print(f"  • {type(e).__name__}")
        sys.exit(1)
    setup_logging(settings.log_level)
    app = (ApplicationBuilder().token(settings.bot_token.get_secret_value())
           .post_init(post_init).post_shutdown(post_shutdown).build())
    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, throttle), group=-1)
    for name, fn in [("start", user.cmd_start), ("help", user.cmd_help), ("cancel", user.cmd_cancel),
                     ("orders", user.cmd_orders), ("wallet", user.cmd_wallet), ("profile", user.cmd_profile),
                     ("support", user.cmd_support), ("stock", user.cmd_stock), ("admin", admin.cmd_admin),
                     ("weblogin", user.cmd_weblogin)]:
        app.add_handler(CommandHandler(name, fn, filters=private))
    app.add_handler(CallbackQueryHandler(admin.on_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(user.on_callback, pattern=r"^(u|pay):"))
    app.add_handler(MessageHandler(private & filters.Text(kb.MENU_TEXTS), user.on_menu))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
