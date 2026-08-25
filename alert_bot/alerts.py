"""Связка детектора с рассылкой: кому, что и с какими ограничениями.

Здесь же живут предохранители, которых нет в детекторе, потому что они про
пользователя, а не про рынок: тихие часы, дневной потолок алертов и разрешение
порогов (подписка → дефолт пользователя → дефолт конфига).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from alert_bot.bot.notifier import Notifier, Outgoing
from alert_bot.config import get_settings
from alert_bot.db.models import (
    Alert,
    Instrument,
    Level as LevelRow,
    Subscription,
    User,
    UserLevel,
    utcnow,
)
from alert_bot.db.session import session_scope
from alert_bot.market.detector import LevelEvent, Subscriber
from alert_bot.market.levels import HUMAN_KIND

log = logging.getLogger(__name__)

DISCLAIMER_LINE = "<i>Не инвестиционная рекомендация.</i>"


async def load_subscribers(instrument_id: int) -> list[Subscriber]:
    """Пороги: подписка → дефолт пользователя → дефолт конфига."""
    settings = get_settings()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Subscription.tg_id,
                    Subscription.min_score,
                    Subscription.atr_k,
                    Subscription.muted_until,
                    Subscription.threshold_unit,
                    Subscription.threshold_pct,
                    User.def_min_score,
                    User.def_atr_k,
                    User.def_threshold_unit,
                    User.def_threshold_pct,
                    User.direction_filter,
                )
                .join(User, User.tg_id == Subscription.tg_id)
                .where(
                    Subscription.instrument_id == instrument_id,
                    Subscription.enabled.is_(True),
                    User.active.is_(True),
                )
            )
        ).all()

    def pick(*values, fallback):  # noqa: ANN001, ANN202
        """Подписка -> дефолт пользователя -> дефолт конфига."""
        for value in values:
            if value is not None:
                return value
        return fallback

    return [
        Subscriber(
            tg_id=row.tg_id,
            min_score=pick(row.min_score, row.def_min_score, fallback=settings.default_min_score),
            atr_k=pick(row.atr_k, row.def_atr_k, fallback=settings.default_atr_k),
            unit=pick(
                row.threshold_unit, row.def_threshold_unit,
                fallback=settings.default_threshold_unit,
            ),
            threshold_pct=pick(
                row.threshold_pct, row.def_threshold_pct,
                fallback=settings.default_threshold_pct,
            ),
            direction=pick(row.direction_filter, fallback="any"),
            muted_until=row.muted_until,
        )
        for row in rows
    ]


async def cooldown_by_user(tg_ids: list[int]) -> dict[int, timedelta]:
    """Персональная пауза после срабатывания, где она задана."""
    if not tg_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(User.tg_id, User.def_cooldown_hours).where(User.tg_id.in_(tg_ids))
            )
        ).all()
    return {
        tg_id: timedelta(hours=hours) for tg_id, hours in rows if hours
    }


def in_quiet_hours(user: User, now: datetime) -> bool:
    """Тихие часы считаются в таймзоне пользователя, а не сервера."""
    if user.quiet_from is None or user.quiet_to is None:
        return False
    if user.quiet_from == user.quiet_to:
        return False

    try:
        local_hour = now.astimezone(ZoneInfo(user.tz or "UTC")).hour
    except (ZoneInfoNotFoundError, ValueError):
        local_hour = now.astimezone(UTC).hour

    if user.quiet_from < user.quiet_to:
        return user.quiet_from <= local_hour < user.quiet_to
    # Интервал через полночь, например 23 -> 7.
    return local_hour >= user.quiet_from or local_hour < user.quiet_to


async def alerts_sent_last_day(tg_id: int, now: datetime) -> int:
    since = now - timedelta(hours=24)
    async with session_scope() as session:
        rows = (
            await session.scalars(select(Alert.sent_to).where(Alert.ts >= since))
        ).all()
    return sum(1 for recipients in rows if recipients and tg_id in recipients)


async def filter_recipients(recipients: tuple[int, ...], now: datetime) -> list[int]:
    """Отсекает тихие часы и дневной потолок.

    Потолок нужен именно потому, что инструменты добавляет админ: подписка на
    восемь штук без него превращает бота в шум, и его замьютят целиком.
    """
    settings = get_settings()
    allowed: list[int] = []

    async with session_scope() as session:
        users = {
            u.tg_id: u
            for u in (
                await session.scalars(select(User).where(User.tg_id.in_(recipients)))
            ).all()
        }

    for tg_id in recipients:
        user = users.get(tg_id)
        if user is None or not user.active:
            continue
        if in_quiet_hours(user, now):
            log.debug("tg_id=%s в тихих часах", tg_id)
            continue
        limit = user.max_alerts_per_day or settings.max_alerts_per_user_per_day
        if await alerts_sent_last_day(tg_id, now) >= limit:
            log.info("tg_id=%s достиг дневного потолка алертов", tg_id)
            continue
        allowed.append(tg_id)

    return allowed


def format_alert(
    instrument: Instrument,
    event: LevelEvent,
    brief: str | None = None,
    user_level: UserLevel | None = None,
) -> str:
    """Текст алерта. Все числа — из посчитанных данных, не из модели."""
    precision = instrument.price_precision
    price = f"{event.price:,.{precision}f}".replace(",", " ")
    level_price = f"{event.level_price:,.{precision}f}".replace(",", " ")

    is_manual = user_level is not None
    marker = "📌" if is_manual else ("⚡️" if event.kind == "breakout" else "🎯")

    if event.kind == "breakout":
        direction = "вверх" if event.price > event.level_price else "вниз"
        what = f"пробой {direction}"
    else:
        side = "сопротивлению" if event.level_price > event.price else "поддержке"
        what = f"подход к {side}"

    prefix = "ваш уровень · " if is_manual else ""
    headline = f"{marker} <b>{instrument.symbol}</b> — {prefix}{what}"

    lines = [
        headline,
        "",
        f"Цена: <code>{price}</code>",
        f"Уровень: <code>{level_price}</code> · {event.distance_atr:.2f}×ATR",
    ]

    if is_manual:
        assert user_level is not None
        # Порядковый номер срабатывания уже увеличен на момент отправки.
        # Идентификатор уровня человеку ничего не говорит — в списке уровни
        # выбираются нажатием, а не по номеру.
        lines.append(f"Срабатывание №{user_level.trigger_count}")
        if user_level.note:
            lines.append(f"<i>{user_level.note}</i>")
    else:
        kinds = " + ".join(HUMAN_KIND.get(k, k) for k in event.level_kinds)
        lines.append(f"Конфлюэнс: {kinds} · score {event.level_score:.1f}")

    if brief:
        lines += ["", brief]

    lines += ["", DISCLAIMER_LINE]
    return "\n".join(lines)


async def dispatch_event(
    instrument: Instrument,
    event: LevelEvent,
    notifier: Notifier,
    brief: str | None = None,
    now: datetime | None = None,
    user_level: UserLevel | None = None,
) -> int:
    """Фильтрует получателей, пишет alert в БД и ставит сообщения в очередь."""
    now = now or utcnow()
    recipients = await filter_recipients(event.recipients, now)
    if not recipients:
        return 0

    text = format_alert(instrument, event, brief, user_level)

    async with session_scope() as session:
        session.add(
            Alert(
                # У ручного уровня нет строки в levels — ссылку не ставим,
                # а его собственный журнал ведётся в user_level_events.
                level_id=None if user_level is not None else event.level_id,
                instrument_id=instrument.id,
                kind=event.kind,
                price=event.price,
                level_price=event.level_price,
                distance_atr=event.distance_atr,
                ts=now,
                brief_text=brief,
                sent_to=recipients,
            )
        )

    for tg_id in recipients:
        notifier.enqueue(Outgoing(chat_id=tg_id, text=text))

    log.info(
        "%s: %s у %.6g -> %s получателей",
        instrument.symbol,
        event.kind,
        event.level_price,
        len(recipients),
    )
    return len(recipients)


async def persist_decision(level_id: int, state: str, cooldown_until, notified: list[int]) -> None:  # noqa: ANN001
    async with session_scope() as session:
        row = await session.get(LevelRow, level_id)
        if row is None:
            return
        row.state = state
        row.cooldown_until = cooldown_until
        row.notified_users = notified
