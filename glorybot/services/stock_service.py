"""Live squad stock = our credits at the glory provider (1 credit = 1 squad), per tier (basic/premium).
Also: admin out-of-stock switches (Region.enabled), a global pause, and the weekly service window."""
import logging
import time
from datetime import datetime

from sqlalchemy import select

from glorybot.config import get_settings
from glorybot.database.models import BotConfiguration, Region
from glorybot.database.session import read, tx

log = logging.getLogger(__name__)
_cache: dict = {"t": 0.0, "credits": None}


class StockError(Exception):
    """User-facing reason why an order cannot be taken right now."""


# ------------------------------------------------------------------ tiny settings store
async def get_conf(key: str, default: str = "") -> str:
    async with read() as s:
        row = await s.get(BotConfiguration, key)
    return row.value if row else default


async def set_conf(key: str, value: str) -> None:
    async with tx() as s:
        row = await s.get(BotConfiguration, key)
        if row:
            row.value = value
        else:
            s.add(BotConfiguration(key=key, value=value))


async def is_paused() -> bool:
    return (await get_conf("paused", "0")) == "1"


async def window_enabled() -> bool:
    default = "1" if get_settings().service_window else "0"
    return (await get_conf("service_window", default)) == "1"


def window_is_open(now: datetime | None = None) -> bool:
    """Closed from Sunday 9 PM until Monday 12 AM, business timezone."""
    now = (now or datetime.now(get_settings().tz)).astimezone(get_settings().tz)
    return not (now.weekday() == 6 and now.hour >= 21)


# ------------------------------------------------------------------ credits
async def credits(provider, force: bool = False) -> dict[str, int]:
    """{'basic': n, 'premium': n}. Cached 15 s so many users don't hammer the provider API."""
    if not force and _cache["credits"] is not None and time.monotonic() - _cache["t"] < 15:
        return _cache["credits"]
    bal = await provider.get_account_balance()
    out = {"basic": int(bal.get("basic_credits") or 0), "premium": int(bal.get("premium_credits") or 0)}
    _cache.update(t=time.monotonic(), credits=out)
    return out


def invalidate() -> None:
    _cache["t"] = 0.0


async def snapshot(provider, force: bool = False) -> dict:
    """Everything the stock screens need. `credits` is None when the provider can't be reached."""
    async with read() as s:
        regions = list((await s.execute(select(Region).order_by(Region.sort_order))).scalars())
    try:
        cr = await credits(provider, force)
    except Exception as e:  # noqa: BLE001
        log.warning("stock lookup failed: %s", type(e).__name__)
        cr = None
    rows = []
    for r in regions:
        if not r.provider_region_id:
            continue
        n = 0 if cr is None else cr.get(r.tier if r.tier in cr else "basic", 0)
        rows.append({"region": r, "stock": n, "enabled": r.enabled, "available": r.enabled and n > 0})
    return {"credits": cr, "rows": rows, "paused": await is_paused(),
            "window_open": (not await window_enabled()) or window_is_open()}


async def check_orderable(provider, region_code: str, squads: int, force: bool = False) -> int:
    """Raise StockError with a friendly message if this order can't be taken; returns the stock left."""
    if await is_paused():
        raise StockError("⏸ Orders are paused for a short time. Please try again soon.")
    if await window_enabled() and not window_is_open():
        raise StockError("🌙 Service is closed after Sunday 9 PM and opens again Monday 12 AM (Nepal time).")
    async with read() as s:
        r = await s.get(Region, region_code)
    if not r or not r.provider_region_id:
        raise StockError("This region is not available right now.")
    if not r.enabled:
        raise StockError(f"⛔ {r.display_name} is out of stock right now.")
    try:
        cr = await credits(provider, force)
    except Exception as e:  # noqa: BLE001
        raise StockError("Stock can't be checked right now. Please try again in a minute.") from e
    have = cr.get(r.tier if r.tier in cr else "basic", 0)
    if have < squads:
        raise StockError(f"⛔ Only {have} squad(s) in stock for {r.display_name}." if have
                         else f"⛔ {r.display_name} is out of stock right now.")
    return have
