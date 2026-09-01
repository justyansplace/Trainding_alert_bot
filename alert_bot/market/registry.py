"""Реестр инструментов: чтение и изменение из БД.

Циклы перечитывают реестр в начале каждой итерации, поэтому добавление
инструмента через бота вступает в силу без рестарта процесса.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from alert_bot.config import get_settings
from alert_bot.db.models import Instrument, Subscription, utcnow
from alert_bot.db.session import session_scope
from alert_bot.market.providers import ccxt_provider  # noqa: F401 — регистрация в фабрике
from alert_bot.market.providers import oanda_provider  # noqa: F401 — регистрация в фабрике
from alert_bot.market.providers import yahoo_provider  # noqa: F401 — регистрация в фабрике
from alert_bot.market.providers.base import SymbolMeta, derive_round_step, get_provider

log = logging.getLogger(__name__)


class RegistryError(Exception):
    pass


# Площадка задаётся одной строкой, а провайдер выводится из неё: для крипты это
# имя биржи внутри ccxt, для форекса — сам брокер. Так пользователю не нужно
# знать про внутреннее деление на провайдеров.
_PROVIDER_BY_EXCHANGE = {"oanda": "oanda", "yahoo": "yahoo"}
DEFAULT_PROVIDER = "ccxt"


def provider_for(exchange: str) -> str:
    return _PROVIDER_BY_EXCHANGE.get(exchange.lower(), DEFAULT_PROVIDER)


async def list_instruments(enabled_only: bool = False) -> list[Instrument]:
    async with session_scope() as session:
        stmt = select(Instrument).order_by(Instrument.symbol)
        if enabled_only:
            stmt = stmt.where(Instrument.enabled.is_(True))
        return list((await session.scalars(stmt)).all())


async def get_by_symbol(symbol: str, exchange: str) -> Instrument | None:
    async with session_scope() as session:
        return await session.scalar(
            select(Instrument).where(
                Instrument.symbol == symbol.upper(), Instrument.exchange == exchange
            )
        )


async def count_enabled() -> int:
    async with session_scope() as session:
        return await session.scalar(
            select(func.count()).select_from(Instrument).where(Instrument.enabled.is_(True))
        ) or 0


async def validate_candidate(symbol: str, exchange: str) -> SymbolMeta:
    """Проверяет символ у провайдера до записи в БД.

    Без этого опечатка в тикере попадает в реестр и роняет price_loop на каждой
    итерации. Бросает SymbolNotFound (с подсказками) или RegistryError.
    """
    settings = get_settings()

    if await count_enabled() >= settings.max_instruments:
        raise RegistryError(
            f"Достигнут лимит инструментов ({settings.max_instruments}). "
            "Отключите ненужные через /rm_instrument или поднимите MAX_INSTRUMENTS."
        )

    existing = await get_by_symbol(symbol, exchange)
    if existing is not None and existing.enabled:
        raise RegistryError(f"{existing.symbol} на {exchange} уже добавлен.")

    provider = get_provider(provider_for(exchange), exchange=exchange)
    meta = await provider.validate_symbol(symbol)

    # Данные должны реально приходить, а не только числиться в справочнике.
    df = await provider.fetch_ohlcv(meta.symbol, "1h", limit=3)
    if df.empty:
        raise RegistryError(f"{meta.symbol}: площадка знает символ, но не отдаёт свечи.")

    return meta


async def count_added_by(tg_id: int) -> int:
    """Сколько включённых инструментов завёл этот человек."""
    async with session_scope() as session:
        return await session.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(Instrument.added_by == tg_id, Instrument.enabled.is_(True))
        ) or 0


async def add_instrument(
    meta: SymbolMeta,
    exchange: str,
    keywords: list[str],
    added_by: int,
    round_step: float | None = None,
) -> Instrument:
    async with session_scope() as session:
        existing = await session.scalar(
            select(Instrument).where(
                Instrument.symbol == meta.symbol, Instrument.exchange == exchange
            )
        )
        if existing is not None:
            # Реактивация ранее отключённого — история свечей и уровней остаётся.
            existing.enabled = True
            existing.keywords = keywords
            existing.round_step = round_step or derive_round_step(meta.last_price)
            existing.price_precision = meta.price_precision
            existing.provider = provider_for(exchange)
            existing.last_error = None
            log.info("Инструмент %s реактивирован", meta.symbol)
            return existing

        instrument = Instrument(
            symbol=meta.symbol,
            provider=provider_for(exchange),
            exchange=exchange,
            enabled=True,
            round_step=round_step or derive_round_step(meta.last_price),
            price_precision=meta.price_precision,
            keywords=keywords,
            added_by=added_by,
            added_at=utcnow(),
            last_price=meta.last_price,
        )
        session.add(instrument)
        await session.flush()
        log.info("Инструмент %s@%s добавлен (id=%s)", meta.symbol, exchange, instrument.id)
        return instrument


async def disable_instrument(symbol: str, exchange: str | None = None) -> Instrument | None:
    """Мягкое отключение: данные и уровни остаются, цикл перестаёт его опрашивать."""
    async with session_scope() as session:
        stmt = select(Instrument).where(Instrument.symbol == symbol.upper())
        if exchange:
            stmt = stmt.where(Instrument.exchange == exchange)
        instrument = await session.scalar(stmt)
        if instrument is None:
            return None
        instrument.enabled = False
        return instrument


async def subscribe(tg_id: int, instrument_id: int, enabled: bool = True) -> None:
    async with session_scope() as session:
        sub = await session.get(Subscription, (tg_id, instrument_id))
        if sub is None:
            session.add(
                Subscription(tg_id=tg_id, instrument_id=instrument_id, enabled=enabled)
            )
        else:
            sub.enabled = enabled


async def list_subscriptions(tg_id: int) -> dict[int, Subscription]:
    async with session_scope() as session:
        rows = await session.scalars(select(Subscription).where(Subscription.tg_id == tg_id))
        return {row.instrument_id: row for row in rows}
