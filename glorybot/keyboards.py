"""Buttons. Every button is colored with one simple rule:
  GREEN = money / go (shop, pay, add balance, confirm)   BLUE = look / navigate (stock, orders, back, menu)
  RED   = stop (cancel, delete, out of stock).
Colors use Telegram's button `style` (success=green, primary=blue, danger=red; clients from
Feb 2026 on); older apps simply show normal buttons. Premium emoji icons are optional (see .env)."""
import re

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as M
from telegram import KeyboardButton, ReplyKeyboardMarkup

from glorybot.config import get_settings

GREEN, BLUE, RED = "success", "primary", "danger"
M_HOME = "🏠 Menu"
MENU_TEXTS = [M_HOME]
_LEAD = re.compile(r"^[^\w]+", re.UNICODE)


def _extras(style: str | None, icon: str | None) -> dict:
    st, api = get_settings(), {}
    if style and st.button_colors:
        api["style"] = style
    cid = st.emoji_map.get(icon) if (icon and st.premium_emoji_buttons) else None
    if cid:
        api["icon_custom_emoji_id"] = cid
    return api


def btn(text: str, data: str | None = None, *, url: str | None = None, style: str | None = None,
        icon: str | None = None, callback_data: str | None = None) -> B:
    data = data or callback_data
    api = _extras(style or BLUE, icon)
    if "icon_custom_emoji_id" in api:  # the custom icon replaces the plain emoji in front of the label
        text = _LEAD.sub("", text).strip() or text
    kw = {"api_kwargs": api} if api else {}
    if url:
        return B(text, url=url, **kw)
    return B(text, callback_data=data, **kw)


def main_menu() -> ReplyKeyboardMarkup:
    api = _extras(BLUE, "home")
    return ReplyKeyboardMarkup([[KeyboardButton(M_HOME, api_kwargs=api) if api else KeyboardButton(M_HOME)]],
                               resize_keyboard=True, is_persistent=True)


def home_kb() -> M:
    return M([
        [btn("🛒 SHOP NOW", "u:ord", style=GREEN, icon="buy")],
        [btn("🔍 Check SQD available", "u:stock", style=BLUE, icon="stock"), btn("⚔️ My Guilds", "u:gl", style=BLUE, icon="guild")],
        [btn("🧾 My Orders", "u:os:0", style=BLUE, icon="orders"), btn("💰 Wallet", "u:wal", style=GREEN, icon="wallet")],
        [btn("👤 Profile", "u:prof", style=BLUE, icon="profile"), btn("📜 Rules", "u:rules", style=BLUE, icon="rules")],
        [btn("🛟 Support", "u:sup", style=BLUE, icon="support")],
    ])


def nav_home() -> M:
    return M([[btn("🏠 Menu", "u:home", icon="home")]])


def cancel_row(data: str = "u:home") -> list:
    return [btn("✖️ Cancel", data, style=RED, icon="cancel")]


def admin_home_kb(paused: bool = False) -> M:
    return M([
        [btn("🔴 Live Orders", "adm:lo", style=BLUE), btn("📦 Stock & Credits", "adm:st", style=BLUE)],
        [btn("💰 Prices", "adm:pr", style=GREEN), btn("🎟 Redeem Code", "adm:rd", style=GREEN)],
        [btn("💳 Payments", "adm:pm"), btn("👥 Users", "adm:us")],
        [btn("🏆 Groups", "adm:gl"), btn("📢 Broadcast", "adm:bc")],
        [btn("📊 Reports", "adm:rp"), btn("⚙️ Settings", "adm:set")],
        [btn("▶️ Resume orders" if paused else "⏸ Pause orders", "adm:pz", style=GREEN if paused else RED),
         btn("🔄 Refresh", "adm:home", style=BLUE)],
    ])


def back_admin() -> M:
    return M([[btn("⬅️ Dashboard", "adm:home", style=BLUE)]])


def admin_auto_order_kb(order_id: int, tg_id: int) -> M:
    row = [btn("🔎 Order details", f"adm:vw:{order_id}", style=BLUE)]
    if tg_id > 0:
        row.append(btn("💬 Contact", url=f"tg://user?id={tg_id}"))
    return M([row])
