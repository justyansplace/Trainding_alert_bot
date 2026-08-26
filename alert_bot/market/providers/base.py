"""Абстракция источника рыночных данных.

Сейчас реализация одна — ccxt (крипта). Добавление акций/форекса позже сводится
к новому подклассу и строке в фабрике: таблицы, levels.py и detector.py не
меняются. Единственное, что придётся доработать под не-крипту — session_calendar:
для 24/7-рынка "дневная свеча" и "сутки" совпадают, для акций нет.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass(slots=True)
class SymbolMeta:
    """Что провайдер знает о символе на момент валидации."""

    symbol: str
    last_price: float
    price_precision: int
    volume_24h: float | None = None
    extra: dict = field(default_factory=dict)


class SymbolNotFound(Exception):
    """Символа нет у провайдера. Несёт список похожих для подсказки."""

    def __init__(self, symbol: str, suggestions: list[str] | None = None) -> None:
        self.symbol = symbol
        self.suggestions = suggestions or []
        super().__init__(f"символ {symbol!r} не найден")


class ExchangeBanned(Exception):
    """Площадка отказывает по частоте запросов с этого IP.

    Отдельный тип, а не общая ошибка сети: причина не в символе и не в
    доступности площадки, и говорить о ней человеку надо иначе — ждать, а не
    проверять тикер. Живёт здесь, а не в ccxt-провайдере: ограничивать частоту
    умеет любой источник, а знать про конкретный из них планировщику незачем.
    """

    def __init__(self, exchange: str, seconds_left: float) -> None:
        self.exchange = exchange
        self.seconds_left = max(0, int(seconds_left))
        self.minutes_left = max(1, round(self.seconds_left / 60))
        super().__init__(
            f"{exchange} ограничила частоту запросов с этого IP (418/429). "
            f"Запросы к ней возобновятся примерно через {self.minutes_left} мин."
        )


class DataProvider(ABC):
    name: str

    @abstractmethod
    async def validate_symbol(self, symbol: str) -> SymbolMeta:
        """Проверяет наличие символа. Бросает SymbolNotFound с подсказками."""

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, tf: str, limit: int = 500) -> pd.DataFrame:
        """Свечи как DataFrame с колонками [ts, o, h, l, c, v], ts — aware UTC."""

    @abstractmethod
    async def close(self) -> None: ...

    def is_24_7(self) -> bool:
        """Крипта торгуется без выходных. Для акций переопределяется."""
        return True

    def quote_delay_minutes(self, symbol: str) -> int:  # noqa: ARG002
        """На сколько минут котировка отстаёт от рынка.

        Ноль для источников реального времени. Ненулевое значение обязано
        доходить до человека: при пороге в доли ATR десятиминутная задержка
        означает, что алерт приходит после движения, а не до него.
        """
        return 0

    def is_market_open(self, now=None) -> bool:  # noqa: ANN001
        """Идут ли сейчас торги.

        Для круглосуточного рынка всегда да. Для форекса переопределяется:
        застывшую на выходных цену иначе легко принять за сбой источника.
        """
        return True


def derive_round_step(price: float) -> float:
    """Шаг круглых уровней по порядку цены.

    Хардкод 500 на все инструменты был бы бессмыслицей для монеты за $0.40:
    круглые числа должны попадать в тот же масштаб, что и дневной диапазон.

    Шаг = 10^(decade-1), что даёт 1–10% от цены и совпадает с тем, как трейдеры
    и говорят об уровнях: SOL по $10, ETH по $100, DOGE по $0.001. Выше $10k
    работает конвенция BTC — круглые уровни каждые 500, а не 1000.

    Лестница обязана быть вычисляемой, а не набором ветвей с полом внизу: токены
    торгуются и на 1e-8, где любой фиксированный минимум превращает шаг в десятки
    процентов от цены.
    """
    if price <= 0:
        raise ValueError(f"цена должна быть положительной, получено {price!r}")

    decade = math.floor(math.log10(price))
    if decade >= 4:
        return 5 * 10.0 ** (decade - 2)
    return 10.0 ** (decade - 1)


_REGISTRY: dict[str, type[DataProvider]] = {}


def register_provider(cls: type[DataProvider]) -> type[DataProvider]:
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str, **kwargs) -> DataProvider:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"неизвестный провайдер {name!r}; доступны: {sorted(_REGISTRY)}"
        ) from None
    return cls(**kwargs)  # type: ignore[call-arg]
