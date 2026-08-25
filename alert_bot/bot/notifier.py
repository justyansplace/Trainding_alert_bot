"""Рассылка алертов.

Очередь с ограничителем скорости: Telegram режет примерно на 1 сообщении в
секунду в чат и ~30 в секунду суммарно. При десяти инструментах и десятке
подписчиков всплеск легко упирается в лимит, а отправка "в лоб" получает
TelegramRetryAfter и теряет сообщения.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from alert_bot.db.models import User
from alert_bot.db.session import session_scope

log = logging.getLogger(__name__)

GLOBAL_RATE = 25  # сообщений в секунду суммарно, с запасом к лимиту в 30
PER_CHAT_INTERVAL = 1.05  # секунд между сообщениями в один чат


@dataclass(slots=True)
class Outgoing:
    chat_id: int
    text: str
    keyboard: InlineKeyboardMarkup | None = None


class Notifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._queue: asyncio.Queue[Outgoing] = asyncio.Queue()
        self._last_sent: dict[int, float] = {}
        self._stopping = asyncio.Event()

    def enqueue(self, message: Outgoing) -> None:
        self._queue.put_nowait(message)

    async def _respect_limits(self, chat_id: int) -> None:
        last = self._last_sent.get(chat_id)
        if last is not None:
            wait = PER_CHAT_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        await asyncio.sleep(1 / GLOBAL_RATE)

    async def _deactivate(self, tg_id: int) -> None:
        """Пользователь заблокировал бота — молча снимаем с рассылки."""
        async with session_scope() as session:
            user = await session.get(User, tg_id)
            if user is not None:
                user.active = False
        log.info("tg_id=%s заблокировал бота, снят с рассылки", tg_id)

    async def _send(self, message: Outgoing) -> bool:
        await self._respect_limits(message.chat_id)
        try:
            await self._bot.send_message(
                message.chat_id, message.text, reply_markup=message.keyboard
            )
            self._last_sent[message.chat_id] = time.monotonic()
            return True
        except TelegramRetryAfter as exc:
            log.warning("Лимит Telegram, ждём %s c", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 0.5)
            self.enqueue(message)
            return False
        except TelegramForbiddenError:
            await self._deactivate(message.chat_id)
            return False
        except Exception:  # noqa: BLE001 — одно сообщение не должно ронять рассылку
            log.exception("Не удалось отправить сообщение в %s", message.chat_id)
            return False

    async def run(self) -> None:
        log.info("Рассылка запущена")
        while not self._stopping.is_set():
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            await self._send(message)
            self._queue.task_done()

    async def drain(self) -> None:
        """Ждёт, пока очередь опустеет. Нужно на остановке: сообщение, уже
        поставленное в очередь, должно уйти, а не пропасть вместе с процессом."""
        await self._queue.join()

    def stop(self) -> None:
        self._stopping.set()

    @property
    def pending(self) -> int:
        return self._queue.qsize()
