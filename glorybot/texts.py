"""Shared message texts and small formatting helpers."""
import html

from glorybot.config import get_settings


def em(name: str, fallback: str) -> str:
    """Premium (custom) emoji when the owner configured an id for `name`, otherwise the normal emoji."""
    st = get_settings()
    cid = st.emoji_map.get(name) if st.premium_emoji_buttons else None
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>' if cid else fallback


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def website_url() -> str:
    u = get_settings().website_url.strip()
    return u if u.startswith("http") else "https://" + u


BOT_SENT = ("🤖 <b>BOT REQUEST HAVE BEEN SEND</b>\n"
            "👉 <b>PLEASE ACCEPT BOT REQUEST IN GUILD</b>")

DETAILS = ("<b>Details:</b>\n"
           "• Bot runs up to 8 hrs only\n"
           "• Glory added Monday 4:30 AM\n"
           "• Service: Mon → Sun (till 9 PM)\n"
           "• After Sunday 9 PM — unavailable\n\n"
           "<b>Important:</b>\n"
           "• Don't remove bot before Monday (no refund)\n"
           "• Remove after glory is added")


def bot_sent_text(number: str, uid: str, guild_name: str | None, squads: int, partial_note: str = "") -> str:
    name = f"🏷 <b>{esc(guild_name)}</b>\n" if guild_name else ""
    return (f"{BOT_SENT}\n\n{name}🛡 Guild ID: <code>{esc(uid)}</code>\n🧾 Order: <code>{esc(number)}</code> · "
            f"{squads} squad{'s' if squads != 1 else ''}\n{partial_note}\n{DETAILS}")


USER_STATUS = {
    "PENDING_ADMIN_APPROVAL": "⏳ Starting", "APPROVED": "⏳ Starting", "PROCESSING": "⏳ Sending bot request",
    "SUBMITTED_TO_PROVIDER": "🟢 Bot sent — accept in guild", "IN_PROGRESS": "🔵 Running",
    "COMPLETED": "🏁 Completed", "FAILED": "🔴 Failed — refunded to wallet", "REJECTED": "🔴 Rejected",
    "CANCELLED": "🔴 Cancelled", "REQUIRES_RECONCILIATION": "🟡 Under review", "REFUNDED": "↩️ Refunded",
    "DRAFT": "⏳ Starting", "PENDING_PAYMENT": "⏳ Waiting for payment",
}


def user_status(status) -> str:
    v = getattr(status, "value", str(status))
    return USER_STATUS.get(v, v.title())


RUN_HOURS = 8


def _hm(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"


def clock_line(start, now=None) -> str:
    """'⏳ Remaining 7:40 · ✅ Worked 0:20' for a bot that started at `start` and runs 8 hours."""
    from datetime import datetime, timezone
    if start is None:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    elapsed = ((now or datetime.now(timezone.utc)) - start).total_seconds()
    total = RUN_HOURS * 3600
    if elapsed >= total:
        return f"✅ 8-hour run finished · worked {_hm(total)}"
    return f"⏳ Remaining <b>{_hm(total - elapsed)}</b> · ✅ Worked <b>{_hm(elapsed)}</b>  <i>(h:mm, bot runs {RUN_HOURS} hrs)</i>"


DEFAULT_RULES = ("<b>📜 Rules</b>\n\n"
                 "1️⃣ 1 squad = 1 glory push.\n"
                 "2️⃣ <b>Auto Approval OFF</b> and all ranks <b>default</b> in your guild.\n"
                 "3️⃣ Check your Guild ID and region before paying.\n"
                 "4️⃣ Pay by QR — your bot request is sent automatically.\n"
                 "5️⃣ Accept the bot request in your guild.\n"
                 "6️⃣ Bot runs up to 8 hrs only. Glory is added Monday 4:30 AM.\n"
                 "7️⃣ Don't remove the bot before Monday (no refund). Remove it after glory is added.\n"
                 "8️⃣ Service: Mon → Sun (till 9 PM). After Sunday 9 PM — unavailable.\n\n"
                 "❌ Wrong details or settings are not refundable.")


async def get_rules() -> str:
    from glorybot.services import stock_service
    return (await stock_service.get_conf("rules", "")) or DEFAULT_RULES
