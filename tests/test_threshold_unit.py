"""Единица порога, выбранная пользователем, должна применяться везде.

Выбор в «Настройке алертов» — не про один экран: расстояние между уровнем и
ценой показывается в списке уровней, в карточке, в настройках и в самом алерте.
Если хоть где-то остаётся ×ATR у выбравшего проценты, число на экране нельзя
сверить с порогом, по которому алерт реально срабатывает.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot import alerts, threshold
from alert_bot.bot import level_handlers, settings_handlers
from alert_bot.db.models import Instrument, ThresholdUnit, User, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market.detector import (
    LevelEvent,
    LevelSnapshot,
    Subscriber,
    detect_breakout,
    evaluate_level,
)

from tests.test_alerts import StubNotifier, make_instrument

PCT = ThresholdUnit.PERCENT.value
ATR = ThresholdUnit.ATR.value


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #


def test_distance_is_rendered_in_the_chosen_unit() -> None:
    assert threshold.format_distance(ATR, 0.36, 0.32) == "0.36×ATR"
    assert threshold.format_distance(PCT, 0.36, 0.32) == "0.32%"


def test_percent_is_measured_from_the_level_price() -> None:
    """Так же, как в детекторе: полоса срабатывания должна быть неподвижной."""
    assert threshold.pct_between(77_500.0, 77_250.0) == pytest.approx(0.3226, abs=1e-4)


def test_atr_distance_is_omitted_when_atr_is_unknown() -> None:
    """Лучше не показать расстояние, чем показать его не в той единице."""
    assert threshold.gap_text(ATR, 77_500.0, 77_250.0, None) == ""
    assert threshold.gap_text(PCT, 77_500.0, 77_250.0, None) == "0.32%"


def test_unit_falls_back_to_config_default() -> None:
    assert threshold.unit_of(User(tg_id=1)) == ATR
    assert threshold.unit_of(User(tg_id=1, def_threshold_unit=PCT)) == PCT


# --------------------------------------------------------------------------- #
# Событие несёт оба расстояния
# --------------------------------------------------------------------------- #


def snapshot(price: float = 77_500.0) -> LevelSnapshot:
    return LevelSnapshot(
        id=1, price=price, score=8.2, kinds=("PDH",), state="armed",
        cooldown_until=None, notified_users=(),
    )


def test_event_carries_both_distances() -> None:
    """Получатели одного подхода могут мерить в разных единицах."""
    decision = evaluate_level(
        snapshot(),
        77_250.0,
        [77_000.0, 77_250.0],
        700.0,
        [Subscriber(tg_id=1, min_score=0.0, atr_k=0.5)],
        datetime(2026, 8, 22, 12, tzinfo=UTC),
        timedelta(hours=4),
    )
    assert decision.event is not None
    assert decision.event.distance_atr == pytest.approx(0.357, abs=1e-3)
    assert decision.event.distance_pct == pytest.approx(0.3226, abs=1e-4)


def test_breakout_event_carries_percent_too() -> None:
    fired = detect_breakout(
        snapshot(77_000.0),
        closed_open=76_800.0,
        closed_close=77_400.0,
        closed_volume=300.0,
        median_volume=100.0,
        atr=700.0,
        subscribers=[Subscriber(tg_id=1, min_score=0.0, atr_k=0.5)],
        now=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    assert fired is not None
    assert fired.distance_pct == pytest.approx(0.5195, abs=1e-4)


# --------------------------------------------------------------------------- #
# Текст алерта
# --------------------------------------------------------------------------- #


async def make_user_with_unit(tg_id: int, unit: str) -> None:
    async with session_scope() as session:
        session.add(
            User(
                tg_id=tg_id,
                role="user",
                granted_at=utcnow(),
                tz="UTC",
                def_threshold_unit=unit,
                active=True,
            )
        )


def approach(recipients: tuple[int, ...] = (1,)) -> LevelEvent:
    return LevelEvent(
        level_id=1,
        level_price=77_500.0,
        level_score=8.2,
        level_kinds=("PDH", "round"),
        kind="approach",
        price=77_250.0,
        distance_atr=0.36,
        recipients=recipients,
        distance_pct=0.32,
    )


async def test_alert_text_uses_recipient_unit(db) -> None:
    instrument = await make_instrument()

    assert "0.36×ATR" in alerts.format_alert(instrument, approach(), unit=ATR)
    text = alerts.format_alert(instrument, approach(), unit=PCT)
    assert "0.32%" in text and "ATR" not in text


async def test_dispatch_gives_each_recipient_their_own_unit(db) -> None:
    """Событие одно, а мерят его двое по-разному — текстов должно быть два."""
    instrument = await make_instrument()
    await make_user_with_unit(1, ATR)
    await make_user_with_unit(2, PCT)
    notifier = StubNotifier()

    sent = await alerts.dispatch_event(instrument, approach((1, 2)), notifier)

    assert sent == 2
    by_chat = {m.chat_id: m.text for m in notifier.sent}
    assert "0.36×ATR" in by_chat[1]
    assert "0.32%" in by_chat[2] and "ATR" not in by_chat[2]


# --------------------------------------------------------------------------- #
# Экраны
# --------------------------------------------------------------------------- #


class FakeLevel:
    def __init__(self, price: float) -> None:
        self.price = price


class FakeInstrument:
    def __init__(self, last_price: float | None, atr: float | None) -> None:
        self.last_price = last_price
        self.atr = atr


def test_level_list_shows_distance_in_the_chosen_unit() -> None:
    instrument = FakeInstrument(77_250.0, 700.0)
    level = FakeLevel(77_500.0)

    assert level_handlers._distance(level, instrument, ATR) == "▲ 0.36×ATR"
    assert level_handlers._distance(level, instrument, PCT) == "▲ 0.32%"


def test_settings_screen_shows_the_chosen_unit() -> None:
    atr_text = settings_handlers._settings_text(User(tg_id=1))
    pct_text = settings_handlers._settings_text(User(tg_id=1, def_threshold_unit=PCT))

    assert "×ATR" in atr_text
    assert "×ATR" not in pct_text and "%" in pct_text


class FakeMessage:
    def __init__(self) -> None:
        self.text = ""

    async def edit_text(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.text = text


class FakeCallback:
    """Ровно то, что трогает хендлер: data, message и answer."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.toast = None

    async def answer(self, toast: str = "") -> None:
        self.toast = toast


async def test_settings_button_moves_the_active_unit(db) -> None:
    """У выбравшего проценты кнопка «Дальше» обязана двигать процент, а не ATR."""
    await make_user_with_unit(1, PCT)
    stored_before = User(tg_id=1, def_threshold_unit=PCT)

    callback = FakeCallback("set:atr:+")
    await settings_handlers.cb_settings(callback, stored_before)

    async with session_scope() as session:
        user = await session.get(User, 1)
        assert user.def_threshold_pct is not None
        assert user.def_atr_k is None
    assert "%" in callback.message.text and "×ATR" not in callback.message.text


async def test_settings_button_still_moves_atr_for_atr_users(db) -> None:
    await make_user_with_unit(1, ATR)

    await settings_handlers.cb_settings(FakeCallback("set:atr:+"), User(tg_id=1))

    async with session_scope() as session:
        user = await session.get(User, 1)
        assert user.def_atr_k is not None
        assert user.def_threshold_pct is None
