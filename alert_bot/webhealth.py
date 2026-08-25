"""HTTP-эндпоинт живости для платформ, которые проверяют только по HTTP.

Docker умеет запускать команду внутри контейнера, поэтому там достаточно
`alert_bot.health`. Railway и подобные платформы так не умеют: у них
healthcheckPath — это путь, по которому они ходят снаружи. Без такого пути
платформа знает только «процесс жив» и не заметит, что цикл цены встал.

Сервер поднимается, только если задан PORT — его выставляет платформа. Локально
и в docker compose переменной нет, и лишний слушающий сокет не появляется.
"""

from __future__ import annotations

import json
import logging
import os

from aiohttp import web

from alert_bot.health import check

log = logging.getLogger(__name__)


async def _health(_request: web.Request) -> web.Response:
    healthy, message = check()
    # 503, а не 500: сервис жив, но временно не в порядке — платформа должна
    # перезапустить контейнер, а не считать это ошибкой приложения.
    return web.json_response(
        {"status": "ok" if healthy else "unhealthy", "detail": message},
        status=200 if healthy else 503,
        # Без этого кириллица уезжает в \uXXXX и лог платформы нечитаем.
        dumps=lambda data: json.dumps(data, ensure_ascii=False),
    )


async def _root(_request: web.Request) -> web.Response:
    return web.Response(text="alert-bot")


def port_from_env() -> int | None:
    raw = os.environ.get("PORT", "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


async def start_health_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/", _root)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    # 0.0.0.0: платформа ходит снаружи контейнера, слушать только localhost
    # означает, что проверка не достучится и сервис будет вечно «нездоров».
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()  # noqa: S104

    log.info("HTTP-проверка живости слушает 0.0.0.0:%s, путь /health", port)
    return runner
