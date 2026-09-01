"""Автоуборка сирот: инструмент, на который сутки никто не смотрит.

Реестр открыт всем, и без уборки он зарастает тем, что завели попробовать и
забыли: опрос площадки идёт по-прежнему, а слот из общего потолка занят.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot import config
from alert_bot.db.models import Instrument, Role, User, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market import registry, user_levels
from alert_bot.market.providers.base import SymbolMeta
from alert_bot.scheduler import PriceLoop

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def enqueue(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


async def make_user(tg_id: int, role: str = Role.USER.value) -> None:
    async with session_scope() as session:
        session.add(User(tg_id=tg_id, role=role, granted_at=utcnow(), active=True))


async def make_instrument(symbol: str = "BTC/USDT", added_by: int = 1) -> Instrument:
    return await registry.add_instrument(
        meta=SymbolMeta(symbol=symbol, last_price=100.0, price_precision=2),
        exchange="bybit",
        keywords=["x"],
        added_by=added_by,
    )


async def enabled_symbols() -> set[str]:
    return {i.symbol for i in await registry.list_instruments(enabled_only=True)}


# --------------------------------------------------------------------------- #
# Отсчёт
# --------------------------------------------------------------------------- #


async def test_first_pass_only_starts_the_countdown(db) -> None:
    """Сутки считаются с первого наблюдения, а не с добавления."""
    await make_user(1)
    instrument = await make_instrument()

    assert await registry.sweep_orphans(DAY, NOW) == []

    async with session_scope() as session:
        stored = await session.get(Instrument, instrument.id)
    assert stored.enabled is True
    assert stored.orphan_since == NOW


async def test_instrument_retires_after_the_ttl(db) -> None:
    await make_user(1)
    await make_instrument()

    await registry.sweep_orphans(DAY, NOW)
    retired = await registry.sweep_orphans(DAY, NOW + DAY)

    assert [i.symbol for i in retired] == ["BTC/USDT"]
    assert await enabled_symbols() == set()


async def test_it_survives_right_up_to_the_deadline(db) -> None:
    await make_user(1)
    await make_instrument()

    await registry.sweep_orphans(DAY, NOW)
    assert await registry.sweep_orphans(DAY, NOW + DAY - timedelta(minutes=1)) == []
    assert await enabled_symbols() == {"BTC/USDT"}


# --------------------------------------------------------------------------- #
# Кто считается смотрящим
# --------------------------------------------------------------------------- #


async def test_a_subscriber_keeps_it_alive(db) -> None:
    await make_user(1)
    instrument = await make_instrument()
    await registry.subscribe(1, instrument.id)

    assert await registry.sweep_orphans(DAY, NOW) == []
    assert await registry.sweep_orphans(DAY, NOW + DAY * 10) == []


async def test_a_level_counts_as_much_as_a_subscription(db) -> None:
    """Человек мог отписаться от алертов, но отметки оставить."""
    await make_user(1)
    instrument = await make_instrument()
    async with session_scope() as session:
        stored = await session.get(Instrument, instrument.id)
    await user_levels.add_level(1, stored, 105.0)
    await registry.subscribe(1, instrument.id, enabled=False)

    await registry.sweep_orphans(DAY, NOW)
    assert await registry.sweep_orphans(DAY, NOW + DAY * 10) == []


async def test_a_disabled_subscription_does_not_count(db) -> None:
    await make_user(1)
    instrument = await make_instrument()
    await registry.subscribe(1, instrument.id, enabled=False)

    await registry.sweep_orphans(DAY, NOW)
    assert [i.symbol for i in await registry.sweep_orphans(DAY, NOW + DAY)] == ["BTC/USDT"]


async def test_a_revoked_user_does_not_keep_it_alive(db) -> None:
    """Отозванный доступ — это не подписчик, сколько бы подписок ни осталось."""
    await make_user(1)
    instrument = await make_instrument()
    await registry.subscribe(1, instrument.id)
    async with session_scope() as session:
        user = await session.get(User, 1)
        user.active = False

    await registry.sweep_orphans(DAY, NOW)
    assert [i.symbol for i in await registry.sweep_orphans(DAY, NOW + DAY)] == ["BTC/USDT"]


# --------------------------------------------------------------------------- #
# Отсчёт сбрасывается
# --------------------------------------------------------------------------- #


async def test_subscribing_resets_the_countdown_immediately(db) -> None:
    """Не на ближайшем проходе: уборка через час не должна застать старую отметку."""
    await make_user(1)
    instrument = await make_instrument()
    await registry.sweep_orphans(DAY, NOW)

    await registry.subscribe(1, instrument.id)

    async with session_scope() as session:
        stored = await session.get(Instrument, instrument.id)
    assert stored.orphan_since is None


async def test_countdown_restarts_after_the_watcher_leaves(db) -> None:
    await make_user(1)
    instrument = await make_instrument()
    await registry.subscribe(1, instrument.id)
    await registry.sweep_orphans(DAY, NOW)

    await registry.subscribe(1, instrument.id, enabled=False)
    await registry.sweep_orphans(DAY, NOW + DAY)  # отсчёт начинается только сейчас

    assert await enabled_symbols() == {"BTC/USDT"}
    assert [i.symbol for i in await registry.sweep_orphans(DAY, NOW + DAY * 2)] == ["BTC/USDT"]


async def test_readding_brings_it_back(db) -> None:
    """Мягкое отключение: свечи, уровни и история остаются на месте."""
    await make_user(1)
    await make_instrument()
    await registry.sweep_orphans(DAY, NOW)
    await registry.sweep_orphans(DAY, NOW + DAY)

    await make_instrument()

    assert await enabled_symbols() == {"BTC/USDT"}
    async with session_scope() as session:
        stored = await session.scalar(
            __import__("sqlalchemy").select(Instrument).where(Instrument.symbol == "BTC/USDT")
        )
    assert stored.orphan_since is None


async def test_zero_ttl_disables_the_sweep(db) -> None:
    await make_user(1)
    await make_instrument()

    assert await registry.sweep_orphans(timedelta(0), NOW) == []
    assert await registry.sweep_orphans(timedelta(0), NOW + DAY * 10) == []
    assert await enabled_symbols() == {"BTC/USDT"}


# --------------------------------------------------------------------------- #
# В цикле
# --------------------------------------------------------------------------- #


@pytest.fixture
def ttl_hours(monkeypatch):
    def _set(hours: int) -> None:
        monkeypatch.setenv("ORPHAN_TTL_HOURS", str(hours))
        config._settings = None

    yield _set
    config._settings = None


async def test_loop_tells_the_person_who_added_it(db, ttl_hours) -> None:
    ttl_hours(24)
    await make_user(7)
    await make_instrument(added_by=7)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)

    await loop.sweep_orphans(NOW)
    assert notifier.sent == []

    await loop.sweep_orphans(NOW + DAY)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].chat_id == 7
    assert "BTC/USDT" in notifier.sent[0].text
    assert "/add_instrument BTC/USDT bybit" in notifier.sent[0].text


async def test_loop_sweeps_at_most_once_an_hour(db, ttl_hours) -> None:
    """Тик идёт раз в десять секунд — запрос по всем подпискам столько раз лишний."""
    ttl_hours(24)
    await make_user(1)
    await make_instrument()

    loop = PriceLoop(notifier=StubNotifier())
    await loop.sweep_orphans(NOW)

    async with session_scope() as session:
        stored = await session.scalar(
            __import__("sqlalchemy").select(Instrument)
        )
        stored.orphan_since = None

    await loop.sweep_orphans(NOW + timedelta(minutes=30))  # слишком рано

    async with session_scope() as session:
        stored = await session.scalar(__import__("sqlalchemy").select(Instrument))
    assert stored.orphan_since is None


async def test_loop_respects_the_off_switch(db, ttl_hours) -> None:
    ttl_hours(0)
    await make_user(1)
    await make_instrument()

    loop = PriceLoop(notifier=StubNotifier())
    await loop.sweep_orphans(NOW)
    await loop.sweep_orphans(NOW + DAY * 10)

    assert await enabled_symbols() == {"BTC/USDT"}
