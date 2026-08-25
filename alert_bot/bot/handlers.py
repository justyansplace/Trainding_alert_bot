"""Пользовательские команды: /start, /redeem, /subscribe, /brief, /status.

Каждый экран рисуется отдельной render-функцией, чтобы кнопка меню и набранная
руками команда показывали одно и то же.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.bot.access import RedeemError, fmt_dt, redeem_invite
from alert_bot.bot.menu import DISCLAIMER, HELP_TEXT, back_kb, greeting, main_menu_kb
from alert_bot.db.models import User
from alert_bot.market import registry, user_levels
from alert_bot.news.context import build_context

log = logging.getLogger(__name__)

router = Router(name="user")


@router.message(CommandStart())
async def cmd_start(message: Message, user: User | None) -> None:
    await message.answer(
        greeting(user),
        reply_markup=main_menu_kb(user.is_admin) if user else None,
    )


@router.message(Command("help"))
async def cmd_help(message: Message, user: User | None) -> None:
    await message.answer(HELP_TEXT, reply_markup=back_kb() if user else None)


@router.message(Command("redeem"))
async def cmd_redeem(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip()
    if not code:
        await message.answer("Использование: <code>/redeem ВАШ_КОД</code>")
        return

    assert message.from_user is not None
    try:
        user = await redeem_invite(code, message.from_user.id, message.from_user.username)
    except RedeemError as exc:
        await message.answer(f"❌ {exc}")
        return

    await message.answer(
        "✅ Доступ выдан.\n\n" + greeting(user), reply_markup=main_menu_kb(user.is_admin)
    )


# --------------------------------------------------------------------------- #
# Подписки на инструменты
# --------------------------------------------------------------------------- #


def _subscribe_kb(instruments, subs) -> InlineKeyboardMarkup:  # noqa: ANN001
    rows = []
    for ins in instruments:
        sub = subs.get(ins.id)
        on = sub is not None and sub.enabled
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'🔔' if on else '🔕'} {ins.symbol}",
                    callback_data=f"sub:toggle:{ins.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_subscribe(user: User) -> tuple[str, InlineKeyboardMarkup]:
    instruments = await registry.list_instruments(enabled_only=True)
    if not instruments:
        return "Инструментов пока нет — администратор ещё не добавил ни одного.", back_kb()

    subs = await registry.list_subscriptions(user.tg_id)
    text = (
        "<b>🔔 Инструменты</b>\n\n"
        "Отметьте те, по которым хотите получать алерты. Уровни ставятся "
        "только по подписанным инструментам."
    )
    return text, _subscribe_kb(instruments, subs)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, user: User) -> None:
    text, keyboard = await render_subscribe(user)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:subscribe")
async def cb_menu_subscribe(callback: CallbackQuery, user: User) -> None:
    text, keyboard = await render_subscribe(user)
    assert callback.message is not None
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("sub:toggle:"))
async def cb_toggle_subscription(callback: CallbackQuery, user: User) -> None:
    assert callback.data is not None
    instrument_id = int(callback.data.rsplit(":", 1)[1])

    subs = await registry.list_subscriptions(user.tg_id)
    current = subs.get(instrument_id)
    new_state = not (current is not None and current.enabled)
    await registry.subscribe(user.tg_id, instrument_id, enabled=new_state)

    _, keyboard = await render_subscribe(user)
    assert callback.message is not None
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer("Подписан" if new_state else "Отписан")


# --------------------------------------------------------------------------- #
# Сводка и статус
# --------------------------------------------------------------------------- #


async def render_brief(user: User, symbol: str = "") -> str:
    instruments = await registry.list_instruments(enabled_only=True)
    if not instruments:
        return "Инструментов пока нет."

    if symbol:
        target = next((i for i in instruments if i.symbol.startswith(symbol.upper())), None)
        if target is None:
            return f"Инструмент <code>{symbol.upper()}</code> не найден."
    else:
        subs = await registry.list_subscriptions(user.tg_id)
        subscribed = [i for i in instruments if subs.get(i.id) and subs[i.id].enabled]
        target = (subscribed or instruments)[0]

    context = await build_context(target.symbol)
    if context.is_empty:
        return (
            f"<b>{target.symbol}</b>\n\n"
            "За последние 12 часов релевантных материалов нет.\n\n"
            "<i>Новости собираются, но для их разбора нужен ключ Anthropic — "
            "сейчас он не задан.</i>"
        )

    header = (
        f"<b>📰 {target.symbol}</b>\n\n"
        f"Материалов за 12ч: {context.article_count}\n"
        f"Тон: {context.sentiment:+.2f} ({context.sentiment_label()})\n"
        f"Максимальная значимость: {context.max_impact}/3"
    )
    if context.topics:
        header += f"\nТемы: {', '.join(context.topics)}"

    theses = "\n".join(
        f"• <i>{item.thesis}</i> <code>[{item.source_name}]</code>"
        for item in context.top_items(4)
    )
    return f"{header}\n\n{theses}\n\n{DISCLAIMER}"


@router.message(Command("brief"))
async def cmd_brief(message: Message, command: CommandObject, user: User) -> None:
    status = await message.answer("⏳ Собираю сводку…")
    text = await render_brief(user, (command.args or "").strip())
    await status.edit_text(text, reply_markup=back_kb())


@router.callback_query(F.data == "menu:brief")
async def cb_menu_brief(callback: CallbackQuery, user: User) -> None:
    await callback.answer("Собираю…")
    text = await render_brief(user)
    assert callback.message is not None
    await callback.message.edit_text(text, reply_markup=back_kb())


async def render_status(user: User) -> str:
    instruments = await registry.list_instruments(enabled_only=True)
    subs = await registry.list_subscriptions(user.tg_id)
    stats = await user_levels.stats(user.tg_id)

    lines = ["<b>📊 Статус</b>", ""]

    if not instruments:
        lines.append("Инструментов нет — админ ещё не добавил ни одного.")

    for ins in instruments:
        sub = subs.get(ins.id)
        mark = "🔔" if sub and sub.enabled else "🔕"
        price = (
            f"{ins.last_price:,.{ins.price_precision}f}".replace(",", " ")
            if ins.last_price
            else "—"
        )
        # Счётчик строго свой: общий выдавал бы, сколько отметок поставили другие.
        count = await user_levels.count_active(ins.id, tg_id=user.tg_id)
        lines.append(f"{mark} <b>{ins.symbol}</b> — {price}")
        lines.append(f"    тик {fmt_dt(ins.last_tick_at)} · ваших уровней: {count}")
        if ins.last_error:
            lines.append(f"    ⚠️ {ins.last_error[:90]}")

    lines += [
        "",
        f"Ваших уровней: <b>{stats.active}</b>",
        f"Срабатываний всего: <b>{stats.triggers}</b>",
    ]
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message, user: User) -> None:
    await message.answer(await render_status(user), reply_markup=back_kb())


@router.callback_query(F.data == "menu:status")
async def cb_menu_status(callback: CallbackQuery, user: User) -> None:
    assert callback.message is not None
    await callback.message.edit_text(await render_status(user), reply_markup=back_kb())
    await callback.answer()
