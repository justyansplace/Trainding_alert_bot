"""Единица измерения расстояния до уровня — одна на всех экранах.

Пользователь выбирает её один раз в «Настройке алертов», и дальше всё, что
показывает расстояние между уровнем и ценой, обязано считать в ней же: список
уровней, карточка уровня, экран настроек, текст алерта. Иначе выбор выглядит
непримененным — человек поставил проценты, а везде по-прежнему ×ATR, и числу
на экране нельзя верить: оно измерено не тем, чем задан порог.

Процент считается от цены УРОВНЯ, а не от текущей — ровно как в детекторе
(`detector.evaluate_level`), иначе показанное расстояние не сходилось бы с тем,
по которому реально срабатывает алерт.
"""

from __future__ import annotations

from alert_bot.config import get_settings
from alert_bot.db.models import ThresholdUnit, User


def unit_of(user: User | None) -> str:
    """Действующая единица: своя или дефолт конфига."""
    if user is not None and user.def_threshold_unit:
        return user.def_threshold_unit
    return get_settings().default_threshold_unit


def is_percent(unit: str) -> bool:
    return unit == ThresholdUnit.PERCENT.value


def pct_between(level_price: float, price: float) -> float:
    """Расстояние в процентах от цены уровня."""
    if level_price <= 0:
        return 0.0
    return abs(price - level_price) / level_price * 100


def format_distance(unit: str, distance_atr: float | None, distance_pct: float) -> str:
    """«0.36×ATR» или «0.32%» — смотря в чём мерит пользователь.

    Пустая строка, если считать не в чем: в режиме ATR у инструмента может не
    быть посчитанного ATR, и лучше не показать расстояние вовсе, чем показать
    его в единице, которую человек не выбирал.
    """
    if is_percent(unit):
        return f"{distance_pct:.2f}%"
    if distance_atr is None:
        return ""
    return f"{distance_atr:.2f}×ATR"


def gap_text(unit: str, level_price: float, price: float, atr: float | None) -> str:
    """Расстояние между уровнем и ценой по данным инструмента."""
    if is_percent(unit):
        return format_distance(unit, None, pct_between(level_price, price))
    if not atr:
        return ""
    return format_distance(unit, abs(price - level_price) / atr, 0.0)
