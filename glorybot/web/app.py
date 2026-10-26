"""BNY SHOP website — a JSON API + one single-page front end, served from inside the bot process.
It uses the very same services as the Telegram bot (orders, wallet, stock, payments), so both always agree.
Secrets (provider keys, payment keys) never leave the server; the browser only talks to /api/*."""
import asyncio
import contextlib
import html
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from pathlib import Path

import uvicorn
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from glorybot import texts
from glorybot.config import get_settings
from glorybot.database.models import (Order, OrderStatus, PaymentIntent, ProviderRequest, ProviderRequestStatus, Region,
                                      User, utcnow)
from glorybot.database.session import read, tx
from glorybot.handlers.common import notify_admins
from glorybot.payments.base import GatewayError
from glorybot.services import glory_service
from glorybot.services import order_service as osv
from glorybot.services import payment_service as pay
from glorybot.services import runner, stock_service
from glorybot.services import wallet_service as ws
from glorybot.services.audit import audit
from glorybot.utils.money import fmt_money, order_total, to_money
from glorybot.utils.tasks import spawn
from glorybot.utils.timefmt import fmt_dt
from glorybot.web import auth

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "bny_session"
PAGE = 10
TX_LABEL = {"DEPOSIT": "Added", "RELEASE": "Returned", "SPEND": "Spent", "REFUND": "Refund",
            "ADJUSTMENT": "Adjusted", "RESERVE": "Held", "WHEEL_PRIZE": "Bonus"}
ACTIVE_STATES = (OrderStatus.SUBMITTED_TO_PROVIDER, OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED)

_hits: dict[str, deque] = defaultdict(deque)       # naive per-IP rate limiter
_redeem_hits: list[float] = []
_last_check: dict[int, float] = {}


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.msg, self.status = msg, status


# ------------------------------------------------------------------ helpers
def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def rate_limit(request: Request, key: str, limit: int, window: int) -> None:
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?"))
    dq, now = _hits[f"{key}:{ip}"], time.monotonic()
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        raise ApiError("Too many tries. Please wait a minute and try again.", 429)
    dq.append(now)


_ALLOWED = {"b", "i", "code", "u", "s"}


def safe_html(text: str) -> str:
    """Telegram-style HTML (<b>, <i>, <code>) -> a safe subset for the page. Everything else is escaped."""
    out = html.escape(text or "")
    for t in _ALLOWED:
        out = out.replace(f"&lt;{t}&gt;", f"<{t}>").replace(f"&lt;/{t}&gt;", f"</{t}>")
    return out.replace("\n", "<br>")


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise ApiError("Bad request.") from None
    if not isinstance(data, dict):
        raise ApiError("Bad request.")
    return data


def need(data: dict, key: str, kind: type = str) -> Any:
    v = data.get(key)
    if v is None or (kind is str and not isinstance(v, str)):
        raise ApiError(f"Missing {key}.")
    return v.strip() if isinstance(v, str) else v


def is_admin_user(u: User) -> bool:
    return u.telegram_id in get_settings().admin_ids


def ptb(request: Request):
    return request.app.state.ptb


def provider(request: Request):
    return request.app.state.ptb.bot_data["provider"]


async def current_user(request: Request) -> User:
    u = await auth.user_for_token(request.cookies.get(COOKIE))
    if u is None:
        raise ApiError("Please log in.", 401)
    if u.status != "ACTIVE":
        raise ApiError("Your account is suspended. Please contact support.", 403)
    return u


