"""Настройки процесса.

Здесь живут только секреты и системные дефолты. Бизнес-данные — инструменты и
источники новостей — лежат в БД и управляются из бота, а не отсюда.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Секреты ---
    telegram_bot_token: str
    admin_tg_id: int
    cryptopanic_token: str = ""

    # --- OANDA (форекс, металлы, индексы) ---
    oanda_api_token: str = ""
    oanda_environment: Literal["practice", "live"] = "practice"

    # --- Провайдер модели ---
    llm_provider: Literal["openai", "anthropic"] = "openai"
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Хранилище ---
    db_path: Path = Path("data/alert_bot.db")

    # --- Циклы ---
    price_poll_seconds: int = 30
    news_poll_seconds: int = 600

    # --- Предохранители ---
    max_instruments: int = 10
    max_alerts_per_user_per_day: int = 25
    daily_llm_budget_usd: float = 0.25

    # --- Дефолты детектора ---
    default_atr_k: float = 0.3
    default_min_score: float = 4.0
    cooldown_hours: int = 4

    # --- Модели ---
    # Значения по умолчанию — для openai. При llm_provider=anthropic задайте
    # claude-haiku-4-5 и claude-sonnet-5 в .env.
    extraction_model: str = "gpt-5-mini"
    brief_model: str = "gpt-5-mini"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Ленивая инициализация — чтобы импорт модуля не требовал наличия .env."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
        _settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return _settings
