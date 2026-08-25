"""Детектор событий на уровнях.

Функция evaluate_level — чистая: принимает снимок уровня, цену, ATR и пороги
подписчиков, возвращает решение. Никакой БД и сети, поэтому scripts/replay.py
прогоняет ровно этот код по историческим свечам и позволяет подобрать пороги на
данных, а не на глаз.

Про анти-спам. Состояние уровня одно на всех, а пороги у каждого свои, и это
противоречие приходится разрешать явно:

  * в зону уровень входит по самому широкому atr_k среди подписчиков — иначе
    пользователь с широким порогом не получил бы ничего;
  * пока цена в зоне, каждый подписчик проверяется по своему порогу и попадает
    в notified_users максимум один раз — иначе пользователь с узким порогом
    молчал бы всегда, потому что уровень уже "сработал" для соседа;
  * выход из зоны (гистерезис) чистит notified_users и включает cooldown —
    без этого болтанка цены вокруг уровня даёт очередь одинаковых алертов.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alert_bot.db.models import AlertKind, LevelState

# Насколько далеко должна уйти цена, чтобы уровень снова считался незатронутым.
REARM_ATR_MULT = 1.0

# Во сколько раз объём пробойной свечи должен превышать медианный.
BREAKOUT_VOLUME_MULT = 1.5


@dataclass(frozen=True, slots=True)
class Subscriber:
    tg_id: int
    min_score: float
    atr_k: float
    muted_until: datetime | None = None
    unit: str = "atr"
    threshold_pct: float = 0.3
    direction: str = "any"

    def is_muted(self, now: datetime) -> bool:
        return self.muted_until is not None and now < self.muted_until

    def reach(self, distance_atr: float, distance_pct: float) -> float:
        """Насколько цена «дошла» до порога: 1.0 — ровно на пороге.

        Обе единицы приводятся к общей доле, чтобы дальше сравнивать
        подписчиков между собой независимо от того, кто в чём мерит.
        """
        if self.unit == "percent":
            return distance_pct / self.threshold_pct if self.threshold_pct > 0 else 9e9
        return distance_atr / self.atr_k if self.atr_k > 0 else 9e9

    def wants_direction(self, price: float, level_price: float) -> bool:
        """Интересует ли подход с этой стороны.

        «Вверх» означает, что цена идёт к уровню снизу — то есть уровень выше
        текущей цены и работает как сопротивление.
        """
        if self.direction == "up":
            return level_price >= price
        if self.direction == "down":
            return level_price <= price
        return True

    def accepts(
        self,
        score: float,
        distance_atr: float,
        now: datetime,
        distance_pct: float = 0.0,
        price: float = 0.0,
        level_price: float = 0.0,
    ) -> bool:
        return (
            not self.is_muted(now)
            and score >= self.min_score
            and self.reach(distance_atr, distance_pct) <= 1.0
            and self.wants_direction(price, level_price)
        )


@dataclass(frozen=True, slots=True)
class LevelSnapshot:
    id: int
    price: float
    score: float
    kinds: tuple[str, ...]
    state: str
    cooldown_until: datetime | None
    notified_users: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LevelEvent:
    level_id: int
    level_price: float
    level_score: float
    level_kinds: tuple[str, ...]
    kind: str
    price: float
    distance_atr: float
    recipients: tuple[int, ...]


@dataclass(slots=True)
class LevelDecision:
    """Что должно измениться в строке уровня и кому слать."""

    state: str
    cooldown_until: datetime | None
    notified_users: list[int] = field(default_factory=list)
    event: LevelEvent | None = None

    @property
    def changed(self) -> bool:
        return True


def moving_toward(history: list[float], level_price: float) -> bool:
    """Цена приближается к уровню, а не удаляется от него.

    Без этой проверки алерт «подход к уровню» приходил бы и на отходе от него —
    формально расстояние то же, а торговый смысл противоположный.
    """
    if len(history) < 2:
        return False
    return abs(history[-1] - level_price) < abs(history[-2] - level_price)


def evaluate_level(
    level: LevelSnapshot,
    price: float,
    history: list[float],
    atr: float,
    subscribers: list[Subscriber],
    now: datetime,
    cooldown: timedelta,
    rearm_mult: float = REARM_ATR_MULT,
) -> LevelDecision:
    """Решение по одному уровню на одном тике."""
    unchanged = LevelDecision(
        state=level.state,
        cooldown_until=level.cooldown_until,
        notified_users=list(level.notified_users),
    )

    if not math.isfinite(atr) or atr <= 0 or not subscribers:
        return unchanged

    distance_atr = abs(price - level.price) / atr
    distance_pct = abs(price - level.price) / price * 100 if price > 0 else 9e9

    # --- Гистерезис: цена ушла достаточно далеко, уровень перезаряжается. ---
    if level.state == LevelState.TRIGGERED.value:
        if distance_atr > rearm_mult:
            return LevelDecision(
                state=LevelState.ARMED.value,
                cooldown_until=now + cooldown,
                notified_users=[],
            )
    else:
        # --- Вход в зону по самому широкому порогу среди подписчиков. ---
        # Пороги приводятся к общей доле, поэтому ATR и проценты сравнимы.
        if min(s.reach(distance_atr, distance_pct) for s in subscribers) > 1.0:
            return unchanged
        if level.cooldown_until is not None and now < level.cooldown_until:
            return unchanged
        if not moving_toward(history, level.price):
            return unchanged

    # --- В зоне: добираем тех, кто ещё не получил алерт по этому подходу. ---
    already = set(level.notified_users)
    recipients = [
        s.tg_id
        for s in subscribers
        if s.tg_id not in already
        and s.accepts(level.score, distance_atr, now, distance_pct, price, level.price)
    ]

    if not recipients:
        return LevelDecision(
            state=LevelState.TRIGGERED.value,
            cooldown_until=level.cooldown_until,
            notified_users=list(level.notified_users),
        )

    return LevelDecision(
        state=LevelState.TRIGGERED.value,
        cooldown_until=level.cooldown_until,
        notified_users=[*level.notified_users, *recipients],
        event=LevelEvent(
            level_id=level.id,
            level_price=level.price,
            level_score=level.score,
            level_kinds=level.kinds,
            kind=AlertKind.APPROACH.value,
            price=price,
            distance_atr=distance_atr,
            recipients=tuple(recipients),
        ),
    )


def detect_breakout(
    level: LevelSnapshot,
    closed_open: float,
    closed_close: float,
    closed_volume: float,
    median_volume: float,
    atr: float,
    subscribers: list[Subscriber],
    now: datetime,
) -> LevelEvent | None:
    """Пробой: закрытая свеча пересекла уровень на повышенном объёме.

    Считается только по *закрытой* свече — внутри формирующейся цена может
    сходить за уровень и вернуться, и алерт о пробое окажется ложным.
    """
    if not math.isfinite(atr) or atr <= 0 or not subscribers:
        return None
    if median_volume <= 0 or closed_volume < median_volume * BREAKOUT_VOLUME_MULT:
        return None

    crossed_up = closed_open <= level.price < closed_close
    crossed_down = closed_open >= level.price > closed_close
    if not (crossed_up or crossed_down):
        return None

    distance_atr = abs(closed_close - level.price) / atr
    recipients = [
        s.tg_id
        for s in subscribers
        if not s.is_muted(now)
        and level.score >= s.min_score
        and s.wants_direction(closed_close, level.price)
    ]
    if not recipients:
        return None

    return LevelEvent(
        level_id=level.id,
        level_price=level.price,
        level_score=level.score,
        level_kinds=level.kinds,
        kind=AlertKind.BREAKOUT.value,
        price=closed_close,
        distance_atr=distance_atr,
        recipients=tuple(recipients),
    )
