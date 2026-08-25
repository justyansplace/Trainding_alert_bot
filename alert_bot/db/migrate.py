"""Лёгкие миграции схемы.

`Base.metadata.create_all` создаёт недостающие таблицы, но не трогает уже
существующие: новая колонка в модели просто не появится в старой базе, и
приложение упадёт на первом же запросе. Полноценный Alembic для проекта такого
размера — лишняя машинерия, поэтому здесь ровно то, что нужно: добавление
недостающих колонок через ALTER TABLE.

ALTER TABLE ADD COLUMN в SQLite безопасен и мгновенен, но требует, чтобы
значение по умолчанию было константой. Поэтому дефолты задаются явно и просто.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)

# (таблица, колонка, определение). Определение попадает в ALTER TABLE как есть.
COLUMNS: list[tuple[str, str, str]] = [
    # Единица измерения расстояния до уровня и порог в процентах. ATR
    # подстраивается под волатильность инструмента, процент проще понять —
    # выбор остаётся за человеком.
    ("users", "def_threshold_unit", "VARCHAR(8)"),
    ("users", "def_threshold_pct", "FLOAT"),
    ("subscriptions", "threshold_unit", "VARCHAR(8)"),
    ("subscriptions", "threshold_pct", "FLOAT"),
    # Персональные предохранители: раньше были общими на всех в конфиге.
    ("users", "def_cooldown_hours", "INTEGER"),
    ("users", "max_alerts_per_day", "INTEGER"),
    # Направление подхода: кому-то интересны только пробои вверх.
    ("users", "direction_filter", "VARCHAR(8)"),
]


async def _existing_columns(connection: AsyncConnection, table: str) -> set[str]:
    result = await connection.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result}


async def _table_exists(connection: AsyncConnection, table: str) -> bool:
    result = await connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    )
    return result.first() is not None


async def apply(connection: AsyncConnection) -> int:
    """Добавляет недостающие колонки. Возвращает, сколько добавлено."""
    added = 0

    for table, column, definition in COLUMNS:
        if not await _table_exists(connection, table):
            # Таблицы ещё нет — её создаст create_all уже с нужными колонками.
            continue

        if column in await _existing_columns(connection, table):
            continue

        await connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        )
        log.info("Миграция: %s.%s добавлена", table, column)
        added += 1

    return added