def api(*, login=True, admin=False, write=False):
    """Wrap a handler: login check, CSRF check for writes, uniform JSON errors."""
    def deco(f):
        async def handler(request: Request):
            try:
                if write and request.headers.get("x-requested-with") != "bny":
                    raise ApiError("Bad request.", 403)
                user = await current_user(request) if (login or admin) else None
                if admin and not is_admin_user(user):
                    raise ApiError("Admins only.", 403)
                data = await f(request, user) if (login or admin) else await f(request)
                return JSONResponse(data if data is not None else {"ok": True}, headers={"Cache-Control": "no-store"})
            except ApiError as e:
                return JSONResponse({"error": e.msg}, status_code=e.status)
            except (osv.OrderError, stock_service.StockError) as e:
                return JSONResponse({"error": plain(str(e))}, status_code=400)
            except ws.InsufficientFunds as e:
                return JSONResponse({"error": "Wallet balance is too low.", "short": str(e.shortfall)}, status_code=400)
            except GatewayError:
                return JSONResponse({"error": "Payment is unavailable right now. Please try again in a minute."}, status_code=502)
            except Exception:  # noqa: BLE001
                log.exception("web api error on %s", request.url.path)
                return JSONResponse({"error": "Something went wrong. Please try again."}, status_code=500)
        return handler
    return deco


def user_json(u: User, bal: Decimal | None = None) -> dict:
    d: dict = {"id": u.id, "name": u.display_name or u.login_username or "Player", "username": u.login_username,
         "country": u.country, "currency": u.currency or "NPR", "admin": is_admin_user(u),
         "telegram": u.telegram_id > 0, "squads": u.total_squads}
    if bal is not None:
        d["balance"] = float(bal)
        d["balance_fmt"] = fmt_money(bal, u.currency or "NPR")
    return d


async def balance(u: User) -> Decimal:
    async with tx() as s:
        w = await ws.get_wallet(s, u.id, u.currency or "NPR", lock=False)
        return w.available


def order_json(o: Order, region: Region | None = None, start: datetime | None = None) -> dict:
    return {"id": o.id, "number": o.order_number, "region": o.region_code, "region_name": region.display_name if region else o.region_code,
            "region_emoji": region.emoji if region else "", "uid": o.guild_uid, "guild": o.guild_name, "squads": o.squads,
            "asked": o.ordered_squads, "total": float(o.total_amount), "total_fmt": fmt_money(o.total_amount, o.currency),
            "method": "wallet" if o.payment_method == "WALLET" else "qr", "status": o.status.value,
            "status_text": plain(texts.user_status(o.status)), "created": iso(o.created_at), "created_fmt": fmt_dt(o.created_at),
            "start": iso(start), "mock": bool(o.is_mock)}


def intent_json(it: PaymentIntent, order: Order | None = None) -> dict:
    now = utcnow()
    exp = pay.aware(it.expires_at)
    left = max(0, int((exp - now).total_seconds()))
    status = it.status
    if status == "PENDING":
        state = "waiting" if (it.ui_open and left > 0) else ("cancelled" if not it.ui_open and left > 0 else "expired")
    elif status in ("PAID",):
        state = "paid"
    elif status == "CREDITED":
        state = "credited"          # paid, but the order couldn't start -> money is in the wallet
    else:
        state = "failed"
    return {"id": it.id, "kind": it.kind, "state": state, "raw": status, "amount": float(it.amount),
            "amount_fmt": fmt_money(it.amount, it.currency), "left": left, "expires": iso(exp),
            "qr_data": it.qr_data if (it.qr_data or "").startswith("data:image") else None,
            "qr_url": it.qr_url, "qr_string": it.qr_string, "checkout_url": it.checkout_url,
            "region": it.region_code or None, "uid": it.guild_uid or None, "squads": it.squads, "test": bool(it.is_mock),
            "order": order_json(order) if order else None}


