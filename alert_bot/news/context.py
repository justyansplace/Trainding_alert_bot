"""Сборка рыночного контекста по инструменту.

Из разобранных материалов за последние часы собирается компактная сводка, к
которой обращается brief.py в момент алерта. Считается кодом, а не моделью:
среднее и веса — арифметика, и модели тут делать нечего.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from alert_bot.db.models import Article, Extraction, Source, utcnow
from alert_bot.db.session import session_scope
from alert_bot.news.ingest import MACRO_SYMBOL

# Окно, за которое новость ещё считается актуальной для внутридневной торговли.
CONTEXT_WINDOW = timedelta(hours=12)

# Период полураспада веса новости. Материал шестичасовой давности весит вдвое
# меньше свежего: для one-day-торговли вчерашняя повестка почти не важна.
HALF_LIFE_HOURS = 6.0


@dataclass(slots=True)
class ContextItem:
    title: str
    thesis: str
    sentiment: float
    impact: int
    weight: float
    source_name: str
    published_at: datetime | None
    is_macro: bool


@dataclass(slots=True)
class MarketContext:
    symbol: str
    article_count: int = 0
    sentiment: float = 0.0
    max_impact: int = 0
    topics: list[str] = field(default_factory=list)
    mentioned_levels: list[float] = field(default_factory=list)
    items: list[ContextItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.article_count == 0

    def sentiment_label(self) -> str:
        if self.sentiment >= 0.35:
            return "позитивный"
        if self.sentiment <= -0.35:
            return "негативный"
        return "нейтральный"

    def top_items(self, count: int = 3) -> list[ContextItem]:
        return sorted(self.items, key=lambda i: -i.weight)[:count]


def recency_weight(published_at: datetime | None, now: datetime) -> float:
    if published_at is None:
        return 0.5  # без даты — не отбрасываем, но и не считаем свежим
    age_hours = max((now - published_at).total_seconds() / 3600.0, 0.0)
    return 0.5 ** (age_hours / HALF_LIFE_HOURS)


async def build_context(
    symbol: str, now: datetime | None = None, window: timedelta = CONTEXT_WINDOW
) -> MarketContext:
    """Контекст по инструменту: его собственные новости плюс макро."""
    now = now or utcnow()
    since = now - window

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Extraction, Article, Source.name, Source.weight)
                .join(Article, Article.url_hash == Extraction.url_hash)
                .join(Source, Source.id == Article.source_id)
                .where(Extraction.created_at >= since)
            )
        ).all()

    context = MarketContext(symbol=symbol)
    weighted_sum = 0.0
    total_weight = 0.0
    topics: Counter[str] = Counter()
    levels: list[float] = []

    for extraction, article, source_name, source_weight in rows:
        symbols = extraction.relevant_symbols or []
        is_macro = MACRO_SYMBOL in symbols
        if symbol not in symbols and not is_macro:
            continue

        # Вес = значимость × свежесть × доверие к источнику. Материал с impact=0
        # не должен двигать средний тон вообще.
        weight = (
            extraction.impact
            * recency_weight(article.published_at, now)
            * float(source_weight or 1.0)
        )
        if is_macro and symbol not in symbols:
            weight *= 0.7  # макро релевантно, но менее адресно

        context.article_count += 1
        topics.update(extraction.topics or [])
        levels.extend(extraction.mentioned_levels or [])
        context.max_impact = max(context.max_impact, extraction.impact)

        if weight > 0:
            weighted_sum += extraction.sentiment * weight
            total_weight += weight

        context.items.append(
            ContextItem(
                title=article.title,
                thesis=extraction.thesis,
                sentiment=extraction.sentiment,
                impact=extraction.impact,
                weight=weight,
                source_name=source_name,
                published_at=article.published_at,
                is_macro=is_macro,
            )
        )

    if total_weight > 0:
        context.sentiment = round(weighted_sum / total_weight, 3)

    context.topics = [topic for topic, _ in topics.most_common(3)]
    context.mentioned_levels = sorted({round(v, 8) for v in levels if math.isfinite(v)})

    return context


def render_for_prompt(context: MarketContext, limit: int = 8) -> str:
    """Контекст в виде текста для brief.py."""
    if context.is_empty:
        return "За последние 12 часов релевантных материалов не было."

    header = (
        f"Материалов за 12ч: {context.article_count}. "
        f"Взвешенный тон: {context.sentiment:+.2f} ({context.sentiment_label()}). "
        f"Максимальная значимость: {context.max_impact}/3."
    )
    if context.topics:
        header += f" Темы: {', '.join(context.topics)}."
    if context.mentioned_levels:
        shown = ", ".join(f"{v:g}" for v in context.mentioned_levels[:6])
        header += f" Уровни, названные аналитиками: {shown}."

    lines = [header, "", "Материалы:"]
    for item in sorted(context.items, key=lambda i: -i.weight)[:limit]:
        tag = " [макро]" if item.is_macro else ""
        lines.append(
            f"- ({item.source_name}, значимость {item.impact}/3, тон {item.sentiment:+.1f})"
            f"{tag} {item.thesis}"
        )

    return "\n".join(lines)
