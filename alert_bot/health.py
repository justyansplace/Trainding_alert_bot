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


_write_failed_logged = False


def touch_heartbeat() -> None:
    """Отметка «цикл сделал оборот». Ошибки записи не должны ронять цикл."""
    global _write_failed_logged
    try:
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        # Один раз громко, дальше молча: если каталог недоступен на запись,
        # это повторяется каждый оборот цикла. Молчать нельзя — снаружи это
        # выглядит как «сервис нездоров» без единого намёка на причину.
        if not _write_failed_logged:
            log.error("Не удаётся записать пульс в %s: %s", heartbeat_path(), exc)
            _write_failed_logged = True


def check_data_dir_writable() -> tuple[bool, str]:
    """Доступен ли каталог данных на запись.

    Частая причина на хостингах: том подключается от root, а процесс работает
    от непривилегированного пользователя. Тогда молча не пишется ни база, ни
    пульс, и наружу это выглядит просто как «healthcheck failure».
    """
    from alert_bot.config import get_settings

    directory = get_settings().db_path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, f"каталог данных доступен на запись: {directory}"
    except OSError as exc:
        return False, (
            f"КАТАЛОГ ДАННЫХ НЕДОСТУПЕН НА ЗАПИСЬ: {directory} ({exc}). "
            f"На хостинге это обычно значит, что том подключён от root, "
            f"а процесс работает от uid {os.getuid()}."
        )


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
