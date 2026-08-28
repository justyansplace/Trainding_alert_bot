"""Тесты цикла цены: подавление всплеска и политика пересчёта ATR."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot import config
from alert_bot.db.models import Instrument, LevelState, Subscription, User, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market import user_levels
from alert_bot.scheduler import PriceLoop

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _archive_off(monkeypatch):
    """Автоочистка выключена: здесь проверяется подавление всплеска.

    Уровни тут стоят в 3–14% от цены — именно чтобы часть из них не проходила
    порог. С включённой очисткой они уезжали бы в архив, и тесты проверяли бы
    её, а не то, ради чего написаны.
    """
    monkeypatch.setenv("AUTO_ARCHIVE_PCT", "0")
    config._settings = None
    yield
    config._settings = None


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def enqueue(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


async def setup(level_prices: list[float], atr_k: float = 3.0) -> Instrument:
    async with session_scope() as session:
        instrument = Instrument(
            symbol="BTC/USDT",
            provider="ccxt",
            exchange="binance",
            round_step=500.0,
            price_precision=2,
            keywords=[],
            added_by=1,
            last_price=1000.0,
            atr=100.0,
        )
        session.add(instrument)
        await session.flush()
        session.add(User(tg_id=1, role="user", granted_at=utcnow(), active=True))
        await session.flush()
        session.add(
            Subscription(
                tg_id=1, instrument_id=instrument.id, enabled=True, atr_k=atr_k, min_score=1.0
            )
        )
        instrument_id = instrument.id

    async with session_scope() as session:
        stored = await session.get(Instrument, instrument_id)

    for price in level_prices:
        await user_levels.add_level(1, stored, price)

    return stored


def loop_at(instrument_id: int, notifier, prices: list[float]) -> PriceLoop:  # noqa: ANN001
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument_id)
    state.atr = 100.0
    for price in prices:
        state.push_price(price)
    return loop


async def test_only_nearest_level_fires_per_tick(db) -> None:
    """Одно движение вниз проходит порог сразу по нескольким уровням под ценой.

    Без ограничения человек получает пачку сообщений об одном и том же движении,
    а дневной потолок выедается за один тик.
    """
    instrument = await setup([970.0, 940.0, 910.0, 880.0])

    notifier = StubNotifier()
    loop = loop_at(instrument.id, notifier, [1010.0, 1000.0])
    await loop.run_user_levels(instrument, loop.runtime(instrument.id), NOW)

    assert len(notifier.sent) == 1, "за тик уходит только ближайший уровень"
    assert "ваш уровень" in notifier.sent[0].text
    assert "970.00" in notifier.sent[0].text


async def test_deferred_levels_stay_armed(db) -> None:
    """Отложенные уровни не должны считаться отработанными."""
    instrument = await setup([970.0, 940.0])

    loop = loop_at(instrument.id, StubNotifier(), [1010.0, 1000.0])
    await loop.run_user_levels(instrument, loop.runtime(instrument.id), NOW)

    rows = {round(r.price): r for r in await user_levels.list_levels(1)}
    assert rows[970].state == LevelState.TRIGGERED.value
    assert rows[940].state == LevelState.ARMED.value
    assert rows[940].notified_users == []


async def test_deferred_level_fires_once_nearest_is_done(db) -> None:
    """Дальний уровень дожидается очереди, а не теряется навсегда."""
    instrument = await setup([970.0, 940.0])

    notifier = StubNotifier()
    loop = loop_at(instrument.id, notifier, [1010.0, 1000.0])
    state = loop.runtime(instrument.id)
    await loop.run_user_levels(instrument, state, NOW)
    assert len(notifier.sent) == 1

    state.push_price(950.0)
    await loop.run_user_levels(instrument, state, NOW + timedelta(minutes=1))

    assert len(notifier.sent) == 2
    assert "940.00" in notifier.sent[1].text


async def test_without_notifier_nothing_is_touched(db) -> None:
    """Цикл должен уметь работать в режиме сбора данных без рассылки."""
    instrument = await setup([970.0])

    loop = loop_at(instrument.id, None, [1010.0, 1000.0])
    await loop.run_user_levels(instrument, loop.runtime(instrument.id), NOW)

    rows = await user_levels.list_levels(1)
    assert rows[0].state == LevelState.ARMED.value
    assert rows[0].trigger_count == 0


async def test_atr_recompute_policy(db) -> None:
    loop = PriceLoop()
    assert loop._needs_recompute(1, NOW) is True, "холодный старт обязан посчитать ATR"

    loop.runtime(1).last_recompute = NOW
    assert loop._needs_recompute(1, NOW + timedelta(minutes=30)) is False
    assert loop._needs_recompute(1, NOW + timedelta(hours=1)) is True


async def test_atr_recompute_forced_on_new_utc_day(db) -> None:
    loop = PriceLoop()
    loop.runtime(1).last_recompute = datetime(2026, 8, 22, 23, 50, tzinfo=UTC)
    assert loop._needs_recompute(1, datetime(2026, 8, 23, 0, 5, tzinfo=UTC)) is True