# ------------------------------------------------------------------ pages
async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: Request):
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ accounts
def _set_cookie(request: Request, resp: JSONResponse, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")


async def _login_response(request: Request, u: User) -> JSONResponse:
    token = await auth.create_session(u.id)
    resp = JSONResponse({"ok": True, "user": user_json(u, await balance(u))}, headers={"Cache-Control": "no-store"})
    _set_cookie(request, resp, token)
    return resp


async def register(request: Request):
    try:
        if request.headers.get("x-requested-with") != "bny":
            raise ApiError("Bad request.", 403)
        rate_limit(request, "reg", 5, 600)
        d = await body(request)
        username, password = need(d, "username"), need(d, "password")
        name = (d.get("name") or username).strip()[:40]
        country = (d.get("country") or "Nepal").strip()[:32]
        if not auth.valid_username(username):
            raise ApiError("Username: 4–20 letters, numbers or _ .")
        if not 8 <= len(password) <= 100:
            raise ApiError("Password must be at least 8 characters.")
        for _ in range(5):
            try:
                async with tx() as s:
                    taken = (await s.execute(select(User.id).where(User.login_username_lower == username.lower()))).first()
                    if taken:
                        raise ApiError("That username is taken.")
                    u = User(telegram_id=-(secrets.randbelow(10 ** 12) + 1), display_name=name, country=country, currency="NPR",
                             login_username=username, login_username_lower=username.lower(),
                             referral_code=secrets.token_hex(4).upper(), total_spent=0)
                    s.add(u)
                    await s.flush()
                    await ws.get_wallet(s, u.id, "NPR", lock=False)
                break
            except IntegrityError:
                continue
        else:
            raise ApiError("Could not create the account. Try again.")
        await auth.set_credentials(u.id, None, password)
        return await _login_response(request, u)
    except ApiError as e:
        return JSONResponse({"error": e.msg}, status_code=e.status)
    except Exception:  # noqa: BLE001
        log.exception("register failed")
        return JSONResponse({"error": "Something went wrong. Please try again."}, status_code=500)


async def login(request: Request):
    try:
        if request.headers.get("x-requested-with") != "bny":
            raise ApiError("Bad request.", 403)
        rate_limit(request, "login", 10, 300)
        d = await body(request)
        if "code" in d:
            u = await auth.consume_login_code(str(d.get("code", "")))
            if u is None:
                raise ApiError("That code is wrong or expired. Send /weblogin to the bot for a new one.", 401)
        else:
            u = await auth.check_password_login(need(d, "username"), need(d, "password"))
            if u is None:
                raise ApiError("Wrong username or password.", 401)
        return await _login_response(request, u)
    except ApiError as e:
        return JSONResponse({"error": e.msg}, status_code=e.status)
    except Exception:  # noqa: BLE001
        log.exception("login failed")
        return JSONResponse({"error": "Something went wrong. Please try again."}, status_code=500)


async def logout(request: Request):
    if request.headers.get("x-requested-with") != "bny":
        return JSONResponse({"error": "Bad request."}, status_code=403)
    await auth.drop_session(request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@api(write=False)
async def me(request, u):
    st = get_settings()
    async with read() as s:
        price = await osv.current_price(s, None, u.currency or "NPR")
    return {"user": user_json(u, await balance(u)), "price": float(price), "price_fmt": fmt_money(price, u.currency or "NPR"),
            "support": st.support_username.lstrip("@"), "guild_site": texts.website_url(), "max_squads": st.max_squads_per_order,
            "run_hours": texts.RUN_HOURS, "topups": pay.TOPUP_PRESETS[u.currency or "NPR"],
            "topup_min": float(pay.TOPUP_MIN[u.currency or "NPR"])}


@api(write=True)
async def profile(request, u):
    d = await body(request)
    name = need(d, "name")[:40]
    country = need(d, "country")[:32]
    if len(name) < 2:
        raise ApiError("Please enter a name (2+ letters).")
    async with tx() as s:
        row = await s.get(User, u.id)
        row.display_name, row.country = name, country
        u = row
    return {"user": user_json(u, await balance(u))}


@api(write=True)
async def set_password(request, u):
    d = await body(request)
    password = need(d, "password")
    username = (d.get("username") or "").strip() or None
    if not 8 <= len(password) <= 100:
        raise ApiError("Password must be at least 8 characters.")
    if u.login_username is None:
        if not username or not auth.valid_username(username):
            raise ApiError("Choose a username: 4–20 letters, numbers or _ .")
        async with read() as s:
            if (await s.execute(select(User.id).where(User.login_username_lower == username.lower()))).first():
                raise ApiError("That username is taken.")
    await auth.set_credentials(u.id, username, password)
    return {"ok": True}


# ------------------------------------------------------------------ shop
@api(login=False)
async def stock(request):
    snap = await stock_service.snapshot(provider(request))
    price = None
    async with read() as s:
        price = await osv.current_price(s, None, "NPR")
    c = snap["credits"]
    return {"normal": None if c is None else c["basic"], "premium": None if c is None else c["premium"], "unavailable": c is None,
            "paused": snap["paused"], "closed": not snap["window_open"], "price": float(price), "price_fmt": fmt_money(price, "NPR"),
            "regions": [{"code": r["region"].code, "name": r["region"].display_name, "emoji": r["region"].emoji,
                         "premium": r["region"].tier == "premium", "available": r["available"]} for r in snap["rows"]],
            "time": iso(utcnow())}


@api(login=False)
async def rules(request):
    return {"html": safe_html(await texts.get_rules())}


@api(write=True)
async def quote(request, u):
    d = await body(request)
    region = need(d, "region")
    have = await stock_service.check_orderable(provider(request), region, 1, force=True)
    cur = u.currency or "NPR"
    async with read() as s:
        price = await osv.current_price(s, region, cur)
    top = min(get_settings().max_squads_per_order, have)
    return {"price": float(price), "max": top, "options": [{"n": n, "total": float(order_total(price, n)),
                                                              "total_fmt": fmt_money(order_total(price, n), cur)} for n in range(1, top + 1)],
            "balance": float(await balance(u))}


def _check_order_args(d: dict) -> tuple[str, str, int]:
    region, uid = need(d, "region"), need(d, "uid")
    squads = need(d, "squads", int)
    if not isinstance(squads, int) or isinstance(squads, bool):
        raise ApiError("Choose how many squads.")
    if not re.fullmatch(r"\d{4,20}", uid):
        raise ApiError("Guild ID must be numbers only.")
    if not 1 <= squads <= get_settings().max_squads_per_order:
        raise ApiError("Invalid number of squads.")
    return region, uid, squads


@api(write=True)
async def order_qr(request, u):
    region, uid, squads = _check_order_args(await body(request))
    it = await pay.start_payment(ptb(request), u, region, uid, squads, deliver=False)
    return {"payment": intent_json(it)}


@api(write=True)
async def order_wallet(request, u):
    region, uid, squads = _check_order_args(await body(request))
    app, prov = ptb(request), provider(request)
    await stock_service.check_orderable(prov, region, squads, force=True)
    async with tx() as s:
        user = await s.get(User, u.id)
        order = await osv.create_order(s, user, region, uid, squads, prov.is_mock)
        if await osv.approve_order(s, order.id, 0, prov.is_mock) != "ok":
            raise osv.OrderError("Could not start your order. Please try again.")
        number, oid, total, cur = order.order_number, order.id, order.total_amount, order.currency
    await notify_admins(app.bot, f"🆕 <b>NEW ORDER PLACED</b> (wallet · website){' 🧪 MOCK' if prov.is_mock else ''}\n\n🧾 <code>{number}</code>\n"
                                 f"👤 {texts.esc(u.display_name)} (web)\n🌍 {texts.esc(region)} · UID <code>{texts.esc(uid)}</code>\n"
                                 f"👥 {squads} squad(s) · 💳 {fmt_money(total, cur)}\n🕐 {fmt_dt(utcnow())}\n"
                                 "✅ Placed automatically — nothing for you to confirm.",
                        _admin_kb(oid, u.telegram_id))
    spawn(app, runner.run_order(app, oid))
    return {"order": {"id": oid, "number": number}, "balance": float(await balance(u))}


def _admin_kb(oid: int, tg: int):
    from glorybot import keyboards as kb
    return kb.admin_auto_order_kb(oid, tg)


@api(write=True)
async def topup(request, u):
    d = await body(request)
    amount = pay.parse_amount(str(d.get("amount", "")))
    if amount is None:
        raise ApiError("Enter a valid amount.")
    it = await pay.start_topup(ptb(request), u, amount, deliver=False)
    return {"payment": intent_json(it)}


async def _own_intent(u: User, pid: int) -> PaymentIntent:
    async with read() as s:
        it = await s.get(PaymentIntent, pid)
    if it is None or it.user_id != u.id:
        raise ApiError("Payment not found.", 404)
    return it


@api()
async def payment_status(request, u):
    pid = int(request.path_params["pid"])
    it = await _own_intent(u, pid)
    if request.query_params.get("check") and it.status == "PENDING":
        now = time.monotonic()
        if now - _last_check.get(pid, 0) >= 4:          # the background watcher also polls; this just speeds it up
            _last_check[pid] = now
            await pay.check_now(ptb(request), pid)
            it = await _own_intent(u, pid)
    order = None
    if it.order_id:
        async with read() as s:
            order = await s.get(Order, it.order_id)
    out: dict = {"payment": intent_json(it, order)}
    if it.kind == "TOPUP" and it.status == "PAID":
        out["balance"] = float(await balance(u))
    return out


@api(write=True)
async def payment_cancel(request, u):
    pid = int(request.path_params["pid"])
    await _own_intent(u, pid)
    await pay.cancel_ui(ptb(request), pid, u.id)
    return {"ok": True}


# ------------------------------------------------------------------ wallet / orders / guilds
@api()
async def wallet(request, u):
    cur = u.currency or "NPR"
    async with tx() as s:
        w = await ws.get_wallet(s, u.id, cur, lock=False)
        hist = [t for t in await ws.recent(s, u.id, 30, cur) if t.type.value != "RESERVE"][:15]
    return {"balance": float(w.available), "balance_fmt": fmt_money(w.available, cur),
            "history": [{"type": t.type.value, "label": TX_LABEL.get(t.type.value, t.type.value.title()),
                         "amount": float(t.amount), "amount_fmt": fmt_money(t.amount, cur), "time": fmt_dt(t.created_at),
                         "sign": -1 if (t.type.value == "SPEND" or (t.type.value == "ADJUSTMENT" and (t.reason or "").startswith("[-]"))) else 1}
                        for t in hist]}


@api()
async def orders(request, u):
    page = max(0, int(request.query_params.get("page", 0) or 0))
    async with read() as s:
        total = (await s.execute(select(func.count(Order.id)).where(Order.user_id == u.id))).scalar_one()
        rows = list((await s.execute(select(Order).where(Order.user_id == u.id).order_by(Order.id.desc())
                                     .offset(page * PAGE).limit(PAGE))).scalars())
        regions = {r.code: r for r in (await s.execute(select(Region))).scalars()}
    return {"total": total, "page": page, "pages": max(1, -(-total // PAGE)),
            "orders": [order_json(o, regions.get(o.region_code)) for o in rows]}


@api()
async def order_detail(request, u):
    oid = int(request.path_params["oid"])
    async with read() as s:
        o = await s.get(Order, oid)
        if not o or o.user_id != u.id:
            raise ApiError("Order not found.", 404)
        region = await s.get(Region, o.region_code)
        start = None
        if o.status in (OrderStatus.SUBMITTED_TO_PROVIDER, OrderStatus.IN_PROGRESS):
            start = (await s.execute(select(func.max(ProviderRequest.sent_at)).where(
                ProviderRequest.order_id == o.id, ProviderRequest.status == ProviderRequestStatus.CREATED))).scalar_one_or_none()
    d = order_json(o, region, start)
    d["live"] = o.status in ACTIVE_STATES
    d["sent_notice"] = o.status in (OrderStatus.SUBMITTED_TO_PROVIDER, OrderStatus.IN_PROGRESS)
    return {"order": d, "details": plain(texts.DETAILS)}


@api()
async def guilds(request, u):
    rows = await glory_service.user_guilds(u.id)
    async with read() as s:
        regions = {r.code: r for r in (await s.execute(select(Region))).scalars()}
    return {"guilds": [{"region": r, "region_name": regions[r].display_name if r in regions else r,
                        "emoji": regions[r].emoji if r in regions else "", "uid": uid, "name": n} for r, uid, n in rows]}


@api()
async def guild(request, u):
    region, uid = request.query_params.get("region", ""), request.query_params.get("uid", "")
    if not region or not re.fullmatch(r"\d{4,20}", uid):
        raise ApiError("Bad guild.")
    try:
        info = await glory_service.guild_info(provider(request), u.id, region, uid, force=bool(request.query_params.get("force")))
    except Exception:  # noqa: BLE001
        raise ApiError("Live data is unavailable right now. Try again in a minute.", 502) from None
    async with read() as s:
        reg = await s.get(Region, region)
    return {"guild": {"uid": uid, "region": region, "region_name": reg.display_name if reg else region,
                      "emoji": reg.emoji if reg else "", "name": info.get("name"), "glory": info["glory"], "squads": info["squads"],
                      "running": info["running"], "bots": info["bots"], "bots_running": info["bots_running"], "orders": info["orders"],
                      "start": iso(info.get("start")), "run_hours": texts.RUN_HOURS,
                      "groups": [{"glory": g["glory"], "status": g["status"], "start": iso(g.get("start"))} for g in info["groups"]]},
            "now": iso(utcnow()), "details": plain(texts.DETAILS)}


# ------------------------------------------------------------------ admin
@api(admin=True)
async def admin_overview(request, u):
    prov = provider(request)
    snap = await stock_service.snapshot(prov, force=True)
    async with read() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar_one()
        price = await osv.current_price(s, None, "NPR")
        sales = (await s.execute(select(func.coalesce(func.sum(Order.total_amount), 0)).where(
            Order.status.in_(list(ACTIVE_STATES)), Order.is_mock.is_(False)))).scalar_one()
    c = snap["credits"]
    return {"users": users, "orders": await osv.count_orders(), "sales_fmt": fmt_money(sales, "NPR"), "paused": snap["paused"],
            "credits": c, "price": float(price), "price_fmt": fmt_money(price, "NPR"), "mock": bool(prov.is_mock),
            "window": await stock_service.window_enabled(), "window_open": snap["window_open"],
            "regions": [{"code": r["region"].code, "name": r["region"].display_name, "emoji": r["region"].emoji,
                         "enabled": r["enabled"], "stock": r["stock"], "premium": r["region"].tier == "premium"} for r in snap["rows"]]}


@api(admin=True)
async def admin_live(request, u):
    rows = await osv.live_orders(30)
    async with read() as s:
        users = {x.id: x for x in (await s.execute(select(User).where(User.id.in_([o.user_id for o in rows] or [0])))).scalars()}
    out = []
    for o in rows:
        d = order_json(o)
        d["user"] = users[o.user_id].display_name if o.user_id in users else "?"
        out.append(d)
    return {"orders": out}


@api(admin=True, write=True)
async def admin_region(request, u):
    code = need(await body(request), "code")
    async with tx() as s:
        r = await s.get(Region, code)
        if r is None:
            raise ApiError("Unknown region.", 404)
        r.enabled = not r.enabled
        await audit(s, u.telegram_id, "REGION_TOGGLE", code, "on" if r.enabled else "off")
        state = r.enabled
    stock_service.invalidate()
    return {"enabled": state}


@api(admin=True, write=True)
async def admin_pause(request, u):
    now = not await stock_service.is_paused()
    await stock_service.set_conf("paused", "1" if now else "0")
    return {"paused": now}


@api(admin=True, write=True)
async def admin_price(request, u):
    d = await body(request)
    price = pay.parse_amount(str(d.get("price", "")))
    if price is None or not Decimal("1") <= price <= Decimal("100000"):
        raise ApiError("Price must be between NPR 1 and NPR 100,000.")
    async with tx() as s:
        await osv.set_price(s, price, u.telegram_id, "NPR")
    return {"price": float(price), "price_fmt": fmt_money(price, "NPR")}


@api(admin=True, write=True)
async def admin_redeem(request, u):
    code = need(await body(request), "code")[:80]
    now = time.monotonic()
    _redeem_hits[:] = [t for t in _redeem_hits if now - t < 60]
    if len(_redeem_hits) >= 5:
        raise ApiError("The provider allows 5 tries per minute. Wait a moment.", 429)
    _redeem_hits.append(now)
    prov = provider(request)
    try:
        res = await prov.redeem_coupon(code)
    except NotImplementedError:
        res = {"_ok": False, "error": "redeem is not available in this mode"}
    except Exception as e:  # noqa: BLE001
        res = {"_ok": False, "error": f"request failed ({type(e).__name__})"}
    async with tx() as s:
        await audit(s, u.telegram_id, "REDEEM_COUPON" if res.get("_ok") else "REDEEM_COUPON_FAILED", None, code[:3] + "***")
    stock_service.invalidate()
    msg = str(res.get("message") or res.get("error") or res.get("detail") or "")[:200]
    credits = None
    with contextlib.suppress(Exception):
        credits = await stock_service.credits(prov, force=True)
    return {"ok": bool(res.get("_ok")), "message": msg, "credits": credits}


@api(admin=True)
async def admin_users(request, u):
    q = request.query_params.get("q", "").strip()[:40]
    async with read() as s:
        stmt = select(User).order_by(User.id.desc()).limit(30)
        if q:
            cond = [User.display_name.ilike(f"%{q}%"), User.login_username_lower == q.lower(), User.telegram_username.ilike(f"%{q}%")]
            if q.lstrip("-").isdigit():
                cond.append(User.telegram_id == int(q))
            stmt = select(User).where(or_(*cond)).order_by(User.id.desc()).limit(30)
        rows = list((await s.execute(stmt)).scalars())
    return {"users": [{"id": x.id, "name": x.display_name or x.login_username, "username": x.login_username,
                       "tg": x.telegram_id if x.telegram_id > 0 else None, "status": x.status, "squads": x.total_squads,
                       "source": "telegram" if x.telegram_id > 0 else "website"} for x in rows]}


@api(admin=True, write=True)
async def admin_suspend(request, u):
    uid = need(await body(request), "id", int)
    async with tx() as s:
        t = await s.get(User, uid)
        if t is None or t.telegram_id in get_settings().admin_ids:
            raise ApiError("Cannot change this user.")
        t.status = "SUSPENDED" if t.status == "ACTIVE" else "ACTIVE"
        await audit(s, u.telegram_id, "SUSPEND_USER" if t.status == "SUSPENDED" else "REACTIVATE_USER", f"user:{uid}")
        st = t.status
    return {"status": st}


@api(admin=True, write=True)
async def admin_adjust(request, u):
    d = await body(request)
    uid = need(d, "id", int)
    try:
        delta = to_money(Decimal(str(d.get("amount"))))
    except Exception:  # noqa: BLE001
        raise ApiError("Enter a valid amount.") from None
    if delta == 0 or abs(delta) > Decimal("100000"):
        raise ApiError("Enter an amount between -100,000 and 100,000 (not 0).")
    async with tx() as s:
        t = await s.get(User, uid)
        if t is None:
            raise ApiError("User not found.", 404)
        await ws.adjust(s, uid, delta, "Admin adjustment (website)", f"admin:{u.telegram_id}", currency=t.currency or "NPR")
        await audit(s, u.telegram_id, "WALLET_ADJUST", f"user:{uid}", str(delta))
    return {"ok": True}


@api(admin=True, write=True)
async def admin_rules(request, u):
    d = await body(request)
    if d.get("reset"):
        await stock_service.set_conf("rules", "")
    else:
        text = need(d, "text")
        if not 20 <= len(text) <= 3000:
            raise ApiError("Rules must be 20–3000 characters.")
        await stock_service.set_conf("rules", "<b>📜 Rules</b>\n\n" + html.escape(text))
    return {"html": safe_html(await texts.get_rules()), "text": plain(await texts.get_rules())}


@api(admin=True)
async def admin_rules_get(request, u):
    return {"text": re.sub(r"^📜 Rules\s*", "", plain(await texts.get_rules())).strip()}


@api(admin=True, write=True)
async def admin_broadcast(request, u):
    text = need(await body(request), "text")
    if not 2 <= len(text) <= 3500:
        raise ApiError("Message must be 2–3500 characters.")
    async with read() as s:
        ids = [r for (r,) in (await s.execute(select(User.telegram_id).where(User.telegram_id > 0, User.status == "ACTIVE")))]
    app = ptb(request)

    async def run():
        sent = failed = 0
        for tg in ids:
            try:
                await app.bot.send_message(tg, html.escape(text), parse_mode="HTML")
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
            await asyncio.sleep(0.05)
        await notify_admins(app.bot, f"📢 Website broadcast finished: sent {sent}, failed {failed}.")

    spawn(app, run())
    return {"queued": len(ids)}


# ------------------------------------------------------------------ app
class Headers:
    """Security headers on every response."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                for k, v in {
                    "x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "same-origin",
                    "content-security-policy": ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
                                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
                }.items():
                    h.append((k.encode(), v.encode()))
            await send(message)
        await self.app(scope, receive, wrapped)


def build_app(ptb_app) -> Starlette:
    r = [
        Route("/", index), Route("/healthz", health),
        Route("/api/register", register, methods=["POST"]), Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/me", me), Route("/api/profile", profile, methods=["POST"]), Route("/api/password", set_password, methods=["POST"]),
        Route("/api/stock", stock), Route("/api/rules", rules), Route("/api/quote", quote, methods=["POST"]),
        Route("/api/order/qr", order_qr, methods=["POST"]), Route("/api/order/wallet", order_wallet, methods=["POST"]),
        Route("/api/topup", topup, methods=["POST"]),
        Route("/api/payment/{pid:int}", payment_status), Route("/api/payment/{pid:int}/cancel", payment_cancel, methods=["POST"]),
        Route("/api/wallet", wallet), Route("/api/orders", orders), Route("/api/orders/{oid:int}", order_detail),
        Route("/api/guilds", guilds), Route("/api/guild", guild),
        Route("/api/admin/overview", admin_overview), Route("/api/admin/live", admin_live),
        Route("/api/admin/region", admin_region, methods=["POST"]), Route("/api/admin/pause", admin_pause, methods=["POST"]),
        Route("/api/admin/price", admin_price, methods=["POST"]), Route("/api/admin/redeem", admin_redeem, methods=["POST"]),
        Route("/api/admin/users", admin_users), Route("/api/admin/suspend", admin_suspend, methods=["POST"]),
        Route("/api/admin/adjust", admin_adjust, methods=["POST"]),
        Route("/api/admin/rules", admin_rules, methods=["POST"]), Route("/api/admin/rules/get", admin_rules_get),
        Route("/api/admin/broadcast", admin_broadcast, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ]
    app = Starlette(routes=r)
    app.add_middleware(Headers)
    app.state.ptb = ptb_app
    return app


class _Server(uvicorn.Server):
    """Runs inside the bot's event loop; the bot keeps control of Ctrl-C / stop signals."""
    def install_signal_handlers(self) -> None:  # older uvicorn
        return None

    @contextlib.contextmanager
    def capture_signals(self):  # newer uvicorn
        yield


async def start(ptb_app) -> _Server:
    st = get_settings()
    cfg = uvicorn.Config(build_app(ptb_app), host=st.web_host, port=st.web_port, log_level="warning", proxy_headers=True,
                         forwarded_allow_ips="*", access_log=False)
    server = _Server(cfg)
    ptb_app.bot_data["web_server"] = server
    spawn(ptb_app, server.serve())
    log.info("BNY SHOP website listening on %s:%s", st.web_host, st.web_port)
    return server


async def stop(ptb_app) -> None:
    server = ptb_app.bot_data.get("web_server")
    if server is not None:
        server.should_exit = True
