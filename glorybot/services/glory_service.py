import asyncio
import logging
import time

from sqlalchemy import select, update

from glorybot.database.models import Order, OrderStatus, ProviderRequest, ProviderRequestStatus as PR
from glorybot.database.session import read, tx

log = logging.getLogger(__name__)
_cache: dict = {"t": 0.0, "hist": {}, "live": {}}
_lock = asyncio.Lock()
ACTIVE = [OrderStatus.SUBMITTED_TO_PROVIDER, OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED]


async def snapshot(provider, ttl: float = 8.0, force: bool = False):
    """Provider groups + glory history, cached a few seconds so many users don't hammer the API."""
    async with _lock:
        if force or time.monotonic() - _cache["t"] > ttl:
            hist = {str(h.get("group_id")): h for h in await provider.group_history()}
            live = {g.group_id: g for g in await provider.list_groups()}
            _cache.update(t=time.monotonic(), hist=hist, live=live)
        return _cache["hist"], _cache["live"]


def _glory(h: dict) -> int:
    try:
        return int(h.get("glory_farmed") or 0)
    except (TypeError, ValueError):
        return 0


async def order_glory(provider, order_ids: list[int]) -> dict[int, dict]:
    if not order_ids:
        return {}
    async with read() as s:
        rows = (await s.execute(select(ProviderRequest.order_id, ProviderRequest.provider_group_id).where(
            ProviderRequest.order_id.in_(order_ids), ProviderRequest.status == PR.CREATED))).all()
    hist, live = await snapshot(provider)
    out = {oid: {"glory": 0, "groups": []} for oid in order_ids}
    for oid, gid in rows:
        if not gid:
            continue
        h, g = hist.get(gid, {}), live.get(gid)
        gl = _glory(h)
        out[oid]["glory"] += gl
        out[oid]["groups"].append({"id": gid, "glory": gl, "status": (g.status if g else h.get("status", "unknown"))})
    return out


async def user_guilds(user_id: int) -> list[tuple[str, str, str | None]]:
    """Distinct (region, guild uid, guild name) the user has ordered for, newest first."""
    async with read() as s:
        rows = (await s.execute(select(Order.region_code, Order.guild_uid, Order.guild_name).where(
            Order.user_id == user_id, Order.status.in_(ACTIVE)).order_by(Order.id.desc()))).all()
    seen, out = set(), {}
    for r, u, n in rows:
        if (r, u) not in seen:
            seen.add((r, u))
            out[(r, u)] = n
        elif n and not out[(r, u)]:
            out[(r, u)] = n
    return [(r, u, n) for (r, u), n in out.items()]


from glorybot.providers.base import extract_name  # noqa: E402


async def fetch_guild_name(provider, group_id: str) -> str | None:
    try:
        return extract_name(await provider.clan_info(group_id))
    except Exception as e:  # noqa: BLE001
        log.info("guild name lookup failed: %s", type(e).__name__)
        return None


async def remember_guild_name(provider, uid: str, group_id: str) -> str | None:
    """Fetch the guild's name once (as the website shows it) and store it on all orders for that guild."""
    name = await fetch_guild_name(provider, group_id)
    if name:
        async with tx() as s:
            await s.execute(update(Order).where(Order.guild_uid == uid, Order.guild_name.is_(None))
                            .values(guild_name=name[:80]).execution_options(synchronize_session=False))
    return name


def _naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def live_name(live: dict, gid: str) -> str | None:
    g = live.get(gid)
    return g.name if g and g.name else None


async def guild_info(provider, user_id: int, region: str, uid: str, force: bool = False) -> dict:
    async with read() as s:
        rows = (await s.execute(
            select(ProviderRequest.provider_group_id, Order.id, ProviderRequest.sent_at).join(Order, Order.id == ProviderRequest.order_id)
            .where(Order.user_id == user_id, Order.guild_uid == uid, Order.region_code == region,
                   Order.status.in_(ACTIVE), ProviderRequest.status == PR.CREATED))).all()
    hist, live = await snapshot(provider, force=force)
    async with read() as s:
        name = (await s.execute(select(Order.guild_name).where(
            Order.user_id == user_id, Order.guild_uid == uid, Order.guild_name.is_not(None))
            .order_by(Order.id.desc()).limit(1))).scalar_one_or_none()
    first = next((gid for gid, _, _ in rows if gid), None)
    if not name and first:
        name = live_name(live, first) or extract_name(hist.get(first, {}))
        if name:
            async with tx() as s:
                await s.execute(update(Order).where(Order.guild_uid == uid, Order.guild_name.is_(None))
                                .values(guild_name=name[:80]).execution_options(synchronize_session=False))
    if not name and first:
        name = await remember_guild_name(provider, uid, first)
    info = {"glory": 0, "squads": 0, "running": 0, "bots": 0, "bots_running": 0, "uptime": None,
            "orders": len({oid for _, oid, _ in rows}), "name": name, "uid": uid, "region": region,
            "groups": [], "start": None}
    for gid, _, sent in rows:
        if not gid:
            continue
        info["squads"] += 1
        gl = _glory(hist.get(gid, {}))
        info["glory"] += gl
        g = live.get(gid)
        info["groups"].append({"id": gid, "glory": gl, "status": g.status if g else hist.get(gid, {}).get("status", "ended"),
                               "start": sent})
        if sent and (info["start"] is None or _naive(sent) > _naive(info["start"])):
            info["start"] = sent
        if g:
            info["running"] += 1 if g.status == "running" else 0
            info["bots"] += g.container_count
            info["bots_running"] += g.running_count
            info["uptime"] = info["uptime"] or g.uptime
    return info
