"""Отказ площадки по частоте запросов: пауза вместо повторов.

Binance отвечает 418 после проигнорированных 429, и бан у неё нарастающий —
каждый запрос во время паузы её продлевает. Цикл цены идёт раз в десять секунд,
поэтому «просто повторить позже» означает держать бан бесконечно.
"""

from __future__ import annotations

import ccxt.async_support as ccxt
import pytest

from alert_bot.market.providers import ccxt_provider as prov
from alert_bot.market.providers.base import ExchangeBanned


@pytest.fixture(autouse=True)
def _clean_bans():
    prov.reset_bans()
    yield
    prov.reset_bans()


def ddos() -> Exception:
    """Ровно то, во что ccxt превращает 418 от Binance."""
    return ccxt.DDoSProtection(
        'binance 418 Unknown {"code":-1003,"msg":"Way too many requests."}'
    )


async def test_rate_limit_answer_starts_a_pause() -> None:
    async def boom():
        raise ddos()

    with pytest.raises(ExchangeBanned) as caught:
        await prov._call("binance", boom)

    assert caught.value.exchange == "binance"
    assert prov.ban_seconds_left("binance") == pytest.approx(120, abs=2)


async def test_requests_during_the_pause_never_reach_the_network() -> None:
    """Главное: запрос во время паузы не уходит, иначе бан продлевается."""
    calls = []

    async def boom():
        calls.append("boom")
        raise ddos()

    async def ok():
        calls.append("ok")
        return "данные"

    with pytest.raises(ExchangeBanned):
        await prov._call("binance", boom)
    with pytest.raises(ExchangeBanned):
        await prov._call("binance", ok)

    assert calls == ["boom"]


async def test_pause_grows_with_each_violation() -> None:
    async def boom():
        raise ddos()

    seen = []
    for _ in range(3):
        prov._banned_until["binance"] = 0.0  # пауза истекла естественным путём
        with pytest.raises(ExchangeBanned) as caught:
            await prov._call("binance", boom)
        seen.append(caught.value.seconds_left)

    assert seen == [120, 300, 900]


async def test_successful_answer_resets_the_escalation() -> None:
    """Иначе давний бан навсегда оставил бы паузу часовой."""
    async def boom():
        raise ddos()

    async def ok():
        return "данные"

    with pytest.raises(ExchangeBanned):
        await prov._call("binance", boom)
    prov._banned_until["binance"] = 0.0

    assert await prov._call("binance", ok) == "данные"
    assert prov.ban_seconds_left("binance") == 0

    with pytest.raises(ExchangeBanned) as caught:
        await prov._call("binance", boom)
    assert caught.value.seconds_left == 120


async def test_pause_is_per_exchange() -> None:
    """Бан висит на IP у конкретной площадки — bybit тут ни при чём."""
    async def boom():
        raise ddos()

    async def ok():
        return "данные"

    with pytest.raises(ExchangeBanned):
        await prov._call("binance", boom)

    assert await prov._call("bybit", ok) == "данные"


async def test_other_errors_do_not_start_a_pause() -> None:
    """Таймаут — не отказ по частоте: тормозить из-за него весь цикл нельзя."""
    async def boom():
        raise ccxt.RequestTimeout("binance timeout")

    with pytest.raises(ccxt.RequestTimeout):
        await prov._call("binance", boom)
    assert prov.ban_seconds_left("binance") == 0


async def test_message_names_the_wait_not_the_symbol() -> None:
    exc = ExchangeBanned("binance", 300)
    assert "binance" in str(exc)
    assert "5 мин" in str(exc)


# --------------------------------------------------------------------------- #
# Через провайдер и через админку
# --------------------------------------------------------------------------- #


async def test_provider_stops_before_touching_the_exchange() -> None:
    """Во время паузы не создаётся даже инстанс биржи — сети не касаемся."""
    prov._banned_until["binance"] = prov.time.monotonic() + 300
    provider = prov.CcxtProvider(exchange="binance")

    with pytest.raises(ExchangeBanned):
        await provider.fetch_ohlcv("BTC/USDT", "1h", limit=3)
    with pytest.raises(ExchangeBanned):
        await provider.validate_symbol("BTC/USDT")

    assert "binance" not in prov._exchanges


class StubStatus:
    def __init__(self) -> None:
        self.text = ""

    async def edit_text(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.text = text


class StubMessage:
    def __init__(self) -> None:
        self.status = StubStatus()

    async def answer(self, text: str, reply_markup=None):  # noqa: ANN001, ANN202
        return self.status


async def test_admin_sees_the_wait_and_a_way_around(db, monkeypatch) -> None:
    """Вместо обрезанного JSON — сколько ждать и что можно сделать сейчас."""
    from alert_bot.bot import instruments
    from alert_bot.db.models import User

    async def banned(symbol: str, exchange: str):  # noqa: ANN202
        raise ExchangeBanned(exchange, 300)

    monkeypatch.setattr(instruments.registry, "validate_candidate", banned)

    message = StubMessage()
    await instruments._add_many(
        message, User(tg_id=1), [("BTC/USDT", "binance"), ("ETH/USDT", "binance")]
    )

    text = message.status.text
    assert "5 мин" in text
    assert "bybit" in text
    # Код 418 остаётся — по нему гуглится причина. Сырого тела ответа быть не должно.
    assert "418" in text
    assert "Way too many" not in text and "msg" not in text
    assert text.count("⏳") == 1  # одна площадка — одно объяснение, не по разу на символ
