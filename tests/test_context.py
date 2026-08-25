"""Тесты сборки рыночного контекста.

Взвешивание тона считается кодом, а не моделью, поэтому его можно и нужно
проверять точно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot.db.models import Article, Extraction, Source, utcnow
from alert_bot.db.session import session_scope
from alert_bot.news.context import build_context, recency_weight, render_for_prompt
from alert_bot.news.ingest import MACRO_SYMBOL

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


async def make_source(name: str = "CoinDesk", weight: float = 1.0) -> int:
    async with session_scope() as session:
        source = Source(
            name=name,
            kind="rss",
            url=f"https://example.com/{name}",
            weight=weight,
            added_by=1,
            added_at=utcnow(),
        )
        session.add(source)
        await session.flush()
        return source.id


async def add_article(
    source_id: int,
    key: str,
    title: str,
    *,
    symbols: list[str],
    sentiment: float,
    impact: int,
    published_at: datetime | None = NOW,
    levels: list[float] | None = None,
    topics: list[str] | None = None,
    created_at: datetime | None = None,
) -> None:
    async with session_scope() as session:
        session.add(
            Article(
                url_hash=key,
                source_id=source_id,
                url=f"https://example.com/{key}",
                title=title,
                published_at=published_at,
                fetched_at=created_at or NOW,
                simhash=0,
                excerpt="",
            )
        )
        await session.flush()
        session.add(
            Extraction(
                url_hash=key,
                sentiment=sentiment,
                impact=impact,
                horizon="intraday",
                thesis=f"Тезис: {title}",
                relevant_symbols=symbols,
                mentioned_levels=levels or [],
                topics=topics or [],
                model="test",
                created_at=created_at or NOW,
            )
        )


# --------------------------------------------------------------------------- #
# Отбор материалов
# --------------------------------------------------------------------------- #


async def test_context_includes_own_and_macro_news(db) -> None:
    source = await make_source()
    await add_article(source, "a", "BTC news", symbols=["BTC/USDT"], sentiment=0.5, impact=2)
    await add_article(source, "b", "Fed news", symbols=[MACRO_SYMBOL], sentiment=-0.5, impact=3)
    await add_article(source, "c", "ETH news", symbols=["ETH/USDT"], sentiment=0.9, impact=3)

    context = await build_context("BTC/USDT", now=NOW)

    assert context.article_count == 2, "чужой инструмент попадать не должен"
    titles = {item.title for item in context.items}
    assert titles == {"BTC news", "Fed news"}


async def test_macro_news_reaches_every_instrument(db) -> None:
    """Решение ФРС двигает рынок целиком — оно нужно каждому инструменту."""
    source = await make_source()
    await add_article(source, "m", "Fed cuts", symbols=[MACRO_SYMBOL], sentiment=0.8, impact=3)

    for symbol in ("BTC/USDT", "ETH/USDT", "DOGE/USDT"):
        assert (await build_context(symbol, now=NOW)).article_count == 1


async def test_stale_news_falls_out_of_window(db) -> None:
    source = await make_source()
    await add_article(
        source,
        "old",
        "Вчерашняя новость",
        symbols=["BTC/USDT"],
        sentiment=0.9,
        impact=3,
        created_at=NOW - timedelta(hours=20),
    )

    assert (await build_context("BTC/USDT", now=NOW)).is_empty


async def test_empty_context_for_unknown_symbol(db) -> None:
    context = await build_context("NOTHING/USDT", now=NOW)
    assert context.is_empty
    assert context.sentiment == 0.0


# --------------------------------------------------------------------------- #
# Взвешивание тона
# --------------------------------------------------------------------------- #


async def test_impact_zero_does_not_move_sentiment(db) -> None:
    """Пересказ вчерашних движений не должен влиять на общий тон."""
    source = await make_source()
    await add_article(source, "real", "Важное", symbols=["BTC/USDT"], sentiment=1.0, impact=3)
    await add_article(source, "noise", "Шум", symbols=["BTC/USDT"], sentiment=-1.0, impact=0)

    context = await build_context("BTC/USDT", now=NOW)

    assert context.article_count == 2
    assert context.sentiment == pytest.approx(1.0), "материал с impact=0 не имеет веса"


async def test_higher_impact_dominates_sentiment(db) -> None:
    source = await make_source()
    await add_article(source, "big", "Сильное", symbols=["BTC/USDT"], sentiment=-1.0, impact=3)
    await add_article(source, "small", "Слабое", symbols=["BTC/USDT"], sentiment=1.0, impact=1)

    context = await build_context("BTC/USDT", now=NOW)
    assert context.sentiment < 0


async def test_fresher_news_weighs_more(db) -> None:
    source = await make_source()
    await add_article(
        source, "fresh", "Свежее", symbols=["BTC/USDT"], sentiment=1.0, impact=2,
        published_at=NOW,
    )
    await add_article(
        source, "older", "Постарше", symbols=["BTC/USDT"], sentiment=-1.0, impact=2,
        published_at=NOW - timedelta(hours=11),
    )

    context = await build_context("BTC/USDT", now=NOW)
    assert context.sentiment > 0, "новость 11-часовой давности не должна перевешивать свежую"


async def test_source_weight_affects_sentiment(db) -> None:
    trusted = await make_source("Trusted", weight=1.0)
    weak = await make_source("Weak", weight=0.2)

    await add_article(trusted, "t", "От надёжного", symbols=["BTC/USDT"], sentiment=1.0, impact=2)
    await add_article(weak, "w", "От слабого", symbols=["BTC/USDT"], sentiment=-1.0, impact=2)

    context = await build_context("BTC/USDT", now=NOW)
    assert context.sentiment > 0


async def test_macro_counts_less_than_direct_coverage(db) -> None:
    """Макро релевантно, но менее адресно, чем новость про сам инструмент."""
    source = await make_source()
    await add_article(source, "direct", "Про BTC", symbols=["BTC/USDT"], sentiment=1.0, impact=2)
    await add_article(source, "macro", "Про ФРС", symbols=[MACRO_SYMBOL], sentiment=-1.0, impact=2)

    context = await build_context("BTC/USDT", now=NOW)
    assert context.sentiment > 0


@pytest.mark.parametrize(
    ("age_hours", "expected"),
    [(0, 1.0), (6, 0.5), (12, 0.25)],
)
def test_recency_weight_halves_every_six_hours(age_hours: float, expected: float) -> None:
    published = NOW - timedelta(hours=age_hours)
    assert recency_weight(published, NOW) == pytest.approx(expected)


def test_missing_date_gets_middling_weight() -> None:
    """Часть лент не отдаёт дату — такой материал не выбрасываем и не считаем свежим."""
    assert 0.0 < recency_weight(None, NOW) < 1.0


# --------------------------------------------------------------------------- #
# Сводные поля и отрисовка
# --------------------------------------------------------------------------- #


async def test_context_collects_topics_and_mentioned_levels(db) -> None:
    source = await make_source()
    await add_article(
        source, "a", "A", symbols=["BTC/USDT"], sentiment=0.2, impact=2,
        levels=[70000.0, 72000.0], topics=["etf", "regulation"],
    )
    await add_article(
        source, "b", "B", symbols=["BTC/USDT"], sentiment=0.3, impact=2,
        levels=[70000.0], topics=["etf"],
    )

    context = await build_context("BTC/USDT", now=NOW)

    assert context.topics[0] == "etf"
    assert context.mentioned_levels == [70000.0, 72000.0], "дубли уровней схлопываются"
    assert context.max_impact == 2


@pytest.mark.parametrize(
    ("sentiment", "label"),
    [(0.6, "позитивный"), (-0.6, "негативный"), (0.1, "нейтральный")],
)
async def test_sentiment_label(db, sentiment: float, label: str) -> None:
    source = await make_source()
    await add_article(source, "a", "A", symbols=["BTC/USDT"], sentiment=sentiment, impact=3)
    assert (await build_context("BTC/USDT", now=NOW)).sentiment_label() == label


async def test_render_states_when_there_is_nothing(db) -> None:
    context = await build_context("BTC/USDT", now=NOW)
    assert "не было" in render_for_prompt(context)


async def test_render_includes_theses_and_macro_marker(db) -> None:
    source = await make_source()
    await add_article(source, "a", "Про BTC", symbols=["BTC/USDT"], sentiment=0.4, impact=2)
    await add_article(source, "m", "Про ФРС", symbols=[MACRO_SYMBOL], sentiment=-0.3, impact=3)

    rendered = render_for_prompt(await build_context("BTC/USDT", now=NOW))

    assert "Тезис: Про BTC" in rendered
    assert "[макро]" in rendered
    assert "Материалов за 12ч: 2" in rendered
