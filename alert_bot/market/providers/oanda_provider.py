"""Провайдер котировок OANDA v20.

Зачем он появился. Крипто-биржа отдаёт крипту прекрасно, но форекс и металлы
там существуют только суррогатами: EUR/USDT — это евро к стейблкоину на рынке
в сто раз тоньше настоящего, а XAUT — токен на золото со своей премией. На
живых данных EUR/USDT разошёлся с реальным EUR/USD на 1.1×ATR при пороге
срабатывания 0.3×ATR, то есть алерт приходил туда, где на торговой платформе
цены нет.

OANDA — брокер с документированным REST API и бесплатным демо-счётом, дающим
живые котировки. Это те же межбанковские цены, что и у любого другого брокера,
поэтому расхождение с площадкой, где вы торгуете, падает до размера спреда.

Главное отличие от крипты — рынок не круглосуточный, и это не мелочь: см.
is_market_open ниже.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import aiohttp
import pandas as pd

from alert_bot.config import get_settings
from alert_bot.market.providers.base import (
    DataProvider,
    SymbolMeta,
    SymbolNotFound,
    register_provider,
)

log = logging.getLogger(__name__)

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

# Наши таймфреймы -> гранулярность OANDA.
GRANULARITY = {"1m": "M1", "5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "1d": "D"}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
_MAX_CONCURRENT = 5
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
_session: aiohttp.ClientSession | None = None

# Форекс торгуется с вечера воскресенья до вечера пятницы по Нью-Йорку.
# Считать надо именно в нью-йоркской зоне: граница ездит вместе с переходом
# на летнее время, и в UTC она не фиксирована.
_NY = ZoneInfo("America/New_York")
_SESSION_OPEN_HOUR = 17  # воскресенье, 17:00 NY
_SESSION_CLOSE_HOUR = 17  # пятница, 17:00 NY


def normalize_symbol(symbol: str) -> str:
    """EUR/USD, eur-usd, EUR_USD -> EUR_USD."""
    return symbol.strip().upper().replace("/", "_").replace("-", "_")


def display_symbol(symbol: str) -> str:
    """EUR_USD -> EUR/USD, чтобы вид совпадал с крипто-инструментами."""
    return normalize_symbol(symbol).replace("_", "/")


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


@register_provider
class OandaProvider(DataProvider):
    name = "oanda"

    def __init__(self, exchange: str = "oanda") -> None:
        settings = get_settings()
        self.environment = settings.oanda_environment
        self.token = settings.oanda_api_token
        self.host = HOSTS.get(self.environment, HOSTS["practice"])

    # ----------------------------------------------------------------- #
    # Торговые часы
    # ----------------------------------------------------------------- #

    def is_24_7(self) -> bool:
        return False

    def is_market_open(self, now: datetime | None = None) -> bool:
        """Открыт ли рынок сейчас.

        Пока рынок закрыт, опрашивать нечего: цена не меняется, а детектор,
        сравнивая одно и то же значение, всё равно ничего не выдаст. Хуже, что
        застывшую цену легко принять за сбой источника и записать инструменту
        ошибку. Поэтому выходные распознаются явно.
        """
        now = (now or datetime.now(UTC)).astimezone(_NY)
        weekday = now.weekday()  # понедельник = 0, воскресенье = 6

        if weekday == 5:  # суббота
            return False
        if weekday == 6:  # воскресенье — открытие вечером
            return now.hour >= _SESSION_OPEN_HOUR
        if weekday == 4:  # пятница — закрытие вечером
            return now.hour < _SESSION_CLOSE_HOUR
        return True

    # ----------------------------------------------------------------- #
    # Запросы
    # ----------------------------------------------------------------- #

    async def _request(self, path: str, params: dict) -> dict:
        if not self.token:
            raise ValueError("OANDA_API_TOKEN не задан")

        session = await _get_session()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Datetime-Format": "RFC3339",
        }

        async with _semaphore:
            async with session.get(
                f"{self.host}{path}", params=params, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status in (400, 404):
                    raise SymbolNotFound(path, [])
                if response.status == 401:
                    raise ValueError("OANDA отклонила токен (401)")
                if response.status != 200:
                    raise RuntimeError(f"OANDA вернула HTTP {response.status}")
                return await response.json()

    async def _candles(self, symbol: str, granularity: str, count: int) -> list[dict]:
        payload = await self._request(
            f"/v3/instruments/{normalize_symbol(symbol)}/candles",
            {"granularity": granularity, "count": str(count), "price": "M"},
        )
        return payload.get("candles", [])

    async def validate_symbol(self, symbol: str) -> SymbolMeta:
        normalized = normalize_symbol(symbol)
        try:
            candles = await self._candles(normalized, "H1", 2)
        except SymbolNotFound:
            raise SymbolNotFound(display_symbol(symbol), self._suggest(normalized)) from None

        if not candles:
            raise SymbolNotFound(display_symbol(symbol), [])

        last = candles[-1]["mid"]
        price = float(last["c"])

        # Знаки после запятой берём из самой котировки: у EUR_USD их пять,
        # у XAU_USD — три, и угадывать по величине цены здесь нельзя.
        precision = len(str(last["c"]).partition(".")[2])

        return SymbolMeta(
            symbol=display_symbol(normalized),
            last_price=price,
            price_precision=precision or 2,
            volume_24h=None,  # у форекса нет объёма в привычном смысле
            extra={"oanda_symbol": normalized, "environment": self.environment},
        )

    @staticmethod
    def _suggest(symbol: str) -> list[str]:
        """Подсказки без обращения к API — по популярным инструментам."""
        common = [
            "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD",
            "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY",
            "XAU_USD", "XAG_USD", "WTICO_USD", "BCO_USD",
            "SPX500_USD", "NAS100_USD", "US30_USD", "DE30_EUR",
        ]
        import difflib

        return [display_symbol(s) for s in difflib.get_close_matches(symbol, common, n=5, cutoff=0.5)]

    async def fetch_ohlcv(self, symbol: str, tf: str, limit: int = 200) -> pd.DataFrame:
        granularity = GRANULARITY.get(tf)
        if granularity is None:
            raise ValueError(f"OANDA не знает таймфрейм {tf!r}")

        candles = await self._candles(symbol, granularity, limit)
        rows = [
            {
                "ts": pd.Timestamp(c["time"]).tz_convert("UTC"),
                "o": float(c["mid"]["o"]),
                "h": float(c["mid"]["h"]),
                "l": float(c["mid"]["l"]),
                "c": float(c["mid"]["c"]),
                # У форекса объём тиковый, не денежный. Для наших расчётов
                # он используется только в детекторе пробоя, где важна
                # относительная величина, так что тиковый подходит.
                "v": float(c.get("volume", 0)),
            }
            for c in candles
        ]

        if not rows:
            return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "v"])

        return pd.DataFrame(rows)

    async def close(self) -> None:
        # Сессия общая на процесс — закрывается через close_session().
        return None
