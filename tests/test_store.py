"""Тесты персистентности — прежде всего переноса состояния уровней.

Пересчёт уровней идёт раз в час. Если он сбрасывает cooldown и гистерезис, бот
шлёт один и тот же алерт каждый час, и никакие пороги этого не спасут.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from alert_bot.db.models import Instrument, LevelState, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market import store
from alert_bot.market.levels import Level
from tests.test_indicators import make_df


async def make_instrument(symbol: str = "TEST/USDT", price: float = 100.0) -> int:
    async with session_scope() as session:
        instrument = Instrument(
            symbol=symbol,
            provider="ccxt",
            exchange="binance",
            round_step=1.0,
            price_precision=2,
            keywords=[],
            added_by=1,
            last_price=price,
        )
        session.add(instrument)
        await session.flush()
        return instrument.id


def lv(price: float, score: float = 5.0, kinds: list[str] | None = None) -> Level:
    return Level(price=price, kinds=kinds or ["PDH"], touches=3, score=score)


# --------------------------------------------------------------------------- #
# Перенос состояния
# --------------------------------------------------------------------------- #


async def test_recompute_preserves_triggered_state(db) -> None:
    instrument_id = await make_instrument()
    await store.replace_levels(instrument_id, [lv(100.0), lv(110.0)])

    cooldown = utcnow() + timedelta(hours=4)
    async with session_scope() as session:
        rows = await store.load_levels(instrument_id)
        row = next(r for r in rows if r.price == 100.0)
        stored = await session.get(type(row), row.id)
        stored.state = LevelState.TRIGGERED.value
        stored.cooldown_until = cooldown

    # Пересчёт слегка сдвинул цену уровня — так и бывает при обновлении VWAP.
    await store.replace_levels(instrument_id, [lv(100.02), lv(110.0)])

    rows = await store.load_levels(instrument_id)
    carried = next(r for r in rows if abs(r.price - 100.0) < 1)
    assert carried.state == LevelState.TRIGGERED.value
    assert carried.cooldown_until == pytest.approx(cooldown, abs=timedelta(seconds=1))


async def test_distant_level_does_not_inherit_state(db) -> None:
    """Соседний уровень не должен перехватывать чужой cooldown."""
    instrument_id = await make_instrument()
    await store.replace_levels(instrument_id, [lv(100.0)])

    async with session_scope() as session:
        row = (await store.load_levels(instrument_id))[0]
        stored = await session.get(type(row), row.id)
        stored.state = LevelState.TRIGGERED.value

    # 5% в стороне — это уже другой уровень, а не сдвинувшийся прежний.
    await store.replace_levels(instrument_id, [lv(105.0)])

    rows = await store.load_levels(instrument_id)
    assert rows[0].state == LevelState.ARMED.value


async def test_each_old_level_is_claimed_at_most_once(db) -> None:
    """Два новых уровня рядом со старым не могут оба унаследовать его состояние."""
    instrument_id = await make_instrument()
    await store.replace_levels(instrument_id, [lv(100.0)])

    async with session_scope() as session:
        row = (await store.load_levels(instrument_id))[0]
        stored = await session.get(type(row), row.id)
        stored.state = LevelState.TRIGGERED.value

    await store.replace_levels(instrument_id, [lv(100.01), lv(100.03)])

    rows = await store.load_levels(instrument_id)
    triggered = [r for r in rows if r.state == LevelState.TRIGGERED.value]
    assert len(triggered) == 1


async def test_new_levels_start_armed(db) -> None:
    instrument_id = await make_instrument()
    await store.replace_levels(instrument_id, [lv(100.0), lv(200.0)])

    rows = await store.load_levels(instrument_id)
    assert all(r.state == LevelState.ARMED.value for r in rows)
    assert all(r.cooldown_until is None for r in rows)


async def test_replace_levels_is_scoped_to_instrument(db) -> None:
    first = await make_instrument("AAA/USDT")
    second = await make_instrument("BBB/USDT")

    await store.replace_levels(first, [lv(100.0)])
    await store.replace_levels(second, [lv(200.0), lv(210.0)])
    await store.replace_levels(first, [lv(101.0)])

    assert len(await store.load_levels(second)) == 2


async def test_replace_with_empty_clears_levels(db) -> None:
    instrument_id = await make_instrument()
    await store.replace_levels(instrument_id, [lv(100.0)])
    await store.replace_levels(instrument_id, [])
    assert await store.load_levels(instrument_id) == []


# --------------------------------------------------------------------------- #
# Свечи
# --------------------------------------------------------------------------- #


async def test_candle_upsert_is_idempotent(db) -> None:
    instrument_id = await make_instrument()
    df = make_df([(10, 12, 9, 11, 100.0), (11, 13, 10, 12, 120.0)])

    await store.upsert_candles(instrument_id, "1h", df)
    await store.upsert_candles(instrument_id, "1h", df)

    stored = await store.load_candles(instrument_id, "1h")
    assert len(stored) == 2


async def test_forming_candle_is_updated_not_duplicated(db) -> None:
    """Последняя свеча ещё формируется — на следующем тике она придёт другой."""
    instrument_id = await make_instrument()

    await store.upsert_candles(instrument_id, "1h", make_df([(10, 11, 9, 10, 50.0)]))
    await store.upsert_candles(instrument_id, "1h", make_df([(10, 15, 9, 14, 90.0)]))

    stored = await store.load_candles(instrument_id, "1h")
    assert len(stored) == 1
    assert float(stored.iloc[0]["h"]) == 15.0
    assert float(stored.iloc[0]["c"]) == 14.0
    assert float(stored.iloc[0]["v"]) == 90.0


async def test_candles_load_in_chronological_order(db) -> None:
    instrument_id = await make_instrument()
    df = make_df([(i, i + 1, i - 1, i, 1.0) for i in range(10, 20)])
    await store.upsert_candles(instrument_id, "1h", df)

    stored = await store.load_candles(instrument_id, "1h", limit=5)
    assert len(stored) == 5
    assert stored["ts"].is_monotonic_increasing
    # limit берёт последние по времени, а не первые.
    assert float(stored.iloc[-1]["c"]) == 19.0


async def test_candles_separated_by_timeframe(db) -> None:
    instrument_id = await make_instrument()
    await store.upsert_candles(instrument_id, "1h", make_df([(10, 11, 9, 10, 1.0)]))
    await store.upsert_candles(instrument_id, "1d", make_df([(20, 21, 19, 20, 2.0)]))

    assert len(await store.load_candles(instrument_id, "1h")) == 1
    assert float((await store.load_candles(instrument_id, "1d")).iloc[0]["c"]) == 20.0


async def test_load_candles_empty_returns_typed_frame(db) -> None:
    instrument_id = await make_instrument()
    stored = await store.load_candles(instrument_id, "1h")
    assert isinstance(stored, pd.DataFrame)
    assert list(stored.columns) == ["ts", "o", "h", "l", "c", "v"]


async def test_touch_instrument_records_price_and_atr(db) -> None:
    instrument_id = await make_instrument()
    await store.touch_instrument(instrument_id, price=123.45, atr_value=6.78)

    async with session_scope() as session:
        instrument = await session.get(Instrument, instrument_id)
        assert instrument.last_price == pytest.approx(123.45)
        assert instrument.atr == pytest.approx(6.78)
        assert instrument.last_tick_at is not None


async def test_touch_instrument_ignores_nan_atr(db) -> None:
    """ATR приходит NaN, пока истории меньше периода — записывать его нельзя."""
    instrument_id = await make_instrument()
    await store.touch_instrument(instrument_id, price=100.0, atr_value=5.0)
    await store.touch_instrument(instrument_id, price=101.0, atr_value=float("nan"))

    async with session_scope() as session:
        instrument = await session.get(Instrument, instrument_id)
        assert instrument.atr == pytest.approx(5.0)
