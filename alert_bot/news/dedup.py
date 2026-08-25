"""Дедупликация новостей.

Два независимых механизма, потому что дубли бывают двух разных видов:

  * один и тот же URL с разными метками (utm, реферер, якорь) — ловится
    канонизацией и точным хешем;
  * одна и та же новость, перепечатанная другим изданием под чуть иным
    заголовком — ловится simhash по заголовку.

Без первого одна публикация уходит в LLM столько раз, сколько у неё вариантов
ссылки. Без второго за одну новость платим по числу изданий, её перепечатавших.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Метки, не влияющие на содержимое страницы.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_reader", "utm_brand",
        "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "twclid",
        "ref", "referrer", "source", "src", "cmpid", "mc_cid", "mc_eid",
        "_ga", "_gl", "amp", "at_medium", "at_campaign",
    }
)

SIMHASH_BITS = 64
SIMHASH_MAX_DISTANCE = 3

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)

# Слова, встречающиеся почти в каждом финансовом заголовке: как признаки
# сходства они бесполезны и только сближают несвязанные новости.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
        "with", "as", "is", "are", "was", "were", "be", "been", "it", "its",
        "this", "that", "these", "those", "from", "by", "amid", "after", "says",
        "и", "в", "на", "с", "по", "за", "из", "о", "об", "не", "что", "как",
    }
)


def canonical_url(url: str) -> str:
    """Убирает всё, что не влияет на содержимое: метки, якорь, регистр хоста."""
    parts = urlsplit(url.strip())

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query.sort()

    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    scheme = parts.scheme.lower() or "https"
    if scheme == "http":
        scheme = "https"  # один и тот же материал по http и https — один материал

    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    words = [w.lower() for w in _WORD_RE.findall(text)]
    meaningful = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    return meaningful or words


def simhash(text: str, bits: int = SIMHASH_BITS) -> int:
    """Классический simhash по словам заголовка."""
    tokens = _tokens(text)
    if not tokens:
        return 0

    vector = [0] * bits
    for token in tokens:
        digest = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=bits // 8).digest(), "big"
        )
        for bit in range(bits):
            vector[bit] += 1 if digest >> bit & 1 else -1

    result = 0
    for bit in range(bits):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming(left: int, right: int) -> int:
    return ((left ^ right) & ((1 << SIMHASH_BITS) - 1)).bit_count()


def is_near_duplicate(
    left: int, right: int, max_distance: int = SIMHASH_MAX_DISTANCE
) -> bool:
    return hamming(left, right) <= max_distance


def to_signed_64(value: int) -> int:
    """SQLite хранит INTEGER знаковым — 64-битный хеш надо укладывать в диапазон."""
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= (1 << 63) else value


def from_signed_64(value: int) -> int:
    return value + (1 << 64) if value < 0 else value
