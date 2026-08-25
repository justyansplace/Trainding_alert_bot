"""Тесты настройки алертов: единицы порога, направление, персональные лимиты."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alert_bot.bot import alert_settings as views
from alert_bot.db.models import Direction, ThresholdUnit, User
from alert_bot.market.detector import Subscriber

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Две единицы порога
# --------------------------------------------------------------------------- #


def test_atr_and_percent_are_not_interchangeable() -> None:
    """Один и тот же порог в разных единицах означает разную близость.

    У BTC при ATR 700 и цене 80000 порог 0.3×ATR — это 0.26%; у EUR/USD при
    ATR 0.001 и цене 1.17 те же 0.3×ATR — 0.026%, на порядок меньше. Поэтому
    переключение единицы не пересчитывает значение, а помнит своё для каждой.
    """
    atr_sub = Subscriber(1, 0.0, atr_k=0.3, unit=ThresholdUnit.ATR.value)
    pct_sub = Subscriber(2, 0.0, atr_k=0.3, unit=ThresholdUnit.PERCENT.value, threshold_pct=0.25)

    # Цена в 0.5×ATR и одновременно в 0.1% от уровня.
    assert atr_sub.accepts(0, 0.5, NOW, 0.1, 100, 101) is False
    assert pct_sub.accepts(0, 0.5, NOW, 0.1, 100, 101) is True


@pytest.mark.parametrize(
    ("unit", "atr_k", "pct", "distance_atr", "distance_pct", "expected"),
    [
        ("atr", 0.3, 0.25, 0.2, 5.0, True),
        ("atr", 0.3, 0.25, 0.4, 0.01, False),
        ("percent", 0.3, 0.25, 9.0, 0.2, True),
        ("percent", 0.3, 0.25, 0.01, 0.4, False),
    ],
)
def test_threshold_respects_chosen_unit(
    unit: str, atr_k: float, pct: float, distance_atr: float, distance_pct: float, expected: bool
) -> None:
    sub = Subscriber(1, 0.0, atr_k=atr_k, unit=unit, threshold_pct=pct)
    assert sub.accepts(0, distance_atr, NOW, distance_pct, 100, 101) is expected


def test_zero_threshold_never_fires() -> None:
    """Ноль в пороге не должен делить на ноль и не должен пропускать всё."""
    assert Subscriber(1, 0.0, atr_k=0.0).reach(0.1, 0.1) > 1.0
    assert Subscriber(1, 0.0, atr_k=0.3, unit="percent", threshold_pct=0.0).reach(0.1, 0.1) > 1.0


# --------------------------------------------------------------------------- #
# Направление подхода
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("direction", "level_above", "level_below"),
    [("any", True, True), ("up", True, False), ("down", False, True)],
)
def test_direction_filter(direction: str, level_above: bool, level_below: bool) -> None:
    """«Вверх» — уровень выше цены, то есть сопротивление."""
    sub = Subscriber(1, 0.0, atr_k=0.3, direction=direction)
    assert sub.wants_direction(100.0, 105.0) is level_above
    assert sub.wants_direction(100.0, 95.0) is level_below


# --------------------------------------------------------------------------- #
# Экран настроек
# --------------------------------------------------------------------------- #


def test_defaults_shown_when_user_set_nothing() -> None:
    current = views.resolve(User(tg_id=1))
    assert current["unit"] == "atr"
    assert current["atr_k"] == pytest.approx(0.3)
    assert current["direction"] == Direction.ANY.value


def test_slider_moves_between_steps_and_clamps() -> None:
    assert views.shift(0.3, views.ATR_STEPS, +1) == 0.5
    assert views.shift(0.3, views.ATR_STEPS, -1) == 0.2
    # На краях шкала не уезжает за пределы.
    assert views.shift(views.ATR_STEPS[0], views.ATR_STEPS, -1) == views.ATR_STEPS[0]
    assert views.shift(views.ATR_STEPS[-1], views.ATR_STEPS, +1) == views.ATR_STEPS[-1]


def test_value_off_the_grid_snaps_to_nearest() -> None:
    """Значение из конфига может не совпасть с сеткой ползунка."""
    assert views.shift(0.33, views.ATR_STEPS, +1) == 0.5


def test_scale_marks_current_position() -> None:
    bar = views._bar(0.3, views.ATR_STEPS)
    assert bar.count("●") == 1
    assert len(bar) == len(views.ATR_STEPS)


def test_screen_explains_difference_between_units() -> None:
    atr_text, _ = views.render(User(tg_id=1))
    pct_text, _ = views.render(User(tg_id=1, def_threshold_unit="percent"))

    assert "ATR" in atr_text and "волатильн" in atr_text
    assert "%" in pct_text and "не учитывает волатильность" in pct_text


def test_screen_shows_all_personal_settings() -> None:
    user = User(
        tg_id=1,
        def_threshold_unit="percent",
        def_threshold_pct=0.6,
        direction_filter="up",
        def_cooldown_hours=8,
        max_alerts_per_day=50,
    )
    text, _ = views.render(user)
    assert "0.60 %" in text
    assert "только вверх" in text
    assert "8 ч" in text
    assert "50" in text


# --------------------------------------------------------------------------- #
# Сохранение и сброс
# --------------------------------------------------------------------------- #


async def make_user(db, tg_id: int = 1) -> User:  # noqa: ANN001
    from alert_bot.db.models import utcnow
    from alert_bot.db.session import session_scope

    async with session_scope() as session:
        session.add(User(tg_id=tg_id, role="user", granted_at=utcnow(), active=True))
    async with session_scope() as session:
        return await session.get(User, tg_id)


async def test_switching_unit_keeps_other_units_value(db) -> None:
    """Переключение единицы не пересчитывает порог.

    ATR и проценты означают разное, и автоматический пересчёт дал бы человеку
    неожиданное число вместо того, что он выставлял раньше.
    """
    await make_user(db)

    await views._mutate(1, "thr", "+")  # ATR: 0.3 -> 0.5
    await views._mutate(1, "unit", "percent")
    await views._mutate(1, "thr", "+")  # проценты двигаются со своей сетки
    updated = await views._mutate(1, "unit", "atr")

    assert updated.def_atr_k == pytest.approx(0.5), "значение ATR сохранилось"
    assert updated.def_threshold_pct != pytest.approx(0.5), "проценты живут своей сеткой"


async def test_direction_cycles_through_three_states(db) -> None:
    await make_user(db)
    seen = []
    for _ in range(4):
        seen.append((await views._mutate(1, "dir", "")).direction_filter)
    assert seen == ["up", "down", "any", "up"]


async def test_reset_returns_to_config_defaults(db) -> None:
    await make_user(db)
    await views._mutate(1, "thr", "+")
    await views._mutate(1, "cd", "+")
    await views._mutate(1, "cap", "+")
    await views._mutate(1, "dir", "")

    user = await views._mutate(1, "reset", "")

    assert user.def_atr_k is None
    assert user.def_cooldown_hours is None
    assert user.max_alerts_per_day is None
    assert user.direction_filter is None
    assert views.resolve(user)["atr_k"] == pytest.approx(0.3)


async def test_personal_cooldown_reaches_detector(db) -> None:
    """Пауза после срабатывания стала персональной, а не общей на всех."""
    from datetime import timedelta

    from alert_bot import alerts

    await make_user(db, 1)
    await make_user(db, 2)
    await views._mutate(1, "cd", "+")  # 4 -> 8 часов

    mapping = await alerts.cooldown_by_user([1, 2])
    assert mapping[1] == timedelta(hours=8)
    assert 2 not in mapping, "у кого не задано — берётся общий дефолт"


# --------------------------------------------------------------------------- #
# Формула процентного порога
# --------------------------------------------------------------------------- #


def test_percent_threshold_is_measured_from_level_not_price() -> None:
    """Сигнал, когда |уровень − цена| ≤ уровень × ставка.

    Считать процент от текущей цены было бы неверно: полоса срабатывания
    ползла бы вместе с ней, и один и тот же порог означал бы разную ширину
    в разные моменты. Уровень неподвижен — от него и считаем.
    """
    from alert_bot.market.detector import LevelSnapshot, evaluate_level

    level = LevelSnapshot(
        id=1, price=81000.0, score=999.0, kinds=("manual",),
        state="armed", cooldown_until=None, notified_users=(),
    )
    sub = Subscriber(1, 0.0, atr_k=99.0, unit="percent", threshold_pct=0.25)
    cooldown = __import__("datetime").timedelta(hours=4)

    # Порог = 81000 × 0.25% = 202.5, то есть срабатывание на 80797.5
    just_outside = 81000 - 210
    just_inside = 81000 - 195

    decision = evaluate_level(
        level, just_outside, [just_outside - 50, just_outside], 700.0, [sub],
        NOW, cooldown,
    )
    assert decision.event is None, "за полосой сигнала быть не должно"

    decision = evaluate_level(
        level, just_inside, [just_inside - 50, just_inside], 700.0, [sub],
        NOW, cooldown,
    )
    assert decision.event is not None, "внутри полосы сигнал обязан быть"


def test_percent_band_has_constant_width() -> None:
    """Ширина полосы не зависит от того, где сейчас цена."""
    sub = Subscriber(1, 0.0, atr_k=99.0, unit="percent", threshold_pct=0.5)

    # Одинаковое расстояние в процентах от уровня даёт одинаковый reach,
    # независимо от абсолютных значений.
    assert sub.reach(0.0, 0.5) == pytest.approx(1.0)
    assert sub.reach(0.0, 0.25) == pytest.approx(0.5)
