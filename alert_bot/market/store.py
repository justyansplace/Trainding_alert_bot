"""Персистентность свечей и уровней.

Ключевая тонкость — merge_levels: пересчёт уровней раз в час не должен сбрасывать
их состояние. Если просто удалить старые строки и вставить новые, все cooldown'ы
обнулятся, гистерезис забудет, что уровень уже отработан, и бот начнёт слать один
и тот же алерт каждый час. Анти-спам держится именно на переносе состояния.
"""

from __future__ import annotations

import logging
import math

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from alert_bot.db.models import Candle, Instrument, Level as LevelRow, LevelState, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market.levels import Level

log = logging.getLogger(__name__)

# Насколько близко новый уровень должен оказаться к старому, чтобы считаться тем
# же самым и унаследовать состояние. Уже допуска кластеризации (0.15%), иначе
# соседние уровни начнут перехватывать чужие cooldown'ы.
STATE_CARRY_TOLERANCE_PCT = 0.0008


async def upsert_candles(instrument_id: int, tf: str, df: pd.DataFrame) -> int:
    """Пишет свечи идемпотентно: последняя свеча ещё формируется и будет меняться."""
    if df.empty:
        return 0

    rows = [
        {
            "instrument_id": instrument_id,
            "tf": tf,
            "ts": ts.to_pydatetime(),
            "o": float(o),
            "h": float(h),
            "l": float(low),
            "c": float(c),
            "v": float(v),
        }
        for ts, o, h, low, c, v in zip(
            pd.to_datetime(df["ts"], utc=True),
            df["o"], df["h"], df["l"], df["c"], df["v"],
            strict=True,
        )
    ]

    async with session_scope() as session:
        stmt = sqlite_insert(Candle).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Candle.instrument_id, Candle.tf, Candle.ts],
            set_={
                "o": stmt.excluded.o,
                "h": stmt.excluded.h,
                "l": stmt.excluded.l,
                "c": stmt.excluded.c,
                "v": stmt.excluded.v,
            },
        )
        await session.execute(stmt)

    return len(rows)


async def load_candles(instrument_id: int, tf: str, limit: int = 700) -> pd.DataFrame:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Candle.ts, Candle.o, Candle.h, Candle.l, Candle.c, Candle.v)
                .where(Candle.instrument_id == instrument_id, Candle.tf == tf)
                .order_by(Candle.ts.desc())
                .limit(limit)
            )
        ).all()

    if not rows:
        return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "v"])

    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.iloc[::-1].reset_index(drop=True)


def _carry_state(new: Level, old_rows: list[LevelRow]) -> LevelRow | None:
    """Ищет строку, соответствующую тому же уровню в прошлом расчёте."""
    tolerance = new.price * STATE_CARRY_TOLERANCE_PCT
    best: LevelRow | None = None
    best_distance = tolerance

    for row in old_rows:
        distance = abs(row.price - new.price)
        if distance <= best_distance:
            best, best_distance = row, distance

    return best


async def replace_levels(instrument_id: int, levels: list[Level]) -> int:
    """Перезаписывает уровни инструмента, перенося состояние совпавших."""
    async with session_scope() as session:
        old_rows = list(
            (
                await session.scalars(
                    select(LevelRow).where(LevelRow.instrument_id == instrument_id)
                )
            ).all()
        )

        await session.execute(delete(LevelRow).where(LevelRow.instrument_id == instrument_id))
        await session.flush()

        unmatched = list(old_rows)
        carried = 0

        for level in levels:
            previous = _carry_state(level, unmatched)
            if previous is not None:
                unmatched.remove(previous)
                carried += 1

            session.add(
                LevelRow(
                    instrument_id=instrument_id,
                    price=level.price,
                    kinds=level.kinds,
                    score=level.score,
                    touches=level.touches,
                    computed_at=utcnow(),
                    state=previous.state if previous else LevelState.ARMED.value,
                    cooldown_until=previous.cooldown_until if previous else None,
                )
            )

    log.debug(
        "instrument=%s уровней=%s, состояние перенесено для %s",
        instrument_id,
        len(levels),
        carried,
    )
    return len(levels)


async def load_levels(instrument_id: int) -> list[LevelRow]:
    async with session_scope() as session:
        return list(
            (
                await session.scalars(
                    select(LevelRow)
                    .where(LevelRow.instrument_id == instrument_id)
                    .order_by(LevelRow.price)
                )
            ).all()
        )


async def touch_instrument(
    instrument_id: int,
    price: float | None,
    error: str | None = None,
    atr_value: float | None = None,
) -> None:
    """Отметка живости инструмента — то, что показывает /status и /instruments."""
    async with session_scope() as session:
        instrument = await session.get(Instrument, instrument_id)
        if instrument is None:
            return
        instrument.last_tick_at = utcnow()
        if price is not None:
            instrument.last_price = price
        if atr_value is not None and math.isfinite(atr_value):
            instrument.atr = atr_value
        instrument.last_error = error
