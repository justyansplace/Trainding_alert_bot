"""Ручные уровни: те, которые пользователь задал сам.

Отличий от посчитанных два, и оба принципиальные:

  * они не участвуют в скоринге. Человек поставил уровень осознанно, поэтому
    min_score к нему не применяется — иначе бот молча игнорировал бы то, о чём
    его прямо попросили. Порог расстояния (atr_k) применяется: это настройка
    того, за сколько предупреждать, а не того, что считать важным;
  * они принадлежат автору: алерт по уровню уходит только тому, кто его
    поставил, и чужие уровни в списке не видны.

Уровень можно править и удалять. Журнал срабатываний живёт, пока живёт сам
уровень: он нужен, чтобы посмотреть, как рынок отрабатывал отметку, — а у
удалённой отметки смотреть уже нечего.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from alert_bot.db.models import (
    Instrument,
    LevelState,
    UserLevel,
    UserLevelEvent,
    utcnow,
)
from alert_bot.db.session import session_scope
from alert_bot.market.detector import LevelSnapshot
from alert_bot.threshold import pct_between

log = logging.getLogger(__name__)

MANUAL_KIND = "manual"

# Скор, с которым ручной уровень идёт в детектор. Заведомо выше любого порога,
# который человек может себе выставить: фильтр значимости к своему же уровню
# применять бессмысленно.
MANUAL_SCORE = 1_000.0

MAX_LEVELS_PER_USER_PER_INSTRUMENT = 20

# Почему уровень исчез из списка. Причина хранится, чтобы отличать уборку
# ботом от удаления руками: во втором случае человек знает, что сделал.
ARCHIVE_PRICE_LEFT = "price_left"

# Насколько близко к существующему уровню считается тем же самым.
DUPLICATE_TOLERANCE_PCT = 0.0005


class UserLevelError(Exception):
    pass


@dataclass(slots=True)
class LevelStats:
    active: int
    triggers: int


@dataclass(frozen=True, slots=True)
class ArchivedLevel:
    """Что нужно сказать человеку об убранном уровне."""

    id: int
    tg_id: int
    price: float
    note: str | None
    trigger_count: int
    distance_pct: float


async def add_level(
    tg_id: int, instrument: Instrument, price: float, note: str | None = None
) -> UserLevel:
    if price <= 0:
        raise UserLevelError("Цена уровня должна быть положительной.")

    async with session_scope() as session:
        active = list(
            (
                await session.scalars(
                    select(UserLevel).where(
                        UserLevel.tg_id == tg_id,
                        UserLevel.instrument_id == instrument.id,
                        UserLevel.active.is_(True),
                    )
                )
            ).all()
        )

        if len(active) >= MAX_LEVELS_PER_USER_PER_INSTRUMENT:
            raise UserLevelError(
                f"Достигнут предел в {MAX_LEVELS_PER_USER_PER_INSTRUMENT} уровней "
                f"на инструмент. Снимите ненужные: /dellevel"
            )

        tolerance = price * DUPLICATE_TOLERANCE_PCT
        for existing in active:
            if abs(existing.price - price) <= tolerance:
                raise UserLevelError(
                    f"Уровень {existing.price:g} уже стоит здесь же (id={existing.id})."
                )

        level = UserLevel(
            tg_id=tg_id,
            instrument_id=instrument.id,
            price=price,
            note=(note or "").strip()[:200] or None,
            created_at=utcnow(),
        )
        session.add(level)
        await session.flush()
        log.info("Ручной уровень %s для %s: %s", level.id, instrument.symbol, price)
        return level


async def get_level(tg_id: int, level_id: int) -> UserLevel | None:
    """Уровень по id, но только свой: чужие уровни недоступны никак."""
    async with session_scope() as session:
        level = await session.get(UserLevel, level_id)
        return level if level is not None and level.tg_id == tg_id else None


async def delete_level(tg_id: int, level_id: int) -> UserLevel | None:
    """Удаляет уровень вместе с его журналом срабатываний."""
    async with session_scope() as session:
        level = await session.get(UserLevel, level_id)
        if level is None or level.tg_id != tg_id:
            return None
        snapshot = UserLevel(
            id=level.id,
            tg_id=level.tg_id,
            instrument_id=level.instrument_id,
            price=level.price,
            note=level.note,
            trigger_count=level.trigger_count,
        )
        await session.delete(level)
        log.info("Уровень %s удалён пользователем %s", level_id, tg_id)
        return snapshot


async def update_price(tg_id: int, level_id: int, price: float) -> UserLevel | None:
    """Меняет цену уровня.

    Состояние сбрасывается в исходное: cooldown и отметка «уже отправлено»
    относились к прежней цене, и переносить их на новую нельзя — иначе уровень
    промолчит при первом же подходе.
    """
    if price <= 0:
        raise UserLevelError("Цена уровня должна быть положительной.")

    async with session_scope() as session:
        level = await session.get(UserLevel, level_id)
        if level is None or level.tg_id != tg_id:
            return None
        level.price = price
        level.state = LevelState.ARMED.value
        level.cooldown_until = None
        level.notified_users = []
        return level


async def update_note(tg_id: int, level_id: int, note: str | None) -> UserLevel | None:
    async with session_scope() as session:
        level = await session.get(UserLevel, level_id)
        if level is None or level.tg_id != tg_id:
            return None
        level.note = (note or "").strip()[:200] or None
        return level


async def archive_stale(
    instrument_id: int, price: float, threshold_pct: float
) -> list[ArchivedLevel]:
    """Убирает уровни, от которых цена ушла дальше порога.

    Архив, а не удаление: журнал срабатываний переживает архивацию, и по
    убранной отметке всё ещё видно, как рынок её отрабатывал. Обратно уровень
    не возвращается — если он снова нужен, его ставят заново, и это честнее,
    чем воскрешать отметку, о которой человек уже забыл.

    Расстояние считается от цены уровня — как и порог срабатывания. Иначе
    «далеко» на одном конце и «близко» на другом мерились бы разными линейками.
    """
    if threshold_pct <= 0 or price <= 0:
        return []

    archived: list[ArchivedLevel] = []
    now = utcnow()

    async with session_scope() as session:
        levels = (
            await session.scalars(
                select(UserLevel).where(
                    UserLevel.instrument_id == instrument_id,
                    UserLevel.active.is_(True),
                )
            )
        ).all()

        for level in levels:
            if level.price <= 0:
                continue
            gap = pct_between(level.price, price)
            if gap <= threshold_pct:
                continue

            level.active = False
            level.archived_at = now
            level.archive_reason = ARCHIVE_PRICE_LEFT
            archived.append(
                ArchivedLevel(
                    id=level.id,
                    tg_id=level.tg_id,
                    price=level.price,
                    note=level.note,
                    trigger_count=level.trigger_count,
                    distance_pct=gap,
                )
            )

    if archived:
        log.info(
            "Инструмент %s: в архив ушло уровней — %s (цена %.6g)",
            instrument_id, len(archived), price,
        )
    return archived


async def list_levels(tg_id: int, instrument_id: int | None = None) -> list[UserLevel]:
    async with session_scope() as session:
        stmt = select(UserLevel).where(
            UserLevel.tg_id == tg_id, UserLevel.active.is_(True)
        )
        if instrument_id is not None:
            stmt = stmt.where(UserLevel.instrument_id == instrument_id)
        return list((await session.scalars(stmt.order_by(UserLevel.price))).all())


async def count_active(instrument_id: int, tg_id: int | None = None) -> int:
    """Число активных уровней по инструменту.

    `tg_id` обязателен везде, где число показывается человеку: уровни приватны,
    и общий счётчик выдаёт, сколько отметок поставили другие. Без владельца
    считаем только для служебных нужд — лога цикла.
    """
    async with session_scope() as session:
        stmt = (
            select(func.count())
            .select_from(UserLevel)
            .where(
                UserLevel.instrument_id == instrument_id,
                UserLevel.active.is_(True),
            )
        )
        if tg_id is not None:
            stmt = stmt.where(UserLevel.tg_id == tg_id)
        return int(await session.scalar(stmt) or 0)


async def active_levels_for_instrument(instrument_id: int) -> list[UserLevel]:
    """Все активные ручные уровни по инструменту — для цикла цены."""
    async with session_scope() as session:
        return list(
            (
                await session.scalars(
                    select(UserLevel).where(
                        UserLevel.instrument_id == instrument_id,
                        UserLevel.active.is_(True),
                    )
                )
            ).all()
        )


def to_snapshot(level: UserLevel) -> LevelSnapshot:
    return LevelSnapshot(
        id=level.id,
        price=level.price,
        score=MANUAL_SCORE,
        kinds=(MANUAL_KIND,),
        state=level.state,
        cooldown_until=level.cooldown_until,
        notified_users=tuple(level.notified_users or []),
    )


async def persist_decision(level_id: int, decision) -> None:  # noqa: ANN001
    async with session_scope() as session:
        level = await session.get(UserLevel, level_id)
        if level is None:
            return
        level.state = decision.state
        level.cooldown_until = decision.cooldown_until
        level.notified_users = decision.notified_users


async def record_trigger(
    level_id: int, kind: str, price: float, level_price: float, distance_atr: float,
    now: datetime | None = None,
) -> int:
    """Пишет срабатывание в журнал уровня и возвращает его порядковый номер.

    Журнал отдельный от alerts и переживает архивацию: ради него ручные уровни
    и заводятся — посмотреть потом, как рынок отрабатывал именно эту отметку.

    Номер возвращается, потому что вызывающий держит объект уровня, прочитанный
    до инкремента, и в тексте алерта иначе оказывается предыдущее значение.
    """
    now = now or utcnow()
    async with session_scope() as session:
        level = await session.get(UserLevel, level_id)
        if level is None:
            return 0
        level.trigger_count += 1
        level.last_triggered_at = now
        level.last_distance_atr = distance_atr
        session.add(
            UserLevelEvent(
                user_level_id=level_id,
                ts=now,
                kind=kind,
                price=price,
                level_price=level_price,
                distance_atr=distance_atr,
            )
        )
        return level.trigger_count


async def level_history(level_id: int, limit: int = 20) -> list[UserLevelEvent]:
    async with session_scope() as session:
        return list(
            (
                await session.scalars(
                    select(UserLevelEvent)
                    .where(UserLevelEvent.user_level_id == level_id)
                    .order_by(UserLevelEvent.ts.desc())
                    .limit(limit)
                )
            ).all()
        )


async def stats(tg_id: int) -> LevelStats:
    async with session_scope() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(UserLevel)
            .where(UserLevel.tg_id == tg_id, UserLevel.active.is_(True))
        )
        triggers = await session.scalar(
            select(func.coalesce(func.sum(UserLevel.trigger_count), 0)).where(
                UserLevel.tg_id == tg_id, UserLevel.active.is_(True)
            )
        )
    return LevelStats(active=int(active or 0), triggers=int(triggers or 0))
