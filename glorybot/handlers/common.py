import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from glorybot.config import get_settings
from glorybot.database.models import OrderStatus

log = logging.getLogger(__name__)
HTML = ParseMode.HTML

STATUS_EMOJI = {
    "PENDING_ADMIN_APPROVAL": "🟡", "APPROVED": "🔵", "PROCESSING": "🔵", "SUBMITTED_TO_PROVIDER": "🟢",
    "IN_PROGRESS": "🔵", "COMPLETED": "🟢", "REJECTED": "🔴", "FAILED": "🔴", "CANCELLED": "🔴",
    "REQUIRES_RECONCILIATION": "🟡", "REFUNDED": "🔵", "DRAFT": "⚪", "PENDING_PAYMENT": "🟡",
}

def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def is_admin(uid: int) -> bool:
    return uid in get_settings().admin_ids


def status_label(status) -> str:
    v = status.value if isinstance(status, OrderStatus) else str(status)
    return f"{STATUS_EMOJI.get(v, '⚪')} {v.replace('_', ' ').title()}"


async def reply(update: Update, text: str, markup=None, photo: str | None = None) -> None:
    """Show a screen: edit the tapped message when possible, otherwise send a new one.
    With `photo` (a Telegram file id) the screen is a picture with a caption."""
    q = update.callback_query
    msg = q.message if q is not None else update.effective_message
    if photo:
        try:
            await msg.reply_photo(photo, caption=text[:1024], parse_mode=HTML, reply_markup=markup)
            if q is not None:
                try:
                    await q.message.delete()
                except TelegramError:
                    pass
            return
        except TelegramError as e:
            log.debug("photo screen failed: %s", e)
    if q is not None:
        try:
            if q.message.photo:
                await q.edit_message_caption(caption=text[:1024], parse_mode=HTML, reply_markup=markup)
            else:
                await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except TelegramError as e:
            log.debug("edit failed: %s", e)
        await q.message.reply_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    else:
        await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=markup,
                                                  disable_web_page_preview=True)


async def safe_send(bot, chat_id: int, text: str, markup=None) -> bool:
    if chat_id <= 0:  # website-only account: there is no Telegram chat to message
        return False
    try:
        await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
        return True
    except TelegramError as e:
        log.warning("send to %s failed: %s", chat_id, type(e).__name__)
        return False


async def notify_admins(bot, text: str, markup=None) -> None:
    for aid in get_settings().admin_ids:
        await safe_send(bot, aid, text, markup)
