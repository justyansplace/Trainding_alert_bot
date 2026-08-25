"""Уровни пользователя — основная функция бота.

Уровни задаются только вручную: бот ничего не вычисляет за человека и сообщает
ровно о тех отметках, которые тот поставил сам.

Работа с уровнем идёт через кнопки, а не через идентификаторы. Показывать id в
списке и просить набрать «/dellevel 7» — значит заставлять человека работать
за интерфейс: id ему ничего не говорит, а ошибиться в нём легко.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.bot.access import fmt_dt
from alert_bot.bot.menu import back_kb
from alert_bot.db.models import User
from alert_bot.market import registry, user_levels
from alert_bot.threshold import gap_text, unit_of

log = logging.getLogger(__name__)

router = Router(name="user_levels")


class AddLevel(StatesGroup):
    entering_price = State()


class EditLevel(StatesGroup):
    entering_price = State()
    entering_note = State()


def _fmt(value: float, precision: int) -> str:
    return f"{value:,.{precision}f}".replace(",", " ")


def parse_price(raw: str) -> float | None:
    """Принимает 78000, 78 000, 78,000.5, 78000.5 и суффиксы k/к."""
    cleaned = raw.strip().lower().replace(" ", "").replace(" ", "")
    multiplier = 1.0
    if cleaned.endswith(("k", "к")):
        multiplier, cleaned = 1000.0, cleaned[:-1]

    # Запятая может быть и разделителем тысяч, и десятичной: если после неё
    # ровно три цифры и есть точка — это тысячи, иначе десятичная.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        cleaned = f"{head}{tail}" if len(tail) == 3 and head else f"{head}.{tail}"

    try:
        value = float(cleaned) * multiplier
    except ValueError:
        return None
    return value if value > 0 else None


async def _resolve_instrument(symbol: str):  # noqa: ANN202
    instruments = await registry.list_instruments(enabled_only=True)
    symbol = symbol.upper()
    return next(
        (i for i in instruments if i.symbol == symbol or i.symbol.startswith(symbol)), None
    )


async def _subscribed_instruments(user: User) -> list:
    instruments = await registry.list_instruments(enabled_only=True)
    subs = await registry.list_subscriptions(user.tg_id)
    return [i for i in instruments if subs.get(i.id) and subs[i.id].enabled]


def _distance(level, instrument, unit: str) -> str:  # noqa: ANN001
    """Расстояние до уровня в той единице, что выбрал пользователь."""
    if not instrument.last_price:
        return ""
    gap = gap_text(unit, level.price, instrument.last_price, instrument.atr)
    if not gap:
        return ""
    side = "▲" if level.price > instrument.last_price else "▼"
    return f"{side} {gap}"


# --------------------------------------------------------------------------- #
# Список уровней
# --------------------------------------------------------------------------- #


async def render_levels(user: User) -> tuple[str, InlineKeyboardMarkup]:
    instruments = {i.id: i for i in await registry.list_instruments()}
    rows = await user_levels.list_levels(user.tg_id)
    unit = unit_of(user)

    if not rows:
        return (
            "<b>📌 Ваши уровни</b>\n\nПока ни одного.\n\n"
            "Поставьте первый — бот будет следить за ценой и напишет, "
            "когда она подойдёт.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить", callback_data="menu:addlevel")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
                ]
            ),
        )

    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.instrument_id, []).append(row)

    lines = ["<b>📌 Ваши уровни</b>", "", "Нажмите на уровень, чтобы изменить или удалить."]
    buttons: list[list[InlineKeyboardButton]] = []

    for instrument_id, levels in grouped.items():
        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue

        header = f"\n<b>{instrument.symbol}</b>"
        if instrument.last_price:
            header += f" — сейчас {_fmt(instrument.last_price, instrument.price_precision)}"
        lines.append(header)

        for level in levels:
            price_txt = _fmt(level.price, instrument.price_precision)
            distance = _distance(level, instrument, unit)
            caption = " · ".join(filter(None, [price_txt, distance]))
            if level.trigger_count:
                caption += f" · {level.trigger_count}×"
            if level.note:
                lines.append(f"{price_txt} — <i>{level.note}</i>")

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{instrument.symbol}  {caption}",
                        callback_data=f"lvl:open:{level.id}",
                    )
                ]
            )

    buttons.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="menu:addlevel"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("mylevels", "levels"))
async def cmd_mylevels(message: Message, user: User) -> None:
    text, keyboard = await render_levels(user)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:mylevels")
async def cb_mylevels(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    text, keyboard = await render_levels(user)
    assert callback.message is not None
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# --------------------------------------------------------------------------- #
# Карточка уровня
# --------------------------------------------------------------------------- #


async def render_level_card(user: User, level_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    level = await user_levels.get_level(user.tg_id, level_id)
    if level is None:
        return None

    instruments = {i.id: i for i in await registry.list_instruments()}
    instrument = instruments.get(level.instrument_id)
    precision = instrument.price_precision if instrument else 2
    symbol = instrument.symbol if instrument else "—"

    lines = [
        f"<b>📌 {symbol}</b>",
        "",
        f"Уровень: <code>{_fmt(level.price, precision)}</code>",
    ]
    if instrument and instrument.last_price:
        lines.append(f"Сейчас: <code>{_fmt(instrument.last_price, precision)}</code>")
        distance = _distance(level, instrument, unit_of(user))
        if distance:
            lines.append(f"Расстояние: {distance}")

    lines.append(f"Заметка: {f'<i>{level.note}</i>' if level.note else '—'}")
    lines.append(f"Поставлен: {fmt_dt(level.created_at)}")

    if level.trigger_count:
        lines.append(
            f"Срабатываний: <b>{level.trigger_count}</b>, "
            f"последнее {fmt_dt(level.last_triggered_at)}"
        )
        events = await user_levels.level_history(level_id, limit=5)
        if events:
            lines.append("")
            for item in events:
                kind = "пробой" if item.kind == "breakout" else "подход"
                lines.append(
                    f"• {fmt_dt(item.ts)} — {kind}, цена "
                    f"<code>{_fmt(item.price, precision)}</code>"
                )
    else:
        lines.append("Срабатываний ещё не было.")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Цена", callback_data=f"lvl:price:{level_id}"
                ),
                InlineKeyboardButton(
                    text="📝 Заметка", callback_data=f"lvl:note:{level_id}"
                ),
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"lvl:del:{level_id}")],
            [InlineKeyboardButton(text="◀️ К списку", callback_data="menu:mylevels")],
        ]
    )
    return "\n".join(lines), keyboard


@router.callback_query(F.data.startswith("lvl:open:"))
async def cb_open_level(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    assert callback.data is not None and callback.message is not None
    rendered = await render_level_card(user, int(callback.data.rsplit(":", 1)[1]))
    if rendered is None:
        await callback.answer("Уровень не найден", show_alert=True)
        return
    await callback.message.edit_text(rendered[0], reply_markup=rendered[1])
    await callback.answer()


# --------------------------------------------------------------------------- #
# Редактирование
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("lvl:price:"))
async def cb_edit_price(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    assert callback.data is not None and callback.message is not None
    level_id = int(callback.data.rsplit(":", 1)[1])
    level = await user_levels.get_level(user.tg_id, level_id)
    if level is None:
        await callback.answer("Уровень не найден", show_alert=True)
        return

    await state.set_state(EditLevel.entering_price)
    await state.update_data(level_id=level_id)
    await callback.message.edit_text(
        f"Текущая цена: <code>{level.price:g}</code>\n\n"
        "Пришлите новую одним сообщением.\n"
        "Форматы: <code>78000</code>, <code>78k</code>, <code>1.1675</code>\n\n"
        "<code>/cancel</code> — отменить"
    )
    await callback.answer()


@router.message(EditLevel.entering_price, F.text)
async def on_new_price(message: Message, state: FSMContext, user: User) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        text, keyboard = await render_levels(user)
        await message.answer(text, reply_markup=keyboard)
        return

    price = parse_price(raw)
    if price is None:
        await message.answer(f"Не понял цену <code>{raw}</code>. Например: 78000 или 1.1675.")
        return

    data = await state.get_data()
    await state.clear()

    try:
        level = await user_levels.update_price(user.tg_id, data["level_id"], price)
    except user_levels.UserLevelError as exc:
        await message.answer(f"❌ {exc}", reply_markup=back_kb())
        return

    if level is None:
        await message.answer("Уровень не найден.", reply_markup=back_kb())
        return

    rendered = await render_level_card(user, level.id)
    assert rendered is not None
    await message.answer("✅ Цена изменена.\n\n" + rendered[0], reply_markup=rendered[1])


@router.callback_query(F.data.startswith("lvl:note:"))
async def cb_edit_note(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    assert callback.data is not None and callback.message is not None
    level_id = int(callback.data.rsplit(":", 1)[1])
    level = await user_levels.get_level(user.tg_id, level_id)
    if level is None:
        await callback.answer("Уровень не найден", show_alert=True)
        return

    await state.set_state(EditLevel.entering_note)
    await state.update_data(level_id=level_id)
    await callback.message.edit_text(
        f"Заметка сейчас: {f'<i>{level.note}</i>' if level.note else '—'}\n\n"
        "Пришлите новую одним сообщением.\n"
        "<code>-</code> — убрать заметку\n"
        "<code>/cancel</code> — отменить"
    )
    await callback.answer()


@router.message(EditLevel.entering_note, F.text)
async def on_new_note(message: Message, state: FSMContext, user: User) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        text, keyboard = await render_levels(user)
        await message.answer(text, reply_markup=keyboard)
        return

    data = await state.get_data()
    await state.clear()

    level = await user_levels.update_note(
        user.tg_id, data["level_id"], None if raw == "-" else raw
    )
    if level is None:
        await message.answer("Уровень не найден.", reply_markup=back_kb())
        return

    rendered = await render_level_card(user, level.id)
    assert rendered is not None
    await message.answer("✅ Заметка обновлена.\n\n" + rendered[0], reply_markup=rendered[1])


# --------------------------------------------------------------------------- #
# Удаление
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("lvl:del:"))
async def cb_confirm_delete(callback: CallbackQuery, user: User) -> None:
    assert callback.data is not None and callback.message is not None
    level_id = int(callback.data.rsplit(":", 1)[1])
    level = await user_levels.get_level(user.tg_id, level_id)
    if level is None:
        await callback.answer("Уровень не найден", show_alert=True)
        return

    warning = ""
    if level.trigger_count:
        warning = (
            f"\n\nВместе с ним удалится история "
            f"({level.trigger_count} срабатываний)."
        )

    await callback.message.edit_text(
        f"Удалить уровень <code>{level.price:g}</code>?{warning}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Да, удалить", callback_data=f"lvl:delyes:{level_id}"
                    ),
                    InlineKeyboardButton(
                        text="Отмена", callback_data=f"lvl:open:{level_id}"
                    ),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lvl:delyes:"))
async def cb_delete(callback: CallbackQuery, user: User) -> None:
    assert callback.data is not None and callback.message is not None
    level = await user_levels.delete_level(user.tg_id, int(callback.data.rsplit(":", 1)[1]))
    if level is None:
        await callback.answer("Уровень уже удалён", show_alert=True)
        return

    text, keyboard = await render_levels(user)
    await callback.message.edit_text(
        f"🗑 Уровень <code>{level.price:g}</code> удалён.\n\n" + text,
        reply_markup=keyboard,
    )
    await callback.answer("Удалён")


# --------------------------------------------------------------------------- #
# Добавление
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "menu:addlevel")
async def cb_addlevel(callback: CallbackQuery, user: User) -> None:
    instruments = await _subscribed_instruments(user)
    assert callback.message is not None

    if not instruments:
        await callback.message.edit_text(
            "Сначала подпишитесь хотя бы на один инструмент — уровни ставятся "
            "по подписанным.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔔 Инструменты", callback_data="menu:subscribe")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
                ]
            ),
        )
        await callback.answer()
        return

    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{i.symbol} · {_fmt(i.last_price, i.price_precision)}"
                    if i.last_price
                    else i.symbol
                ),
                callback_data=f"lvl:pick:{i.id}",
            )
        ]
        for i in instruments
    ]
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")])

    await callback.message.edit_text(
        "<b>➕ Новый уровень</b>\n\nПо какому инструменту?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lvl:pick:"))
async def cb_pick_instrument(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    assert callback.data is not None and callback.message is not None
    instrument_id = int(callback.data.rsplit(":", 1)[1])

    instruments = {i.id: i for i in await registry.list_instruments(enabled_only=True)}
    instrument = instruments.get(instrument_id)
    if instrument is None:
        await callback.answer("Инструмент недоступен", show_alert=True)
        return

    await state.set_state(AddLevel.entering_price)
    await state.update_data(instrument_id=instrument_id)

    hint = ""
    if instrument.last_price:
        hint = f"\n\nСейчас: <code>{_fmt(instrument.last_price, instrument.price_precision)}</code>"

    await callback.message.edit_text(
        f"<b>➕ {instrument.symbol}</b>{hint}\n\n"
        "Пришлите цену уровня одним сообщением.\n"
        "Можно с заметкой: <code>78000 пробой максимума</code>\n\n"
        "Форматы: <code>78000</code>, <code>78k</code>, <code>1.1675</code>\n\n"
        "<code>/cancel</code> — отменить",
    )
    await callback.answer()


@router.message(AddLevel.entering_price, F.text)
async def on_price_entered(message: Message, state: FSMContext, user: User) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=back_kb())
        return

    parts = raw.split(maxsplit=1)
    price = parse_price(parts[0])
    if price is None:
        await message.answer(
            f"Не понял цену <code>{parts[0]}</code>.\n"
            "Например: <code>78000</code>, <code>78k</code> или <code>1.1675</code>."
        )
        return

    note = parts[1] if len(parts) > 1 else None
    data = await state.get_data()
    await state.clear()

    instruments = {i.id: i for i in await registry.list_instruments(enabled_only=True)}
    instrument = instruments.get(data.get("instrument_id"))
    if instrument is None:
        await message.answer("Инструмент стал недоступен.", reply_markup=back_kb())
        return

    await _create_level(message, user, instrument, price, note)


async def _create_level(message: Message, user: User, instrument, price: float, note) -> None:  # noqa: ANN001
    try:
        level = await user_levels.add_level(user.tg_id, instrument, price, note)
    except user_levels.UserLevelError as exc:
        await message.answer(f"❌ {exc}", reply_markup=back_kb())
        return

    distance = ""
    if instrument.last_price:
        gap = gap_text(unit_of(user), level.price, instrument.last_price, instrument.atr)
        if gap:
            side = "выше" if price > instrument.last_price else "ниже"
            distance = f"\nОт текущей цены: {side} на {gap}"

    subs = await registry.list_subscriptions(user.tg_id)
    warning = ""
    if not (subs.get(instrument.id) and subs[instrument.id].enabled):
        warning = (
            "\n\n⚠️ Вы не подписаны на этот инструмент — уровень сохранён, но "
            "алерты по нему приходить не будут."
        )

    await message.answer(
        f"📌 Уровень <code>{_fmt(level.price, instrument.price_precision)}</code> "
        f"по <b>{instrument.symbol}</b> поставлен."
        + (f"\nЗаметка: <i>{level.note}</i>" if level.note else "")
        + distance
        + warning,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Ещё", callback_data="menu:addlevel"),
                    InlineKeyboardButton(text="📌 Мои уровни", callback_data="menu:mylevels"),
                ],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
            ]
        ),
    )


@router.message(Command("addlevel"))
async def cmd_addlevel(message: Message, command: CommandObject, user: User) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/addlevel SYMBOL ЦЕНА [заметка]</code>\n\n"
            "Например: <code>/addlevel BTC/USDT 78000 пробой максимума</code>\n\n"
            "Или проще — кнопкой:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="➕ Добавить уровень", callback_data="menu:addlevel"
                        )
                    ]
                ]
            ),
        )
        return

    instrument = await _resolve_instrument(args[0])
    if instrument is None:
        await message.answer(
            f"Инструмент <code>{args[0].upper()}</code> не найден среди включённых."
        )
        return

    price = parse_price(args[1])
    if price is None:
        await message.answer(f"Не понял цену <code>{args[1]}</code>. Например: 78000 или 78k.")
        return

    await _create_level(
        message, user, instrument, price, " ".join(args[2:]) if len(args) > 2 else None
    )
