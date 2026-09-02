"""Провайдер крипто-данных поверх ccxt.

Инстанс биржи — один на всех инструментов этой биржи (ccxt сам держит троттлер
на инстансе, поэтому несколько инстансов = обход собственного rate limit и путь
к бану по IP). Сверху общий семафор на число одновременных запросов.

Про бан по IP. Binance отвечает 418 после того, как ответы 429 были
проигнорированы, и бан у неё нарастающий: от двух минут до трёх суток, причём
каждый запрос во время бана продлевает его. Поэтому 418/429 здесь означает не
«повторить позже», а «прекратить ходить на эту площадку совсем» — до конца
паузы запросы даже не уходят в сеть. Иначе цикл цены с его тиком в десять
секунд держал бы бан бесконечно, а причину было бы не отличить от поломки
символа: пользователь видит «не вышло добавить BTC», хотя дело не в BTC.

Бан общий на IP, а не на ключ или символ, поэтому и пауза общая на площадку.
На арендованном хостинге адрес разделяется с чужими сервисами, так что бан
может прилететь и без вины бота — тем более важно его пережидать, а не
разгонять.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import math
import time

import ccxt.async_support as ccxt
import pandas as pd
from ccxt.base.decimal_to_precision import SIGNIFICANT_DIGITS

from alert_bot.market.providers.base import (
    DataProvider,
    ExchangeBanned,
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

# Сколько ждать после отказа по частоте. Шаги растут, потому что растёт и сам
# бан у площадки: повторное нарушение стоит дороже первого.
_BACKOFF_SECONDS = (120, 300, 900, 1800, 3600)

_banned_until: dict[str, float] = {}
_ban_level: dict[str, int] = {}


def ban_seconds_left(exchange: str) -> float:
    """Сколько осталось до конца паузы. Ноль — паузы нет."""
    return max(0.0, _banned_until.get(exchange, 0.0) - time.monotonic())


def _raise_if_banned(exchange: str) -> None:
    left = ban_seconds_left(exchange)
    if left > 0:
        raise ExchangeBanned(exchange, left)


def _register_ban(exchange: str) -> ExchangeBanned:
    level = _ban_level.get(exchange, 0)
    delay = _BACKOFF_SECONDS[min(level, len(_BACKOFF_SECONDS) - 1)]
    _ban_level[exchange] = level + 1
    _banned_until[exchange] = time.monotonic() + delay
    log.warning(
        "%s ответила отказом по частоте запросов; пауза %s c (нарушение №%s)",
        exchange, delay, level + 1,
    )
    return ExchangeBanned(exchange, delay)


def _clear_ban(exchange: str) -> None:
    """Успешный ответ снимает эскалацию: следующий бан снова начнётся с малого."""
    if _ban_level.pop(exchange, None) is not None:
        _banned_until.pop(exchange, None)


def reset_bans() -> None:
    _banned_until.clear()
    _ban_level.clear()


async def _call(exchange: str, request):  # noqa: ANN001, ANN202
    """Единственная точка, через которую уходят запросы к площадке.

    Запрос принимается функцией, а не готовой корутиной: во время паузы он не
    должен даже создаваться, иначе получаем несожранную корутину и предупреждение
    вместо тишины.
    """
    _raise_if_banned(exchange)
    try:
        result = await request()
    except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as exc:
        raise _register_ban(exchange) from exc
    _clear_ban(exchange)
    return result


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
        await _call(name, exchange.load_markets)
        _markets_loaded.add(name)


def _price_precision(market: dict, mode: int | None = None, price: float | None = None) -> int:
    """Число знаков после запятой в цене.

    ccxt отдаёт precision.price в трёх несовместимых видах, и какой именно —
    говорит precisionMode биржи, а не тип значения:

      * TICK_SIZE — сам шаг (0.01). Так делают Binance, Bybit, OKX.
      * DECIMAL_PLACES — уже число знаков.
      * SIGNIFICANT_DIGITS — значащих цифр всего, а не после запятой. Так
        делает Bitfinex, и по одному значению 5 отличить его от «пяти знаков
        после запятой» нельзя: у BTC это ноль знаков, у монеты за $0.09 —
        шесть. Поэтому нужна ещё и сама цена.

    Без разбора режима BTC на Bitfinex рисовался бы как 76 750.00000, а у
    дешёвой монеты знаков, наоборот, не хватило бы.
    """
    raw = market.get("precision", {}).get("price")

    if mode == SIGNIFICANT_DIGITS and isinstance(raw, (int, float)) and raw > 0:
        if not price or price <= 0:
            return 2
        decimals = int(raw) - 1 - math.floor(math.log10(price))
        return max(0, min(decimals, 12))

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
    reset_bans()


@register_provider
class CcxtProvider(DataProvider):
    name = "ccxt"

    def __init__(self, exchange: str = "binance") -> None:
        self.exchange_name = exchange

    async def _exchange(self) -> ccxt.Exchange:
        _raise_if_banned(self.exchange_name)
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
            ticker = await _call(self.exchange_name, lambda: ex.fetch_ticker(symbol))

        last = ticker.get("last") or ticker.get("close")
        if last is None:
            raise SymbolNotFound(symbol, [])

        return SymbolMeta(
            symbol=symbol,
            last_price=float(last),
            price_precision=_price_precision(market, ex.precisionMode, float(last)),
            volume_24h=ticker.get("quoteVolume"),
            extra={"base": market.get("base"), "quote": market.get("quote")},
        )

    async def fetch_ohlcv(self, symbol: str, tf: str, limit: int = 500) -> pd.DataFrame:
        ex = await self._exchange()
        async with _semaphore:
            rows = await _call(
                self.exchange_name,
                lambda: ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit),
            )

        df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    async def close(self) -> None:
        # Инстансы общие на процесс — закрываются через close_all_exchanges().
        return None
