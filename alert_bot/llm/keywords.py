"""Ключевые слова инструмента для фильтра релевантности новостей.

Один вызов Sonnet 5 на инструмент за всё время его жизни — при добавлении.
Если ключа нет или бюджет исчерпан, откатываемся на детерминированный вывод из
тикера: хуже по полноте, но добавление инструмента не должно падать из-за LLM.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from alert_bot.config import get_settings
from alert_bot.llm.client import BudgetExceeded, ensure_budget, record_usage
from alert_bot.llm.complete import LlmError, parse_structured

log = logging.getLogger(__name__)

KEYWORDS_SYSTEM = (
    "Ты помогаешь настроить фильтр релевантности новостей для торгового инструмента.\n"
    "По тикеру верни ключевые слова в нижнем регистре, по которым новость про этот "
    "актив можно опознать в заголовке: полное название актива, распространённые "
    "сокращения и тикеры, название сети или эмитента.\n"
    "Только то, что реально встречается в финансовых заголовках. Никаких общих слов "
    "вроде 'crypto', 'price', 'market' — они дадут ложные срабатывания.\n"
    "От 3 до 8 элементов."
)

# Распространённые тикеры — чтобы не ходить в LLM за очевидным.
_KNOWN_BASES: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
    "XRP": ["ripple", "xrp"],
    "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink", "link"],
    "BNB": ["binance coin", "bnb"],
    "TON": ["toncoin", "ton"],
}


class KeywordSet(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=12)


def fallback_keywords(symbol: str) -> list[str]:
    base = symbol.split("/")[0].split(":")[0].upper()
    if base in _KNOWN_BASES:
        return list(_KNOWN_BASES[base])
    return [base.lower()]


async def suggest_keywords(symbol: str) -> tuple[list[str], bool]:
    """Возвращает (keywords, использовалась_ли_LLM)."""
    base = symbol.split("/")[0].split(":")[0].upper()
    if base in _KNOWN_BASES:
        return list(_KNOWN_BASES[base]), False

    settings = get_settings()
    try:
        await ensure_budget("keywords")
        parsed, usage = await parse_structured(
            model=settings.extraction_model,
            system=KEYWORDS_SYSTEM,
            user=f"Тикер: {symbol}",
            schema=KeywordSet,
            max_tokens=1000,
        )
        await record_usage(settings.extraction_model, "keywords", usage)

        if parsed is None or not parsed.keywords:
            raise ValueError("пустой ответ модели")

        keywords = [k.strip().lower() for k in parsed.keywords if k.strip()]
        return sorted(set(keywords)), True

    except (BudgetExceeded, LlmError) as exc:
        log.info("keywords для %s через fallback: %s", symbol, exc)
        return fallback_keywords(symbol), False
    except Exception:  # noqa: BLE001 — добавление инструмента важнее качества keywords
        log.warning("LLM не дала keywords для %s, fallback", symbol, exc_info=True)
        return fallback_keywords(symbol), False
