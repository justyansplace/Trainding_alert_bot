"""Движок, фабрика сессий и первичная инициализация БД."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from alert_bot.config import get_settings
from alert_bot.db.models import Base, Role, Source, SourceKind, User, utcnow

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


# Стартовый набор источников. Дальше админ управляет ими из бота — это только
# то, с чего бот начинает жизнь на пустой БД.
SEED_SOURCES: list[tuple[str, str, str, float]] = [
    ("CoinDesk", SourceKind.RSS.value, "https://www.coindesk.com/arc/outboundfeeds/rss/", 1.0),
    ("Cointelegraph", SourceKind.RSS.value, "https://cointelegraph.com/rss", 0.9),
    ("The Block", SourceKind.RSS.value, "https://www.theblock.co/rss.xml", 1.0),
    ("Decrypt", SourceKind.RSS.value, "https://decrypt.co/feed", 0.8),
]


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.db_url, echo=False)

        # WAL + busy_timeout: три asyncio-таска пишут в один файл, без этого
        # ловим "database is locked" на первом же совпадении циклов.
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с коммитом на выходе и откатом на исключении."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Создаёт таблицы, заводит админа и сид-источники."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = get_settings()
    async with session_scope() as session:
        admin = await session.get(User, settings.admin_tg_id)
        if admin is None:
            session.add(
                User(
                    tg_id=settings.admin_tg_id,
                    role=Role.ADMIN.value,
                    granted_at=utcnow(),
                    invite_code=None,
                )
            )
            log.info("Создан админ %s", settings.admin_tg_id)
        elif admin.role != Role.ADMIN.value:
            admin.role = Role.ADMIN.value

        existing = set((await session.scalars(select(Source.url))).all())
        for name, kind, url, weight in SEED_SOURCES:
            if url not in existing:
                session.add(
                    Source(
                        name=name,
                        kind=kind,
                        url=url,
                        weight=weight,
                        poll_interval=settings.news_poll_seconds,
                        added_by=settings.admin_tg_id,
                    )
                )
                log.info("Добавлен сид-источник %s", name)

        if settings.cryptopanic_token:
            cp_url = "https://cryptopanic.com/api/developer/v2/posts/"
            if cp_url not in existing:
                session.add(
                    Source(
                        name="CryptoPanic",
                        kind=SourceKind.CRYPTOPANIC.value,
                        url=cp_url,
                        weight=1.0,
                        poll_interval=settings.news_poll_seconds,
                        added_by=settings.admin_tg_id,
                    )
                )


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


def invite_lifetime() -> timedelta:
    return timedelta(days=7)
