"""Схемы структурированного вывода модели.

Через них проходит всё, что модель возвращает: SDK валидирует ответ по схеме и
переспрашивает при несовпадении, поэтому разбирать текст руками не приходится.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ArticleInsight(BaseModel):
    """Разбор одной публикации."""

    article_index: int = Field(description="Номер материала из входного списка, с 1")
    relevant_symbols: list[str] = Field(
        default_factory=list,
        description="Символы из списка отслеживаемых, которых материал реально касается; "
        "MACRO — если он про рынок в целом",
    )
    sentiment: float = Field(
        ge=-1.0, le=1.0, description="Тон в отношении актива: -1 негатив, 0 нейтрально, +1 позитив"
    )
    impact: int = Field(
        ge=0, le=3, description="Значимость для цены: 0 шум, 1 слабая, 2 заметная, 3 сильная"
    )
    horizon: Literal["intraday", "days", "weeks"]
    thesis: str = Field(description="Суть в одном-двух предложениях, по-русски")
    mentioned_levels: list[float] = Field(
        default_factory=list,
        description="Ценовые уровни, названные в самом материале. Не вычислять и не додумывать",
    )
    topics: list[str] = Field(default_factory=list, description="1-3 темы одним словом")


class InsightBatch(BaseModel):
    insights: list[ArticleInsight]


class KeywordSet(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=12)
