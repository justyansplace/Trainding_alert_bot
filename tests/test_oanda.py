"""Тесты провайдера OANDA: символы, торговые часы, поведение вокруг выходных.

Живых запросов здесь нет — проверяется то, что принадлежит нам: приведение
символов, разбор ответа и календарь сессий.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from alert_bot.market import registry
from alert_bot.market.providers import oanda_provider as oanda

NY = ZoneInfo("America/New_York")


def ny(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=NY)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
    from alert_bot import config

    config._settings = None
    yield oanda.OandaProvider()
    config._settings = None


# --------------------------------------------------------------------------- #
# Символы
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EUR/USD", "EUR_USD"),
        ("eur-usd", "EUR_USD"),
        ("EUR_USD", "EUR_USD"),
        (" xau/usd ", "XAU_USD"),
        ("GBP/JPY", "GBP_JPY"),
    ],
)
def test_symbol_normalisation(raw: str, expected: str) -> None:
    """Человек пишет EUR/USD, OANDA понимает только EUR_USD."""
    assert oanda.normalize_symbol(raw) == expected


def test_display_symbol_matches_crypto_style() -> None:
    """В боте уже есть BTC/USDT — форекс должен выглядеть так же."""
    assert oanda.display_symbol("EUR_USD") == "EUR/USD"
    assert oanda.display_symbol("XAU_USD") == "XAU/USD"


def test_symbol_roundtrip_is_stable() -> None:
    for symbol in ("EUR_USD", "XAU_USD", "GBP_JPY"):
        assert oanda.normalize_symbol(oanda.display_symbol(symbol)) == symbol


# --------------------------------------------------------------------------- #
# Торговые часы
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("moment", "is_open", "why"),
    [
        (ny(2026, 8, 19, 12), True, "среда, разгар дня"),
        (ny(2026, 8, 21, 16), True, "пятница до закрытия"),
        (ny(2026, 8, 21, 17), False, "пятница, момент закрытия"),
        (ny(2026, 8, 21, 20), False, "вечер пятницы"),
        (ny(2026, 8, 22, 12), False, "суббота"),
        (ny(2026, 8, 23, 12), False, "воскресенье до открытия"),
        (ny(2026, 8, 23, 17), True, "воскресенье, момент открытия"),
        (ny(2026, 8, 24, 3), True, "ночь понедельника"),
    ],
)
def test_forex_session_calendar(provider, moment: datetime, is_open: bool, why: str) -> None:
    """Форекс стоит с вечера пятницы до вечера воскресенья.

    Граница ездит вместе с переходом на летнее время, поэтому считается она в
    нью-йоркской зоне, а не в фиксированном смещении от UTC.
    """
    assert provider.is_market_open(moment) is is_open, why


def test_session_boundary_is_computed_in_new_york(provider) -> None:
    """Один и тот же момент UTC по разные стороны от перехода на летнее время.

    В июле 17:00 Нью-Йорка — это 21:00 UTC, в январе — 22:00 UTC. Захардкоженное
    смещение ошибалось бы на час полгода в году.
    """
    summer = datetime(2026, 7, 17, 21, 30, tzinfo=UTC)  # пятница, уже закрыт
    winter = datetime(2026, 1, 16, 21, 30, tzinfo=UTC)  # пятница, ещё открыт

    assert provider.is_market_open(summer) is False
    assert provider.is_market_open(winter) is True


def test_crypto_provider_is_always_open() -> None:
    from alert_bot.market.providers.ccxt_provider import CcxtProvider

    assert CcxtProvider("binance").is_market_open(ny(2026, 8, 22, 3)) is True
    assert CcxtProvider("binance").is_24_7() is True


def test_oanda_is_not_24_7(provider) -> None:
    assert provider.is_24_7() is False


# --------------------------------------------------------------------------- #
# Маршрутизация провайдеров
# --------------------------------------------------------------------------- #


def test_exchange_name_selects_provider() -> None:
    """Пользователь называет площадку, а не провайдера."""
    assert registry.provider_for("oanda") == "oanda"
    assert registry.provider_for("binance") == "ccxt"
    assert registry.provider_for("bybit") == "ccxt"
    assert registry.provider_for("OANDA") == "oanda"


def test_both_providers_are_registered() -> None:
    from alert_bot.market.providers.base import get_provider

    assert get_provider("ccxt", exchange="binance") is not None
    assert get_provider("oanda", exchange="oanda") is not None


# --------------------------------------------------------------------------- #
# Разрыв истории после выходных
# --------------------------------------------------------------------------- #


async def test_history_cleared_after_market_reopens(db) -> None:
    """Между пятничным и понедельничным тиком лежит гэп.

    Сравнивать их как соседние значения нельзя: детектор решит, что цена
    «подошла» к уровню, хотя она его перепрыгнула на открытии.
    """
    from alert_bot.db.models import Instrument
    from alert_bot.db.session import session_scope
    from alert_bot.scheduler import PriceLoop

    async with session_scope() as session:
        instrument = Instrument(
            symbol="EUR/USD",
            provider="oanda",
            exchange="oanda",
            round_step=0.01,
            price_precision=5,
            keywords=[],
            added_by=1,
            last_price=1.17,
            atr=0.0023,
        )
        session.add(instrument)
        await session.flush()
        stored = instrument

    loop = PriceLoop()
    state = loop.runtime(stored.id)
    state.push_price(1.1700)
    state.push_price(1.1705)
    assert len(state.price_history) == 2

    # Пятница, рынок уже закрыт.
    await loop.process_instrument(stored, datetime(2026, 8, 21, 22, tzinfo=UTC))
    assert state.market_closed is True
    assert len(state.price_history) == 2, "закрытие не должно ничего стирать само по себе"


# --------------------------------------------------------------------------- #
# Провайдер Yahoo
# --------------------------------------------------------------------------- #


from alert_bot.market.providers import yahoo_provider as yahoo  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USD/CAD", "USD/CAD"),
        ("usd-cad", "USD/CAD"),
        ("USD_CAD", "USD/CAD"),
        ("usdcad", "USD/CAD"),
        ("brent", "BRENT"),
        ("xau/usd", "XAU/USD"),
    ],
)
def test_yahoo_symbol_normalisation(raw: str, expected: str) -> None:
    """Человек пишет как привык, включая слитно."""
    assert yahoo.normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [
        ("USD/CAD", "USDCAD=X"),
        ("AUD/USD", "AUDUSD=X"),
        ("BRENT", "BZ=F"),
        ("XAU/USD", "GC=F"),
        ("WTI", "CL=F"),
    ],
)
def test_yahoo_ticker_mapping(symbol: str, ticker: str) -> None:
    assert yahoo.to_ticker(symbol) == ticker


def test_unknown_symbol_has_no_ticker() -> None:
    assert yahoo.to_ticker("НЕТУ/ТАКОГО") is None


@pytest.mark.parametrize(
    ("symbol", "delay"),
    [
        ("EUR/USD", 0),
        ("USD/CAD", 0),
        ("AUD/USD", 0),
        ("BRENT", 10),
        ("XAU/USD", 10),
        ("XAG/USD", 10),
        ("SPX500", 10),
    ],
)
def test_quote_delay_is_reported_per_symbol(symbol: str, delay: int) -> None:
    """Задержка обязана доходить до человека до добавления инструмента.

    При пороге в 0.3×ATR десятиминутное отставание означает, что алерт приходит
    после движения. Молча подсунуть такой инструмент — обмануть пользователя.
    """
    assert yahoo.YahooProvider().quote_delay_minutes(symbol) == delay


def test_crypto_provider_reports_no_delay() -> None:
    from alert_bot.market.providers.ccxt_provider import CcxtProvider

    assert CcxtProvider("binance").quote_delay_minutes("BTC/USDT") == 0


@pytest.mark.parametrize(
    ("moment", "is_open"),
    [
        (ny(2026, 8, 19, 12), True),
        (ny(2026, 8, 22, 12), False),
        (ny(2026, 8, 23, 18), True),
        (ny(2026, 8, 21, 18), False),
    ],
)
def test_yahoo_respects_weekend(moment: datetime, is_open: bool) -> None:
    assert yahoo.YahooProvider().is_market_open(moment) is is_open


def test_yahoo_exchange_routes_to_yahoo_provider() -> None:
    assert registry.provider_for("yahoo") == "yahoo"


def test_all_three_providers_registered() -> None:
    from alert_bot.market.providers.base import get_provider

    for name in ("ccxt", "oanda", "yahoo"):
        assert get_provider(name, exchange=name) is not None


def test_symbol_map_has_no_broken_reverse() -> None:
    """Каждый тикер должен иметь понятное имя для показа человеку."""
    for human, ticker in yahoo.SYMBOL_MAP.items():
        assert yahoo._REVERSE.get(ticker), f"нет обратного имени для {ticker} ({human})"


def test_float32_artifacts_are_cleaned() -> None:
    """Yahoo отдаёт цены как float32.

    При переводе в float64 вылезает мусор в хвосте: 1.386 превращается в
    1.3860000371932983. Такие числа попадают в базу и в сравнения уровней —
    хвост срезается по фактической точности float32.
    """
    assert yahoo._clean(1.3860000371932983) == 1.386
    assert yahoo._clean(92.08999633789062) == 92.09
    assert yahoo._clean(4696.10009765625) == 4696.1


def test_cleaning_keeps_meaningful_precision() -> None:
    """Срезать надо мусор, а не значащие цифры."""
    assert yahoo._clean(0.09096) == 0.09096
    assert yahoo._clean(80750.12) == 80750.12
    assert yahoo._clean(1.16725) == 1.16725


# --------------------------------------------------------------------------- #
# HTTP-проверка живости (нужна платформам вроде Railway)
# --------------------------------------------------------------------------- #


def test_port_read_only_when_platform_sets_it(monkeypatch) -> None:
    """Локально и в docker compose PORT не задан — лишний сокет не открывается."""
    from alert_bot import webhealth

    monkeypatch.delenv("PORT", raising=False)
    assert webhealth.port_from_env() is None

    monkeypatch.setenv("PORT", "8080")
    assert webhealth.port_from_env() == 8080

    monkeypatch.setenv("PORT", "не-число")
    assert webhealth.port_from_env() is None


async def test_health_endpoint_reflects_heartbeat(db, monkeypatch, tmp_path) -> None:
    """503 при мёртвом цикле — иначе платформа не узнает, что бот встал.

    Docker умеет выполнять команду внутри контейнера, Railway ходит только
    HTTP-запросом снаружи, и без этого пути живость там не проверяется вовсе.
    """
    import time

    from alert_bot import health

    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setattr(health, "heartbeat_path", lambda: heartbeat)

    assert health.check()[0] is False, "без пульса — нездоров"

    heartbeat.write_text(str(time.time()))
    assert health.check()[0] is True

    heartbeat.write_text(str(time.time() - 99999))
    healthy, detail = health.check()
    assert healthy is False and "протух" in detail


@pytest.mark.parametrize(
    ("terminal_name", "canonical"),
    [
        ("USTEC", "NAS100"),
        ("US500", "SPX500"),
        ("UKOIL", "BRENT"),
        ("USOIL", "WTI"),
        ("DAX", "DE40"),
        ("usdcnh", "USD/CNH"),
    ],
)
def test_terminal_names_are_understood(terminal_name: str, canonical: str) -> None:
    """Люди переносят названия прямо из своего терминала.

    Одно и то же там зовётся по-разному: US500 против SPX500, USTEC против
    NAS100. Заставлять человека знать наше внутреннее имя незачем.
    """
    assert yahoo.normalize_symbol(terminal_name) == canonical
    assert canonical in yahoo.SYMBOL_MAP


def test_bulk_parser_guesses_venue() -> None:
    """Площадка угадывается, чтобы не писать её у каждого из четырнадцати."""
    from alert_bot.bot.admin_instruments import _parse_bulk

    assert _parse_bulk(["EURUSD", "BTC/USDT", "SOL/USDT@bybit", "US500"]) == [
        ("EURUSD", "yahoo"),
        ("BTC/USDT", "binance"),
        ("SOL/USDT", "bybit"),
        ("US500", "yahoo"),
    ]


def test_bulk_parser_tolerates_commas_and_blanks() -> None:
    from alert_bot.bot.admin_instruments import _parse_bulk

    assert _parse_bulk(["EURUSD,", "", "  ", "XAUUSD"]) == [
        ("EURUSD", "yahoo"),
        ("XAUUSD", "yahoo"),
    ]


def test_presets_reference_known_symbols() -> None:
    """Набор с опечаткой обнаружился бы только при попытке добавления."""
    from alert_bot.bot.admin_instruments import PRESETS

    for _title, items in PRESETS.values():
        for symbol, exchange in items:
            if exchange == "yahoo":
                assert yahoo.normalize_symbol(symbol) in yahoo.SYMBOL_MAP, symbol
