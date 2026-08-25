"""Тесты новостного контура: разбор лент, здоровье источников, релевантность."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot.db.models import Article, Instrument, Source, utcnow
from alert_bot.db.session import session_scope
from alert_bot.news import ingest, registry
from alert_bot.news.fetch import parse_feed

RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test feed</title>
  <item>
    <title>Bitcoin breaks above $78,000 as ETF inflows accelerate</title>
    <link>https://example.com/btc-78k?utm_source=rss</link>
    <pubDate>Fri, 22 Aug 2026 10:30:00 GMT</pubDate>
    <description>&lt;p&gt;Spot &lt;b&gt;ETF&lt;/b&gt; demand picked up.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Local football club wins the cup</title>
    <link>https://example.com/football</link>
    <pubDate>Fri, 22 Aug 2026 09:00:00 GMT</pubDate>
    <description>Nothing to do with markets.</description>
  </item>
  <item>
    <title></title>
    <link>https://example.com/no-title</link>
  </item>
</channel></rss>
"""


# --------------------------------------------------------------------------- #
# Разбор ленты
# --------------------------------------------------------------------------- #


def test_parse_feed_extracts_entries() -> None:
    articles = parse_feed(RSS_SAMPLE)
    assert len(articles) == 2, "запись без заголовка должна отбрасываться"

    first = articles[0]
    assert first.title == "Bitcoin breaks above $78,000 as ETF inflows accelerate"
    assert first.published_at == datetime(2026, 8, 22, 10, 30, tzinfo=UTC)


def test_parse_feed_strips_html_from_excerpt() -> None:
    """В описании приходит разметка — в модель она попадать не должна."""
    excerpt = parse_feed(RSS_SAMPLE)[0].excerpt
    assert "<" not in excerpt and ">" not in excerpt
    assert "ETF demand picked up." in excerpt


def test_parse_feed_survives_garbage() -> None:
    assert parse_feed(b"not a feed at all") == []
    assert parse_feed(b"") == []


# --------------------------------------------------------------------------- #
# Релевантность
# --------------------------------------------------------------------------- #


def index_for(pairs: dict[str, list[str]]) -> dict[str, list[str]]:
    instruments = [
        type("Ins", (), {"symbol": symbol, "keywords": keywords})()
        for symbol, keywords in pairs.items()
    ]
    return ingest.build_keyword_index(instruments)


def test_article_tagged_with_matching_instrument() -> None:
    index = index_for({"BTC/USDT": ["bitcoin", "btc"], "ETH/USDT": ["ethereum", "eth"]})
    assert ingest.relevant_symbols("Bitcoin rally continues", index) == ["BTC/USDT"]


def test_article_can_match_several_instruments() -> None:
    index = index_for({"BTC/USDT": ["bitcoin"], "ETH/USDT": ["ethereum"]})
    symbols = ingest.relevant_symbols("Bitcoin and Ethereum both rally", index)
    assert set(symbols) == {"BTC/USDT", "ETH/USDT"}


def test_macro_news_is_relevant_to_everything() -> None:
    """Решение ФРС двигает весь рынок — такая новость нужна каждому инструменту."""
    index = index_for({"BTC/USDT": ["bitcoin"]})
    assert ingest.MACRO_SYMBOL in ingest.relevant_symbols(
        "Fed signals a rate cut in September", index
    )


def test_irrelevant_article_matches_nothing() -> None:
    """Именно это отсекает оплату LLM за спортивные новости на крипто-сайте."""
    index = index_for({"BTC/USDT": ["bitcoin", "btc"]})
    assert ingest.relevant_symbols("Local football club wins the cup", index) == []


def test_relevance_is_case_insensitive() -> None:
    index = index_for({"BTC/USDT": ["bitcoin"]})
    assert ingest.relevant_symbols("BITCOIN SURGES", index) == ["BTC/USDT"]


def test_instrument_without_keywords_matches_nothing() -> None:
    index = index_for({"XYZ/USDT": []})
    assert ingest.relevant_symbols("XYZ is up", index) == []


# --------------------------------------------------------------------------- #
# Здоровье источников
# --------------------------------------------------------------------------- #


async def make_source(name: str = "Test", url: str = "https://example.com/feed") -> Source:
    async with session_scope() as session:
        source = Source(name=name, kind="rss", url=url, added_by=1, added_at=utcnow())
        session.add(source)
        await session.flush()
        return source


