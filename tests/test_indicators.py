"""Тесты индикаторов — на тихие искажения, которые не падают, а врут."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alert_bot.market import indicators


def make_df(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """rows: (o, h, l, c, v) — ts проставляется как последовательные часы UTC."""
    ts = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "ts": ts,
            "o": [r[0] for r in rows],
            "h": [r[1] for r in rows],
            "l": [r[2] for r in rows],
            "c": [r[3] for r in rows],
            "v": [r[4] for r in rows],
        }
    )


def flat_df(n: int, price: float = 100.0, spread: float = 1.0, volume: float = 10.0):
    return make_df([(price, price + spread, price - spread, price, volume)] * n)


# --------------------------------------------------------------------------- #
# Касания
# --------------------------------------------------------------------------- #


def test_consecutive_candles_are_one_touch() -> None:
    """Главный тест скоринга.

    Если считать свечи, а не визиты, то боковик даёт сотню "касаний", log1p(100)
    вдвое больше log1p(5), и любая протухшая зона консолидации выносится выше
    свежего конфлюэнса из трёх типов уровней.
    """
    rows = [(100, 101, 99, 100, 1.0)] * 10  # десять свечей внутри коридора
    df = make_df(rows)
    assert indicators.count_touches(df, price=100.0, tolerance=2.0) == 1


def test_separate_visits_are_counted_separately() -> None:
    inside = (100, 101, 99, 100, 1.0)
    outside = (150, 151, 149, 150, 1.0)
    df = make_df([inside, inside, outside, outside, inside, outside, inside])
    assert indicators.count_touches(df, price=100.0, tolerance=2.0) == 3


def test_touch_requires_range_overlap_not_close_proximity() -> None:
    df = make_df([(200, 205, 195, 200, 1.0)])  # диапазон 195..205 накрывает 100? нет
    assert indicators.count_touches(df, price=100.0, tolerance=2.0) == 0
    # А свеча, чей фитиль дотянулся до коридора, засчитывается.
    df2 = make_df([(200, 205, 101, 200, 1.0)])
    assert indicators.count_touches(df2, price=100.0, tolerance=2.0) == 1


def test_zero_tolerance_gives_no_touches() -> None:
    assert indicators.count_touches(flat_df(5), price=100.0, tolerance=0.0) == 0


def test_touches_on_empty_frame() -> None:
    empty = make_df([])
    assert indicators.count_touches(empty, price=100.0, tolerance=1.0) == 0


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #


def test_atr_on_constant_range_equals_that_range() -> None:
    df = flat_df(50, price=100.0, spread=1.0)  # TR всегда 2.0
    assert indicators.atr(df, 14) == pytest.approx(2.0, rel=1e-6)


def test_atr_is_nan_when_history_too_short() -> None:
    assert np.isnan(indicators.atr(flat_df(5), 14))


def test_single_spike_moves_atr_by_one_period_fraction() -> None:
    """Выброс сдвигает RMA на 1/period своей величины, а не заменяет собой ATR."""
    rows = [(100, 101, 99, 100, 1.0)] * 30 + [(100, 140, 60, 100, 1.0)]
    assert indicators.atr(make_df(rows), 14) == pytest.approx(2.0 + (80.0 - 2.0) / 14, rel=1e-3)


def test_atr_decays_smoothly_instead_of_dropping_when_spike_leaves_window() -> None:
    """RMA против SMA — не косметика.

    У SMA выброс выпадает из окна ровно через `period` свечей, и ATR обваливается
    скачком. Порог 0.3×ATR прыгает вместе с ним, то есть алерт срабатывает от
    смены окна, а не от движения цены. RMA затухает плавно и такого края не имеет.
    """
    spike_at = 30
    period = 14
    base = [(100, 101, 99, 100, 1.0)]  # TR = 2
    spike = [(100, 140, 60, 100, 1.0)]  # TR = 80

    rows = base * spike_at + spike + base * (period + 2)
    tr = indicators.true_range(make_df(rows))
    atr = indicators.atr_series(make_df(rows), period)

    # Момент, когда выброс покидает SMA-окно.
    before = spike_at + period - 1
    after = spike_at + period

    sma_before = float(tr.iloc[before - period + 1 : before + 1].mean())
    sma_after = float(tr.iloc[after - period + 1 : after + 1].mean())
    assert sma_before / sma_after > 3, "SMA обязана обвалиться — это и есть её дефект"

    rma_before, rma_after = float(atr.iloc[before]), float(atr.iloc[after])
    assert rma_before / rma_after < 1.1, "RMA не должна прыгать на выходе выброса из окна"
    assert rma_after > sma_after, "RMA ещё помнит выброс, SMA уже нет"


def test_true_range_accounts_for_gap_through_prev_close() -> None:
    df = make_df([(100, 101, 99, 100, 1.0), (120, 121, 119, 120, 1.0)])
    # Диапазон второй свечи 2, но разрыв от закрытия 100 до максимума 121 = 21.
    assert float(indicators.true_range(df).iloc[-1]) == pytest.approx(21.0)


# --------------------------------------------------------------------------- #
# VWAP
# --------------------------------------------------------------------------- #


def test_vwap_covers_only_current_session() -> None:
    """VWAP внутридневной: вчерашние свечи в него попадать не должны."""
    yesterday = pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")
    today = pd.date_range("2026-01-02", periods=5, freq="h", tz="UTC")

    df = pd.DataFrame(
        {
            "ts": list(yesterday) + list(today),
            "o": [10.0] * 5 + [100.0] * 5,
            "h": [10.0] * 5 + [100.0] * 5,
            "l": [10.0] * 5 + [100.0] * 5,
            "c": [10.0] * 5 + [100.0] * 5,
            "v": [1.0] * 10,
        }
    )

    vwap, sigma = indicators.session_vwap(df)
    assert vwap == pytest.approx(100.0), "вчерашняя цена 10 не должна тянуть VWAP вниз"
    assert sigma == pytest.approx(0.0)


def test_vwap_is_volume_weighted() -> None:
    df = make_df([(10, 10, 10, 10, 1.0), (20, 20, 20, 20, 9.0)])
    vwap, _ = indicators.session_vwap(df)
    assert vwap == pytest.approx(19.0)


def test_vwap_nan_without_volume() -> None:
    df = make_df([(10, 10, 10, 10, 0.0)])
    vwap, sigma = indicators.session_vwap(df)
    assert np.isnan(vwap) and np.isnan(sigma)


# --------------------------------------------------------------------------- #
# Фракталы и ресемплинг
# --------------------------------------------------------------------------- #


def test_swing_points_need_confirmation_on_the_right() -> None:
    """Последние `right` свечей не дают точек — экстремум ещё может быть переписан."""
    rows = [(100, 100 + h, 100 - h, 100, 1.0) for h in [1, 2, 9, 2, 1, 1, 1]]
    highs, lows = indicators.swing_points(make_df(rows), left=2, right=2)
    assert highs == [109.0]

    truncated = make_df(rows[:4])  # тот же пик, но подтверждения справа нет
    assert indicators.swing_points(truncated, left=2, right=2)[0] == []


def test_swing_points_ignore_plateau() -> None:
    """Два одинаковых максимума подряд — не фрактал, а полка."""
    rows = [(100, 101, 99, 100, 1.0)] * 2 + [(100, 105, 99, 100, 1.0)] * 2 + [
        (100, 101, 99, 100, 1.0)
    ] * 2
    highs, _ = indicators.swing_points(make_df(rows), left=2, right=2)
    assert highs == []


def test_resample_aggregates_ohlcv_correctly() -> None:
    rows = [(10, 15, 5, 12, 1.0), (12, 20, 11, 18, 2.0)]
    df = make_df(rows)
    out = indicators.resample_ohlcv(df, "1D")

    assert len(out) == 1
    assert out.iloc[0]["o"] == 10  # первое открытие
    assert out.iloc[0]["h"] == 20  # максимум максимумов
    assert out.iloc[0]["l"] == 5  # минимум минимумов
    assert out.iloc[0]["c"] == 18  # последнее закрытие
    assert out.iloc[0]["v"] == 3.0  # сумма объёмов


def test_missing_columns_fail_loudly() -> None:
    with pytest.raises(ValueError, match="нет колонок"):
        indicators.atr(pd.DataFrame({"ts": [], "c": []}))
