"""Изолированная БД на каждый тест.

config и session кэшируют синглтоны в модульных глобалах, поэтому между тестами
их надо сбрасывать — иначе второй тест пишет в БД первого.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from alert_bot import config
from alert_bot.db import session as db_session

ADMIN_ID = 424242


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ADMIN_TG_ID", str(ADMIN_ID))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CRYPTOPANIC_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    config._settings = None
    db_session._engine = None
    db_session._sessionmaker = None

    await db_session.init_db()
    yield
    await db_session.dispose_engine()

    config._settings = None
    db_session._engine = None
    db_session._sessionmaker = None


@pytest.fixture
def admin_id() -> int:
    return ADMIN_ID
