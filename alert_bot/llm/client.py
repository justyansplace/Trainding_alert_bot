"""Клиент Claude API: учёт расхода и дневной предохранитель.

Инструменты добавляет админ из бота, то есть расход на LLM растёт по чужому
решению. Без жёсткого дневного потолка это добавление денег вслепую, поэтому
каждый вызов проходит через ensure_budget() и пишет строку в llm_usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from alert_bot.config import get_settings
from alert_bot.db.models import LlmUsage, utcnow
from alert_bot.db.session import session_scope
from alert_bot.llm.complete import Usage, has_llm  # noqa: F401 — реэкспорт

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Price:
    """Цена за 1M токенов."""

    input_usd: float
    output_usd: float


# Цены за 1M токенов. Anthropic: кэш-чтение ≈ 0.1× входа, запись ≈ 1.25×.
# Интро-цена Sonnet 5 ($2/$10) не заложена намеренно: считаем по полной ставке,
# чтобы оценка расхода не занижалась после её окончания.
PRICING: dict[str, Price] = {
    # OpenAI
    "gpt-5-nano": Price(0.05, 0.40),
    "gpt-5-mini": Price(0.25, 2.00),
    "gpt-5": Price(1.25, 10.00),
    "gpt-4.1-nano": Price(0.10, 0.40),
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4o-mini": Price(0.15, 0.60),
    # Anthropic
    "claude-opus-5": Price(5.00, 25.00),
    "claude-sonnet-5": Price(3.00, 15.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
}

# Кэшированный вход у OpenAI дешевле примерно вдесятеро, как и у Anthropic;
# записи в кэш там нет — он включается сам и отдельно не тарифицируется.
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25

class BudgetExceeded(Exception):
    """Дневной лимит исчерпан. Вызывающий обязан деградировать, а не падать."""


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    price = PRICING.get(model)
    if price is None:
        log.warning("нет прайса для модели %s — считаем по верхней ставке", model)
        price = max(PRICING.values(), key=lambda p: p.output_usd)

    return (
        input_tokens * price.input_usd
        + cache_read_tokens * price.input_usd * _CACHE_READ_MULTIPLIER
        + cache_write_tokens * price.input_usd * _CACHE_WRITE_MULTIPLIER
        + output_tokens * price.output_usd
    ) / 1_000_000


async def record_usage(model: str, purpose: str, usage: Usage) -> float:
    """Пишет строку в llm_usage. usage уже нормализован в complete.py."""
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cache_read = usage.cached_tokens
    cache_write = usage.cache_write_tokens

    cost = estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write)

    async with session_scope() as session:
        session.add(
            LlmUsage(
                ts=utcnow(),
                model=model,
                purpose=purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cache_read,
                cost_usd=cost,
            )
        )

    if cache_read == 0 and cache_write == 0 and input_tokens > 4096:
        # Кэшируется только неизменная начальная часть промпта, и она должна
        # сама по себе превышать порог провайдера (~1024 токена). У нас
        # системный промпт короче, а дальше идут разные статьи — поэтому на
        # обычных пакетах кэш и не должен срабатывать. Ругаемся только когда
        # вход настолько велик, что общая часть уж точно была.
        log.warning("purpose=%s: кэш не сработал на %s входных токенов", purpose, input_tokens)

    return cost


async def daily_spend_usd() -> float:
    """Расход за последние 24 часа."""
    since = utcnow() - timedelta(hours=24)
    async with session_scope() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)).where(LlmUsage.ts >= since)
        )
    return float(total or 0.0)


async def ensure_budget(purpose: str) -> None:
    settings = get_settings()
    spent = await daily_spend_usd()
    if spent >= settings.daily_llm_budget_usd:
        raise BudgetExceeded(
            f"дневной лимит ${settings.daily_llm_budget_usd:.2f} исчерпан "
            f"(потрачено ${spent:.2f}), пропускаем {purpose}"
        )


async def usage_report(hours: int = 24) -> list[tuple[str, str, int, int, float]]:
    """(model, purpose, input, output, cost) с разбивкой — для /usage."""
    since = utcnow() - timedelta(hours=hours)
    async with session_scope() as session:
        rows = await session.execute(
            select(
                LlmUsage.model,
                LlmUsage.purpose,
                func.sum(LlmUsage.input_tokens),
                func.sum(LlmUsage.output_tokens),
                func.sum(LlmUsage.cost_usd),
            )
            .where(LlmUsage.ts >= since)
            .group_by(LlmUsage.model, LlmUsage.purpose)
            .order_by(func.sum(LlmUsage.cost_usd).desc())
        )
        return [(m, p, int(i or 0), int(o or 0), float(c or 0.0)) for m, p, i, o, c in rows]
