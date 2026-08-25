"""Настройки пользователя: пороги, тихие часы, пауза алертов.

Пороги задаются глобально и переопределяются на инструмент — потому что порог,
разумный для BTC, для менее ликвидной пары даёт либо тишину, либо поток.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from alert_bot.config import get_settings
from alert_bot.db.models import Subscription, User, utcnow  # noqa: F401
from alert_bot.db.session import session_scope
from alert_bot.market import registry

log = logging.getLogger(__name__)

router = Router(name="settings")

DURATION_RE = re.compile(r"^(\d+)\s*([mhdмчд])$", re.IGNORECASE)
_UNIT_MINUTES = {"m": 1, "м": 1, "h": 60, "ч": 60, "d": 1440, "д": 1440}


def parse_duration(raw: str) -> timedelta | None:
    """Принимает 30m / 2h / 1d и русские м/ч/д."""
    match = DURATION_RE.match(raw.strip())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    minutes = amount * _UNIT_MINUTES[unit]
    return timedelta(minutes=minutes) if 0 < minutes <= 60 * 24 * 30 else None


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, user: User) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Использование:\n"
            "<code>/mute 2h</code> — пауза по всем инструментам\n"
            "<code>/mute BTC/USDT 4h</code> — пауза по одному\n"
            "<code>/mute off</code> — снять паузу"
        )
        return

    if args[0].lower() in ("off", "стоп"):
        async with session_scope() as session:
            subs = (
                await session.scalars(
                    select(Subscription).where(Subscription.tg_id == user.tg_id)
                )
            ).all()
            for sub in subs:
                sub.muted_until = None
        await message.answer("🔔 Пауза снята по всем инструментам.")
        return

    symbol = args[0].upper() if len(args) > 1 else None
    duration = parse_duration(args[-1])
    if duration is None:
        await message.answer("Не понял длительность. Примеры: <code>30m</code>, <code>2h</code>, <code>1d</code>.")
        return

    until = utcnow() + duration
    instruments = await registry.list_instruments(enabled_only=True)
    targets = [i for i in instruments if symbol is None or i.symbol == symbol]

    if symbol is not None and not targets:
        await message.answer(f"Инструмент <code>{symbol}</code> не найден.")
        return

    async with session_scope() as session:
        for instrument in targets:
            sub = await session.get(Subscription, (user.tg_id, instrument.id))
            if sub is not None:
                sub.muted_until = until

    scope = symbol or "всем инструментам"
    await message.answer(
        f"🔕 Пауза по {scope} до {until.strftime('%H:%M')} UTC."
    )


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="◀️ Ближе", callback_data="set:atr:-"),
                InlineKeyboardButton(text="Дальше ▶️", callback_data="set:atr:+"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
        ]
    )


def _settings_text(user: User) -> str:
    settings = get_settings()
    atr_k = user.def_atr_k if user.def_atr_k is not None else settings.default_atr_k
    quiet = (
        f"{user.quiet_from:02d}:00–{user.quiet_to:02d}:00 ({user.tz})"
        if user.quiet_from is not None and user.quiet_to is not None
        else "не заданы"
    )

    return (
        "<b>⚙️ Настройки</b>\n\n"
        f"Предупреждать за: <code>{atr_k:.2f}×ATR</code> до уровня\n"
        "<i>ATR — средний часовой ход цены. Чем меньше значение, тем ближе к "
        "уровню должна подойти цена, прежде чем придёт алерт.</i>\n\n"
        f"Тихие часы: {quiet}\n"
        "<code>/quiet 23 7 Europe/Moscow</code> — задать\n"
        "<code>/quiet off</code> — снять"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, user: User) -> None:
    await message.answer(_settings_text(user), reply_markup=_settings_kb())


@router.callback_query(F.data == "menu:settings")
async def cb_menu_settings(callback: CallbackQuery, user: User) -> None:
    assert callback.message is not None
    await callback.message.edit_text(_settings_text(user), reply_markup=_settings_kb())
    await callback.answer()


MUTE_OPTIONS = ((30, "30 минут"), (120, "2 часа"), (480, "8 часов"), (1440, "сутки"))


def _mute_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"mute:set:{minutes}")
            for minutes, label in MUTE_OPTIONS[:2]
        ],
        [
            InlineKeyboardButton(text=label, callback_data=f"mute:set:{minutes}")
            for minutes, label in MUTE_OPTIONS[2:]
        ],
        [InlineKeyboardButton(text="🔔 Снять паузу", callback_data="mute:off")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:mute")
async def cb_menu_mute(callback: CallbackQuery, user: User) -> None:
    assert callback.message is not None
    await callback.message.edit_text(
        "<b>🔕 Пауза</b>\n\nНа сколько заглушить алерты по всем инструментам?",
        reply_markup=_mute_kb(),
    )
    await callback.answer()


async def _apply_mute(tg_id: int, until) -> int:  # noqa: ANN001
    async with session_scope() as session:
        subs = (
            await session.scalars(select(Subscription).where(Subscription.tg_id == tg_id))
        ).all()
        for sub in subs:
            sub.muted_until = until
        return len(subs)


@router.callback_query(F.data.startswith("mute:set:"))
async def cb_mute_set(callback: CallbackQuery, user: User) -> None:
    assert callback.data is not None and callback.message is not None
    minutes = int(callback.data.rsplit(":", 1)[1])
    until = utcnow() + timedelta(minutes=minutes)

    await _apply_mute(user.tg_id, until)
    await callback.message.edit_text(
        f"🔕 Алерты заглушены до {until.strftime('%H:%M')} UTC.",
        reply_markup=_mute_kb(),
    )
    await callback.answer("Пауза включена")


@router.callback_query(F.data == "mute:off")
async def cb_mute_off(callback: CallbackQuery, user: User) -> None:
    assert callback.message is not None
    await _apply_mute(user.tg_id, None)
    await callback.message.edit_text("🔔 Пауза снята.", reply_markup=_mute_kb())
    await callback.answer("Пауза снята")


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(callback: CallbackQuery, user: User) -> None:
    assert callback.data is not None
    _, field, direction = callback.data.split(":")
    settings = get_settings()
    step = 1 if direction == "+" else -1

    async with session_scope() as session:
        stored = await session.get(User, user.tg_id)
        assert stored is not None

        current = stored.def_atr_k if stored.def_atr_k is not None else settings.default_atr_k
        stored.def_atr_k = round(min(max(current + 0.05 * step, 0.05), 2.0), 2)
        refreshed = stored

    assert callback.message is not None
    await callback.message.edit_text(_settings_text(refreshed), reply_markup=_settings_kb())
    await callback.answer("Сохранено")


@router.message(Command("quiet"))
async def cmd_quiet(message: Message, command: CommandObject, user: User) -> None:
    args = (command.args or "").split()

    if args and args[0].lower() in ("off", "выкл"):
        async with session_scope() as session:
            stored = await session.get(User, user.tg_id)
            stored.quiet_from = stored.quiet_to = None
        await message.answer("Тихие часы отключены.")
        return

    if len(args) < 2 or not (args[0].isdigit() and args[1].isdigit()):
        await message.answer(
            "Использование: <code>/quiet С ДО [таймзона]</code>\n"
            "Например: <code>/quiet 23 7 Europe/Moscow</code>"
        )
        return

    start, end = int(args[0]), int(args[1])
    if not (0 <= start <= 23 and 0 <= end <= 23):
        await message.answer("Часы должны быть от 0 до 23.")
        return

    tz = args[2] if len(args) > 2 else (user.tz or "UTC")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — пользователь мог прислать что угодно
        await message.answer(f"Не знаю таймзону <code>{tz}</code>. Пример: <code>Europe/Moscow</code>.")
        return

    async with session_scope() as session:
        stored = await session.get(User, user.tg_id)
        stored.quiet_from, stored.quiet_to, stored.tz = start, end, tz

    await message.answer(f"🔕 Тихие часы: {start:02d}:00–{end:02d}:00 ({tz}).")
