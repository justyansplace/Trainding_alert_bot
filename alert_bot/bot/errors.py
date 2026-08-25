"""Обработка ошибок Telegram, которые не являются поломкой.

Две штуки шумят в логе и сбивают с толку пользователя, хотя чинить в коде
нечего:

  * **Устаревший callback.** Кнопки живут в переписке вечно, а идентификатор
    нажатия Telegram считает действительным лишь короткое время. После
    перезапуска бота — то есть после каждого обновления на сервере — нажатие на
    старую кнопку падает с «query is too old». Пользователь при этом видит
    ровно ничего: кнопка нажалась и не сработала. Правильный ответ здесь —
    объяснить и предложить открыть меню заново.

  * **Повторное нажатие.** Если экран перерисовывается тем же содержимым,
    Telegram отвечает «message is not modified». Это не ошибка, а сообщение
    «менять нечего».
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

router = Router(name="errors")

STALE_MARKERS = ("query is too old", "query id is invalid", "message to edit not found")
UNCHANGED_MARKER = "message is not modified"

STALE_TEXT = (
    "⏳ Эта кнопка из старого сообщения и больше не работает — бот успел "
    "перезапуститься.\n\nОткройте меню заново:"
)


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Открыть меню", callback_data="menu:home")]]
    )


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    exception = event.exception

    if not isinstance(exception, TelegramBadRequest):
        log.exception("Необработанная ошибка", exc_info=exception)
        return False

    message = str(exception).lower()

    if UNCHANGED_MARKER in message:
        # Экран уже показывает нужное — гасим «часики» на кнопке и молчим.
        callback = getattr(event.update, "callback_query", None)
        if isinstance(callback, CallbackQuery):
            with_suppress = getattr(callback, "answer", None)
            if with_suppress is not None:
                try:
                    await callback.answer()
                except TelegramBadRequest:
                    pass
        return True

    if any(marker in message for marker in STALE_MARKERS):
        callback = getattr(event.update, "callback_query", None)
        if isinstance(callback, CallbackQuery) and callback.message is not None:
            try:
                await callback.message.answer(STALE_TEXT, reply_markup=_menu_kb())
            except TelegramBadRequest:
                log.debug("не удалось ответить на устаревший callback", exc_info=True)
        log.info("Нажата кнопка из устаревшего сообщения")
        return True

    log.warning("Telegram отклонил запрос: %s", exception)
    return True
