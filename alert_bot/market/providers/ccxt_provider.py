"""Провайдер крипто-данных поверх ccxt.

Инстанс биржи — один на всех инструментов этой биржи (ccxt сам держит троттлер
на инстансе, поэтому несколько инстансов = обход собственного rate limit и путь
к бану по IP). Сверху общий семафор на число одновременных запросов.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import math

import ccxt.async_support as ccxt
import pandas as pd

from alert_bot.market.providers.base import (
    DataProvider,
    SymbolMeta,
    SymbolNotFound,
    register_provider,
)

log = logging.getLogger(__name__)

_MAX_CONCURRENT_REQUESTS = 5
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
_exchanges: dict[str, ccxt.Exchange] = {}
_markets_loaded: set[str] = set()
_load_lock = asyncio.Lock()


async def _get_exchange(name: str) -> ccxt.Exchange:
    if name not in _exchanges:
        try:
            cls = getattr(ccxt, name)
        except AttributeError:
            raise ValueError(f"ccxt не знает биржу {name!r}") from None
        _exchanges[name] = cls({"enableRateLimit": True, "timeout": 15_000})
    return _exchanges[name]


async def _ensure_markets(exchange: ccxt.Exchange, name: str) -> None:
    if name in _markets_loaded:
        return
    async with _load_lock:
        if name in _markets_loaded:
            return
        await exchange.load_markets()
        _markets_loaded.add(name)


def _price_precision(market: dict) -> int:
    """Число знаков после запятой в цене.

    ccxt отдаёт precision.price в двух несовместимых видах в зависимости от
    precisionMode биржи: DECIMAL_PLACES — это число знаков, TICK_SIZE — сам шаг
    (0.01). Различаем по типу и величине.
    """
    raw = market.get("precision", {}).get("price")

    if isinstance(raw, int):
        return max(0, min(raw, 12))

    if isinstance(raw, float):
        if raw <= 0:
            return 2
        if raw.is_integer() and raw >= 1:
            # Шаг тика вида 1.0 / 10.0 — знаков после запятой нет.
            return 0
        return max(0, min(int(round(-math.log10(raw))), 12))

    return 2


async def close_all_exchanges() -> None:
    for name, exchange in list(_exchanges.items()):
        try:
            await exchange.close()
        except Exception:  # noqa: BLE001 — на shutdown важно закрыть остальные
            log.warning("не удалось закрыть биржу %s", name, exc_info=True)
    _exchanges.clear()
    _markets_loaded.clear()


@register_provider
class CcxtProvider(DataProvider):
    name = "ccxt"

    def __init__(self, exchange: str = "binance") -> None:
        self.exchange_name = exchange

    async def _exchange(self) -> ccxt.Exchange:
        ex = await _get_exchange(self.exchange_name)
        await _ensure_markets(ex, self.exchange_name)
        return ex

    async def validate_symbol(self, symbol: str) -> SymbolMeta:
        ex = await self._exchange()
        symbol = symbol.upper().strip()

        if symbol not in ex.markets:
            suggestions = difflib.get_close_matches(symbol, list(ex.markets), n=5, cutoff=0.6)
            raise SymbolNotFound(symbol, suggestions)

        market = ex.markets[symbol]
        async with _semaphore:
            ticker = await ex.fetch_ticker(symbol)

        last = ticker.get("last") or ticker.get("close")
        if last is None:
            raise SymbolNotFound(symbol, [])

        return SymbolMeta(
            symbol=symbol,
            last_price=float(last),
            price_precision=_price_precision(market),
            volume_24h=ticker.get("quoteVolume"),
            extra={"base": market.get("base"), "quote": market.get("quote")},
        )

    async def fetch_ohlcv(self, symbol: str, tf: str, limit: int = 500) -> pd.DataFrame:
        ex = await self._exchange()
        async with _semaphore:
            rows = await ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)

        df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    async def close(self) -> None:
        # Инстансы общие на процесс — закрываются через close_all_exchanges().
        return None
