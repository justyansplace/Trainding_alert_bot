"""Главное меню кнопками.

Telegram-кнопка не умеет «набрать команду» за пользователя — она шлёт callback.
Поэтому каждая кнопка сразу делает то, что написано, а не подсказывает, что
ввести руками. Команды при этом никуда не деваются: кто привык набирать —
набирает.

Экраны меню рисуются теми же функциями, что и команды, чтобы вид не разъезжался
между «нажал кнопку» и «ввёл /mylevels».
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.db.models import User

log = logging.getLogger(__name__)

router = Router(name="menu")

DISCLAIMER = (
    "⚠️ <b>Не инвестиционная рекомендация.</b> Бот следит за уровнями, которые "
    "вы задали сами, и сообщает, когда цена к ним подходит."
)


def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="➕ Добавить уровень", callback_data="menu:addlevel"),
            InlineKeyboardButton(text="📌 Мои уровни", callback_data="menu:mylevels"),
        ],
        [
            InlineKeyboardButton(text="🔔 Настройка алертов", callback_data="menu:alerts"),
        ],
        [InlineKeyboardButton(text="📈 Инструменты", callback_data="menu:subscribe")],
        [
            InlineKeyboardButton(text="📰 Сводка", callback_data="menu:brief"),
            InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
            InlineKeyboardButton(text="🔕 Пауза", callback_data="menu:mute"),
        ],
        [InlineKeyboardButton(text="❓ Справка", callback_data="menu:help")],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(text="🛠 Админка", callback_data="menu:admin")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")]]
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📈 Инструменты", callback_data="menu:instruments"),
                InlineKeyboardButton(text="📰 Источники", callback_data="menu:sources"),
            ],
            [
                InlineKeyboardButton(text="🎟 Инвайт", callback_data="menu:invite"),
                InlineKeyboardButton(text="👥 Люди", callback_data="menu:users"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
        ]
    )


def greeting(user: User | None) -> str:
    if user is None:
        return (
            "👋 Это приватный бот уровней.\n\n"
            "Вы ставите свои ценовые отметки, бот следит за ценой и предупреждает, "
            "когда она к ним подходит. По каждой отметке ведётся история "
            "срабатываний.\n\n"
            "Доступ по инвайт-коду:\n<code>/redeem ВАШ_КОД</code>\n\n" + DISCLAIMER
        )

    return (
        "👋 <b>Бот уровней</b>\n\n"
        "Вы ставите свои ценовые отметки — бот следит за ценой и предупреждает, "
        "когда она к ним подходит. По каждой ведётся история срабатываний.\n\n"
        "Выберите действие:"
    )


def _command_list(items: list[tuple[str, str]]) -> str:
    """Команды столбиком: команда, перенос, описание с отступом.

    Команда и описание в одной строке переносятся посреди слова на узком
    экране и разъезжаются — читать такой список невозможно.
    """
    return "\n".join(f"<code>{cmd}</code>\n    <i>{what}</i>" for cmd, what in items)


USER_COMMAND_LIST = [
    ("/addlevel BTC/USDT 78000", "поставить уровень"),
    ("/mylevels", "мои уровни — изменить или удалить"),
    ("/alerts", "порог, направление, лимиты"),
    ("/subscribe", "выбрать инструменты"),
    ("/brief", "сводка по новостям"),
    ("/status", "состояние и цены"),
    ("/settings", "за сколько предупреждать"),
    ("/quiet 23 7 Europe/Moscow", "тихие часы"),
    ("/mute 2h", "пауза на время"),
    ("/mute off", "снять паузу"),
]

ADMIN_COMMAND_LIST = [
    ("/add_instrument BTC/USDT", "добавить инструмент"),
    ("/rm_instrument BTC/USDT", "отключить инструмент"),
    ("/instruments", "список инструментов"),
    ("/add_source rss URL", "добавить ленту новостей"),
    ("/sources", "источники и их здоровье"),
    ("/toggle_source ID", "включить или отключить ленту"),
    ("/rm_source ID", "удалить ленту"),
    ("/gen_invite", "создать код доступа"),
    ("/invites", "неиспользованные коды"),
    ("/users", "список людей"),
    ("/revoke ID", "отозвать доступ"),
    ("/usage", "расход на модель"),
]

HELP_TEXT = (
    "<b>❓ Как это работает</b>\n\n"
    "1. Админ добавляет инструменты.\n"
    "2. Вы подписываетесь на нужные — кнопка «Инструменты».\n"
    "3. Ставите свои уровни — кнопка «Добавить уровень».\n"
    "4. Бот следит за ценой и пишет, когда она подходит к вашей отметке.\n\n"
    "<b>Важные детали</b>\n\n"
    "• Алерт приходит только при движении <i>к</i> уровню, а не от него.\n\n"
    "• Повторно по тому же уровню бот не пишет, пока цена не уйдёт и не "
    "вернётся — иначе сообщения шли бы очередью.\n\n"
    "• Уровень можно изменить или удалить — нажмите на него в списке.\n\n"
    "• Порог задаётся в ATR или в процентах — кнопка «Настройка алертов». "
    "ATR подстраивается под волатильность инструмента, процент проще "
    "соотнести с графиком.\n\n"
    "<b>Команды</b>\n\n" + _command_list(USER_COMMAND_LIST) + "\n\n" + DISCLAIMER
)

ADMIN_HELP = "<b>🛠 Админка</b>\n\n" + _command_list(ADMIN_COMMAND_LIST)


async def show_home(message: Message, user: User | None) -> None:
    await message.answer(
        greeting(user),
        reply_markup=main_menu_kb(user.is_admin) if user else None,
    )


@router.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.clear()
    assert callback.message is not None
    await callback.message.edit_text(
        greeting(user), reply_markup=main_menu_kb(user.is_admin)
    )
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery) -> None:
    assert callback.message is not None
    await callback.message.edit_text(HELP_TEXT, reply_markup=back_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:admin")
async def cb_admin(callback: CallbackQuery, user: User) -> None:
    if not user.is_admin:
        await callback.answer("Только для администратора", show_alert=True)
        return
    assert callback.message is not None
    await callback.message.edit_text(ADMIN_HELP, reply_markup=admin_menu_kb())
    await callback.answer()
