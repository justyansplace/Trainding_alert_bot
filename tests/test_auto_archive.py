"""Автоочистка уровней и мелкая сетка процентного порога.

Уровень, от которого цена ушла дальше порога, уводится в архив: список не
должен зарастать тем, что рынок давно прошёл. Архив, а не удаление — журнал
срабатываний по отметке переживает уборку.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alert_bot import config
from alert_bot.bot.alert_settings import (
    PCT_STEPS,
    _bar,
    render,
    shift,
    steps_from,
    threshold_buttons,
)
from alert_bot.db.models import Instrument, ThresholdUnit, User, UserLevel, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market import user_levels
from alert_bot.scheduler import PriceLoop

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
PCT = ThresholdUnit.PERCENT.value


# --------------------------------------------------------------------------- #
# Сетка процентов
# --------------------------------------------------------------------------- #


def test_percent_grid_is_hundredths() -> None:
    assert PCT_STEPS[:5] == [0.01, 0.02, 0.03, 0.04, 0.05]
    assert PCT_STEPS[-1] == 2.5
    assert all(
        round(b - a, 2) == 0.01 for a, b in zip(PCT_STEPS, PCT_STEPS[1:], strict=False)
    )


def test_percent_moves_by_a_hundredth() -> None:
    assert shift(0.07, PCT_STEPS, +1) == 0.08
    assert shift(0.07, PCT_STEPS, -1) == 0.06


def test_coarse_step_moves_by_a_tenth() -> None:
    """Дойти с 0.25 до 1.00 по сотой — семьдесят пять нажатий."""
    assert shift(0.25, PCT_STEPS, +10) == 0.35
    assert shift(0.25, PCT_STEPS, -10) == 0.15


def test_steps_stop_at_the_edges() -> None:
    assert shift(0.01, PCT_STEPS, -10) == 0.01
    assert shift(2.5, PCT_STEPS, +10) == 2.5


def test_scale_stays_readable_on_a_long_grid() -> None:
    """250 точек не влезли бы ни в один экран."""
    assert len(_bar(0.07, PCT_STEPS)) == 11
    assert _bar(0.01, PCT_STEPS).startswith("●")
    assert _bar(2.5, PCT_STEPS).endswith("●")


def test_short_grid_still_draws_one_cell_per_step() -> None:
    from alert_bot.bot.alert_settings import ATR_STEPS

    assert len(_bar(0.3, ATR_STEPS)) == len(ATR_STEPS)


def test_old_buttons_from_already_sent_screens_still_work() -> None:
    """В истории чата висят экраны с «as:thr:+» — они не должны ломаться."""
    assert steps_from("+") == 1
    assert steps_from("-") == -1
    assert steps_from("+10") == 10
    assert steps_from("мусор") == 0


def test_percent_mode_offers_both_step_sizes() -> None:
    labels = [b.text for b in threshold_buttons(PCT, "as:thr")]
    assert labels == ["−0.10", "−0.01", "+0.01", "+0.10"]

    atr_labels = [b.text for b in threshold_buttons(ThresholdUnit.ATR.value, "as:thr")]
    assert len(atr_labels) == 2


def test_screen_shows_hundredths() -> None:
    text, _ = render(User(tg_id=1, def_threshold_unit=PCT, def_threshold_pct=0.07))
    assert "0.07 %" in text


# --------------------------------------------------------------------------- #
# Автоочистка
# --------------------------------------------------------------------------- #


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def enqueue(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _archive_at_3(monkeypatch):
    monkeypatch.setenv("AUTO_ARCHIVE_PCT", "3")
    config._settings = None
    yield
    config._settings = None


async def make_instrument(price: float = 1000.0) -> Instrument:
    async with session_scope() as session:
        instrument = Instrument(
            symbol="BTC/USDT", provider="ccxt", exchange="bybit", round_step=100.0,
            price_precision=2, keywords=["btc"], added_by=1, last_price=price, atr=100.0,
        )
        session.add(instrument)
        await session.flush()
        return instrument


async def make_user(tg_id: int) -> None:
    async with session_scope() as session:
        session.add(User(tg_id=tg_id, role="user", granted_at=utcnow(), active=True))


async def test_level_left_behind_goes_to_the_archive(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 900.0, "старая цель")

    archived = await user_levels.archive_stale(instrument.id, 1000.0, 3.0)

    assert [a.id for a in archived] == [level.id]
    assert archived[0].distance_pct == pytest.approx(11.11, abs=0.01)
    assert await user_levels.list_levels(1) == []


async def test_level_within_the_threshold_is_kept(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await user_levels.add_level(1, instrument, 980.0)  # 2.04%

    assert await user_levels.archive_stale(instrument.id, 1000.0, 3.0) == []
    assert len(await user_levels.list_levels(1)) == 1


async def test_distance_is_measured_from_the_level_price(db) -> None:
    """Как и порог срабатывания: иначе «далеко» мерилось бы двумя линейками."""
    instrument = await make_instrument()
    await make_user(1)
    # От уровня 970 до цены 1000 — 3.09%; от цены до уровня было бы 3.00%.
    await user_levels.add_level(1, instrument, 970.0)

    archived = await user_levels.archive_stale(instrument.id, 1000.0, 3.0)
    assert archived and archived[0].distance_pct == pytest.approx(3.09, abs=0.01)


async def test_history_survives_the_archive(db) -> None:
    """Смысл ручного уровня — посмотреть потом, как рынок его отрабатывал."""
    instrument = await make_instrument()
    await make_user(1)
    level = await user_levels.add_level(1, instrument, 900.0)
    await user_levels.record_trigger(level.id, "approach", 905.0, 900.0, 0.05, utcnow())

    await user_levels.archive_stale(instrument.id, 1000.0, 3.0)

    assert len(await user_levels.level_history(level.id)) == 1
    async with session_scope() as session:
        stored = await session.get(UserLevel, level.id)
    assert stored.active is False
    assert stored.archive_reason == user_levels.ARCHIVE_PRICE_LEFT
    assert stored.archived_at is not None


async def test_zero_threshold_disables_the_cleanup(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await user_levels.add_level(1, instrument, 500.0)  # 100% в стороне

    assert await user_levels.archive_stale(instrument.id, 1000.0, 0) == []
    assert len(await user_levels.list_levels(1)) == 1


async def test_owner_is_told_what_disappeared(db) -> None:
    """Молча убрать поставленное руками — значит выглядеть как потеря данных."""
    instrument = await make_instrument()
    await make_user(1)
    await user_levels.add_level(1, instrument, 900.0, "старая цель")

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)

    assert len(notifier.sent) == 1
    text = notifier.sent[0].text
    assert notifier.sent[0].chat_id == 1
    assert "900.00" in text
    assert "11.11%" in text
    assert "старая цель" in text
    assert "История срабатываний сохранена" in text


async def test_each_owner_hears_only_about_their_own(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await make_user(2)
    await user_levels.add_level(1, instrument, 900.0)
    await user_levels.add_level(2, instrument, 880.0)

    notifier = StubNotifier()
    loop = PriceLoop(notifier=notifier)
    state = loop.runtime(instrument.id)
    state.atr = 100.0
    state.push_price(1000.0)

    await loop.run_user_levels(instrument, state, NOW)

    by_chat = {m.chat_id: m.text for m in notifier.sent}
    assert set(by_chat) == {1, 2}
    assert "900.00" in by_chat[1] and "880.00" not in by_chat[1]
    assert "880.00" in by_chat[2] and "900.00" not in by_chat[2]
