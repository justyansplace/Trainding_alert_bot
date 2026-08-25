"""Тесты ручных уровней: жизненный цикл, история, изоляция между людьми."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot.db.models import (
    Instrument,
    LevelState,
    Subscription,
    User,
    UserLevel,
    UserLevelEvent,
    utcnow,
)
from alert_bot.db.session import session_scope
from alert_bot.bot.level_handlers import parse_price
from alert_bot.market import store, user_levels
from alert_bot.market.levels import Level
from alert_bot.scheduler import PriceLoop

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def enqueue(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


async def make_instrument(symbol: str = "BTC/USDT", price: float = 1000.0) -> Instrument:
    async with session_scope() as session:
        instrument = Instrument(
            symbol=symbol,
            provider="ccxt",
            exchange="binance",
            round_step=500.0,
            price_precision=2,
            keywords=[],
            added_by=1,
            last_price=price,
            atr=100.0,
        )
        session.add(instrument)
        await session.flush()
        return instrument


async def make_user(tg_id: int, instrument_id: int | None = None, atr_k: float = 3.0) -> None:
    async with session_scope() as session:
        session.add(User(tg_id=tg_id, role="user", granted_at=utcnow(), active=True))
        await session.flush()
        if instrument_id is not None:
            session.add(
                Subscription(
                    tg_id=tg_id,
                    instrument_id=instrument_id,
                    enabled=True,
                    atr_k=atr_k,
                    min_score=1.0,
                )
            )


# --------------------------------------------------------------------------- #
# Разбор цены
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("78000", 78000.0),
        ("78 000", 78000.0),
        ("78k", 78000.0),
        ("78к", 78000.0),  # русская к
        ("78000.5", 78000.5),
        ("0.09096", 0.09096),
        ("2,5", 2.5),  # запятая как десятичный разделитель
        ("78,000", 78000.0),  # запятая как разделитель тысяч
        ("1,234.5", 1234.5),
    ],
)
def test_price_parsing_accepts_human_input(raw: str, expected: float) -> None:
    """Человек вводит цену как удобно, а не как удобно парсеру."""
    assert parse_price(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "абв", "-100", "0", "12.3.4"])
def test_bad_price_rejected(raw: str) -> None:
    assert parse_price(raw) is None


# --------------------------------------------------------------------------- #
# Жизненный цикл
# --------------------------------------------------------------------------- #


async def test_add_and_list_level(db) -> None:
    instrument = await make_instrument()
    await make_user(1)

    level = await user_levels.add_level(1, instrument, 1200.0, "недельный максимум")

    rows = await user_levels.list_levels(1)
    assert len(rows) == 1
    assert rows[0].id == level.id
    assert rows[0].note == "недельный максимум"
    assert rows[0].active


async def test_duplicate_level_rejected(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await user_levels.add_level(1, instrument, 1200.0)

    with pytest.raises(user_levels.UserLevelError, match="уже стоит"):
        await user_levels.add_level(1, instrument, 1200.3)


async def test_nearby_but_distinct_level_allowed(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await user_levels.add_level(1, instrument, 1200.0)
    await user_levels.add_level(1, instrument, 1210.0)
    assert len(await user_levels.list_levels(1)) == 2


async def test_per_instrument_limit(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    for i in range(user_levels.MAX_LEVELS_PER_USER_PER_INSTRUMENT):
        await user_levels.add_level(1, instrument, 1000.0 + i * 50)

    with pytest.raises(user_levels.UserLevelError, match="предел"):
        await user_levels.add_level(1, instrument, 9999.0)


async def test_negative_price_rejected(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    with pytest.raises(user_levels.UserLevelError, match="положительной"):
        await user_levels.add_level(1, instrument, -5.0)


# --------------------------------------------------------------------------- #
# Архив вместо удаления
# --------------------------------------------------------------------------- #


async def test_delete_removes_level_and_its_history(db) -> None:
    """Удаление — настоящее: уровня и его журнала больше нет."""
    from sqlalchemy import select

    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0)
    await user_levels.record_trigger(level.id, "approach", 1190.0, 1200.0, 0.1, NOW)
    await user_levels.record_trigger(level.id, "approach", 1195.0, 1200.0, 0.05, NOW)

    removed = await user_levels.delete_level(1, level.id)

    assert removed is not None and removed.price == 1200.0
    assert await user_levels.list_levels(1) == []
    async with session_scope() as session:
        events = (await session.scalars(select(UserLevelEvent))).all()
    assert events == [], "журнал уходит вместе с уровнем"


async def test_delete_is_idempotent(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0)

    assert await user_levels.delete_level(1, level.id) is not None
    assert await user_levels.delete_level(1, level.id) is None


async def test_edit_price_resets_alert_state(db) -> None:
    """Cooldown относился к прежней цене.

    Перенести его на новую — значит промолчать при первом же подходе к ней.
    """
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0)

    async with session_scope() as session:
        stored = await session.get(UserLevel, level.id)
        stored.state = LevelState.TRIGGERED.value
        stored.notified_users = [1]
        stored.cooldown_until = NOW + timedelta(hours=4)

    updated = await user_levels.update_price(1, level.id, 1250.0)

    assert updated is not None and updated.price == 1250.0
    assert updated.state == LevelState.ARMED.value
    assert updated.cooldown_until is None
    assert updated.notified_users == []


async def test_edit_price_keeps_trigger_history(db) -> None:
    """Правка цены — не повод терять историю: отметка та же, сдвинулась."""
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0)
    await user_levels.record_trigger(level.id, "approach", 1190.0, 1200.0, 0.1, NOW)

    updated = await user_levels.update_price(1, level.id, 1250.0)

    assert updated.trigger_count == 1
    assert len(await user_levels.level_history(level.id)) == 1


async def test_edit_note(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0, "старая")

    assert (await user_levels.update_note(1, level.id, "новая")).note == "новая"
    assert (await user_levels.update_note(1, level.id, None)).note is None


async def test_edit_rejects_bad_price(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0)

    with pytest.raises(user_levels.UserLevelError, match="положительной"):
        await user_levels.update_price(1, level.id, -5.0)


async def test_cannot_touch_other_users_level(db) -> None:
    """Чужой уровень нельзя ни прочитать, ни изменить, ни удалить."""
    instrument = await make_instrument()
    await make_user(1)
    await make_user(2)
    level = await user_levels.add_level(1, instrument, 1200.0)

    assert await user_levels.get_level(2, level.id) is None
    assert await user_levels.delete_level(2, level.id) is None
    assert await user_levels.update_price(2, level.id, 1300.0) is None
    assert await user_levels.update_note(2, level.id, "чужая") is None
    assert await user_levels.list_levels(2) == []

    # Уровень цел и не тронут.
    mine = await user_levels.get_level(1, level.id)
    assert mine is not None and mine.price == 1200.0 and mine.note is None


async def test_stats_counts_only_live_levels(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    first = await user_levels.add_level(1, instrument, 1100.0)
    await user_levels.add_level(1, instrument, 1200.0)
    await user_levels.record_trigger(first.id, "approach", 1090.0, 1100.0, 0.1, NOW)
    await user_levels.delete_level(1, first.id)

    stats = await user_levels.stats(1)
    assert stats.active == 1
    assert stats.triggers == 0, "срабатывания удалённого уровня не считаются"


# --------------------------------------------------------------------------- #
# Уровни переживают пересчёт
# --------------------------------------------------------------------------- #


async def test_user_level_survives_computed_level_recompute(db) -> None:
    """Главная причина отдельной таблицы.

    replace_levels стирает все посчитанные уровни каждый час — ручной уровень
    в той же таблице не пережил бы первый час работы бота.
    """
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0, "моя отметка")

    for _ in range(3):
        await store.replace_levels(
            instrument.id, [Level(price=1150.0, kinds=["PDH"], touches=1, score=5.0)]
        )

    rows = await user_levels.list_levels(1)
    assert len(rows) == 1
    assert rows[0].id == level.id
    assert rows[0].note == "моя отметка"


# --------------------------------------------------------------------------- #
# Срабатывание в цикле
# --------------------------------------------------------------------------- #


async def test_user_level_fires_and_records_history(db) -> None:
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    level = await user_levels.add_level(1, instrument, 970.0, "цель")

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)

    assert len(notifier.sent) == 1
    text = notifier.sent[0].text
    assert "ваш уровень" in text
    assert "970.00" in text
    assert "цель" in text

    async with session_scope() as session:
        stored = await session.get(UserLevel, level.id)
        events = await user_levels.level_history(level.id)

    assert stored.trigger_count == 1
    assert stored.last_triggered_at is not None
    assert "№1" in text, "в тексте должен быть номер этого срабатывания, а не предыдущего"
    assert len(events) == 1
    assert events[0].distance_atr == pytest.approx(0.3)


async def test_user_level_ignores_min_score(db) -> None:
    """Фильтровать по значимости уровень, который человек поставил сам, бессмысленно."""
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    async with session_scope() as session:
        sub = await session.get(Subscription, (1, instrument.id))
        sub.min_score = 999.0  # заведомо выше любого посчитанного score

    await user_levels.add_level(1, instrument, 970.0)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)
    assert len(notifier.sent) == 1


async def test_user_level_respects_distance_threshold(db) -> None:
    """Порог расстояния остаётся: это настройка того, за сколько предупреждать."""
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id, atr_k=0.1)
    await user_levels.add_level(1, instrument, 900.0)  # 1.0xATR — далеко

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)
    assert notifier.sent == []


async def test_only_owner_receives_alert(db) -> None:
    """Ручной уровень принадлежит человеку — чужие его получать не должны."""
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    await make_user(2, instrument.id)
    await user_levels.add_level(1, instrument, 970.0)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)

    assert len(notifier.sent) == 1
    assert notifier.sent[0].chat_id == 1


async def test_one_user_does_not_mute_another(db) -> None:
    """Ограничение «один алерт за тик» здесь считается на человека."""
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    await make_user(2, instrument.id)
    await user_levels.add_level(1, instrument, 975.0)
    await user_levels.add_level(2, instrument, 970.0)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)

    assert {m.chat_id for m in notifier.sent} == {1, 2}


async def test_repeat_tick_does_not_resend(db) -> None:
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    await user_levels.add_level(1, instrument, 970.0)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)
    state.push_price(995.0)
    await loop.run_user_levels(instrument, state, NOW + timedelta(minutes=1))

    assert len(notifier.sent) == 1


async def test_deleted_level_stops_firing(db) -> None:
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    level = await user_levels.add_level(1, instrument, 970.0)
    await user_levels.delete_level(1, level.id)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)
    assert notifier.sent == []


async def test_unsubscribed_owner_gets_nothing_but_level_is_kept(db) -> None:
    """Уровень — собственность человека: отписка не повод его терять."""
    instrument = await make_instrument(price=1000.0)
    await make_user(1)  # подписки нет
    level = await user_levels.add_level(1, instrument, 970.0)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)

    assert notifier.sent == []
    assert len(await user_levels.list_levels(1)) == 1
    async with session_scope() as session:
        assert (await session.get(UserLevel, level.id)).active


async def test_manual_alert_row_has_no_level_reference(db) -> None:
    """У ручного уровня нет строки в levels — ссылка привела бы к битому FK."""
    instrument = await make_instrument(price=1000.0)
    await make_user(1, instrument.id)
    await user_levels.add_level(1, instrument, 970.0)

    loop = PriceLoop(notifier=StubNotifier())
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1010.0)
    state.push_price(1000.0)
    await loop.run_user_levels(instrument, state, NOW)

    from sqlalchemy import select

    from alert_bot.db.models import Alert

    async with session_scope() as session:
        alert = (await session.scalars(select(Alert))).one()

    assert alert.level_id is None
    assert alert.level_price == pytest.approx(970.0)


async def test_history_survives_instrument_level_churn(db) -> None:
    """История срабатываний не должна зависеть от жизни посчитанных уровней."""
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 1200.0)
    await user_levels.record_trigger(level.id, "approach", 1190.0, 1200.0, 0.1, NOW)

    for _ in range(5):
        await store.replace_levels(
            instrument.id, [Level(price=1150.0, kinds=["PDH"], touches=1, score=5.0)]
        )

    async with session_scope() as session:
        from sqlalchemy import select

        events = (await session.scalars(select(UserLevelEvent))).all()

    assert len(events) == 1


async def test_status_counts_only_own_levels(db) -> None:
    """Счётчик в /status обязан быть строго своим.

    Общий выдавал бы, сколько отметок поставили другие: сами цены не видны, но
    сам факт «здесь у кого-то 5 уровней» — это чужие данные.
    """
    from alert_bot.bot.handlers import render_status

    instrument = await make_instrument()
    await make_user(1, instrument.id)
    await make_user(2, instrument.id)

    await user_levels.add_level(1, instrument, 1100.0)
    await user_levels.add_level(2, instrument, 1200.0)
    await user_levels.add_level(2, instrument, 1300.0)

    async with session_scope() as session:
        first = await session.get(User, 1)

    text = await render_status(first)
    assert "ваших уровней: 1" in text
    assert "1200" not in text and "1300" not in text


async def test_count_active_is_scoped_by_owner(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await make_user(2)
    await user_levels.add_level(1, instrument, 1100.0)
    await user_levels.add_level(2, instrument, 1200.0)

    assert await user_levels.count_active(instrument.id, tg_id=1) == 1
    assert await user_levels.count_active(instrument.id, tg_id=2) == 1
    # Без владельца — служебный подсчёт для лога цикла, там видно всё.
    assert await user_levels.count_active(instrument.id) == 2


async def test_level_list_never_leaks_another_users_levels(db) -> None:
    from alert_bot.bot.level_handlers import render_levels

    instrument = await make_instrument()
    await make_user(1, instrument.id)
    await make_user(2, instrument.id)

    await user_levels.add_level(1, instrument, 1111.0, "моя отметка")
    await user_levels.add_level(2, instrument, 2222.0, "чужая отметка")

    async with session_scope() as session:
        first = await session.get(User, 1)

    text, _ = await render_levels(first)
    # Цена печатается с разделителем разрядов, поэтому сравниваем по заметке
    # и по отформатированному виду, а не по голым цифрам.
    assert "моя отметка" in text
    assert "1 111" in text
    assert "чужая отметка" not in text
    assert "2 222" not in text
