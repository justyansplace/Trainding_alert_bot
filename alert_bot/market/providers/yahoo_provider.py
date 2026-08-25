"""Провайдер котировок Yahoo Finance.

Закрывает то, чего нет на крипто-бирже: валютные пары, металлы, нефть. Ключа и
регистрации не требует, поэтому годится там, где заводить брокерский счёт ради
одних только цен не хочется.

Две особенности, о которых обязан знать пользователь:

  * **Задержка по сырью.** Валютные пары приходят в реальном времени, а золото,
    серебро и нефть — с отставанием около десяти минут. Для порога в 0.3×ATR
    это означает, что алерт по Brent придёт уже после движения. Задержка
    хранится в свойстве quote_delay_minutes и показывается при добавлении
    инструмента: пусть человек решает сам, а не обнаруживает это по факту.

  * **Это неофициальный источник.** Yahoo не обещает стабильности формата, и
    библиотека может перестать работать после их изменений. Для боевых денег
    стоит брокерский API; здесь это осознанный размен на отсутствие регистрации.

Металлы и нефть отдаются фьючерсами (GC=F, BZ=F), а не спотом: спотовых тикеров
у Yahoo нет — XAUUSD=X и XAU=X пустые. Фьючерс идёт с премией к споту, обычно
небольшой, но уровни всё равно стоит ставить по тому графику, на который смотрит
бот, а не по споту у брокера.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from alert_bot.market.providers.base import (
    DataProvider,
    SymbolMeta,
    SymbolNotFound,
    register_provider,
)

log = logging.getLogger(__name__)

# Понятное имя -> тикер Yahoo. Слева то, что пишет человек, справа внутреннее.
SYMBOL_MAP: dict[str, str] = {
    # Валютные пары
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "AUD/USD": "AUDUSD=X",
    "USD/AUD": "USDAUD=X",
    "NZD/USD": "NZDUSD=X",
    "USD/CAD": "USDCAD=X",
    # Обратная котировка того же рынка. Держим обе: на торговой платформе пара
    # почти всегда называется USD/CAD, но кто-то мыслит в CAD/USD, и молча
    # подменять направление нельзя — уровень окажется перевёрнутым.
    "CAD/USD": "CADUSD=X",
    "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X",
    "EUR/GBP": "EURGBP=X",
    "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X",
    "AUD/JPY": "AUDJPY=X",
    "AUD/CAD": "AUDCAD=X",
    "CHF/USD": "CHFUSD=X",
    "JPY/USD": "JPYUSD=X",
    # Металлы и сырьё — фьючерсы, спота у Yahoo нет
    "XAU/USD": "GC=F",
    "GOLD": "GC=F",
    "XAG/USD": "SI=F",
    "SILVER": "SI=F",
    "BRENT": "BZ=F",
    "BCO/USD": "BZ=F",
    "WTI": "CL=F",
    "COPPER": "HG=F",
    "NATGAS": "NG=F",
    "USD/CNH": "USDCNH=X",
    # Индексы
    "SPX500": "^GSPC",
    "NAS100": "^NDX",
    "US30": "^DJI",
    "DE40": "^GDAXI",
}

# Синонимы: у каждой платформы свои названия одного и того же инструмента.
# Слева то, как оно подписано в терминале, справа — наше каноническое имя.
ALIASES: dict[str, str] = {
    "US500": "SPX500",
    "SPX": "SPX500",
    "USTEC": "NAS100",
    "NAS": "NAS100",
    "NDX": "NAS100",
    "DJI": "US30",
    "DAX": "DE40",
    "DE30": "DE40",
    "GOLD": "XAU/USD",
    "SILVER": "XAG/USD",
    "UKOIL": "BRENT",
    "USOIL": "WTI",
    "BCO": "BRENT",
    "XTIUSD": "WTI",
    "XBRUSD": "BRENT",
}

# Обратная карта — чтобы показывать человеку понятное имя.
_REVERSE = {}
for _human, _ticker in SYMBOL_MAP.items():
    _REVERSE.setdefault(_ticker, _human)

# Тикеры, приходящие с задержкой. Валюты идут в реальном времени, биржевые
# инструменты — нет.
_DELAYED_SUFFIXES = ("=F", "^")
QUOTE_DELAY_MINUTES = 10

INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}

# Yahoo не публикует лимитов, но долбить его каждые десять секунд — верный
# способ получить бан по адресу. Ответ переиспользуется в пределах окна.
_CACHE_TTL_SECONDS = 25.0
_cache: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}

_NY = ZoneInfo("America/New_York")


def normalize_symbol(symbol: str) -> str:
    """USD/CAD, usd-cad, USDCAD, USTEC -> каноническое имя.

    Люди переносят названия прямо из своего терминала, а там одно и то же
    зовётся по-разному: US500 против SPX500, USTEC против NAS100.
    """
    raw = symbol.strip().upper().replace("_", "/").replace("-", "/")
    if raw in SYMBOL_MAP:
        return raw
    if raw in ALIASES:
        return ALIASES[raw]

    # Человек мог написать без разделителя: USDCAD.
    if "/" not in raw and len(raw) == 6:
        candidate = f"{raw[:3]}/{raw[3:]}"
        if candidate in SYMBOL_MAP:
            return candidate
        if candidate in ALIASES:
            return ALIASES[candidate]
    return raw


def to_ticker(symbol: str) -> str | None:
    return SYMBOL_MAP.get(normalize_symbol(symbol))


# Yahoo отдаёт цены как float32. При переводе в float64 вылезает мусор в
# хвосте: 1.386 превращается в 1.3860000371932983. На расчёты это не влияет,
# но такие числа попадают в базу и в сравнения уровней, поэтому хвост срезаем.
_SIGNIFICANT_DIGITS = 7  # ровно столько значащих цифр несёт float32


def _clean(value: float) -> float:
    return float(f"{float(value):.{_SIGNIFICANT_DIGITS}g}")


def is_delayed(ticker: str) -> bool:
    return ticker.endswith(_DELAYED_SUFFIXES) or ticker.startswith("^")


async def _download(ticker: str, interval: str, limit: int) -> pd.DataFrame:
    """Загрузка в отдельном потоке: yfinance синхронный."""
    key = (ticker, interval, limit)
    now = time.monotonic()

    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    def _fetch() -> pd.DataFrame:
        import yfinance as yf

        period = _period_for(interval, limit)
        return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)

    frame = await asyncio.to_thread(_fetch)
    _cache[key] = (now, frame)
    return frame


def _period_for(interval: str, limit: int) -> str:
    """Сколько истории просить, чтобы набралось `limit` свечей.

    У Yahoo для минутных данных жёсткие ограничения по глубине, поэтому период
    подбирается под интервал, а не берётся с запасом.
    """
    if interval == "1m":
        return "7d"
    if interval in ("5m", "15m"):
        return "60d"
    if interval == "1h":
        days = max(3, min(int(limit / 24) + 3, 730))
        return f"{days}d"
    return "2y"


@register_provider
class YahooProvider(DataProvider):
    name = "yahoo"

    def __init__(self, exchange: str = "yahoo") -> None:
        self.exchange = exchange

    def is_24_7(self) -> bool:
        return False

    def is_market_open(self, now: datetime | None = None) -> bool:
        """Выходные. Валюты и фьючерсы стоят с вечера пятницы до вечера воскресенья.

        Считается в нью-йоркской зоне: граница ездит вместе с переходом на
        летнее время, и в UTC она не фиксирована.
        """
        now = (now or datetime.now(UTC)).astimezone(_NY)
        weekday = now.weekday()
        if weekday == 5:
            return False
        if weekday == 6:
            return now.hour >= 17
        if weekday == 4:
            return now.hour < 17
        return True

    def quote_delay_minutes(self, symbol: str) -> int:
        ticker = to_ticker(symbol)
        return QUOTE_DELAY_MINUTES if ticker and is_delayed(ticker) else 0

    async def validate_symbol(self, symbol: str) -> SymbolMeta:
        normalized = normalize_symbol(symbol)
        ticker = SYMBOL_MAP.get(normalized)

        if ticker is None:
            import difflib

            raise SymbolNotFound(
                normalized,
                difflib.get_close_matches(normalized, list(SYMBOL_MAP), n=5, cutoff=0.4),
            )

        frame = await _download(ticker, "1h", 5)
        if frame.empty:
            raise SymbolNotFound(normalized, [])

        price = _clean(frame["Close"].iloc[-1])

        # Знаки после запятой — по величине котировки: у валютных пар их
        # четыре-пять, у нефти два.
        precision = 5 if price < 10 else (4 if price < 100 else 2)

        return SymbolMeta(
            symbol=normalized,
            last_price=price,
            price_precision=precision,
            volume_24h=None,
            extra={
                "yahoo_ticker": ticker,
                "delay_minutes": QUOTE_DELAY_MINUTES if is_delayed(ticker) else 0,
            },
        )

    async def fetch_ohlcv(self, symbol: str, tf: str, limit: int = 200) -> pd.DataFrame:
        interval = INTERVALS.get(tf)
        if interval is None:
            raise ValueError(f"Yahoo не знает таймфрейм {tf!r}")

        ticker = to_ticker(symbol)
        if ticker is None:
            raise SymbolNotFound(symbol, [])

        frame = await _download(ticker, interval, limit)
        if frame.empty:
            return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "v"])

        out = pd.DataFrame(
            {
                "ts": frame.index.tz_convert("UTC"),
                "o": frame["Open"].astype(float).map(_clean),
                "h": frame["High"].astype(float).map(_clean),
                "l": frame["Low"].astype(float).map(_clean),
                "c": frame["Close"].astype(float).map(_clean),
                "v": frame["Volume"].astype(float),
            }
        ).reset_index(drop=True)

        # У валютных пар объём приходит нулевым — детектор пробоя на нём
        # работать не сможет, но подход к уровню считается без объёма.
        return out.dropna(subset=["o", "h", "l", "c"]).tail(limit).reset_index(drop=True)

    async def close(self) -> None:
        return None
