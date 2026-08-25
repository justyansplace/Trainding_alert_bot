"""Извлечение смысла из новостей моделью.

Здесь единственное место, где LLM обрабатывает поток материалов, и всё построено
вокруг того, чтобы платить как можно меньше:

  * пакет из нескольких материалов за один запрос — системный промпт
    оплачивается один раз на пакет, а не на каждую статью;
  * системный промпт стабилен побайтово и кэшируется; пересобирается только при
    изменении набора инструментов, символы в нём отсортированы, никаких дат и
    счётчиков внутри;
  * одна статья разбирается ровно один раз и раздаётся во все релевантные
    контексты через relevant_symbols — поэтому добавление инструмента почти не
    увеличивает расход на извлечение;
  * дешёвая модель: разметка текста по схеме не требует рассуждения, и на этом
    шаге разница между моделями почти не видна, а цена отличается втрое.

Параметр effort здесь не передаётся намеренно: Haiku его не принимает, а рядом
с output_format он ещё и рискует затереть схему, которую SDK подставляет сам.
"""

from __future__ import annotations

import logging
import re

from alert_bot.config import get_settings
from alert_bot.db.models import Article, Extraction, Instrument, utcnow
from alert_bot.db.session import session_scope
from alert_bot.llm.client import BudgetExceeded, ensure_budget, record_usage
from alert_bot.llm.complete import LlmUnavailable, has_llm, parse_structured
from alert_bot.llm.schemas import InsightBatch
from alert_bot.news.ingest import MACRO_SYMBOL
from alert_bot.news.registry import SourceError  # noqa: F401 — реэкспорт для симметрии

log = logging.getLogger(__name__)

BATCH_SIZE = 12
MAX_EXCERPT_CHARS = 400

SYSTEM_PREAMBLE = """Ты разбираешь финансовые новости для трейдингового бота.

По каждому материалу верни структурированный разбор. Правила:

1. relevant_symbols — только те символы из списка ниже, которых материал
   действительно касается. Если он про рынок в целом, макроэкономику,
   регулирование или решения центробанков — добавь MACRO. Пустой список, если
   материал не относится ни к чему из списка.
2. sentiment — тон в отношении цены актива, а не эмоциональность текста.
   Шкала непрерывная, пользуйся всем диапазоном: ±0.2 — лёгкий уклон, ±0.5 —
   отчётливый, ±0.8 — сильный. Значения ±1.0 оставь для однозначных событий
   вроде краха биржи или одобрения спотового ETF. Обычная новость почти
   никогда не заслуживает крайнего значения.
3. impact — насколько это способно сдвинуть цену. Пересказ прошлых движений и
   ценовые прогнозы блогеров — это 0. Регуляторные решения, взломы, запуски ETF,
   макростатистика — 2 или 3.
4. mentioned_levels — только те числа, которые названы в самом материале.
   Ничего не вычисляй и не оценивай сам: эти уровни идут пользователю как цитата.
5. thesis — по-русски, одно-два предложения, без вводных оборотов.

Отслеживаемые инструменты:
"""


def build_system_prompt(instruments: list[Instrument]) -> str:
    """Системный промпт с перечнем инструментов.

    Порядок символов детерминирован, внутри нет ни дат, ни счётчиков — иначе
    префикс менялся бы на каждом запросе и кэш не работал бы никогда.
    """
    lines = []
    for instrument in sorted(instruments, key=lambda i: i.symbol):
        keywords = ", ".join(sorted(instrument.keywords or [])) or "—"
        lines.append(f"- {instrument.symbol} (ключевые слова: {keywords})")
    lines.append(f"- {MACRO_SYMBOL} — рынок в целом, макро, регулирование")
    return SYSTEM_PREAMBLE + "\n".join(lines)


_NUMBER_RE = re.compile(r"\d[\d\s,.]*")


def verify_levels(levels: list[float], text: str) -> list[float]:
    """Оставляет только числа, которые действительно встречаются в материале.

    Уровни из этого поля показываются пользователю как процитированные
    аналитиками, поэтому выдуманное число здесь опаснее отсутствующего.
    На коротких заголовках модель регулярно промахивается — на живых данных
    примерно каждое восьмое число оказалось не из текста, включая «77» вместо
    77000. Проверка дешёвая и детерминированная, так что делает её код.
    """
    if not levels:
        return []

    haystack = text.lower()
    found = {
        match.group().replace(",", "").replace(" ", "").rstrip(".")
        for match in _NUMBER_RE.finditer(haystack)
    }

    kept: list[float] = []
    for level in levels:
        candidates = {f"{level:.0f}", f"{level:g}"}
        if level >= 1000 and level % 1000 == 0:
            # В заголовках круглые суммы часто пишут как "78k".
            candidates.add(f"{int(level / 1000)}k")
        if any(c in found for c in candidates) or any(c in haystack for c in candidates):
            kept.append(level)

    return kept


