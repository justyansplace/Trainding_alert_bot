"""Сводка от аналитиков для алерта.

Модель работает только с текстом новостей. Все числа — цена, уровень, ATR,
score — посчитаны кодом и передаются ей как факты, менять которые запрещено.
Это не стилистическое требование: LLM, которой позволено «уточнить» уровень,
рано или поздно назовёт цену, которой на графике нет, и алерт станет опаснее
своего отсутствия.
"""

from __future__ import annotations

import logging

from alert_bot.config import get_settings
from alert_bot.db.models import Instrument
from alert_bot.llm.client import BudgetExceeded, ensure_budget, record_usage
from alert_bot.llm.complete import LlmRefusal, LlmUnavailable, generate_text, has_llm
from alert_bot.market.detector import LevelEvent
from alert_bot.market.levels import HUMAN_KIND
from alert_bot.news.context import MarketContext, render_for_prompt

log = logging.getLogger(__name__)

BRIEF_SYSTEM = """Ты пишешь короткую сводку для трейдингового алерта.

Тебе дают: техническое событие с уже посчитанными числами и выжимку из
публикаций аналитиков за последние часы.

Жёсткие правила:

1. Никаких чисел, которых нет во входных данных. Не вычисляй, не округляй, не
   пересчитывай, не оценивай «примерно». Если числа нет — не упоминай его.
2. Не давай торговых указаний. Не пиши «покупай», «продавай», «ставь стоп»,
   «заходи». Формулируй как наблюдение: что произошло и что об этом пишут.
3. Не выдумывай события, источники и мнения. Опирайся только на переданные
   тезисы. Если материалов мало или они не по теме — так и скажи одной строкой.
4. Не пересказывай техническую часть: цену и уровень пользователь уже видит
   выше в сообщении. Твоя часть — только новостной контекст.
5. По-русски, не более трёх предложений. Без вводных оборотов, без «стоит
   отметить», без обращений к читателю.

Начинай сразу с содержания."""

NO_CONTEXT_TEXT = "📰 Свежих материалов по инструменту за последние 12 часов нет."


def render_payload(
    instrument: Instrument, event: LevelEvent, context: MarketContext
) -> str:
    """Что видит модель. Числа уже посчитаны и подаются как данность."""
    precision = instrument.price_precision
    kinds = " + ".join(HUMAN_KIND.get(k, k) for k in event.level_kinds)
    action = "пробила" if event.kind == "breakout" else "приближается к"

    return (
        f"ТЕХНИЧЕСКОЕ СОБЫТИЕ (числа посчитаны, менять запрещено)\n"
        f"Инструмент: {instrument.symbol}\n"
        f"Текущая цена: {event.price:.{precision}f}\n"
        f"Цена {action} уровню: {event.level_price:.{precision}f}\n"
        f"Расстояние: {event.distance_atr:.2f} ATR\n"
        f"Состав уровня: {kinds}\n"
        f"Оценка значимости уровня: {event.level_score:.1f}\n\n"
        f"ЧТО ПИШУТ АНАЛИТИКИ\n"
        f"{render_for_prompt(context)}"
    )


def format_context_header(context: MarketContext) -> str:
    return (
        f"📰 Аналитики ({context.article_count} материалов за 12ч): "
        f"тон {context.sentiment:+.2f}, {context.sentiment_label()}"
    )


async def make_brief(
    instrument: Instrument, event: LevelEvent, context: MarketContext
) -> str | None:
    """Текст сводки. None означает «алерт уходит без неё» — это не ошибка.

    Событие на графике важнее сводки о нём: если модель недоступна или дневной
    бюджет исчерпан, алерт всё равно должен дойти вовремя.
    """
    if context.is_empty:
        return NO_CONTEXT_TEXT

    settings = get_settings()

    try:
        await ensure_budget("brief")
        text, usage = await generate_text(
            model=settings.brief_model,
            system=BRIEF_SYSTEM,
            user=render_payload(instrument, event, context),
            max_tokens=700,
            effort="low",
        )
        await record_usage(settings.brief_model, "brief", usage)

        if not text:
            return format_context_header(context)

        return f"{format_context_header(context)}\n{text}"

    except LlmUnavailable as exc:
        log.warning("Сводка пропущена, доступ к модели закрыт: %s", exc)
        return format_context_header(context)
    except LlmRefusal as exc:
        log.warning("Модель отказалась писать сводку: %s", exc)
        return format_context_header(context)
    except BudgetExceeded as exc:
        log.warning("Сводка пропущена: %s", exc)
        return (
            f"{format_context_header(context)}\n"
            "<i>Сводка недоступна: исчерпан дневной лимит на модель.</i>"
        )
    except Exception:  # noqa: BLE001 — алерт важнее сводки о нём
        log.exception("Не удалось получить сводку для %s", instrument.symbol)
        return format_context_header(context)
