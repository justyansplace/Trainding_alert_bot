"""Единая точка вызова модели поверх двух провайдеров.

Остальной код не должен знать, чей SDK стоит за вызовом: ему нужны разбор по
схеме и короткий текст. Здесь же нормализуется учёт токенов — у провайдеров он
называется по-разному, а считать расход надо одинаково.

Про параметры конкретных провайдеров. У обоих есть настройка глубины
рассуждения, и она несовместима по имени и по допустимым моделям: у Anthropic
это output_config.effort, который Haiku 4.5 вовсе не принимает, у OpenAI —
reasoning_effort, работающий только на семействе gpt-5. Разметка новостей по
схеме в рассуждении не нуждается, поэтому там оно выключается — на gpt-5 без
этого модель тратит выходные токены на размышления, а платим мы именно за них.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from alert_bot.config import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

_openai_client: Any = None
_anthropic_client: Any = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Расход одного вызова, приведённый к общему виду."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


class LlmError(Exception):
    pass


class LlmRefusal(LlmError):
    """Модель отказалась отвечать."""


class LlmUnavailable(LlmError):
    """Доступ к модели закрыт: нет средств, ключ отозван, нет прав.

    Отличается от обычного сбоя тем, что следующая попытка ничего не изменит.
    Провайдер отдаёт нехватку средств как 429, а SDK считает 429 временным и
    ретраит — в итоге каждый проход по новостям превращается в десятки заведомо
    безнадёжных запросов и стену в логе. Такое состояние обязано останавливать
    весь проход, а не отдельный пакет.
    """


_TERMINAL_MARKERS = (
    "insufficient_quota",
    "invalid_api_key",
    "account_deactivated",
    "billing",
)


def _classify(exc: Exception) -> Exception:
    """Отделяет безнадёжные ошибки доступа от временных сбоев."""
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)

    if any(marker in text for marker in _TERMINAL_MARKERS) or status in (401, 403):
        return LlmUnavailable(str(exc))
    return exc


# --------------------------------------------------------------------------- #
# Клиенты
# --------------------------------------------------------------------------- #


def has_llm() -> bool:
    """Настроен ли доступ к модели для выбранного провайдера.

    Бот обязан работать и без ключа: алерты по уровням — это арифметика, и
    отсутствие сводки к ним не повод молчать. Проверять надо заранее, а не
    ловить исключение на каждом цикле.
    """
    settings = get_settings()
    if settings.llm_provider == PROVIDER_OPENAI:
        return bool(settings.openai_api_key)
    return bool(settings.anthropic_api_key)


def _openai():  # noqa: ANN202
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        settings = get_settings()
        if not settings.openai_api_key:
            raise LlmError("OPENAI_API_KEY не задан")
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=3)
    return _openai_client


def _anthropic():  # noqa: ANN202
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic

        settings = get_settings()
        if not settings.anthropic_api_key:
            raise LlmError("ANTHROPIC_API_KEY не задан")
        _anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)
    return _anthropic_client


def reset_clients() -> None:
    """Сброс кэша клиентов — нужен тестам и смене ключа на лету."""
    global _openai_client, _anthropic_client
    _openai_client = _anthropic_client = None


# --------------------------------------------------------------------------- #
# Особенности провайдеров
# --------------------------------------------------------------------------- #

# Anthropic: output_config.effort. Haiku 4.5 его не принимает и отвечает ошибкой.
_ANTHROPIC_EFFORT_MODELS = ("claude-opus-5", "claude-opus-4", "claude-sonnet-5", "claude-fable-5")

# OpenAI: reasoning_effort понимает только семейство gpt-5.
_OPENAI_REASONING_MODELS = ("gpt-5",)


def anthropic_effort(model: str, effort: str) -> dict:
    return (
        {"output_config": {"effort": effort}}
        if model.startswith(_ANTHROPIC_EFFORT_MODELS)
        else {}
    )


def openai_reasoning(model: str, effort: str) -> dict:
    return (
        {"reasoning_effort": effort} if model.startswith(_OPENAI_REASONING_MODELS) else {}
    )


# --------------------------------------------------------------------------- #
# Вызовы
# --------------------------------------------------------------------------- #


async def parse_structured(
    model: str,
    system: str,
    user: str,
    schema: type[T],
    max_tokens: int = 3000,
) -> tuple[T | None, Usage]:
    """Ответ, разобранный по Pydantic-схеме. None, если модель ничего не дала."""
    provider = get_settings().llm_provider

    if provider == PROVIDER_OPENAI:
        try:
            response = await _openai().chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
                max_completion_tokens=max_tokens,
                **openai_reasoning(model, "minimal"),
            )
        except Exception as exc:  # noqa: BLE001 — классифицируем и пробрасываем
            raise _classify(exc) from exc
        choice = response.choices[0]
        if choice.message.refusal:
            raise LlmRefusal(choice.message.refusal)
        return choice.message.parsed, _openai_usage(response.usage)

    try:
        response = await _anthropic().messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except Exception as exc:  # noqa: BLE001 — классифицируем и пробрасываем
        raise _classify(exc) from exc
    return response.parsed_output, _anthropic_usage(response.usage)


async def generate_text(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 700,
    effort: str = "low",
) -> tuple[str, Usage]:
    """Короткий текст без структуры. Пустая строка, если модель промолчала."""
    provider = get_settings().llm_provider

    if provider == PROVIDER_OPENAI:
        try:
            response = await _openai().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_completion_tokens=max_tokens,
                **openai_reasoning(model, effort),
            )
        except Exception as exc:  # noqa: BLE001 — классифицируем и пробрасываем
            raise _classify(exc) from exc
        choice = response.choices[0]
        if choice.message.refusal:
            raise LlmRefusal(choice.message.refusal)
        return (choice.message.content or "").strip(), _openai_usage(response.usage)

    try:
        response = await _anthropic().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            **anthropic_effort(model, effort),
        )
    except Exception as exc:  # noqa: BLE001 — классифицируем и пробрасываем
        raise _classify(exc) from exc
    if getattr(response, "stop_reason", None) == "refusal":
        raise LlmRefusal(str(getattr(response, "stop_details", "отказ")))

    text = next((b.text for b in response.content if b.type == "text"), "")
    return text.strip(), _anthropic_usage(response.usage)


# --------------------------------------------------------------------------- #
# Нормализация учёта токенов
# --------------------------------------------------------------------------- #


def _openai_usage(usage: Any) -> Usage:
    """У OpenAI кэш включается сам, отдельного счётчика записи в него нет."""
    if usage is None:
        return Usage()
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    return Usage(
        # Кэшированные приходят внутри prompt_tokens — вычитаем, чтобы не
        # оплатить их дважды: по полной ставке и по льготной.
        input_tokens=max(prompt - cached, 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        cached_tokens=cached,
    )


def _anthropic_usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cached_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )
