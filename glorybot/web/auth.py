"""Website accounts: passwords (scrypt), login sessions, and one-time codes issued by the Telegram bot."""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, select

from glorybot.database.models import User, UserCredential, WebLoginCode, WebSession, utcnow
from glorybot.database.session import read, tx

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,20}$")
SESSION_DAYS = 30
CODE_MINUTES = 5


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.fullmatch(name or ""))


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(stored: str, pw: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


def _h(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()


# ------------------------------------------------------------------ sessions
async def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    async with tx() as s:
        s.add(WebSession(token_hash=_h(token), user_id=user_id, expires_at=utcnow() + timedelta(days=SESSION_DAYS)))
        await s.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))
    return token


async def user_for_token(token: str | None) -> User | None:
    if not token:
        return None
    async with read() as s:
        row = (await s.execute(select(WebSession).where(WebSession.token_hash == _h(token)))).scalar_one_or_none()
        if row is None:
            return None
        exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)
        if exp < utcnow():
            return None
        return await s.get(User, row.user_id)


async def drop_session(token: str | None) -> None:
    if token:
        async with tx() as s:
            await s.execute(delete(WebSession).where(WebSession.token_hash == _h(token)))


# ------------------------------------------------------------------ password login
async def check_password_login(username: str, password: str) -> User | None:
    async with read() as s:
        u = (await s.execute(select(User).where(User.login_username_lower == username.lower()))).scalar_one_or_none()
        cred = await s.get(UserCredential, u.id) if u else None
    # always do one hash so timing does not reveal whether the username exists
    ok = verify_password(cred.password_hash if cred else hash_password("x"), password)
    return u if (u and cred and ok and u.status == "ACTIVE") else None


async def set_credentials(user_id: int, username: str | None, password: str) -> None:
    async with tx() as s:
        u = await s.get(User, user_id)
        if username and not u.login_username:
            u.login_username, u.login_username_lower = username, username.lower()
        cred = await s.get(UserCredential, user_id)
        if cred:
            cred.password_hash, cred.must_change = hash_password(password), False
        else:
            s.add(UserCredential(user_id=user_id, password_hash=hash_password(password), must_change=False))


# ------------------------------------------------------------------ Telegram one-time code
async def make_login_code(user_id: int) -> str:
    code = f"{secrets.randbelow(10 ** 8):08d}"
    async with tx() as s:
        await s.execute(delete(WebLoginCode).where(WebLoginCode.user_id == user_id))
        s.add(WebLoginCode(code_hash=_h(code), user_id=user_id, expires_at=utcnow() + timedelta(minutes=CODE_MINUTES)))
    return f"{code[:4]}-{code[4:]}"


async def consume_login_code(code: str) -> User | None:
    digits = re.sub(r"\D", "", code or "")
    if len(digits) != 8:
        return None
    async with tx() as s:
        row = (await s.execute(select(WebLoginCode).where(WebLoginCode.code_hash == _h(digits)))).scalar_one_or_none()
        if row is None or row.used:
            return None
        exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)
        if exp < utcnow():
            return None
        row.used = True
        u = await s.get(User, row.user_id)
    return u if u and u.status == "ACTIVE" else None
