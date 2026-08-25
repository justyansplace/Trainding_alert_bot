"""Точка входа: инициализация БД, запуск циклов и корректная остановка.

Про остановку. Docker при `docker stop` шлёт SIGTERM и через десять секунд
добивает SIGKILL. aiogram сигнал ловит сам, но закрывает свою HTTP-сессию сразу
после выхода из polling — раньше, чем успевают доработать наши фоновые задачи.
Сообщение, оказавшееся в этот момент в полёте, теряется с ошибкой «Connector is
closed». Поэтому остановка навешана на shutdown-хук диспетчера: он вызывается ДО
закрытия сессии, и очередь успевает опустеть.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress

from aiogram import Bot, Dispatcher

from alert_bot.bot.main import build_bot, build_dispatcher, set_commands
from alert_bot.bot.notifier import Notifier
from alert_bot.config import get_settings
from alert_bot.db.session import dispose_engine, init_db
from alert_bot.health import check_data_dir_writable, clear_heartbeat
from alert_bot.market.providers.ccxt_provider import close_all_exchanges
from alert_bot.market.providers.oanda_provider import close_session as close_oanda
from alert_bot.scheduler import NewsLoop, PriceLoop
from alert_bot.webhealth import port_from_env, start_health_server

log = logging.getLogger(__name__)

# Сколько ждать, пока очередь рассылки опустеет на остановке. Docker даёт всего
# десять секунд до SIGKILL, поэтому укладываемся с запасом.
DRAIN_TIMEOUT_SECONDS = 5.0


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("aiogram.event", "ccxt", "httpx2", "openai._base_client", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Runtime:
    """Фоновые задачи и их остановка в правильном порядке."""

    def __init__(self, bot: Bot) -> None:
        self.notifier = Notifier(bot)
        self.price_loop = PriceLoop(notifier=self.notifier)
        self.news_loop = NewsLoop(admin_notifier=self.notifier)
        self.tasks: list[asyncio.Task] = []
        self._health_runner = None

    async def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self.notifier.run(), name="notifier"),
            asyncio.create_task(self.price_loop.run(), name="price_loop"),
            asyncio.create_task(self.news_loop.run(), name="news_loop"),
        ]

    async def stop(self) -> None:
        log.info("Останавливаюсь…")

        # Сначала перестаём порождать новое, потом даём очереди уйти.
        self.price_loop.stop()
        self.news_loop.stop()

        if self.notifier.pending:
            log.info("Досылаю %s сообщений из очереди", self.notifier.pending)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.notifier.drain(), timeout=DRAIN_TIMEOUT_SECONDS)

        self.notifier.stop()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task

        await close_all_exchanges()
        await close_oanda()
        clear_heartbeat()


async def run() -> None:
    setup_logging()
    settings = get_settings()

    # HTTP-проверка поднимается до всего остального. Если дальше что-то упадёт —
    # недоступный том, отвергнутый токен, — платформа увидит внятную причину по
    # /health, а не голое «healthcheck failure» без единого намёка.
    health_runner = None
    port = port_from_env()
    if port is not None:
        health_runner = await start_health_server(port)
    else:
        log.info("PORT не задан — HTTP-проверка живости не поднимается")

    writable, message = check_data_dir_writable()
    log.info(message) if writable else log.error(message)

    try:
        await _serve(settings)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()


async def _serve(settings) -> None:  # noqa: ANN001
    await init_db()
    log.info("БД готова: %s", settings.db_path)

    bot = build_bot()
    dp: Dispatcher = build_dispatcher()
    runtime = Runtime(bot)

    # Хуки диспетчера: shutdown срабатывает до того, как aiogram закроет сессию.
    dp.startup.register(runtime.start)
    dp.shutdown.register(runtime.stop)

    await set_commands(bot)
    me = await bot.get_me()
    log.info("Бот @%s запущен, админ %s", me.username, settings.admin_tg_id)

    try:
        await dp.start_polling(bot)
    finally:
        await dispose_engine()
        log.info("Остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
