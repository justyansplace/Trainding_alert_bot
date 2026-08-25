"""Тесты детектора — сценарии анти-спама.

Без гистерезиса и cooldown бота замьютят на второй день, поэтому основной объём
тестов здесь про то, что алерт НЕ приходит.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot.db.models import AlertKind, LevelState
from alert_bot.market.detector import (
    LevelSnapshot,
    Subscriber,
    detect_breakout,
    evaluate_level,
    moving_toward,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
COOLDOWN = timedelta(hours=4)
ATR = 10.0


def snapshot(
    price: float = 100.0,
    score: float = 6.0,
    state: str = LevelState.ARMED.value,
    cooldown_until: datetime | None = None,
    notified: tuple[int, ...] = (),
) -> LevelSnapshot:
    return LevelSnapshot(
        id=1,
        price=price,
        score=score,
        kinds=("PDH", "round"),
        state=state,
        cooldown_until=cooldown_until,
        notified_users=notified,
    )


def sub(tg_id: int = 1, min_score: float = 4.0, atr_k: float = 0.3, muted=None) -> Subscriber:
    return Subscriber(tg_id=tg_id, min_score=min_score, atr_k=atr_k, muted_until=muted)


def approaching(target: float, current: float) -> list[float]:
    """История цен, идущая К уровню."""
    away = current + (current - target)
    return [away, current]


def receding(target: float, current: float) -> list[float]:
    closer = current - (current - target) / 2
    return [closer, current]


def run(level, price, history, subs, now=NOW, atr=ATR):
    return evaluate_level(level, price, history, atr, subs, now, COOLDOWN)


# --------------------------------------------------------------------------- #
# Срабатывание
# --------------------------------------------------------------------------- #


def test_alert_fires_on_approach_within_threshold() -> None:
    level = snapshot(price=100.0)
    decision = run(level, 98.0, approaching(100.0, 98.0), [sub()])  # 0.2xATR

    assert decision.event is not None
    assert decision.event.kind == AlertKind.APPROACH.value
    assert decision.event.recipients == (1,)
    assert decision.state == LevelState.TRIGGERED.value
    assert decision.event.distance_atr == pytest.approx(0.2)


def test_no_alert_when_still_far() -> None:
    level = snapshot(price=100.0)
    decision = run(level, 90.0, approaching(100.0, 90.0), [sub()])  # 1.0xATR > 0.3
    assert decision.event is None
    assert decision.state == LevelState.ARMED.value


def test_no_alert_when_price_moves_away() -> None:
    """Расстояние то же, торговый смысл противоположный."""
    level = snapshot(price=100.0)
    decision = run(level, 98.0, receding(100.0, 98.0), [sub()])
    assert decision.event is None


def test_no_alert_below_min_score() -> None:
    level = snapshot(price=100.0, score=2.0)
    decision = run(level, 98.0, approaching(100.0, 98.0), [sub(min_score=4.0)])
    assert decision.event is None


def test_muted_subscriber_gets_nothing() -> None:
    level = snapshot(price=100.0)
    muted = sub(muted=NOW + timedelta(hours=1))
    assert run(level, 98.0, approaching(100.0, 98.0), [muted]).event is None


def test_expired_mute_no_longer_blocks() -> None:
    level = snapshot(price=100.0)
    expired = sub(muted=NOW - timedelta(minutes=1))
    assert run(level, 98.0, approaching(100.0, 98.0), [expired]).event is not None


# --------------------------------------------------------------------------- #
# Полный цикл анти-спама
# --------------------------------------------------------------------------- #


def test_full_antispam_cycle() -> None:
    """Сценарий из плана — самый вероятный баг проекта.

    подошёл → алерт → подошёл снова → тишина → ушёл на 1.2 ATR → вернулся →
    снова алерт (но только после истечения cooldown).
    """
    subs = [sub()]
    level = snapshot(price=100.0)

    # 1. Подошёл — алерт.
    first = run(level, 98.0, approaching(100.0, 98.0), subs)
    assert first.event is not None
    level = snapshot(
        state=first.state,
        cooldown_until=first.cooldown_until,
        notified=tuple(first.notified_users),
    )

    # 2. Подошёл ещё ближе — тишина, за этот подход уже отправлено.
    second = run(level, 99.0, approaching(100.0, 99.0), subs)
    assert second.event is None
    assert second.state == LevelState.TRIGGERED.value

    # 3. Ушёл на 1.2 ATR — уровень перезаряжается, включается cooldown.
    third = run(level, 88.0, receding(100.0, 88.0), subs)
    assert third.state == LevelState.ARMED.value
    assert third.notified_users == []
    assert third.cooldown_until == NOW + COOLDOWN
    level = snapshot(
        state=third.state, cooldown_until=third.cooldown_until, notified=()
    )

    # 4. Вернулся внутри cooldown — тишина.
    inside = run(level, 98.0, approaching(100.0, 98.0), subs, now=NOW + timedelta(hours=1))
    assert inside.event is None
    assert inside.state == LevelState.ARMED.value

    # 5. Вернулся после cooldown — снова алерт.
    after = run(level, 98.0, approaching(100.0, 98.0), subs, now=NOW + timedelta(hours=5))
    assert after.event is not None


def test_hysteresis_needs_full_atr_not_just_threshold_exit() -> None:
    """Выход за 0.3xATR не перезаряжает уровень.

    Иначе болтанка вокруг порога даёт очередь одинаковых алертов: вышел на
    0.31, вернулся на 0.29, снова алерт.
    """
    level = snapshot(state=LevelState.TRIGGERED.value, notified=(1,))
    decision = run(level, 95.0, receding(100.0, 95.0), [sub()])  # 0.5xATR
    assert decision.state == LevelState.TRIGGERED.value
    assert decision.notified_users == [1]


# --------------------------------------------------------------------------- #
# Разные пороги у разных подписчиков
# --------------------------------------------------------------------------- #


def test_wide_and_narrow_thresholds_each_get_one_alert() -> None:
    """Состояние уровня общее, пороги разные — оба должны получить по алерту.

    Наивная реализация ставит TRIGGERED на первом же срабатывании, и подписчик
    с узким порогом не получает ничего никогда.
    """
    wide, narrow = sub(tg_id=1, atr_k=0.5), sub(tg_id=2, atr_k=0.1)
    subs = [wide, narrow]
    level = snapshot(price=100.0)

    # Цена в 0.4xATR — попадает под широкий порог, но не под узкий.
    first = run(level, 96.0, approaching(100.0, 96.0), subs)
    assert first.event is not None
    assert first.event.recipients == (1,)

    level = snapshot(
        state=first.state,
        cooldown_until=first.cooldown_until,
        notified=tuple(first.notified_users),
    )

    # Цена подошла на 0.05xATR — теперь проходит и узкий порог.
    second = run(level, 99.5, approaching(100.0, 99.5), subs)
    assert second.event is not None
    assert second.event.recipients == (2,)
    assert set(second.notified_users) == {1, 2}

    # Третий тик — обоим уже отправлено, тишина.
    level = snapshot(
        state=second.state,
        cooldown_until=second.cooldown_until,
        notified=tuple(second.notified_users),
    )
    assert run(level, 99.8, approaching(100.0, 99.8), subs).event is None


def test_zone_entry_uses_widest_subscriber_threshold() -> None:
    level = snapshot(price=100.0)
    subs = [sub(tg_id=1, atr_k=0.05), sub(tg_id=2, atr_k=0.6)]
    decision = run(level, 95.0, approaching(100.0, 95.0), subs)  # 0.5xATR
    assert decision.event is not None
    assert decision.event.recipients == (2,)


def test_per_user_min_score_filters_recipients() -> None:
    level = snapshot(price=100.0, score=5.0)
    subs = [sub(tg_id=1, min_score=4.0), sub(tg_id=2, min_score=9.0)]
    decision = run(level, 98.0, approaching(100.0, 98.0), subs)
    assert decision.event is not None
    assert decision.event.recipients == (1,)


# --------------------------------------------------------------------------- #
# Вырожденные входы
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_atr", [0.0, -1.0, float("nan"), float("inf")])
def test_bad_atr_produces_no_events(bad_atr: float) -> None:
    """ATR приходит NaN на короткой истории — делить на него нельзя."""
    level = snapshot(price=100.0)
    decision = evaluate_level(
        level, 100.0, [101.0, 100.0], bad_atr, [sub()], NOW, COOLDOWN
    )
    assert decision.event is None
    assert decision.state == level.state


def test_no_subscribers_no_events() -> None:
    assert run(snapshot(), 100.0, [101.0, 100.0], []).event is None


def test_short_history_is_not_treated_as_approach() -> None:
    assert not moving_toward([100.0], 100.0)
    assert not moving_toward([], 100.0)
    assert run(snapshot(price=100.0), 98.0, [98.0], [sub()]).event is None


# --------------------------------------------------------------------------- #
# Пробой
# --------------------------------------------------------------------------- #


def test_breakout_requires_close_beyond_level_and_volume() -> None:
    level = snapshot(price=100.0)
    event = detect_breakout(
        level,
        closed_open=98.0,
        closed_close=103.0,
        closed_volume=200.0,
        median_volume=100.0,
        atr=ATR,
        subscribers=[sub()],
        now=NOW,
    )
    assert event is not None
    assert event.kind == AlertKind.BREAKOUT.value


def test_breakout_ignored_on_weak_volume() -> None:
    level = snapshot(price=100.0)
    assert (
        detect_breakout(level, 98.0, 103.0, 110.0, 100.0, ATR, [sub()], NOW) is None
    ), "пробой на среднем объёме чаще ложный, чем настоящий"


def test_wick_through_level_is_not_a_breakout() -> None:
    """Свеча сходила за уровень и закрылась обратно — это не пробой."""
    level = snapshot(price=100.0)
    assert detect_breakout(level, 98.0, 99.0, 300.0, 100.0, ATR, [sub()], NOW) is None


def test_breakout_downward_is_detected() -> None:
    level = snapshot(price=100.0)
    event = detect_breakout(level, 102.0, 97.0, 200.0, 100.0, ATR, [sub()], NOW)
    assert event is not None
    assert event.price == 97.0
