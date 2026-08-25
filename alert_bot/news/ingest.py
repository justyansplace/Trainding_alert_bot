"""Сбор новостей: загрузка → дедупликация → фильтр релевантности → БД.

Фильтр релевантности стоит до LLM намеренно. Ленты отдают по 20-40 записей за
опрос, из которых к торгуемым инструментам относится меньшинство; отправлять в
модель всё подряд — платить за обработку спортивных новостей на крипто-сайте.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import aiohttp
from sqlalchemy import select

from alert_bot.config import get_settings
from alert_bot.db.models import Article, Instrument, Source, SourceKind, utcnow
from alert_bot.db.session import session_scope
from alert_bot.news import dedup, registry
from alert_bot.news.fetch import RawArticle, fetch_cryptopanic, fetch_rss

log = logging.getLogger(__name__)

# Макро-словарь: такие новости релевантны любому инструменту, потому что двигают
# рынок целиком. Для one-day-торговли это половина значимых движений.
MACRO_KEYWORDS = frozenset(
    {
        "fed", "фрс", "fomc", "powell", "пауэлл", "cpi", "инфляц", "inflation",
        "rate cut", "rate hike", "ставк", "nfp", "payrolls", "unemployment",
        "sec", "cftc", "etf", "регулятор", "regulation", "tariff", "recession",
        "treasury", "yield", "liquidity", "ликвидност",
    }
)

MACRO_SYMBOL = "MACRO"

# Насколько назад смотрим при дедупликации по simhash.
NEAR_DUPLICATE_WINDOW = timedelta(hours=48)


@dataclass(slots=True)
class IngestStats:
    fetched: int = 0
    not_modified: int = 0
    stored: int = 0
    duplicates_by_url: int = 0
    duplicates_by_title: int = 0
    irrelevant: int = 0
    failed_sources: list[str] = field(default_factory=list)
    disabled_sources: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"получено {self.fetched}, сохранено {self.stored}, "
            f"дублей {self.duplicates_by_url + self.duplicates_by_title}, "
            f"нерелевантных {self.irrelevant}"
        )


def build_keyword_index(instruments: list[Instrument]) -> dict[str, list[str]]:
    """Символ → ключевые слова. MACRO добавляется всегда."""
    index = {ins.symbol: [k.lower() for k in (ins.keywords or [])] for ins in instruments}
    index[MACRO_SYMBOL] = sorted(MACRO_KEYWORDS)
    return index


def relevant_symbols(text: str, index: dict[str, list[str]]) -> list[str]:
    """Грубый предфильтр по вхождению подстроки.

    Задача — не классифицировать точно, а дёшево отсеять заведомо постороннее.
    Разбираться, о чём материал на самом деле, будет модель.
    """
    haystack = text.lower()
    return [
        symbol
        for symbol, keywords in index.items()
        if any(keyword and keyword in haystack for keyword in keywords)
    ]


async def _existing_simhashes(since: datetime) -> list[int]:
    async with session_scope() as session:
        rows = (
            await session.scalars(select(Article.simhash).where(Article.fetched_at >= since))
        ).all()
    return [dedup.from_signed_64(value) for value in rows]


async def _known_url_hashes(hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(Article.url_hash).where(Article.url_hash.in_(hashes))
            )
        ).all()
    return set(rows)


async def _fetch_source(session: aiohttp.ClientSession, source: Source):  # noqa: ANN202
    settings = get_settings()
    if source.kind == SourceKind.CRYPTOPANIC.value:
        return await fetch_cryptopanic(session, source.url, settings.cryptopanic_token)
    return await fetch_rss(session, source.url, source.etag, source.last_modified)


async def ingest_once(instruments: list[Instrument] | None = None) -> IngestStats:
    """Один проход по всем включённым источникам."""
    from alert_bot.market import registry as market_registry

    stats = IngestStats()
    sources = await registry.list_sources(enabled_only=True)
    if not sources:
        return stats

    if instruments is None:
        instruments = await market_registry.list_instruments(enabled_only=True)

    index = build_keyword_index(instruments)
    now = utcnow()
    recent_simhashes = await _existing_simhashes(now - NEAR_DUPLICATE_WINDOW)

    async with aiohttp.ClientSession() as http:
        for source in sources:
            result = await _fetch_source(http, source)

            if result.not_modified:
                stats.not_modified += 1
                await registry.record_success(source.id, result.etag, result.last_modified)
                continue

            if not result.ok:
                stats.failed_sources.append(f"{source.name}: {result.error}")
                if await registry.record_failure(source.id, result.error or "неизвестно"):
                    stats.disabled_sources.append(source.name)
                continue

            await registry.record_success(source.id, result.etag, result.last_modified)
            stats.fetched += len(result.articles)

            stored = await _store_articles(
                source, result.articles, index, recent_simhashes, stats, now
            )
            stats.stored += stored

    return stats


async def _store_articles(
    source: Source,
    articles: list[RawArticle],
    index: dict[str, list[str]],
    recent_simhashes: list[int],
    stats: IngestStats,
    now: datetime,
) -> int:
    if not articles:
        return 0

    hashes = [dedup.url_hash(a.url) for a in articles]
    known = await _known_url_hashes(hashes)

    fresh: list[tuple[str, RawArticle, int]] = []
    seen_in_batch: set[str] = set()

    for article, url_key in zip(articles, hashes, strict=True):
        if url_key in known or url_key in seen_in_batch:
            stats.duplicates_by_url += 1
            continue

        if not relevant_symbols(f"{article.title} {article.excerpt}", index):
            stats.irrelevant += 1
            continue

        fingerprint = dedup.simhash(article.title)
        if any(dedup.is_near_duplicate(fingerprint, other) for other in recent_simhashes):
            stats.duplicates_by_title += 1
            continue

        seen_in_batch.add(url_key)
        recent_simhashes.append(fingerprint)
        fresh.append((url_key, article, fingerprint))

    if not fresh:
        return 0

    async with session_scope() as session:
        for url_key, article, fingerprint in fresh:
            session.add(
                Article(
                    url_hash=url_key,
                    source_id=source.id,
                    url=article.url,
                    title=article.title,
                    published_at=article.published_at,
                    fetched_at=now,
                    simhash=dedup.to_signed_64(fingerprint),
                    excerpt=article.excerpt,
                    processed=False,
                )
            )

    return len(fresh)


async def pending_articles(limit: int = 40) -> list[Article]:
    """Сохранённые, но ещё не отправленные в модель."""
    async with session_scope() as session:
        return list(
            (
                await session.scalars(
                    select(Article)
                    .where(Article.processed.is_(False))
                    .order_by(Article.fetched_at.desc())
                    .limit(limit)
                )
            ).all()
        )


async def mark_processed(url_hashes: list[str]) -> None:
    if not url_hashes:
        return
    async with session_scope() as session:
        rows = (
            await session.scalars(select(Article).where(Article.url_hash.in_(url_hashes)))
        ).all()
        for row in rows:
            row.processed = True


async def recent_article_count(hours: int = 24) -> int:
    since = datetime.now(UTC) - timedelta(hours=hours)
    async with session_scope() as session:
        rows = (
            await session.scalars(select(Article.url_hash).where(Article.fetched_at >= since))
        ).all()
    return len(rows)
