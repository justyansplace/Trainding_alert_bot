"""Пульс процесса для healthcheck.

Контейнер, который «запущен», и бот, который работает, — разные вещи. Цикл цены
может уткнуться в зависший сокет и молчать часами, пока Docker считает всё
исправным: процесс-то жив. Поэтому цикл на каждой итерации трогает файл, а
healthcheck смотрит на его возраст.

Проверяется именно то, что цикл делает оборот, а не то, что данные приходят:
на выходных форекс закрыт, цены не меняются, и это нормальное состояние, а не
поломка.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

HEARTBEAT_FILENAME = "heartbeat"

# Во сколько раз возраст пульса может превысить интервал цикла, прежде чем
# контейнер считается больным. Запас нужен на медленный ответ биржи и на то,
# что тик с пересчётом ATR тянет 700 свечей и идёт дольше обычного.
STALE_FACTOR = 6
MIN_STALE_SECONDS = 90


def heartbeat_path() -> Path:
    from alert_bot.config import get_settings

    return get_settings().db_path.parent / HEARTBEAT_FILENAME


def touch_heartbeat() -> None:
    """Отметка «цикл сделал оборот». Ошибки записи не должны ронять цикл."""
    try:
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        log.debug("не удалось записать пульс", exc_info=True)


def clear_heartbeat() -> None:
    """Убирается на штатной остановке: мёртвый процесс не должен выглядеть живым."""
    try:
        heartbeat_path().unlink(missing_ok=True)
    except OSError:
        pass


def heartbeat_age_seconds() -> float | None:
    """Возраст пульса. None, если файла нет — процесс не стартовал или остановлен."""
    try:
        raw = heartbeat_path().read_text(encoding="utf-8").strip()
        return max(time.time() - float(raw), 0.0)
    except (OSError, ValueError):
        return None


def stale_after_seconds() -> float:
    from alert_bot.config import get_settings

    return max(get_settings().price_poll_seconds * STALE_FACTOR, MIN_STALE_SECONDS)


def check() -> tuple[bool, str]:
    age = heartbeat_age_seconds()
    if age is None:
        return False, "пульса нет — цикл цены не запускался"

    limit = stale_after_seconds()
    if age > limit:
        return False, f"пульс протух: {age:.0f} c при пороге {limit:.0f} c"
    return True, f"пульс {age:.0f} c назад"


def main() -> None:
    """Точка входа для HEALTHCHECK в контейнере."""
    healthy, message = check()
    print(message)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
