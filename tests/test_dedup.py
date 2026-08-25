"""Тесты дедупликации: без неё за одну новость платим по числу её ссылок."""

from __future__ import annotations

import pytest

from alert_bot.news import dedup


# --------------------------------------------------------------------------- #
# Канонизация URL
# --------------------------------------------------------------------------- #


def test_utm_tags_collapse_to_one_article() -> None:
    """Одна публикация, разошедшаяся по каналам с разными метками."""
    base = "https://www.coindesk.com/markets/2026/08/22/btc-rally/"
    variants = [
        base,
        base + "?utm_source=twitter&utm_medium=social",
        base + "?fbclid=IwAR123",
        base + "?ref=newsletter&utm_campaign=daily",
        base.rstrip("/"),
    ]
    hashes = {dedup.url_hash(url) for url in variants}
    assert len(hashes) == 1


def test_www_and_scheme_normalised() -> None:
    assert dedup.url_hash("http://www.example.com/a") == dedup.url_hash("https://example.com/a")


def test_fragment_ignored() -> None:
    assert dedup.url_hash("https://x.com/a#section-2") == dedup.url_hash("https://x.com/a")


def test_meaningful_query_params_preserved() -> None:
    """Не всякий параметр — метка: id статьи менять нельзя."""
    assert dedup.url_hash("https://x.com/news?id=1") != dedup.url_hash("https://x.com/news?id=2")


def test_query_param_order_does_not_matter() -> None:
    assert dedup.url_hash("https://x.com/a?b=2&a=1") == dedup.url_hash("https://x.com/a?a=1&b=2")


def test_different_articles_stay_different() -> None:
    assert dedup.url_hash("https://x.com/a") != dedup.url_hash("https://x.com/b")


def test_host_case_ignored_but_path_case_kept() -> None:
    assert dedup.url_hash("https://EXAMPLE.com/a") == dedup.url_hash("https://example.com/a")
    assert dedup.url_hash("https://example.com/A") != dedup.url_hash("https://example.com/a")


# --------------------------------------------------------------------------- #
# Simhash заголовков
# --------------------------------------------------------------------------- #


def test_identical_titles_have_zero_distance() -> None:
    title = "Bitcoin breaks above $78,000 as ETF inflows accelerate"
    assert dedup.hamming(dedup.simhash(title), dedup.simhash(title)) == 0


def test_reworded_reprint_is_near_duplicate() -> None:
    """Перепечатка под слегка изменённым заголовком — та же новость."""
    original = "Bitcoin breaks above $78,000 as ETF inflows accelerate"
    reprint = "Bitcoin breaks above $78,000 after ETF inflows accelerate"
    assert dedup.is_near_duplicate(dedup.simhash(original), dedup.simhash(reprint))


def test_unrelated_titles_are_not_duplicates() -> None:
    first = dedup.simhash("Bitcoin breaks above $78,000 as ETF inflows accelerate")
    second = dedup.simhash("Fed holds rates steady, signals caution on inflation path")
    assert not dedup.is_near_duplicate(first, second)


def test_stopwords_alone_do_not_make_titles_similar() -> None:
    """Служебные слова есть в каждом заголовке и не должны сближать новости."""
    first = dedup.simhash("The Fed is on the path of a rate decision")
    second = dedup.simhash("The Ethereum upgrade is on the way for a merge")
    assert not dedup.is_near_duplicate(first, second)


def test_empty_title_does_not_crash() -> None:
    assert dedup.simhash("") == 0
    assert dedup.simhash("   ") == 0


def test_simhash_is_case_insensitive() -> None:
    assert dedup.simhash("Bitcoin Rally") == dedup.simhash("bitcoin rally")


# --------------------------------------------------------------------------- #
# Укладка в SQLite
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [0, 1, 2**62, 2**63 - 1, 2**63, 2**64 - 1],
)
def test_signed_64_roundtrip(value: int) -> None:
    """SQLite хранит INTEGER знаковым — беззнаковый хеш иначе переполняется."""
    packed = dedup.to_signed_64(value)
    assert -(2**63) <= packed < 2**63
    assert dedup.from_signed_64(packed) == value


def test_hamming_survives_signed_roundtrip() -> None:
    first = dedup.simhash("Bitcoin breaks above $78,000 as ETF inflows accelerate")
    second = dedup.simhash("Bitcoin breaks above $78,000 after ETF inflows accelerate")

    restored_first = dedup.from_signed_64(dedup.to_signed_64(first))
    restored_second = dedup.from_signed_64(dedup.to_signed_64(second))

    assert dedup.hamming(restored_first, restored_second) == dedup.hamming(first, second)
