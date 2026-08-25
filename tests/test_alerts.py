"""Тесты связки детектора с рассылкой: пороги, тихие часы, потолок, формат."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alert_bot import alerts
from alert_bot.config import get_settings
from alert_bot.db.models import (
    Alert,
    Instrument,
    Level as LevelRow,
    Subscription,
    User,
    utcnow,
)
from alert_bot.db.session import session_scope
from alert_bot.market.detector import LevelEvent


class StubNotifier:
    """Вместо Telegram — список отправленного."""

    def __init__(self) -> None:
        self.sent: list = []

    def enqueue(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


async def make_instrument(symbol: str = "BTC/USDT") -> Instrument:
    async with session_scope() as session:
        instrument = Instrument(
            symbol=symbol,
            provider="ccxt",
            exchange="binance",
            round_step=500.0,
            price_precision=2,
            keywords=["bitcoin"],
            added_by=1,
            last_price=77_000.0,
            atr=700.0,
        )
        session.add(instrument)
        await session.flush()
        session.add(
            LevelRow(
                id=1,
                instrument_id=instrument.id,
                price=77_500.0,
                kinds=["PDH", "round"],
                score=8.2,
                touches=3,
            )
        )
        return instrument


async def make_user(
    tg_id: int,
    *,
    def_min_score: float | None = None,
    def_atr_k: float | None = None,
    quiet: tuple[int, int] | None = None,
    tz: str = "UTC",
) -> None:
    async with session_scope() as session:
        session.add(
            User(
                tg_id=tg_id,
                role="user",
                granted_at=utcnow(),
                tz=tz,
                quiet_from=quiet[0] if quiet else None,
                quiet_to=quiet[1] if quiet else None,
                def_min_score=def_min_score,
                def_atr_k=def_atr_k,
                active=True,
            )
        )


async def subscribe(tg_id: int, instrument_id: int, **overrides) -> None:
    async with session_scope() as session:
        session.add(
            Subscription(tg_id=tg_id, instrument_id=instrument_id, enabled=True, **overrides)
        )


def event(recipients: tuple[int, ...] = (1,)) -> LevelEvent:
    return LevelEvent(
        level_id=1,
        level_price=77_500.0,
        level_score=8.2,
        level_kinds=("PDH", "round"),
        kind="approach",
        price=77_250.0,
        distance_atr=0.36,
        recipients=recipients,
    )


# --------------------------------------------------------------------------- #
# Разрешение порогов
# --------------------------------------------------------------------------- #


async def test_subscription_override_wins_over_user_default(db) -> None:
    instrument = await make_instrument()
    await make_user(1, def_min_score=4.0, def_atr_k=0.3)
    await subscribe(1, instrument.id, min_score=9.0, atr_k=0.1)

    subs = await alerts.load_subscribers(instrument.id)
    assert subs[0].min_score == 9.0
    assert subs[0].atr_k == 0.1


async def test_user_default_used_when_subscription_is_null(db) -> None:
    instrument = await make_instrument()
    await make_user(1, def_min_score=7.5, def_atr_k=0.45)
    await subscribe(1, instrument.id)

    subs = await alerts.load_subscribers(instrument.id)
    assert subs[0].min_score == 7.5
    assert subs[0].atr_k == 0.45


async def test_config_default_used_when_everything_is_null(db) -> None:
    settings = get_settings()
    instrument = await make_instrument()
    await make_user(1)
    await subscribe(1, instrument.id)

    subs = await alerts.load_subscribers(instrument.id)
    assert subs[0].min_score == settings.default_min_score
    assert subs[0].atr_k == settings.default_atr_k


async def test_disabled_subscription_and_inactive_user_excluded(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    await make_user(2)
    await subscribe(1, instrument.id)
    await subscribe(2, instrument.id)

    async with session_scope() as session:
        sub = await session.get(Subscription, (1, instrument.id))
        sub.enabled = False
        user = await session.get(User, 2)
        user.active = False

    assert await alerts.load_subscribers(instrument.id) == []


# --------------------------------------------------------------------------- #
# Тихие часы
# --------------------------------------------------------------------------- #


def make_quiet_user(quiet_from: int, quiet_to: int, tz: str = "UTC") -> User:
    return User(tg_id=1, quiet_from=quiet_from, quiet_to=quiet_to, tz=tz)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(22, False), (23, True), (2, True), (6, True), (7, False), (12, False)],
)
def test_quiet_hours_across_midnight(hour: int, expected: bool) -> None:
    """Интервал 23→7 проходит через полночь — наивное сравнение здесь ломается."""
    user = make_quiet_user(23, 7)
    now = datetime(2026, 8, 22, hour, 0, tzinfo=UTC)
    assert alerts.in_quiet_hours(user, now) is expected


@pytest.mark.parametrize(("hour", "expected"), [(0, False), (9, True), (17, False)])
def test_quiet_hours_within_same_day(hour: int, expected: bool) -> None:
    user = make_quiet_user(8, 17)
    assert alerts.in_quiet_hours(user, datetime(2026, 8, 22, hour, tzinfo=UTC)) is expected


def test_quiet_hours_respect_user_timezone() -> None:
    """23:00 в Москве — это 20:00 UTC; считать надо по часам пользователя."""
    user = make_quiet_user(23, 7, tz="Europe/Moscow")
    assert alerts.in_quiet_hours(user, datetime(2026, 8, 22, 20, tzinfo=UTC)) is True
    assert alerts.in_quiet_hours(user, datetime(2026, 8, 22, 19, tzinfo=UTC)) is False


def test_unset_or_degenerate_quiet_hours_never_block() -> None:
    assert not alerts.in_quiet_hours(User(tg_id=1, quiet_from=None, quiet_to=None), utcnow())
    assert not alerts.in_quiet_hours(make_quiet_user(5, 5), utcnow())


def test_unknown_timezone_falls_back_to_utc() -> None:
    """Битая таймзона не должна ронять рассылку."""
    user = make_quiet_user(23, 7, tz="Nowhere/Nothing")
    assert alerts.in_quiet_hours(user, datetime(2026, 8, 22, 2, tzinfo=UTC)) is True


# --------------------------------------------------------------------------- #
# Фильтрация получателей и отправка
# --------------------------------------------------------------------------- #


async def test_dispatch_sends_to_subscriber(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    notifier = StubNotifier()

    sent = await alerts.dispatch_event(instrument, event((1,)), notifier)

    assert sent == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0].chat_id == 1


async def test_dispatch_skips_user_in_quiet_hours(db) -> None:
    instrument = await make_instrument()
    await make_user(1, quiet=(0, 23))
    notifier = StubNotifier()

    now = datetime(2026, 8, 22, 5, tzinfo=UTC)
    assert await alerts.dispatch_event(instrument, event((1,)), notifier, now=now) == 0
    assert notifier.sent == []


async def test_daily_cap_stops_further_alerts(db) -> None:
    """Подписка на несколько инструментов без потолка превращает бота в шум."""
    settings = get_settings()
    instrument = await make_instrument()
    await make_user(1)
    now = utcnow()

    async with session_scope() as session:
        for _ in range(settings.max_alerts_per_user_per_day):
            session.add(
                Alert(
                    level_id=1,
                    instrument_id=instrument.id,
                    kind="approach",
                    price=1.0,
                    level_price=1.0,
                    distance_atr=0.1,
                    ts=now - timedelta(minutes=5),
                    sent_to=[1],
                )
            )

    notifier = StubNotifier()
    assert await alerts.dispatch_event(instrument, event((1,)), notifier, now=now) == 0


async def test_yesterdays_alerts_do_not_count_toward_cap(db) -> None:
    settings = get_settings()
    instrument = await make_instrument()
    await make_user(1)
    now = utcnow()

    async with session_scope() as session:
        for _ in range(settings.max_alerts_per_user_per_day):
            session.add(
                Alert(
                    level_id=1,
                    instrument_id=instrument.id,
                    kind="approach",
                    price=1.0,
                    level_price=1.0,
                    distance_atr=0.1,
                    ts=now - timedelta(hours=30),
                    sent_to=[1],
                )
            )

    notifier = StubNotifier()
    assert await alerts.dispatch_event(instrument, event((1,)), notifier, now=now) == 1


async def test_dispatch_persists_alert_row(db) -> None:
    instrument = await make_instrument()
    await make_user(1)
    notifier = StubNotifier()

    await alerts.dispatch_event(instrument, event((1,)), notifier)

    async with session_scope() as session:
        from sqlalchemy import select

        row = (await session.scalars(select(Alert))).one()

    assert row.sent_to == [1]
    assert row.level_price == pytest.approx(77_500.0)
    assert row.distance_atr == pytest.approx(0.36)


# --------------------------------------------------------------------------- #
# Формат сообщения
# --------------------------------------------------------------------------- #


async def test_alert_text_contains_computed_numbers(db) -> None:
    """Числа в сообщении обязаны совпадать с посчитанными, а не быть переписаны."""
    instrument = await make_instrument()
    text = alerts.format_alert(instrument, event())

    assert "77 250.00" in text  # цена
    assert "77 500.00" in text  # уровень
    assert "0.36×ATR" in text
    assert "score 8.2" in text
    assert "макс. вчера + круглое число" in text
    assert "Не инвестиционная рекомендация" in text


async def test_alert_names_resistance_and_support_correctly(db) -> None:
    instrument = await make_instrument()

    above = alerts.format_alert(instrument, event())
    assert "сопротивлению" in above

    below = LevelEvent(
        level_id=1, level_price=76_000.0, level_score=8.2, level_kinds=("PDL",),
        kind="approach", price=77_250.0, distance_atr=0.4, recipients=(1,),
    )
    assert "поддержке" in alerts.format_alert(instrument, below)


async def test_breakout_text_states_direction(db) -> None:
    instrument = await make_instrument()
    up = LevelEvent(
        level_id=1, level_price=77_000.0, level_score=6.0, level_kinds=("PDH",),
        kind="breakout", price=77_800.0, distance_atr=1.1, recipients=(1,),
    )
    assert "пробой вверх" in alerts.format_alert(instrument, up)


async def test_brief_is_included_when_present(db) -> None:
    instrument = await make_instrument()
    text = alerts.format_alert(instrument, event(), brief="📰 Аналитики нейтральны.")
    assert "📰 Аналитики нейтральны." in text


async def test_price_precision_follows_instrument(db) -> None:
    """У DOGE пять знаков — двух не хватит, цена превратится в 0.09."""
    instrument = await make_instrument("DOGE/USDT")
    async with session_scope() as session:
        stored = await session.get(Instrument, instrument.id)
        stored.price_precision = 5

    async with session_scope() as session:
        stored = await session.get(Instrument, instrument.id)

    cheap = LevelEvent(
        level_id=1, level_price=0.09200, level_score=5.0, level_kinds=("round",),
        kind="approach", price=0.09096, distance_atr=0.3, recipients=(1,),
    )
    text = alerts.format_alert(stored, cheap)
    assert "0.09096" in text and "0.09200" in text
