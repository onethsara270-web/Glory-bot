import asyncio
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from glorybot.config import get_settings

_engine = None
_maker = None
# One writer at a time: gives exactly-once money logic on SQLite (no row locks there).
# On PostgreSQL, SELECT ... FOR UPDATE in wallet_service adds row-level protection too.
WRITE_LOCK = asyncio.Lock()


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        _engine = create_async_engine(url, **kwargs)
    return _engine


def session_maker() -> async_sessionmaker[AsyncSession]:
    global _maker
    if _maker is None:
        _maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _maker


@asynccontextmanager
async def tx():
    """Serialized write transaction. Commits on success, rolls back on any exception."""
    async with WRITE_LOCK:
        async with session_maker()() as s:
            async with s.begin():
                yield s


@asynccontextmanager
async def read():
    async with session_maker()() as s:
        yield s


async def init_models() -> None:
    from glorybot.database import models  # noqa: F401
    from glorybot.database.base import Base
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def migrate_data() -> None:
    """One-time, idempotent data fixes for databases created by older versions."""
    from sqlalchemy import or_, select, update

    from glorybot.database.models import PaymentIntent, User, Wallet, WalletAccount, WalletTransaction
    async with tx() as s:
        # profiles: old users keep their username as display name, NPR as currency
        await s.execute(update(User).where(User.display_name.is_(None), User.login_username.is_not(None))
                        .values(display_name=User.login_username).execution_options(synchronize_session=False))
        await s.execute(update(User).where(or_(User.currency.is_(None), User.currency != "NPR")).values(currency="NPR")
                        .execution_options(synchronize_session=False))
        await s.execute(update(WalletTransaction).where(WalletTransaction.currency.is_(None))
                        .values(currency="NPR").execution_options(synchronize_session=False))
        await s.execute(update(PaymentIntent).where(PaymentIntent.kind.is_(None)).values(kind="ORDER")
                        .execution_options(synchronize_session=False))
        await s.execute(update(PaymentIntent).where(PaymentIntent.currency.is_(None)).values(currency="NPR")
                        .execution_options(synchronize_session=False))
        # wallets: copy the old single-currency accounts into the per-currency table (NPR) once
        have = {u for (u,) in (await s.execute(select(Wallet.user_id).where(Wallet.currency == "NPR"))).all()}
        for w in (await s.execute(select(WalletAccount))).scalars():
            if w.user_id not in have:
                s.add(Wallet(user_id=w.user_id, currency="NPR", available=w.available, reserved=w.reserved,
                             lifetime_deposits=w.lifetime_deposits, lifetime_spent=w.lifetime_spent))


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()


def _add_missing_columns(sync_conn) -> None:
    """Tiny forward-only migration: add columns introduced after the database was first created."""
    from sqlalchemy import inspect, text

    from glorybot.database.base import Base
    insp = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ctype = col.type.compile(dialect=sync_conn.dialect)
                sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ctype}'))
