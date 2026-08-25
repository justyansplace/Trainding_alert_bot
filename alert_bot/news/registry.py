"""Реестр источников: чтение, добавление и отслеживание здоровья.

Источники ломаются молча — фид переезжает, начинает отдавать 403, меняет формат
или тихо перестаёт обновляться. Без счётчика подряд идущих ошибок и отметки
last_ok_at выясняется это случайно и через месяц.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from alert_bot.db.models import Source, SourceKind, utcnow
from alert_bot.db.session import session_scope

log = logging.getLogger(__name__)

# После скольких подряд неудач источник отключается автоматически.
MAX_CONSECUTIVE_FAILURES = 5

# Типы, для которых есть парсер. /add_source не может принять произвольный
# JSON-API: под каждую схему ответа нужен код, а не строка в конфиге.
SUPPORTED_KINDS = tuple(k.value for k in SourceKind)


class SourceError(Exception):
    pass


async def list_sources(enabled_only: bool = False) -> list[Source]:
    async with session_scope() as session:
        stmt = select(Source).order_by(Source.name)
        if enabled_only:
            stmt = stmt.where(Source.enabled.is_(True))
        return list((await session.scalars(stmt)).all())


async def get_source(source_id: int) -> Source | None:
    async with session_scope() as session:
        return await session.get(Source, source_id)


async def add_source(
    name: str, kind: str, url: str, added_by: int, weight: float = 1.0, poll_interval: int = 600
) -> Source:
    if kind not in SUPPORTED_KINDS:
        raise SourceError(
            f"тип {kind!r} не поддерживается; доступны: {', '.join(SUPPORTED_KINDS)}"
        )

    async with session_scope() as session:
        existing = await session.scalar(select(Source).where(Source.url == url))
        if existing is not None:
            if existing.enabled:
                raise SourceError(f"источник уже добавлен: {existing.name}")
            existing.enabled = True
            existing.consecutive_failures = 0
            existing.last_error = None
            return existing

        source = Source(
            name=name,
            kind=kind,
            url=url,
            weight=weight,
            poll_interval=poll_interval,
            added_by=added_by,
            added_at=utcnow(),
        )
        session.add(source)
        await session.flush()
        log.info("Источник %s добавлен (id=%s)", name, source.id)
        return source


async def set_enabled(source_id: int, enabled: bool) -> Source | None:
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return None
        source.enabled = enabled
        if enabled:
            source.consecutive_failures = 0
            source.last_error = None
        return source


async def delete_source(source_id: int) -> str | None:
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return None
        name = source.name
        await session.delete(source)
        return name


async def record_success(
    source_id: int, etag: str | None, last_modified: str | None
) -> None:
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return
        source.last_ok_at = utcnow()
        source.last_error = None
        source.consecutive_failures = 0
        if etag:
            source.etag = etag
        if last_modified:
            source.last_modified = last_modified


async def record_failure(source_id: int, error: str) -> bool:
    """Возвращает True, если источник был отключён этой неудачей."""
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return False

        source.consecutive_failures += 1
        source.last_error = error[:300]

        if source.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and source.enabled:
            source.enabled = False
            log.warning(
                "Источник %s отключён после %s неудач подряд: %s",
                source.name,
                source.consecutive_failures,
                error,
            )
            return True

    return False


def health_label(source: Source) -> str:
    if not source.enabled:
        return "⛔ отключён"
    if source.consecutive_failures:
        return f"⚠️ ошибок подряд: {source.consecutive_failures}"
    if source.last_ok_at is None:
        return "⏳ ещё не опрашивался"
    return "🟢 в порядке"
