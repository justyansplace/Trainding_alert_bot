"""Настройка алертов: единица порога, чувствительность, направление, лимиты.

Порог можно задавать в двух единицах, и это не косметика:

  * **ATR** — средний часовой ход инструмента. Порог сам подстраивается под
    волатильность: 0.3×ATR у спокойной валютной пары и у бурного альткоина
    означают одинаковую «близость» в торговом смысле.
  * **Проценты** — проще понять и легче соотнести с графиком, но один и тот же
    процент для разных инструментов значит совершенно разное. У BTC 0.3×ATR —
    это примерно 0.26%, у EUR/USD — 0.026%, разница на порядок.

Поэтому у каждой единицы свой ползунок и свой набор шагов: переключение не
пересчитывает одно в другое, а вспоминает последнее значение для этой единицы.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.config import get_settings
from alert_bot.db.models import Direction, ThresholdUnit, User
from alert_bot.db.session import session_scope

log = logging.getLogger(__name__)

router = Router(name="alert_settings")

# Шаги ползунков подобраны так, чтобы соседние значения давали заметно разную
# частоту алертов, а не отличались косметически.
ATR_STEPS = [0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5]
PCT_STEPS = [0.05, 0.1, 0.15, 0.25, 0.4, 0.6, 1.0, 1.5, 2.5]
COOLDOWN_STEPS = [1, 2, 4, 8, 12, 24]
CAP_STEPS = [5, 10, 25, 50, 100]

DIRECTION_LABEL = {
    Direction.ANY.value: "любые",
    Direction.UP.value: "только вверх ▲",
    Direction.DOWN.value: "только вниз ▼",
}


def _nearest(value: float, steps: list) -> int:  # noqa: ANN001
    """Индекс ближайшего шага — значение могло прийти из конфига мимо сетки."""
    return min(range(len(steps)), key=lambda i: abs(steps[i] - value))


def _shift(value: float, steps: list, direction: int):  # noqa: ANN001, ANN202
    index = min(max(_nearest(value, steps) + direction, 0), len(steps) - 1)
    return steps[index]


def _bar(value: float, steps: list) -> str:  # noqa: ANN001
    """Наглядная шкала: где текущее значение среди возможных."""
    index = _nearest(value, steps)
    return "".join("●" if i == index else "·" for i in range(len(steps)))


def resolve(user: User) -> dict:
    """Действующие настройки: своё значение или дефолт из конфига."""
    settings = get_settings()
    return {
        "unit": user.def_threshold_unit or settings.default_threshold_unit,
        "atr_k": user.def_atr_k if user.def_atr_k is not None else settings.default_atr_k,
        "pct": (
            user.def_threshold_pct
            if user.def_threshold_pct is not None
            else settings.default_threshold_pct
        ),
        "cooldown": user.def_cooldown_hours or settings.cooldown_hours,
        "cap": user.max_alerts_per_day or settings.max_alerts_per_user_per_day,
        "direction": user.direction_filter or Direction.ANY.value,
    }


def render(user: User) -> tuple[str, InlineKeyboardMarkup]:
    current = resolve(user)
    is_atr = current["unit"] == ThresholdUnit.ATR.value

    if is_atr:
        threshold_line = f"<code>{current['atr_k']:.2f} × ATR</code>"
        scale = _bar(current["atr_k"], ATR_STEPS)
        explain = (
            "ATR — средний часовой ход инструмента. Порог сам подстраивается "
            "под волатильность, поэтому одно значение подходит и спокойной "
            "валютной паре, и бурному альткоину."
        )
    else:
        threshold_line = f"<code>{current['pct']:.2f} %</code> от цены уровня"
        scale = _bar(current["pct"], PCT_STEPS)
        explain = (
            "Сигнал, когда |уровень − цена| ≤ уровень × ставка. Процент проще "
            "соотнести с графиком, но не учитывает волатильность: 0.25% для BTC — "
            "это близко, для валютной пары — очень далеко."
        )

    text = (
        "<b>🔔 Настройка алертов</b>\n\n"
        f"<b>Предупреждать за</b> {threshold_line}\n"
        f"<code>{scale}</code>\n"
        f"<i>{explain}</i>\n\n"
        f"<b>Направление:</b> {DIRECTION_LABEL[current['direction']]}\n"
        f"<b>Пауза после срабатывания:</b> {current['cooldown']} ч\n"
        f"<b>Не больше алертов в сутки:</b> {current['cap']}\n\n"
        "<i>Настройки общие для всех ваших инструментов.</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ ATR" if is_atr else "ATR"), callback_data="as:unit:atr"
                ),
                InlineKeyboardButton(
                    text=("✅ Проценты" if not is_atr else "Проценты"),
                    callback_data="as:unit:percent",
                ),
            ],
            [
                InlineKeyboardButton(text="◀️ Ближе", callback_data="as:thr:-"),
                InlineKeyboardButton(text="Дальше ▶️", callback_data="as:thr:+"),
            ],
            [
                InlineKeyboardButton(
                    text=f"Направление: {DIRECTION_LABEL[current['direction']]}",
                    callback_data="as:dir",
                )
            ],
            [
                InlineKeyboardButton(text="Пауза −", callback_data="as:cd:-"),
                InlineKeyboardButton(text="Пауза +", callback_data="as:cd:+"),
            ],
            [
                InlineKeyboardButton(text="Лимит −", callback_data="as:cap:-"),
                InlineKeyboardButton(text="Лимит +", callback_data="as:cap:+"),
            ],
            [
                InlineKeyboardButton(text="↩️ Сбросить", callback_data="as:reset"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
            ],
        ]
    )
    return text, keyboard


async def _mutate(tg_id: int, action: str, arg: str) -> User:
    """Меняет одну настройку и возвращает обновлённого пользователя."""
    async with session_scope() as session:
        user = await session.get(User, tg_id)
        assert user is not None
        current = resolve(user)

        if action == "unit":
            user.def_threshold_unit = arg
            # Значение другой единицы не пересчитывается: у каждой свой
            # смысл и своя сетка, а пересчёт дал бы неожиданное число.
            if arg == ThresholdUnit.ATR.value and user.def_atr_k is None:
                user.def_atr_k = current["atr_k"]
            if arg == ThresholdUnit.PERCENT.value and user.def_threshold_pct is None:
                user.def_threshold_pct = current["pct"]

        elif action == "thr":
            step = 1 if arg == "+" else -1
            if current["unit"] == ThresholdUnit.ATR.value:
                user.def_atr_k = _shift(current["atr_k"], ATR_STEPS, step)
            else:
                user.def_threshold_pct = _shift(current["pct"], PCT_STEPS, step)

        elif action == "dir":
            order = [Direction.ANY.value, Direction.UP.value, Direction.DOWN.value]
            user.direction_filter = order[(order.index(current["direction"]) + 1) % len(order)]

        elif action == "cd":
            step = 1 if arg == "+" else -1
            user.def_cooldown_hours = int(_shift(current["cooldown"], COOLDOWN_STEPS, step))

        elif action == "cap":
            step = 1 if arg == "+" else -1
            user.max_alerts_per_day = int(_shift(current["cap"], CAP_STEPS, step))

        elif action == "reset":
            user.def_threshold_unit = None
            user.def_atr_k = None
            user.def_threshold_pct = None
            user.def_cooldown_hours = None
            user.max_alerts_per_day = None
            user.direction_filter = None

        return user


async def _show(callback: CallbackQuery, user: User, toast: str = "") -> None:
    text, keyboard = render(user)
    assert callback.message is not None
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 — «message is not modified» ловит errors-роутер
        pass
    await callback.answer(toast)


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, user: User) -> None:
    text, keyboard = render(user)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:alerts")
async def cb_menu_alerts(callback: CallbackQuery, user: User) -> None:
    await _show(callback, user)


@router.callback_query(F.data.startswith("as:"))
async def cb_change(callback: CallbackQuery, user: User) -> None:
    assert callback.data is not None
    parts = callback.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else "")

    updated = await _mutate(user.tg_id, action, arg)
    toast = "Сброшено" if action == "reset" else "Сохранено"
    await _show(callback, updated, toast)
