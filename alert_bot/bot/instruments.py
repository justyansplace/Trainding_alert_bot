"""Инструменты: добавление доступно всем, удаление — администратору.

Добавление идёт через подтверждение: сначала символ проверяется у провайдера и
показывается превью (цена, объём, шаг круглых уровней, ключевые слова), и только
потом пишется в БД. Опечатка в тикере иначе попадает в реестр и роняет price_loop.

Почему добавляет любой, а отключает только администратор. Реестр общий: одна
строка на символ и площадку, один опрос на всех подписчиков. Добавление ничего
чужого не ломает — в худшем случае занимает слот, и от этого есть личная квота.
А отключение бьёт по всем, кто на инструмент подписан, включая тех, кто
поставил по нему уровни. Поэтому человеку доступно ровно то, что касается его
самого: подписаться и отписаться.

Если инструмент в реестре уже есть, второй раз он не заводится: человека просто
подписывают на существующий. Для него это то же самое действие с тем же
результатом, а реестр не растёт дубликатами.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.bot.access import AdminOnlyMiddleware, fmt_dt
from alert_bot.bot.menu import back_kb
from alert_bot.config import get_settings
from alert_bot.db.models import User
from alert_bot.llm.keywords import suggest_keywords
from alert_bot.market import registry
from alert_bot.market.providers.base import (
    ExchangeBanned,
    SymbolMeta,
    SymbolNotFound,
    derive_round_step,
)

log = logging.getLogger(__name__)

# Добавление и превью — всем, у кого есть доступ к боту.
router = Router(name="instruments")

# Отключение инструмента и полный список реестра — только администратору.
admin_router = Router(name="admin_instruments")
admin_router.message.middleware(AdminOnlyMiddleware())
admin_router.callback_query.middleware(AdminOnlyMiddleware())

# Площадка по умолчанию для крипты — одна константа на оба пути добавления:
# и на /add_instrument без третьего слова, и на угадывание в /add_many. Двумя
# значениями они бы разъехались, и одна и та же пара уезжала бы на разные
# биржи в зависимости от того, какой командой её завели.
#
# Не Binance: она банит по IP (418, см. ExchangeBanned), а на арендованном
# хостинге адрес общий с чужими сервисами, поэтому бан прилетает регулярно и
# без вины бота. Расхождение котировок между биржами — 0.006% в среднем и
# 0.04% в худшем случае, то есть меньше самого мелкого шага порога (0.05%):
# на момент срабатывания алерта выбор биржи повлиять не может. Зато Bybit
# отдаёт до 720 часовых свечей за запрос против 300 у OKX, а циклу нужно 200.
DEFAULT_EXCHANGE = "bybit"

# Куда уходить, если площадка на паузе. Порядок — очередь запасных: берётся
# первая, которая не та, что отказала.
CRYPTO_VENUES = ("bybit", "okx", "binance")


def ban_hint(exchange: str) -> str:
    """Что предложить, когда площадка отказала по частоте запросов.

    «Попробуйте ещё раз» — плохой совет: бан висит на IP целиком, и повтор его
    же и продлевает. А другая биржа даёт те же пары прямо сейчас: расхождение
    котировок между ними меньше самого мелкого шага порога.
    """
    spare = next((v for v in CRYPTO_VENUES if v != exchange), CRYPTO_VENUES[0])
    return (
        "\n\nБан висит на IP сервера, а не на символе, и каждый запрос во время "
        "паузы её продлевает — поэтому бот к этой площадке пока не ходит.\n\n"
        "Те же пары есть на других биржах:\n"
        f"<code>/add_many BTC/USDT@{spare} ETH/USDT@{spare}</code>"
    )


async def quota_left(user: User) -> int | None:
    """Сколько инструментов человеку ещё можно завести. None — без ограничения."""
    if user.is_admin:
        return None
    limit = get_settings().max_instruments_per_user
    return max(0, limit - await registry.count_added_by(user.tg_id))


def quota_message(user: User) -> str:
    limit = get_settings().max_instruments_per_user
    return (
        f"❌ Вы уже завели {limit} инструментов — это личный потолок.\n\n"
        "Отключите ненужный через кнопку «Инструменты» и попросите "
        "администратора убрать его из реестра, либо подпишитесь на то, "
        "что уже добавили другие."
    )


async def adopt_existing(user: User, symbol: str, exchange: str):  # noqa: ANN201
    """Подписывает на инструмент, который в реестре уже есть.

    Для человека это то же действие с тем же результатом — «хочу следить за
    BTC», — но реестр не растёт дубликатами и слот не тратится.
    """
    existing = await registry.get_by_symbol(symbol, exchange)
    if existing is None or not existing.enabled:
        return None
    await registry.subscribe(user.tg_id, existing.id)
    return existing


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
            f"<b>Крипта</b> (по умолчанию {DEFAULT_EXCHANGE}):\n"
            "<code>/add_instrument BTC/USDT</code>\n"
            "<code>/add_instrument SOL/USDT okx</code>\n"
            "<code>/add_instrument BTC/USDT binance</code>\n\n"
            "<b>Валюты, металлы, нефть</b> — площадка <code>yahoo</code>:\n"
            "<code>/add_instrument USD/CAD yahoo</code>\n"
            "<code>/add_instrument AUD/USD yahoo</code>\n"
            "<code>/add_instrument BRENT yahoo</code>\n"
            "<code>/add_instrument XAU/USD yahoo</code>"
        )
        return

    symbol = args[0].upper()
    exchange = args[1].lower() if len(args) > 1 else DEFAULT_EXCHANGE

    already = await adopt_existing(user, symbol, exchange)
    if already is not None:
        await message.answer(
            f"🔔 <b>{already.symbol}</b> уже в реестре — подписал вас на него.\n\n"
            "Ставьте уровни: кнопка «Добавить уровень».",
            reply_markup=back_kb(),
        )
        return

    if await quota_left(user) == 0:
        await message.answer(quota_message(user), reply_markup=back_kb())
        return

    status = await message.answer(f"⏳ Проверяю {symbol} на {exchange}…")

    try:
        meta = await registry.validate_candidate(symbol, exchange)
    except ExchangeBanned as exc:
        await status.edit_text(f"⏳ {exc}{ban_hint(exc.exchange)}")
        return
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


# --------------------------------------------------------------------------- #
# Пакетное добавление
# --------------------------------------------------------------------------- #

# Готовые наборы: перечислять четырнадцать инструментов по одному — работа,
# которую машина должна делать за человека.
PRESETS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "forex": (
        "Основные валютные пары",
        [(s, "yahoo") for s in
         ("EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD")],
    ),
    "metals": (
        "Металлы",
        [("XAU/USD", "yahoo"), ("XAG/USD", "yahoo")],
    ),
    "energy": (
        "Энергоносители",
        [("BRENT", "yahoo"), ("WTI", "yahoo"), ("NATGAS", "yahoo")],
    ),
    "indices": (
        "Индексы",
        [("SPX500", "yahoo"), ("NAS100", "yahoo"), ("US30", "yahoo"), ("DE40", "yahoo")],
    ),
    "crypto": (
        "Крипта",
        [(s, DEFAULT_EXCHANGE) for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT")],
    ),
}


def _parse_bulk(args: list[str]) -> list[tuple[str, str]]:
    """Разбирает список инструментов.

    Площадка задаётся суффиксом через @ либо угадывается: всё с USDT — это
    крипта на DEFAULT_EXCHANGE, остальное — валюты и сырьё через Yahoo.
    Угадывание избавляет от необходимости писать площадку у каждого из
    четырнадцати.
    """
    parsed: list[tuple[str, str]] = []
    for raw in args:
        token = raw.strip().strip(",")
        if not token:
            continue
        if "@" in token:
            symbol, _, exchange = token.partition("@")
        else:
            symbol = token
            exchange = DEFAULT_EXCHANGE if "USDT" in symbol.upper() else "yahoo"
        parsed.append((symbol, exchange.lower() or "yahoo"))
    return parsed


async def _add_many(
    message: Message, user: User, items: list[tuple[str, str]]
) -> None:
    status = await message.answer(f"⏳ Добавляю {len(items)}…")

    added, skipped, failed = [], [], []
    banned: dict[str, ExchangeBanned] = {}
    subscribed: list[str] = []

    for symbol, exchange in items:
        already = await adopt_existing(user, symbol, exchange)
        if already is not None:
            subscribed.append(already.symbol)
            continue

        if await quota_left(user) == 0:
            skipped.append(f"{symbol}: личный потолок инструментов исчерпан")
            continue

        try:
            meta = await registry.validate_candidate(symbol, exchange)
        except ExchangeBanned as exc:
            # Пауза общая на площадку: остальные её символы отвалятся так же,
            # но проверить их всё равно надо — вдруг часть списка с Yahoo.
            banned[exc.exchange] = exc
            failed.append(f"{symbol}: {exchange} на паузе")
            continue
        except SymbolNotFound as exc:
            hint = f" (похоже на {exc.suggestions[0]})" if exc.suggestions else ""
            failed.append(f"{symbol}: не найден{hint}")
            continue
        except registry.RegistryError as exc:
            skipped.append(f"{symbol}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — площадка может отвалиться как угодно
            failed.append(f"{symbol}: {str(exc)[:60]}")
            continue

        keywords, _ = await suggest_keywords(meta.symbol)
        instrument = await registry.add_instrument(
            meta=meta,
            exchange=exchange,
            keywords=keywords,
            added_by=user.tg_id,
            round_step=derive_round_step(meta.last_price),
        )
        await registry.subscribe(user.tg_id, instrument.id)

        delay = int(meta.extra.get("delay_minutes", 0))
        mark = f" ⏱{delay}м" if delay else ""
        added.append(f"{instrument.symbol} — {meta.last_price:g}{mark}")

    lines = []
    if added:
        lines.append(f"<b>✅ Добавлено ({len(added)})</b>")
        lines += [f"  {x}" for x in added]
    if subscribed:
        lines.append(f"\n<b>🔔 Уже были в реестре, подписал ({len(subscribed)})</b>")
        lines += [f"  {x}" for x in subscribed]
    if skipped:
        lines.append(f"\n<b>⏭ Пропущено ({len(skipped)})</b>")
        lines += [f"  {x}" for x in skipped]
    if failed:
        lines.append(f"\n<b>❌ Не вышло ({len(failed)})</b>")
        lines += [f"  {x}" for x in failed]
    if any("⏱" in x for x in added):
        lines.append("\n<i>⏱ — котировка отстаёт на столько минут.</i>")
    for exc in banned.values():
        lines.append(f"\n⏳ {exc}{ban_hint(exc.exchange)}")

    await status.edit_text("\n".join(lines) or "Нечего добавлять.")


@router.message(Command("add_many"))
async def cmd_add_many(message: Message, command: CommandObject, user: User) -> None:
    args = (command.args or "").replace(",", " ").split()

    if not args:
        presets = "\n".join(
            f"<code>/add_many {key}</code> — {title.lower()}, {len(items)} шт."
            for key, (title, items) in PRESETS.items()
        )
        await message.answer(
            "<b>Пакетное добавление</b>\n\n"
            "Списком через пробел:\n"
            "<code>/add_many EURUSD XAUUSD BRENT US500</code>\n\n"
            f"Площадка угадывается: всё с USDT идёт на {DEFAULT_EXCHANGE}, "
            "остальное через Yahoo. Можно указать явно: "
            "<code>SOL/USDT@okx</code>\n\n"
            "<b>Готовые наборы</b>\n" + presets
        )
        return

    if len(args) == 1 and args[0].lower() in PRESETS:
        title, items = PRESETS[args[0].lower()]
        await _add_many(message, user, items)
        return

    await _add_many(message, user, _parse_bulk(args))


@admin_router.message(Command("instruments"))
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


@admin_router.message(Command("rm_instrument"))
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


@admin_router.callback_query(F.data == "menu:instruments")
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
