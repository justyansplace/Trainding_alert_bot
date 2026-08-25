"""Тесты извлечения с подставным клиентом Claude.

Живой вызов API здесь не делается: проверяется то, что принадлежит нам —
стабильность кэшируемого промпта, отбраковка мусорных ответов модели, учёт
расхода и поведение при исчерпании бюджета.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from alert_bot.db.models import Article, Extraction, Instrument, LlmUsage, Source, utcnow
from alert_bot.db.session import session_scope
from alert_bot.llm import client as llm_client
from alert_bot.llm import complete
from alert_bot.llm.complete import Usage
from alert_bot.llm.schemas import ArticleInsight, InsightBatch
from alert_bot.news import extract
from alert_bot.news.ingest import MACRO_SYMBOL


class FakeCall:
    """Записанный вызов provider-слоя."""

    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs


class FakeLlm:
    """Подменяет parse_structured: живой SDK в тестах не участвует."""

    def __init__(self, result, usage: Usage | None = None) -> None:  # noqa: ANN001
        self.result = result
        self.usage = usage or Usage(input_tokens=2000, output_tokens=300, cached_tokens=1500)
        self.calls: list[FakeCall] = []

    async def __call__(self, **kwargs):  # noqa: ANN003, ANN204
        self.calls.append(FakeCall(kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, self.usage


@pytest.fixture
def fake_llm(monkeypatch):
    """Возвращает установщик подставного ответа модели."""

    def install(result, usage: Usage | None = None):  # noqa: ANN001, ANN202
        fake = FakeLlm(result, usage)
        monkeypatch.setattr(extract, "parse_structured", fake)
        # Подставной слой есть — доступ к модели считается настроенным,
        # хотя реального ключа в тестовом окружении нет.
        monkeypatch.setattr(extract, "has_llm", lambda: True)
        return fake

    return install


def insight(index: int, symbols: list[str], **overrides) -> ArticleInsight:
    data = {
        "article_index": index,
        "relevant_symbols": symbols,
        "sentiment": 0.4,
        "impact": 2,
        "horizon": "intraday",
        "thesis": f"Тезис {index}",
        "mentioned_levels": [],
        "topics": ["etf"],
    }
    data.update(overrides)
    return ArticleInsight(**data)


async def seed_articles(count: int = 2) -> list[Article]:
    async with session_scope() as session:
        source = Source(
            name="Test", kind="rss", url="https://example.com/f", added_by=1, added_at=utcnow()
        )
        session.add(source)
        await session.flush()
        rows = [
            Article(
                url_hash=f"h{i}",
                source_id=source.id,
                url=f"https://example.com/{i}",
                title=f"Заголовок {i}",
                fetched_at=utcnow(),
                simhash=0,
                excerpt=f"Фрагмент {i}",
            )
            for i in range(count)
        ]
        session.add_all(rows)
        await session.flush()
        return rows


def make_instruments() -> list[Instrument]:
    return [
        Instrument(
            id=1, symbol="BTC/USDT", provider="ccxt", exchange="binance",
            round_step=500.0, price_precision=2, keywords=["btc", "bitcoin"], added_by=1,
        ),
        Instrument(
            id=2, symbol="ETH/USDT", provider="ccxt", exchange="binance",
            round_step=100.0, price_precision=2, keywords=["eth", "ethereum"], added_by=1,
        ),
    ]


# --------------------------------------------------------------------------- #
# Стабильность кэшируемого промпта
# --------------------------------------------------------------------------- #


def test_system_prompt_is_byte_stable_across_calls() -> None:
    """Кэш работает по точному совпадению префикса.

    Любая нестабильность — дата, счётчик, случайный порядок — молча превращает
    кэш в ноль, и это заметно только по счёту в конце месяца.
    """
    instruments = make_instruments()
    assert extract.build_system_prompt(instruments) == extract.build_system_prompt(instruments)


def test_system_prompt_ignores_instrument_order() -> None:
    instruments = make_instruments()
    assert extract.build_system_prompt(instruments) == extract.build_system_prompt(
        list(reversed(instruments))
    )


def test_system_prompt_ignores_keyword_order() -> None:
    first = make_instruments()
    second = make_instruments()
    second[0].keywords = ["bitcoin", "btc"]  # тот же набор, другой порядок
    assert extract.build_system_prompt(first) == extract.build_system_prompt(second)


def test_system_prompt_changes_when_instrument_added() -> None:
    instruments = make_instruments()
    extended = [*instruments, Instrument(
        id=3, symbol="SOL/USDT", provider="ccxt", exchange="binance",
        round_step=10.0, price_precision=2, keywords=["solana"], added_by=1,
    )]
    assert extract.build_system_prompt(instruments) != extract.build_system_prompt(extended)


def test_system_prompt_lists_macro() -> None:
    assert MACRO_SYMBOL in extract.build_system_prompt(make_instruments())


def test_batch_rendering_numbers_articles_from_one() -> None:
    class Stub:
        def __init__(self, title, excerpt):  # noqa: ANN001
            self.title, self.excerpt = title, excerpt

    rendered = extract.render_batch([Stub("Первый", "A"), Stub("Второй", "B")])
    assert "Материал 1" in rendered and "Материал 2" in rendered
    assert "Материал 0" not in rendered


# --------------------------------------------------------------------------- #
# Разбор ответа модели
# --------------------------------------------------------------------------- #


async def test_extraction_stores_insights_and_usage(db, fake_llm) -> None:
    articles = await seed_articles(2)
    fake_llm(InsightBatch(insights=[insight(1, ["BTC/USDT"]), insight(2, [MACRO_SYMBOL])]))

    processed, halted = await extract.process_pending(articles, make_instruments())

    assert processed == 2 and halted is None

    async with session_scope() as session:
        from sqlalchemy import select

        stored = (await session.scalars(select(Extraction))).all()
        usage = (await session.scalars(select(LlmUsage))).all()

    assert {e.url_hash for e in stored} == {"h0", "h1"}
    assert len(usage) == 1
    assert usage[0].purpose == "extraction"
    assert usage[0].cached_tokens == 1500


async def test_invented_symbols_are_dropped(db, fake_llm) -> None:
    """Символ вне списка отслеживаемых никуда не попадёт — но и храниться не должен."""
    articles = await seed_articles(1)
    fake_llm(InsightBatch(insights=[insight(1, ["BTC/USDT", "DOGE/USDT", "ВЫДУМАННЫЙ"])]))

    await extract.process_pending(articles, make_instruments())

    async with session_scope() as session:
        from sqlalchemy import select

        stored = (await session.scalars(select(Extraction))).one()

    assert stored.relevant_symbols == ["BTC/USDT"]


async def test_out_of_range_index_is_ignored(db, fake_llm) -> None:
    """Модель может сослаться на материал, которого в пакете нет."""
    articles = await seed_articles(2)
    fake_llm(InsightBatch(insights=[insight(1, ["BTC/USDT"]), insight(99, ["BTC/USDT"])]))

    processed, _ = await extract.process_pending(articles, make_instruments())
    assert processed == 1


async def test_articles_marked_processed_even_without_insights(db, fake_llm) -> None:
    """Иначе неразобранный материал возвращается в каждый проход и оплачивается снова."""
    articles = await seed_articles(2)
    fake_llm(InsightBatch(insights=[]))

    await extract.process_pending(articles, make_instruments())

    from alert_bot.news.ingest import pending_articles

    assert await pending_articles() == []


async def test_empty_parsed_output_does_not_crash(db, fake_llm) -> None:
    articles = await seed_articles(1)
    fake_llm(None)

    processed, halted = await extract.process_pending(articles, make_instruments())
    assert processed == 0 and halted is None


async def test_api_failure_skips_batch_without_stopping(db, fake_llm) -> None:
    articles = await seed_articles(1)
    fake_llm(RuntimeError("сеть отвалилась"))

    processed, halted = await extract.process_pending(articles, make_instruments())
    assert processed == 0
    assert halted is None, "сбой одного пакета не должен останавливать проход"


# --------------------------------------------------------------------------- #
# Бюджет
# --------------------------------------------------------------------------- #


async def test_extraction_halts_when_budget_is_spent(db, fake_llm) -> None:
    """Исчерпание бюджета — не ошибка: алерты продолжают идти без сводки."""
    from alert_bot.config import get_settings

    settings = get_settings()
    articles = await seed_articles(1)
    fake_llm(InsightBatch(insights=[]))

    async with session_scope() as session:
        session.add(
            LlmUsage(
                ts=utcnow(),
                model="gpt-5-nano",
                purpose="extraction",
                input_tokens=1,
                output_tokens=1,
                cost_usd=settings.daily_llm_budget_usd + 1.0,
            )
        )

    processed, halted = await extract.process_pending(articles, make_instruments())
    assert processed == 0
    assert halted is not None and "лимит" in halted


async def test_yesterdays_spend_does_not_block_today(db, fake_llm) -> None:
    from alert_bot.config import get_settings

    settings = get_settings()
    articles = await seed_articles(1)
    fake_llm(InsightBatch(insights=[insight(1, ["BTC/USDT"])]))

    async with session_scope() as session:
        session.add(
            LlmUsage(
                ts=utcnow() - timedelta(hours=30),
                model="gpt-5-nano",
                purpose="extraction",
                input_tokens=1,
                output_tokens=1,
                cost_usd=settings.daily_llm_budget_usd * 10,
            )
        )

    processed, halted = await extract.process_pending(articles, make_instruments())
    assert processed == 1 and halted is None


# --------------------------------------------------------------------------- #
# Расчёт стоимости
# --------------------------------------------------------------------------- #


def test_cached_tokens_cost_a_tenth_of_fresh_ones() -> None:
    fresh = llm_client.estimate_cost("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)
    cached = llm_client.estimate_cost(
        "claude-sonnet-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert cached == pytest.approx(fresh * 0.1)


def test_cost_uses_per_model_pricing() -> None:
    sonnet = llm_client.estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000)
    opus = llm_client.estimate_cost("claude-opus-5", 1_000_000, 1_000_000)
    assert opus > sonnet


def test_unknown_model_falls_back_to_most_expensive() -> None:
    """Неизвестную модель безопаснее переоценить, чем недооценить."""
    unknown = llm_client.estimate_cost("claude-something-new", 1_000_000, 1_000_000)
    opus = llm_client.estimate_cost("claude-opus-5", 1_000_000, 1_000_000)
    assert unknown == pytest.approx(opus)


async def test_usage_report_groups_by_model_and_purpose(db) -> None:
    async with session_scope() as session:
        session.add_all(
            [
                LlmUsage(ts=utcnow(), model="claude-sonnet-5", purpose="extraction",
                         input_tokens=100, output_tokens=10, cost_usd=0.5),
                LlmUsage(ts=utcnow(), model="claude-sonnet-5", purpose="extraction",
                         input_tokens=200, output_tokens=20, cost_usd=0.7),
                LlmUsage(ts=utcnow(), model="claude-opus-5", purpose="brief",
                         input_tokens=50, output_tokens=5, cost_usd=0.3),
            ]
        )

    report = {(m, p): (i, o, c) for m, p, i, o, c in await llm_client.usage_report(24)}

    assert report[("claude-sonnet-5", "extraction")] == (300, 30, pytest.approx(1.2))
    assert report[("claude-opus-5", "brief")] == (50, 5, pytest.approx(0.3))
    assert await llm_client.daily_spend_usd() == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# Совместимость параметров с моделью
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model", "expected"),
    [("claude-haiku-4-5", False), ("claude-sonnet-5", True), ("claude-opus-5", True)],
)
def test_anthropic_effort_only_where_accepted(model: str, expected: bool) -> None:
    """Haiku 4.5 не принимает output_config.effort и отвечает ошибкой."""
    assert bool(complete.anthropic_effort(model, "low")) is expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gpt-5-nano", True), ("gpt-5-mini", True), ("gpt-4o-mini", False), ("gpt-4.1-nano", False)],
)
def test_openai_reasoning_only_for_gpt5(model: str, expected: bool) -> None:
    """reasoning_effort понимает только семейство gpt-5, остальные вернут ошибку."""
    assert bool(complete.openai_reasoning(model, "minimal")) is expected


def test_provider_param_shapes() -> None:
    assert complete.anthropic_effort("claude-sonnet-5", "low") == {
        "output_config": {"effort": "low"}
    }
    assert complete.openai_reasoning("gpt-5-nano", "minimal") == {
        "reasoning_effort": "minimal"
    }


def test_openai_usage_does_not_double_count_cached_tokens() -> None:
    """У OpenAI кэшированные токены лежат внутри prompt_tokens.

    Без вычитания они оплачиваются дважды — по полной ставке в составе prompt
    и ещё раз по льготной, и расход в отчёте завышается.
    """

    class Details:
        cached_tokens = 800

    class RawUsage:
        prompt_tokens = 1000
        completion_tokens = 200
        prompt_tokens_details = Details()

    usage = complete._openai_usage(RawUsage())
    assert usage.input_tokens == 200
    assert usage.cached_tokens == 800
    assert usage.output_tokens == 200


def test_openai_usage_without_details() -> None:
    class RawUsage:
        prompt_tokens = 500
        completion_tokens = 100
        prompt_tokens_details = None

    usage = complete._openai_usage(RawUsage())
    assert usage.input_tokens == 500 and usage.cached_tokens == 0


def test_missing_usage_is_zero_not_crash() -> None:
    assert complete._openai_usage(None) == Usage()
    assert complete._anthropic_usage(None) == Usage()


async def test_extraction_sends_schema_and_batches(db, fake_llm) -> None:
    """12 материалов уходят одним запросом с нужной схемой."""
    articles = await seed_articles(12)
    fake = fake_llm(InsightBatch(insights=[]))

    await extract.process_pending(articles, make_instruments())

    assert len(fake.calls) == 1, "12 материалов должны уйти одним запросом"
    assert fake.calls[0].kwargs["schema"] is InsightBatch


def test_pricing_covers_configured_models() -> None:
    """Модель без цены считалась бы по верхней ставке и врала бы в /usage."""
    from alert_bot.config import get_settings
    from alert_bot.llm.client import PRICING

    settings = get_settings()
    assert settings.extraction_model in PRICING
    assert settings.brief_model in PRICING


def test_openai_is_cheaper_than_anthropic_for_extraction() -> None:
    """Проверка порядка величин: ради этого провайдер и менялся."""
    from alert_bot.llm.client import estimate_cost

    nano = estimate_cost("gpt-5-nano", 6200, 4800)
    haiku = estimate_cost("claude-haiku-4-5", 6200, 4800)
    assert nano < haiku / 5


# --------------------------------------------------------------------------- #
# Безнадёжные ошибки доступа
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}",
        "Error code: 401 - {'error': {'code': 'invalid_api_key'}}",
        "your billing account is not active",
    ],
)
def test_terminal_access_errors_are_classified(message: str) -> None:
    """Нехватка средств приходит как 429, а SDK считает 429 временным.

    Без отдельного класса каждый проход по новостям превращается в десятки
    заведомо безнадёжных запросов с ретраями и стену в логе.
    """
    assert isinstance(complete._classify(RuntimeError(message)), complete.LlmUnavailable)


def test_transient_errors_stay_transient() -> None:
    assert not isinstance(
        complete._classify(RuntimeError("Error code: 500 - server error")),
        complete.LlmUnavailable,
    )
    assert not isinstance(
        complete._classify(TimeoutError("read timeout")), complete.LlmUnavailable
    )


def test_status_code_401_is_terminal() -> None:
    class Denied(Exception):
        status_code = 401

    assert isinstance(complete._classify(Denied("nope")), complete.LlmUnavailable)


async def test_quota_error_halts_whole_pass(db, fake_llm) -> None:
    """Проход прекращается целиком, а не буксует на каждом пакете."""
    articles = await seed_articles(24)  # два пакета
    fake = fake_llm(complete.LlmUnavailable("insufficient_quota"))

    processed, halted = await extract.process_pending(articles, make_instruments())

    assert processed == 0
    assert halted is not None and "доступ к модели закрыт" in halted
    assert len(fake.calls) == 1, "второй пакет не должен даже пробоваться"


async def test_articles_survive_quota_outage(db, fake_llm) -> None:
    """Материалы остаются неразобранными и дождутся пополнения счёта."""
    from alert_bot.news.ingest import pending_articles

    articles = await seed_articles(3)
    fake_llm(complete.LlmUnavailable("insufficient_quota"))

    await extract.process_pending(articles, make_instruments())

    assert len(await pending_articles()) == 3


# --------------------------------------------------------------------------- #
# Проверка уровней, названных в материале
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("levels", "text", "expected"),
    [
        ([78000.0], "Bitcoin breaks above $78,000 as ETF inflows accelerate", [78000.0]),
        ([78000.0], "Bitcoin tops 78k on ETF demand", [78000.0]),
        ([2450.5], "ETH holds 2450.5 support", [2450.5]),
        ([77250.0], "Bitcoin Climbs Higher as $1.2 Billion in Shorts Liquidated", []),
        ([77.0], "Crypto roars back as bitcoin posts its second-best week", []),
        ([29000.0], "ETFs draw $2.6 billion in strongest inflow week", []),
        ([], "любой текст", []),
    ],
)
def test_only_levels_present_in_text_survive(
    levels: list[float], text: str, expected: list[float]
) -> None:
    """Эти числа показываются как процитированные аналитиками.

    Выдуманное число здесь опаснее отсутствующего: пользователь примет его за
    уровень из публикации. На живых данных модель промахивалась примерно на
    каждом восьмом числе, включая «77» вместо 77000.
    """
    assert extract.verify_levels(levels, text) == expected


async def test_fabricated_levels_never_reach_storage(db, fake_llm) -> None:
    articles = await seed_articles(1)
    fake_llm(
        InsightBatch(
            insights=[insight(1, ["BTC/USDT"], mentioned_levels=[99999.0, 12345.0])]
        )
    )

    await extract.process_pending(articles, make_instruments())

    async with session_scope() as session:
        from sqlalchemy import select

        stored = (await session.scalars(select(Extraction))).one()

    assert stored.mentioned_levels == [], "в заголовке «Заголовок 0» этих чисел нет"