def render_batch(articles: list[Article]) -> str:
    blocks = []
    for number, article in enumerate(articles, start=1):
        excerpt = (article.excerpt or "")[:MAX_EXCERPT_CHARS]
        blocks.append(
            f"--- Материал {number} ---\n"
            f"Заголовок: {article.title}\n"
            f"Фрагмент: {excerpt or '(нет)'}"
        )
    return "\n\n".join(blocks)


async def extract_batch(
    articles: list[Article], instruments: list[Instrument]
) -> list[tuple[Article, object]]:
    """Разбирает пакет материалов. Возвращает пары (статья, разбор)."""
    if not articles:
        return []

    settings = get_settings()
    await ensure_budget("extraction")

    parsed, usage = await parse_structured(
        model=settings.extraction_model,
        system=build_system_prompt(instruments),
        user=render_batch(articles),
        schema=InsightBatch,
        max_tokens=3000,
    )

    await record_usage(settings.extraction_model, "extraction", usage)

    if parsed is None:
        log.warning("Модель не вернула разбор для пакета из %s материалов", len(articles))
        return []

    valid_symbols = {i.symbol for i in instruments} | {MACRO_SYMBOL}
    paired: list[tuple[Article, object]] = []

    for insight in parsed.insights:
        index = insight.article_index - 1
        if not 0 <= index < len(articles):
            log.warning("Модель прислала article_index=%s вне пакета", insight.article_index)
            continue
        # Символы вне списка отслеживаемых отбрасываем: контекст строится по
        # ним join'ом, и выдуманный символ просто никуда не попадёт.
        insight.relevant_symbols = [s for s in insight.relevant_symbols if s in valid_symbols]

        article = articles[index]
        insight.mentioned_levels = verify_levels(
            insight.mentioned_levels, f"{article.title} {article.excerpt or ''}"
        )
        paired.append((article, insight))

    return paired


async def store_insights(paired: list[tuple[Article, object]], model: str) -> int:
    if not paired:
        return 0

    async with session_scope() as session:
        for article, insight in paired:
            session.add(
                Extraction(
                    url_hash=article.url_hash,
                    sentiment=insight.sentiment,
                    impact=insight.impact,
                    horizon=insight.horizon,
                    thesis=insight.thesis,
                    relevant_symbols=insight.relevant_symbols,
                    mentioned_levels=insight.mentioned_levels,
                    topics=insight.topics,
                    model=model,
                    created_at=utcnow(),
                )
            )
    return len(paired)


async def process_pending(
    articles: list[Article], instruments: list[Instrument]
) -> tuple[int, str | None]:
    """Разбирает накопленные материалы пакетами.

    Возвращает (сколько разобрано, причина остановки). Исчерпание бюджета — не
    ошибка: алерты продолжают уходить без сводки, а извлечение возобновится,
    когда суточное окно сдвинется.
    """
    if not articles or not instruments:
        return 0, None

    settings = get_settings()

    if not has_llm():
        # Новости продолжают собираться и накапливаться — разбор возобновится,
        # как только появится ключ. Одна строка в лог, а не стектрейс на каждый
        # пакет каждые десять минут.
        key = "OPENAI_API_KEY" if settings.llm_provider == "openai" else "ANTHROPIC_API_KEY"
        return 0, f"{key} не задан — разбор новостей пропущен"
    processed = 0

    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start : start + BATCH_SIZE]
        try:
            paired = await extract_batch(batch, instruments)
        except BudgetExceeded as exc:
            log.warning("Извлечение остановлено: %s", exc)
            return processed, str(exc)
        except LlmUnavailable as exc:
            # Следующая попытка ничего не изменит — прекращаем весь проход,
            # иначе каждый цикл превращается в десятки безнадёжных запросов.
            return processed, f"доступ к модели закрыт: {str(exc)[:160]}"
        except Exception:  # noqa: BLE001 — один сбойный пакет не должен останавливать проход
            log.exception("Пакет из %s материалов не разобран", len(batch))
            continue

        await store_insights(paired, settings.extraction_model)
        # Материалы помечаются обработанными целым пакетом, включая те, по
        # которым модель ничего не вернула: иначе они будут возвращаться в
        # каждый следующий проход и оплачиваться заново.
        from alert_bot.news.ingest import mark_processed

        await mark_processed([a.url_hash for a in batch])
        processed += len(paired)

    return processed, None
