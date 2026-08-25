"""Загрузка источников.

Все запросы — условные (ETag / Last-Modified). RSS-фиды обновляются раз в
десятки минут, а опрашиваются раз в десять; без conditional GET это трафик
впустую и прямая дорога к 429 или бану по IP.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import aiohttp
import feedparser

from alert_bot.db.models import SourceKind

log = logging.getLogger(__name__)

USER_AGENT = "alert-bot/0.1 (+https://github.com/local/alert-bot)"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_ARTICLES_PER_FETCH = 40
EXCERPT_LIMIT = 800


@dataclass(slots=True)
class RawArticle:
    url: str
    title: str
    published_at: datetime | None
    excerpt: str


@dataclass(slots=True)
class FetchResult:
    articles: list[RawArticle] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_entry_date(entry) -> datetime | None:  # noqa: ANN001
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if parsed is None:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime(*struct[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def _clean(text: str) -> str:
    import re

    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(without_tags.split())


def parse_feed(payload: bytes) -> list[RawArticle]:
    parsed = feedparser.parse(payload)
    articles: list[RawArticle] = []

    for entry in parsed.entries[:MAX_ARTICLES_PER_FETCH]:
        link = (entry.get("link") or "").strip()
        title = _clean(entry.get("title") or "")
        if not link or not title:
            continue

        summary = _clean(entry.get("summary") or entry.get("description") or "")
        articles.append(
            RawArticle(
                url=link,
                title=title,
                published_at=_parse_entry_date(entry),
                excerpt=summary[:EXCERPT_LIMIT],
            )
        )

    return articles


async def fetch_rss(
    session: aiohttp.ClientSession,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as response:
            if response.status == 304:
                return FetchResult(not_modified=True, etag=etag, last_modified=last_modified)
            if response.status != 200:
                return FetchResult(error=f"HTTP {response.status}")

            payload = await response.read()
            return FetchResult(
                articles=parse_feed(payload),
                etag=response.headers.get("ETag", etag),
                last_modified=response.headers.get("Last-Modified", last_modified),
            )

    except TimeoutError:
        return FetchResult(error="таймаут запроса")
    except aiohttp.ClientError as exc:
        return FetchResult(error=f"сетевая ошибка: {exc}")
    except Exception as exc:  # noqa: BLE001 — фид может отдать что угодно
        log.warning("Разбор %s упал", url, exc_info=True)
        return FetchResult(error=f"не удалось разобрать: {exc}")


async def fetch_cryptopanic(
    session: aiohttp.ClientSession, url: str, token: str
) -> FetchResult:
    """CryptoPanic — не RSS: собственная схема ответа, отсюда отдельный парсер."""
    if not token:
        return FetchResult(error="CRYPTOPANIC_TOKEN не задан")

    params = {"auth_token": token, "public": "true", "kind": "news"}
    try:
        async with session.get(
            url, params=params, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        ) as response:
            if response.status != 200:
                return FetchResult(error=f"HTTP {response.status}")
            payload = await response.json()
    except TimeoutError:
        return FetchResult(error="таймаут запроса")
    except Exception as exc:  # noqa: BLE001
        return FetchResult(error=f"CryptoPanic: {exc}")

    articles: list[RawArticle] = []
    for item in (payload.get("results") or [])[:MAX_ARTICLES_PER_FETCH]:
        link = item.get("original_url") or item.get("url") or ""
        title = _clean(item.get("title") or "")
        if not link or not title:
            continue

        published = None
        raw_date = item.get("published_at") or item.get("created_at")
        if raw_date:
            try:
                published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                published = None

        articles.append(
            RawArticle(url=link, title=title, published_at=published, excerpt=_clean(
                item.get("description") or ""
            )[:EXCERPT_LIMIT])
        )

    return FetchResult(articles=articles)


async def probe_source(kind: str, url: str, token: str = "") -> FetchResult:
    """Разовая проверка источника для /add_source.

    Показать админу три свежих заголовка до записи в БД — единственный способ
    убедиться, что фид живой и вообще разбирается, а не отдаёт HTML-страницу
    с капчей под видом ленты.
    """
    async with aiohttp.ClientSession() as session:
        if kind == SourceKind.CRYPTOPANIC.value:
            return await fetch_cryptopanic(session, url, token)
        if kind == SourceKind.RSS.value:
            result = await fetch_rss(session, url)
            if result.ok and not result.articles:
                return FetchResult(error="лента разобралась, но не содержит записей")
            return result
        return FetchResult(error=f"неизвестный тип источника: {kind}")


async def gather_with_limit(coros, limit: int = 5):  # noqa: ANN001
    semaphore = asyncio.Semaphore(limit)

    async def guarded(coro):  # noqa: ANN001
        async with semaphore:
            return await coro

    return await asyncio.gather(*(guarded(c) for c in coros))
