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

    # Дополнительные администраторы, через запятую. Роль живёт в БД и выдаётся
    # командой /grant_admin — это список для холодного старта, чтобы второй
    # администратор появился вместе с первым, а не после переписки с первым.
    admin_tg_ids: str = ""

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
    # Ограничение не техническое, а про нагрузку на площадки: тик опрашивает
    # каждый инструмент. При TICK_CONCURRENCY=3 и кэше ответов двадцать штук
    # укладываются в лимиты Binance и Yahoo с большим запасом.
    max_instruments: int = 25

    # Сколько инструментов может завести один человек. Общий потолок про
    # нагрузку на площадки, а этот — про справедливость: без него первый
    # пришедший занимает все слоты, и следующему добавить уже нечего.
    # Администраторов не ограничивает: реестр в конечном счёте их забота.
    max_instruments_per_user: int = 8

    # Через сколько часов без единого подписчика инструмент отключается сам.
    # Реестр открыт всем, и без уборки он зарастает тем, что завели попробовать
    # и забыли: опрашивается такой инструмент по-прежнему и занимает слот из
    # общего потолка. Ноль отключает уборку.
    orphan_ttl_hours: int = 24

    max_alerts_per_user_per_day: int = 25
    daily_llm_budget_usd: float = 0.25

    # --- Дефолты детектора ---
    default_atr_k: float = 0.3
    # Порог в процентах. Не эквивалент 0.3xATR: у BTC 0.3xATR это ~0.26%, у
    # валютной пары ~0.026% — на порядок меньше. Процент проще понять, но он
    # не подстраивается под волатильность инструмента.
    default_threshold_pct: float = 0.25
    default_threshold_unit: str = "atr"
    default_min_score: float = 4.0
    cooldown_hours: int = 4

    # Автоочистка уровней. Отметка, от которой цена ушла дальше этого процента,
    # уходит в архив: список не должен зарастать тем, что рынок давно прошёл.
    # Считается от цены уровня — как и порог срабатывания, чтобы «далеко» на
    # обоих концах означало одно и то же.
    #
    # Ноль отключает очистку целиком. Ставить меньше потолка процентного порога
    # (2.5%) нельзя по смыслу: уровень уезжал бы в архив раньше, чем цена
    # успевала подойти к нему на расстояние срабатывания.
    auto_archive_pct: float = 3.0

    # --- Модели ---
    # Значения по умолчанию — для openai. При llm_provider=anthropic задайте
    # claude-haiku-4-5 и claude-sonnet-5 в .env.
    extraction_model: str = "gpt-5-mini"
    brief_model: str = "gpt-5-mini"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def admin_ids(self) -> list[int]:
        """Кому выдать роль администратора при старте. Первый — владелец.

        Владелец отличается от остальных ровно одним: у него нельзя отобрать
        роль из бота. Иначе два администратора могли бы разжаловать друг друга
        и запереть себя снаружи.
        """
        raw = self.admin_tg_ids.replace(";", ",").split(",")
        ids = [self.admin_tg_id]
        ids += [int(p) for p in (x.strip() for x in raw) if p.lstrip("-").isdigit()]
        return list(dict.fromkeys(ids))


_settings: Settings | None = None


def get_settings() -> Settings:
    """Ленивая инициализация — чтобы импорт модуля не требовал наличия .env."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
        _settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return _settings
