"""Сборка Telegram-бота: Bot, Dispatcher, роутеры, middleware."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from alert_bot.bot import (
    admin,
    admin_sources,
    alert_settings,
    errors,
    handlers,
    instruments,
    level_handlers,
    menu,
    settings_handlers,
)
from alert_bot.bot.access import list_admins
from alert_bot.bot.access import AccessMiddleware
from alert_bot.config import get_settings

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="addlevel", description="Поставить уровень"),
    BotCommand(command="mylevels", description="Мои уровни"),
    BotCommand(command="subscribe", description="Инструменты"),
    BotCommand(command="add_instrument", description="Добавить инструмент"),
    BotCommand(command="add_many", description="Добавить несколько сразу"),
    BotCommand(command="brief", description="Сводка по новостям"),
    BotCommand(command="alerts", description="Настройка алертов"),
    BotCommand(command="settings", description="Тихие часы и прочее"),
    BotCommand(command="mute", description="Пауза алертов"),
    BotCommand(command="status", description="Статус"),
    BotCommand(command="help", description="Справка"),
]


def build_bot() -> Bot:
    settings = get_settings()
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # AccessMiddleware кладёт User в data['user'] и отсекает чужих до хендлеров.
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    dp.include_router(menu.router)
    dp.include_router(handlers.router)
    dp.include_router(level_handlers.router)
    dp.include_router(alert_settings.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(instruments.router)
    dp.include_router(instruments.admin_router)
    dp.include_router(admin_sources.router)
    dp.include_router(admin.router)

    # Последним: перехватывает то, что не поймали хендлеры.
    dp.include_router(errors.router)

    return dp


# Админские команды. Telegram показывает подсказку по «/» только для тех команд,
# что зарегистрированы для этого чата, поэтому админу список ставится отдельно —
# иначе половина возможностей бота существует, но нигде не видна.
ADMIN_COMMANDS = [
    BotCommand(command="instruments", description="Список инструментов"),
    BotCommand(command="rm_instrument", description="Отключить инструмент"),
    BotCommand(command="add_source", description="Добавить ленту новостей"),
    BotCommand(command="sources", description="Источники и их здоровье"),
    BotCommand(command="toggle_source", description="Включить/отключить ленту"),
    BotCommand(command="rm_source", description="Удалить ленту"),
    BotCommand(command="gen_invite", description="Создать код доступа"),
    BotCommand(command="invites", description="Неиспользованные коды"),
    BotCommand(command="users", description="Список людей"),
    BotCommand(command="revoke", description="Отозвать доступ"),
    BotCommand(command="admins", description="Кто администратор"),
    BotCommand(command="grant_admin", description="Выдать роль администратора"),
    BotCommand(command="revoke_admin", description="Снять роль администратора"),
    BotCommand(command="usage", description="Расход на модель"),
]


async def set_commands(bot: Bot) -> None:
    """Подсказки по «/».

    Админский список ставится каждому администратору, а не только владельцу из
    конфига: иначе второй администратор имеет все права, не видит ни одной
    своей команды и вынужден узнавать их от первого.
    """
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())

    for admin_user in await list_admins():
        try:
            await bot.set_my_commands(
                USER_COMMANDS + ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_user.tg_id),
            )
        except Exception:  # noqa: BLE001 — админ мог ещё не открыть чат с ботом
            log.warning(
                "Не удалось поставить админские команды для %s",
                admin_user.tg_id,
                exc_info=True,
            )
