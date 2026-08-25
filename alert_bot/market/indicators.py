"""Технические индикаторы.

Чистые функции над DataFrame со столбцами [ts, o, h, l, c, v]. Никаких обращений
к БД и сети — именно поэтому detector и levels можно прогнать по историческим
свечам в scripts/replay.py тем же кодом, что работает в проде.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ("ts", "o", "h", "l", "c", "v")


def _require_columns(df: pd.DataFrame) -> None:
    missing = set(OHLCV_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"в DataFrame нет колонок: {sorted(missing)}")


def true_range(df: pd.DataFrame) -> pd.Series:
    _require_columns(df)
    prev_close = df["c"].shift(1)
    return pd.concat(
        [df["h"] - df["l"], (df["h"] - prev_close).abs(), (df["l"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR по Уайлдеру (RMA), а не простое среднее.

    Разница существенна: SMA-версия скачет при выпадении старой свечи из окна,
    и порог 0.3×ATR вместе с ней — алерт срабатывал бы от смены окна, а не от цены.
    """
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Последнее значение ATR. NaN, если данных меньше периода."""
    series = atr_series(df, period)
    return float(series.iloc[-1]) if len(series) and not pd.isna(series.iloc[-1]) else float("nan")


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def session_vwap(df: pd.DataFrame, session: str = "D") -> tuple[float, float]:
    """VWAP текущей сессии и волатильность вокруг него.

    Возвращает (vwap, sigma), где sigma — взвешенное по объёму стандартное
    отклонение типичной цены. NaN-пара, если в сессии нет объёма.
    """
    _require_columns(df)
    if df.empty:
        return float("nan"), float("nan")

    ts = pd.to_datetime(df["ts"], utc=True)
    current = ts.dt.floor(session).iloc[-1]
    mask = ts.dt.floor(session) == current
    window = df.loc[mask]

    typical = (window["h"] + window["l"] + window["c"]) / 3
    volume = window["v"]
    total = float(volume.sum())
    if total <= 0:
        return float("nan"), float("nan")

    vwap = float((typical * volume).sum() / total)
    variance = float((volume * (typical - vwap) ** 2).sum() / total)
    return vwap, float(np.sqrt(max(variance, 0.0)))


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> tuple[list[float], list[float]]:
    """Фракталы Уильямса: (максимумы, минимумы).

    Экстремум подтверждён, только если он крайний в окне [i-left, i+right] —
    поэтому последние `right` свечей заведомо не дают точек, и это правильно:
    неподтверждённый экстремум ещё может быть переписан.
    """
    _require_columns(df)
    if len(df) < left + right + 1:
        return [], []

    highs, lows = [], []
    h = df["h"].to_numpy()
    low = df["l"].to_numpy()

    for i in range(left, len(df) - right):
        window_h = h[i - left : i + right + 1]
        window_l = low[i - left : i + right + 1]
        if h[i] == window_h.max() and (window_h == h[i]).sum() == 1:
            highs.append(float(h[i]))
        if low[i] == window_l.min() and (window_l == low[i]).sum() == 1:
            lows.append(float(low[i]))

    return highs, lows


def count_touches(df: pd.DataFrame, price: float, tolerance: float) -> int:
    """Сколько раз цена *подходила* к коридору [price-tolerance, price+tolerance].

    Считаются отдельные визиты, а не свечи: подряд идущие свечи внутри коридора —
    это одно касание. Иначе зона, в которой цена простояла неделю, набирает сотню
    "касаний" и выносит любой протухший боковик выше свежего конфлюэнса, потому
    что log1p(130) вдвое больше log1p(5).

    Число касаний входит в скоринг: уровень, который рынок проверял пять раз,
    весомее свежепосчитанного пивота, которого цена ещё не видела.
    """
    _require_columns(df)
    if tolerance <= 0 or df.empty:
        return 0

    lo, hi = price - tolerance, price + tolerance
    inside = (df["l"] <= hi) & (df["h"] >= lo)
    # Новый визит — переход False -> True.
    return int((inside & ~inside.shift(1, fill_value=False)).sum())


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Пересборка свечей в больший таймфрейм (H1 -> D и т.п.)."""
    _require_columns(df)
    if df.empty:
        return df.copy()

    indexed = df.copy()
    indexed["ts"] = pd.to_datetime(indexed["ts"], utc=True)
    indexed = indexed.set_index("ts")

    out = indexed.resample(rule, label="left", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    )
    return out.dropna(subset=["o"]).reset_index()