async def test_failures_accumulate_then_disable(db) -> None:
    """Источники ломаются молча — без счётчика это выясняется через месяц."""
    source = await make_source()

    for attempt in range(1, registry.MAX_CONSECUTIVE_FAILURES):
        disabled = await registry.record_failure(source.id, f"HTTP 403 (#{attempt})")
        assert disabled is False

    disabled = await registry.record_failure(source.id, "HTTP 403 (последняя)")
    assert disabled is True

    stored = await registry.get_source(source.id)
    assert stored.enabled is False
    assert stored.consecutive_failures == registry.MAX_CONSECUTIVE_FAILURES
    assert "403" in stored.last_error


async def test_success_resets_failure_counter(db) -> None:
    """Разовый сбой сети не должен приближать источник к отключению."""
    source = await make_source()
    await registry.record_failure(source.id, "таймаут")
    await registry.record_failure(source.id, "таймаут")

    await registry.record_success(source.id, etag='W/"abc"', last_modified="Fri, 22 Aug 2026")

    stored = await registry.get_source(source.id)
    assert stored.consecutive_failures == 0
    assert stored.last_error is None
    assert stored.etag == 'W/"abc"'
    assert stored.last_ok_at is not None


async def test_reenabling_clears_error_state(db) -> None:
    source = await make_source()
    for _ in range(registry.MAX_CONSECUTIVE_FAILURES):
        await registry.record_failure(source.id, "HTTP 500")

    await registry.set_enabled(source.id, True)

    stored = await registry.get_source(source.id)
    assert stored.enabled and stored.consecutive_failures == 0 and stored.last_error is None


async def test_health_label_reflects_state(db) -> None:
    source = await make_source()
    assert "не опрашивался" in registry.health_label(source)

    await registry.record_success(source.id, None, None)
    assert "порядке" in registry.health_label(await registry.get_source(source.id))

    await registry.record_failure(source.id, "boom")
    assert "ошибок подряд" in registry.health_label(await registry.get_source(source.id))


async def test_unsupported_kind_is_rejected(db) -> None:
    """Произвольный JSON-API требует парсера, а не строки в конфиге."""
    with pytest.raises(registry.SourceError, match="не поддерживается"):
        await registry.add_source("Custom", "json_api", "https://example.com/api", added_by=1)


async def test_duplicate_url_rejected_but_disabled_one_revives(db) -> None:
    source = await make_source(url="https://example.com/a")

    with pytest.raises(registry.SourceError, match="уже добавлен"):
        await registry.add_source("Copy", "rss", "https://example.com/a", added_by=1)

    await registry.set_enabled(source.id, False)
    revived = await registry.add_source("Copy", "rss", "https://example.com/a", added_by=1)
    assert revived.id == source.id
    assert revived.enabled


# --------------------------------------------------------------------------- #
# Хранение статей
# --------------------------------------------------------------------------- #


async def test_pending_and_mark_processed(db) -> None:
    source = await make_source()
    async with session_scope() as session:
        for i in range(3):
            session.add(
                Article(
                    url_hash=f"hash{i}",
                    source_id=source.id,
                    url=f"https://example.com/{i}",
                    title=f"Article {i}",
                    fetched_at=utcnow() - timedelta(minutes=i),
                    simhash=0,
                    excerpt="",
                )
            )

    assert len(await ingest.pending_articles()) == 3

    await ingest.mark_processed(["hash0", "hash1"])
    remaining = await ingest.pending_articles()
    assert [a.url_hash for a in remaining] == ["hash2"]


async def test_ingest_without_sources_is_a_noop(db) -> None:
    async with session_scope() as session:
        sources = await registry.list_sources()
        for source in sources:
            stored = await session.get(Source, source.id)
            stored.enabled = False

    stats = await ingest.ingest_once(instruments=[])
    assert stats.fetched == 0 and stats.stored == 0


async def test_deleting_source_removes_its_articles(db) -> None:
    source = await make_source()
    async with session_scope() as session:
        session.add(
            Article(
                url_hash="h",
                source_id=source.id,
                url="https://example.com/x",
                title="X",
                fetched_at=utcnow(),
                simhash=0,
                excerpt="",
            )
        )

    await registry.delete_source(source.id)
    assert await ingest.pending_articles() == []


async def test_instruments_keyword_index_includes_macro(db) -> None:
    async with session_scope() as session:
        session.add(
            Instrument(
                symbol="BTC/USDT",
                provider="ccxt",
                exchange="binance",
                round_step=1.0,
                price_precision=2,
                keywords=["bitcoin"],
                added_by=1,
            )
        )

    from alert_bot.market import registry as market_registry

    index = ingest.build_keyword_index(await market_registry.list_instruments())
    assert "BTC/USDT" in index
    assert ingest.MACRO_SYMBOL in index
