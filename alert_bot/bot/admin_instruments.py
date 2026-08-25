"""Админка инструментов: /add_instrument, /instruments, /rm_instrument.

Добавление идёт через подтверждение: сначала символ проверяется у провайдера и
показывается превью (цена, объём, шаг круглых уровней, ключевые слова), и только
потом пишется в БД. Опечатка в тикере иначе попадает в реестр и роняет price_loop.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.bot.access import AdminOnlyMiddleware, fmt_dt
from alert_bot.db.models import User
from alert_bot.llm.keywords import suggest_keywords
from alert_bot.market import registry
from alert_bot.market.providers.base import SymbolMeta, SymbolNotFound, derive_round_step

log = logging.getLogger(__name__)

router = Router(name="admin_instruments")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

DEFAULT_EXCHANGE = "binance"


class AddInstrument(StatesGroup):
    confirming = State()
    editing_keywords = State()


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data="instr:confirm"),
                InlineKeyboardButton(text="✏️ Ключевые слова", callback_data="instr:kw"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="instr:cancel")],
        ]
    )


def _preview_text(
    meta: SymbolMeta,
    exchange: str,
    round_step: float,
    keywords: list[str],
    via_llm: bool,
    delay_minutes: int = 0,
) -> str:
    volume = f"${meta.volume_24h:,.0f}".replace(",", " ") if meta.volume_24h else "—"
    kw_note = " (сгенерированы моделью)" if via_llm else ""

    # Задержку человек обязан увидеть до добавления, а не обнаружить потом по
    # опоздавшим алертам.
    delay = ""
    if delay_minutes:
        delay = (
            f"\n\n⚠️ <b>Котировка отстаёт на ~{delay_minutes} мин.</b>\n"
            "<i>Алерт по этому инструменту придёт уже после движения. "
            "Для входа по уровню это поздно.</i>"
        )

    return (
        f"<b>{meta.symbol}</b> на {exchange}\n\n"
        f"Последняя цена: <code>{meta.last_price:,.{meta.price_precision}f}</code>".replace(",", " ")
        + f"\nОбъём 24ч: {volume}\n"
        f"Знаков в цене: {meta.price_precision}\n"
        f"Шаг круглых уровней: <code>{round_step}</code>"
        + delay
        + f"\n\nКлючевые слова{kw_note}:\n<code>{', '.join(keywords)}</code>\n\n"
        "Добавить инструмент?"
    )


@router.message(Command("add_instrument"))
async def cmd_add_instrument(
    message: Message, command: CommandObject, state: FSMContext, user: User
) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Использование: <code>/add_instrument SYMBOL [площадка]</code>\n\n"
            "<b>Крипта</b> (по умолчанию binance):\n"
            "<code>/add_instrument BTC/USDT</code>\n"
            "<code>/add_instrument SOL/USDT bybit</code>\n\n"
            "<b>Валюты, металлы, нефть</b> — площадка <code>yahoo</code>:\n"
            "<code>/add_instrument USD/CAD yahoo</code>\n"
            "<code>/add_instrument AUD/USD yahoo</code>\n"
            "<code>/add_instrument BRENT yahoo</code>\n"
            "<code>/add_instrument XAU/USD yahoo</code>"
        )
        return

    symbol = args[0].upper()
    exchange = args[1].lower() if len(args) > 1 else DEFAULT_EXCHANGE

    status = await message.answer(f"⏳ Проверяю {symbol} на {exchange}…")

    try:
        meta = await registry.validate_candidate(symbol, exchange)
    except SymbolNotFound as exc:
        hint = ""
        if exc.suggestions:
            hint = "\n\nВозможно, вы имели в виду:\n" + "\n".join(
                f"• <code>{s}</code>" for s in exc.suggestions
            )
        await status.edit_text(f"❌ Символ <code>{symbol}</code> не найден на {exchange}.{hint}")
        return
    except registry.RegistryError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except ValueError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except Exception:  # noqa: BLE001 — сеть/биржа могут отвалиться как угодно
        log.exception("валидация %s@%s упала", symbol, exchange)
        await status.edit_text("❌ Биржа недоступна или вернула неожиданный ответ. Попробуйте позже.")
        return

    keywords, via_llm = await suggest_keywords(meta.symbol)
    round_step = derive_round_step(meta.last_price)
    delay_minutes = int(meta.extra.get("delay_minutes", 0))

    await state.set_state(AddInstrument.confirming)
    await state.update_data(
        symbol=meta.symbol,
        exchange=exchange,
        last_price=meta.last_price,
        price_precision=meta.price_precision,
        volume_24h=meta.volume_24h,
        round_step=round_step,
        keywords=keywords,
        via_llm=via_llm,
        delay_minutes=delay_minutes,
    )

    await status.edit_text(
        _preview_text(meta, exchange, round_step, keywords, via_llm, delay_minutes),
        reply_markup=_confirm_kb(),
    )


@router.callback_query(AddInstrument.confirming, F.data == "instr:confirm")
async def cb_confirm(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    await state.clear()

    meta = SymbolMeta(
        symbol=data["symbol"],
        last_price=data["last_price"],
        price_precision=data["price_precision"],
        volume_24h=data.get("volume_24h"),
    )

    instrument = await registry.add_instrument(
        meta=meta,
        exchange=data["exchange"],
        keywords=data["keywords"],
        added_by=user.tg_id,
        round_step=data["round_step"],
    )
    # Админ подписывается на то, что добавил — иначе алерты некому получать.
    await registry.subscribe(user.tg_id, instrument.id)

    assert callback.message is not None
    await callback.message.edit_text(
        f"✅ <b>{instrument.symbol}</b> на {instrument.exchange} добавлен "
        f"(id={instrument.id}) и вы на него подписаны.\n\n"
        "Рестарт не нужен — цикл подхватит его на следующей итерации."
    )
    await callback.answer()


@router.callback_query(AddInstrument.confirming, F.data == "instr:kw")
async def cb_edit_keywords(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(AddInstrument.editing_keywords)

    assert callback.message is not None
    await callback.message.edit_text(
        f"Текущие ключевые слова для <b>{data['symbol']}</b>:\n"
        f"<code>{', '.join(data['keywords'])}</code>\n\n"
        "Пришлите новый список через запятую одним сообщением.\n"
        "Или /cancel чтобы отменить добавление."
    )
    await callback.answer()


@router.message(AddInstrument.editing_keywords, F.text)
async def on_keywords_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        await message.answer("Добавление отменено.")
        return

    keywords = sorted({k.strip().lower() for k in raw.split(",") if k.strip()})
    if not keywords:
        await message.answer("Пустой список. Пришлите слова через запятую или /cancel.")
        return

    await state.update_data(keywords=keywords, via_llm=False)
    data = await state.get_data()
    await state.set_state(AddInstrument.confirming)

    meta = SymbolMeta(
        symbol=data["symbol"],
        last_price=data["last_price"],
        price_precision=data["price_precision"],
        volume_24h=data.get("volume_24h"),
    )
    await message.answer(
        _preview_text(
            meta,
            data["exchange"],
            data["round_step"],
            keywords,
            False,
            data.get("delay_minutes", 0),
        ),
        reply_markup=_confirm_kb(),
    )


@router.callback_query(F.data == "instr:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    assert callback.message is not None
    await callback.message.edit_text("Добавление отменено.")
    await callback.answer()


@router.message(Command("instruments"))
async def cmd_instruments(message: Message) -> None:
    instruments = await registry.list_instruments()
    if not instruments:
        await message.answer(
            "Инструментов пока нет.\nДобавьте первый: <code>/add_instrument BTC/USDT</code>"
        )
        return

    lines = ["<b>Инструменты</b>\n"]
    for ins in instruments:
        mark = "🟢" if ins.enabled else "⚪"
        price = (
            f"{ins.last_price:,.{ins.price_precision}f}".replace(",", " ")
            if ins.last_price
            else "—"
        )
        lines.append(
            f"{mark} <b>{ins.symbol}</b> · {ins.exchange} · id={ins.id}\n"
            f"    цена {price} · шаг {ins.round_step} · тик {fmt_dt(ins.last_tick_at)}"
        )
        if ins.last_error:
            lines.append(f"    ⚠️ {ins.last_error[:120]}")

    await message.answer("\n".join(lines))


@router.message(Command("rm_instrument"))
async def cmd_rm_instrument(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer("Использование: <code>/rm_instrument SYMBOL [exchange]</code>")
        return

    symbol = args[0].upper()
    exchange = args[1].lower() if len(args) > 1 else None

    instrument = await registry.disable_instrument(symbol, exchange)
    if instrument is None:
        await message.answer(f"❌ Инструмент <code>{symbol}</code> не найден.")
        return

    await message.answer(
        f"⚪ <b>{instrument.symbol}</b> отключён. Свечи, уровни и история алертов "
        "сохранены — повторный /add_instrument вернёт его вместе с ними."
    )


@router.callback_query(F.data == "menu:instruments")
async def cb_instruments(callback: CallbackQuery) -> None:
    from alert_bot.bot.menu import admin_menu_kb

    instruments = await registry.list_instruments()
    if not instruments:
        text = (
            "Инструментов нет.\n\n"
            "Добавить: <code>/add_instrument BTC/USDT</code>"
        )
    else:
        lines = ["<b>📈 Инструменты</b>\n"]
        for ins in instruments:
            mark = "🟢" if ins.enabled else "⚪"
            price = (
                f"{ins.last_price:,.{ins.price_precision}f}".replace(",", " ")
                if ins.last_price
                else "—"
            )
            lines.append(
                f"{mark} <b>{ins.symbol}</b> · {ins.exchange} · id={ins.id}\n"
                f"    {price} · тик {fmt_dt(ins.last_tick_at)}"
            )
            if ins.last_error:
                lines.append(f"    ⚠️ {ins.last_error[:100]}")
        lines.append("\n<code>/add_instrument SYMBOL</code> · <code>/rm_instrument SYMBOL</code>")
        text = "\n".join(lines)

    assert callback.message is not None
    await callback.message.edit_text(text, reply_markup=admin_menu_kb())
    await callback.answer()
