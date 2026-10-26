import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["PROVIDER_MODE"] = "mock"   # a real .env in the project folder must not leak into tests
os.environ["PAYMENT_MODE"] = "mock"
os.environ["SERVICE_WINDOW"] = "false"   # tests must not depend on the day of the week


@pytest.fixture
async def db(monkeypatch):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{f.name}")
    from glorybot import config
    from glorybot.database import session
    config.get_settings.cache_clear()
    session._engine = None
    session._maker = None
    session.WRITE_LOCK = asyncio.Lock()
    from glorybot.providers.mock_provider import MockProvider
    from glorybot.seed import seed
    from glorybot.services import order_service, stock_service
    monkeypatch.setattr(order_service, "VERIFY_WAITS", (0, 0))
    monkeypatch.setattr(order_service, "RETRY_WAITS", (0, 0, 0))
    stock_service.invalidate()
    await session.init_models()
    await session.migrate_data()
    prov = MockProvider()
    await seed(config.get_settings(), prov)
    yield prov
    await session.dispose()
    os.unlink(f.name)
