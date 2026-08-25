"""Расчёт значимых уровней.

Логика в три шага: собрать кандидатов разных типов → склеить близкие в кластеры
→ отскорить по конфлюэнсу. Смысл конфлюэнса в том, что уровень, где сошлись
вчерашний максимум, пивот и круглое число, рынок отрабатывает иначе, чем каждый
из них поодиночке — а одиночные кандидаты отсекаются порогом min_score.

Всё — чистые функции над DataFrame, чтобы scripts/replay.py гонял ровно тот же
код по истории.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from alert_bot.market import indicators

# Относительный допуск склейки: ±0.15% работает одинаково для BTC за $77k
# и для DOGE за $0.09, в отличие от абсолютного значения в долларах.
CLUSTER_TOLERANCE_PCT = 0.0015

# Вклад типа уровня в скор. Вчерашний экстремум видели все и от него реально
# отталкиваются; круглое число — слабый сигнал сам по себе, но хорошо усиливает
# соседей по кластеру.
KIND_WEIGHT: dict[str, float] = {
    "PDH": 1.0,
    "PDL": 1.0,
    "PWH": 0.9,
    "PWL": 0.9,
    "pivot_P": 0.9,
    "pivot_R1": 0.7,
    "pivot_S1": 0.7,
    "pivot_R2": 0.6,
    "pivot_S2": 0.6,
    "pivot_R3": 0.5,
    "pivot_S3": 0.5,
    "daily_open": 0.8,
    "weekly_open": 0.7,
    "vwap": 0.7,
    "vwap_+1σ": 0.5,
    "vwap_-1σ": 0.5,
    "vwap_+2σ": 0.4,
    "vwap_-2σ": 0.4,
    "swing_high": 0.6,
    "swing_low": 0.6,
    "ema50": 0.6,
    "ema200": 0.8,
    "round": 0.4,
}

HUMAN_KIND: dict[str, str] = {
    "PDH": "макс. вчера",
    "PDL": "мин. вчера",
    "PWH": "макс. недели",
    "PWL": "мин. недели",
    "pivot_P": "Pivot P",
    "pivot_R1": "Pivot R1",
    "pivot_S1": "Pivot S1",
    "pivot_R2": "Pivot R2",
    "pivot_S2": "Pivot S2",
    "pivot_R3": "Pivot R3",
    "pivot_S3": "Pivot S3",
    "daily_open": "открытие дня",
    "weekly_open": "открытие недели",
    "vwap": "VWAP",
    "vwap_+1σ": "VWAP +1σ",
    "vwap_-1σ": "VWAP −1σ",
    "vwap_+2σ": "VWAP +2σ",
    "vwap_-2σ": "VWAP −2σ",
    "swing_high": "swing-максимум",
    "swing_low": "swing-минимум",
    "ema50": "EMA50",
    "ema200": "EMA200",
    "round": "круглое число",
}

SCORE_KINDS_WEIGHT = 1.6
SCORE_TOUCHES_WEIGHT = 0.8

# Окно релевантности вокруг текущей цены: для one-day-торговли уровень, до
# которого несколько дневных ходов, значения не имеет. ATR(H1)×8 ≈ полтора
# дневных диапазона; процент страхует инструменты с аномально низким ATR,
# а жёсткий потолок — с аномально высоким (у DOGE ATR(H1) доходит до 3% от
# цены, и без потолка окно раздувается до четверти цены).
RELEVANCE_PCT = 0.06
RELEVANCE_ATR_MULT = 8.0
MAX_RELEVANCE_PCT = 0.12

# Swing-уровни ищутся только в недавней истории: экстремум из ценового режима
# двухнедельной давности сегодня уровнем не является, но кластеров плодит много.
SWING_LOOKBACK = 240  # часов, ~10 суток


@dataclass(slots=True)
class Candidate:
    price: float
    kind: str


@dataclass(slots=True)
class Level:
    price: float
    kinds: list[str] = field(default_factory=list)
    touches: int = 0
    score: float = 0.0

    def describe(self) -> str:
        return " + ".join(HUMAN_KIND.get(k, k) for k in self.kinds)


# --------------------------------------------------------------------------- #
# Кандидаты
# --------------------------------------------------------------------------- #


def pivots_classic(prev_high: float, prev_low: float, prev_close: float) -> list[Candidate]:
    p = (prev_high + prev_low + prev_close) / 3
    rng = prev_high - prev_low
    return [
        Candidate(p, "pivot_P"),
        Candidate(2 * p - prev_low, "pivot_R1"),
        Candidate(2 * p - prev_high, "pivot_S1"),
        Candidate(p + rng, "pivot_R2"),
        Candidate(p - rng, "pivot_S2"),
        Candidate(prev_high + 2 * (p - prev_low), "pivot_R3"),
        Candidate(prev_low - 2 * (prev_high - p), "pivot_S3"),
    ]


def prev_session_extremes(daily: pd.DataFrame, weekly: pd.DataFrame) -> list[Candidate]:
    out: list[Candidate] = []
    if len(daily) >= 2:
        prev = daily.iloc[-2]
        out += [Candidate(float(prev["h"]), "PDH"), Candidate(float(prev["l"]), "PDL")]
    if len(weekly) >= 2:
        prev = weekly.iloc[-2]
        out += [Candidate(float(prev["h"]), "PWH"), Candidate(float(prev["l"]), "PWL")]
    return out


def session_opens(daily: pd.DataFrame, weekly: pd.DataFrame) -> list[Candidate]:
    out: list[Candidate] = []
    if len(daily) >= 1:
        out.append(Candidate(float(daily.iloc[-1]["o"]), "daily_open"))
    if len(weekly) >= 1:
        out.append(Candidate(float(weekly.iloc[-1]["o"]), "weekly_open"))
    return out


def vwap_levels(intraday: pd.DataFrame) -> list[Candidate]:
    vwap, sigma = indicators.session_vwap(intraday)
    if math.isnan(vwap):
        return []
    out = [Candidate(vwap, "vwap")]
    if not math.isnan(sigma) and sigma > 0:
        out += [
            Candidate(vwap + sigma, "vwap_+1σ"),
            Candidate(vwap - sigma, "vwap_-1σ"),
            Candidate(vwap + 2 * sigma, "vwap_+2σ"),
            Candidate(vwap - 2 * sigma, "vwap_-2σ"),
        ]
    return out


def round_numbers(price: float, step: float, count: int = 4) -> list[Candidate]:
    """`count` круглых уровней сверху и снизу от текущей цены."""
    if step <= 0:
        return []
    base = math.floor(price / step) * step
    out: list[Candidate] = []
    for i in range(-count, count + 1):
        value = base + i * step
        if value > 0:
            out.append(Candidate(round(value, 12), "round"))
    return out


def swing_levels(
    intraday: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    lookback: int = SWING_LOOKBACK,
) -> list[Candidate]:
    recent = intraday.tail(lookback) if lookback else intraday
    highs, lows = indicators.swing_points(recent, left, right)
    return [Candidate(p, "swing_high") for p in highs] + [
        Candidate(p, "swing_low") for p in lows
    ]


def ema_levels(intraday: pd.DataFrame) -> list[Candidate]:
    out: list[Candidate] = []
    for period, kind in ((50, "ema50"), (200, "ema200")):
        series = indicators.ema(intraday["c"], period)
        if len(series) and not pd.isna(series.iloc[-1]):
            out.append(Candidate(float(series.iloc[-1]), kind))
    return out


# --------------------------------------------------------------------------- #
# Кластеризация и скоринг
# --------------------------------------------------------------------------- #


def cluster(candidates: list[Candidate], tolerance_pct: float = CLUSTER_TOLERANCE_PCT) -> list[Level]:
    """Склеивает близкие кандидаты в уровни.

    Кандидат присоединяется, если он ближе допуска к *среднему* кластера, а не
    к последнему добавленному: иначе цепочка из десятка почти-соседей уползает
    сколь угодно далеко от того места, где кластер начался.
    """
    usable = [c for c in candidates if c.price > 0 and math.isfinite(c.price)]
    if not usable:
        return []

    usable.sort(key=lambda c: c.price)

    levels: list[Level] = []
    members: list[Candidate] = [usable[0]]
    running_sum = usable[0].price

    def flush() -> None:
        mean = running_sum / len(members)
        kinds = sorted({m.kind for m in members}, key=lambda k: -KIND_WEIGHT.get(k, 0.0))
        levels.append(Level(price=mean, kinds=kinds))

    for candidate in usable[1:]:
        mean = running_sum / len(members)
        if abs(candidate.price - mean) <= mean * tolerance_pct:
            members.append(candidate)
            running_sum += candidate.price
        else:
            flush()
            members = [candidate]
            running_sum = candidate.price

    flush()
    return levels


def score_level(level: Level) -> float:
    kinds_part = SCORE_KINDS_WEIGHT * len(level.kinds)
    touches_part = SCORE_TOUCHES_WEIGHT * math.log1p(level.touches)
    weights_part = sum(KIND_WEIGHT.get(k, 0.3) for k in level.kinds)
    return round(kinds_part + touches_part + weights_part, 3)


def build_levels(
    intraday: pd.DataFrame,
    price: float,
    round_step: float,
    tolerance_pct: float = CLUSTER_TOLERANCE_PCT,
    relevance_pct: float = RELEVANCE_PCT,
    relevance_atr: float = RELEVANCE_ATR_MULT,
) -> list[Level]:
    """Полный расчёт уровней по H1-свечам.

    `intraday` — часовые свечи (желательно 500+, чтобы EMA200 и недельные
    экстремумы вообще существовали).

    Результат ограничен окном вокруг текущей цены: swing-уровень месячной
    давности в 17% отсюда для внутридневной торговли — шум, который сработать
    не может, но ранжирование и вывод /levels засоряет.
    """
    if intraday.empty:
        return []

    daily = indicators.resample_ohlcv(intraday, "1D")
    weekly = indicators.resample_ohlcv(intraday, "1W")

    candidates: list[Candidate] = []
    if len(daily) >= 2:
        prev = daily.iloc[-2]
        candidates += pivots_classic(float(prev["h"]), float(prev["l"]), float(prev["c"]))
    candidates += prev_session_extremes(daily, weekly)
    candidates += session_opens(daily, weekly)
    candidates += vwap_levels(intraday)
    candidates += round_numbers(price, round_step)
    candidates += swing_levels(intraday)
    candidates += ema_levels(intraday)

    levels = cluster(candidates, tolerance_pct)

    # Окно релевантности: в процентах или в ATR — что шире. Процент один на всех
    # не годится, потому что дневной ход BTC и дневной ход стейбл-пары
    # отличаются на порядок.
    atr_value = indicators.atr(intraday, 14)
    window = price * relevance_pct
    if math.isfinite(atr_value):
        window = max(window, atr_value * relevance_atr)
    window = min(window, price * MAX_RELEVANCE_PCT)

    levels = [lv for lv in levels if abs(lv.price - price) <= window]

    for level in levels:
        level.touches = indicators.count_touches(
            intraday, level.price, level.price * tolerance_pct
        )
        level.score = score_level(level)

    levels.sort(key=lambda lv: lv.price)
    return levels


def nearest_levels(levels: list[Level], price: float, count: int = 6) -> list[Level]:
    return sorted(levels, key=lambda lv: abs(lv.price - price))[:count]
