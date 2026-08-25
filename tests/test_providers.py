"""Тесты вывода шага круглых уровней и разбора precision из ccxt."""

from __future__ import annotations

import pytest

from alert_bot.market.providers.base import derive_round_step
from alert_bot.market.providers.ccxt_provider import _price_precision


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (77_000.0, 500.0),  # BTC
        (10_000.0, 500.0),  # граница
        (2_433.0, 100.0),  # ETH
        (145.0, 10.0),  # SOL
        (12.5, 1.0),
        (2.10, 0.1),
        (0.55, 0.01),
        (0.091, 0.001),  # DOGE
        (0.0042, 0.0001),
        (0.0009, 1e-5),
        (1.2e-8, 1e-9),  # SHIB-масштаб
    ],
)
def test_round_step_scales_with_price(price: float, expected: float) -> None:
    """Шаг круглых уровней должен жить в масштабе цены.

    Хардкод 500 на всё сделал бы круглые уровни для монеты за $0.09
    недостижимыми, а сам тип уровня — мёртвым.
    """
    assert derive_round_step(price) == expected


def test_round_step_is_monotonic() -> None:
    prices = [1e-8, 0.001, 0.05, 0.5, 5.0, 50.0, 500.0, 5_000.0, 50_000.0, 500_000.0]
    steps = [derive_round_step(p) for p in prices]
    assert steps == sorted(steps), "шаг обязан расти вместе с ценой"


@pytest.mark.parametrize(
    "price", [77_000.0, 500_000.0, 2_433.0, 145.0, 12.5, 2.1, 0.55, 0.09096, 0.0042, 1.2e-8]
)
def test_round_step_stays_in_sane_ratio(price: float) -> None:
    """Шаг обязан быть заметной, но не абсурдной долей цены.

    Ниже ~0.5% круглых уровней становится столько, что они перестают что-либо
    значить; выше ~10% ближайший круглый уровень оказывается недосягаем за день.
    """
    ratio = derive_round_step(price) / price
    assert 0.005 <= ratio <= 0.10, f"шаг {derive_round_step(price)} = {ratio:.1%} от цены {price}"


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ({"precision": {"price": 2}}, 2),  # DECIMAL_PLACES
        ({"precision": {"price": 8}}, 8),
        ({"precision": {"price": 0.01}}, 2),  # TICK_SIZE
        ({"precision": {"price": 0.00001}}, 5),
        ({"precision": {"price": 1.0}}, 0),  # целый шаг тика
        ({"precision": {"price": 10.0}}, 0),
        ({"precision": {}}, 2),  # нет данных — безопасный дефолт
        ({}, 2),
        ({"precision": {"price": 0}}, 0),
    ],
)
def test_price_precision_handles_both_ccxt_modes(market: dict, expected: int) -> None:
    """ccxt отдаёт precision в двух несовместимых видах в зависимости от биржи.

    Спутать их — значит форматировать цену BTC с пятью знаками или цену DOGE
    с двумя, то есть показать 0.09 вместо 0.09096.
    """
    assert _price_precision(market) == expected
