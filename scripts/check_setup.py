"""Предполётная проверка перед первым запуском.

Проверяет всё, что может отвалиться на старте, по отдельности и с внятным
сообщением — иначе бот падает одной строкой стектрейса, и непонятно, дело в
токене, в сети или в ключе.

    python -m scripts.check_setup
"""

from __future__ import annotations

import asyncio
import sys

OK = "✅"
FAIL = "❌"
WARN = "⚠️ "


results: list[tuple[bool, str]] = []


def report(ok: bool, line: str) -> None:
    results.append((ok, line))
    print(f"{OK if ok else FAIL} {line}")


def warn(line: str) -> None:
    print(f"{WARN} {line}")


async def check_env() -> object | None:
    try:
        from alert_bot.config import get_settings

        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        report(False, f"Конфиг не читается: {exc}")
        print("\n   Создайте .env:  cp .env.example .env")
        print("   и заполните TELEGRAM_BOT_TOKEN, ADMIN_TG_ID, ANTHROPIC_API_KEY")
        return None

    report(True, f"Конфиг прочитан, БД: {settings.db_path}")

    if not settings.telegram_bot_token or ":" not in settings.telegram_bot_token:
        report(False, "TELEGRAM_BOT_TOKEN пустой или не похож на токен")
    if not settings.admin_tg_id:
        report(False, "ADMIN_TG_ID не задан")
    from alert_bot.llm.complete import has_llm

    report(True, f"Провайдер модели: {settings.llm_provider}")
    if not has_llm():
        key = "OPENAI_API_KEY" if settings.llm_provider == "openai" else "ANTHROPIC_API_KEY"
        warn(
            f"{key} пуст — бот запустится, но новости не будут разбираться "
            "и сводок к алертам не будет (сами алерты придут)"
        )
    if not settings.cryptopanic_token:
        warn("CRYPTOPANIC_TOKEN пуст — CryptoPanic не подключится, RSS работают")

    return settings


async def check_db() -> None:
    try:
        from alert_bot.db.session import dispose_engine, init_db

        await init_db()
        await dispose_engine()
        report(True, "БД создаётся и пишется, админ и сид-источники на месте")
    except Exception as exc:  # noqa: BLE001
        report(False, f"БД не инициализируется: {exc}")


async def check_exchange() -> None:
    try:
        from alert_bot.market.providers.base import derive_round_step
        from alert_bot.market.providers.ccxt_provider import (
            CcxtProvider,
            close_all_exchanges,
        )

        provider = CcxtProvider("binance")
        meta = await provider.validate_symbol("BTC/USDT")
        df = await provider.fetch_ohlcv("BTC/USDT", "1h", limit=5)
        await close_all_exchanges()

        report(
            True,
            f"Биржа отвечает: BTC/USDT = {meta.last_price:,.2f}, "
            f"свечей получено {len(df)}, шаг уровней {derive_round_step(meta.last_price)}",
        )
    except Exception as exc:  # noqa: BLE001
        report(False, f"Биржа недоступна: {exc}")
        print("   Если это гео-блокировка Binance — смените биржу при добавлении")
        print("   инструмента: /add_instrument BTC/USDT bybit")


async def check_telegram(settings) -> None:  # noqa: ANN001
    try:
        from alert_bot.bot.main import build_bot

        bot = build_bot()
        me = await bot.get_me()
        await bot.session.close()
        report(True, f"Telegram: бот @{me.username} доступен")
    except Exception as exc:  # noqa: BLE001
        report(False, f"Telegram не отвечает: {exc}")
        print("   Токен берётся у @BotFather командой /newbot")


async def check_news() -> None:
    try:
        import aiohttp

        from alert_bot.news.fetch import fetch_rss

        async with aiohttp.ClientSession() as session:
            result = await fetch_rss(session, "https://cointelegraph.com/rss")

        if result.ok and result.articles:
            report(True, f"Новостные ленты читаются ({len(result.articles)} записей)")
        else:
            report(False, f"Лента не разобралась: {result.error}")
    except Exception as exc:  # noqa: BLE001
        report(False, f"Новости недоступны: {exc}")


async def check_llm(settings) -> None:  # noqa: ANN001
    from alert_bot.llm.client import PRICING, estimate_cost
    from alert_bot.llm.complete import generate_text, has_llm

    for role, model in (("извлечение", settings.extraction_model), ("сводки", settings.brief_model)):
        if model in PRICING:
            price = PRICING[model]
            report(True, f"Модель на {role}: {model} (${price.input_usd}/${price.output_usd} за 1M)")
        else:
            report(False, f"Для модели {model} нет цены в прайсе — расход считаться не будет")

    if not has_llm():
        warn("Проверка вызова модели пропущена — ключа нет")
        return

    try:
        text, usage = await generate_text(
            model=settings.extraction_model,
            system="Отвечай одним словом.",
            user="Скажи: работает",
            max_tokens=200,
        )
        cost = estimate_cost(
            settings.extraction_model, usage.input_tokens, usage.output_tokens, usage.cached_tokens
        )
        report(
            True,
            f"Модель отвечает: {text[:40]!r}, токенов "
            f"{usage.input_tokens}→{usage.output_tokens}, цена вызова ${cost:.6f}",
        )
    except Exception as exc:  # noqa: BLE001
        report(False, f"Модель недоступна: {exc}")
        where = (
            "platform.openai.com/api-keys"
            if settings.llm_provider == "openai"
            else "console.anthropic.com"
        )
        print(f"   Ключ берётся на {where}")


async def main() -> None:
    print("Предполётная проверка\n" + "─" * 60)

    settings = await check_env()
    if settings is None:
        sys.exit(1)

    await check_db()
    await check_exchange()
    await check_telegram(settings)
    await check_news()
    await check_llm(settings)

    print("─" * 60)
    failed = [line for ok, line in results if not ok]
    if failed:
        print(f"{FAIL} Не готово к запуску, проблем: {len(failed)}")
        sys.exit(1)

    _production_warnings()
    print(f"{OK} Всё готово. Запуск:  .venv/bin/python -m alert_bot.main")


def _production_warnings() -> None:
    """Настройки, уместные при отладке и вредные на сервере."""
    from alert_bot.config import get_settings

    settings = get_settings()

    if settings.price_poll_seconds < 15:
        warn(
            f"PRICE_POLL_SECONDS={settings.price_poll_seconds} — это отладочное "
            "значение. На сервере ставьте 30: чаще опрашивать бирж нет смысла, "
            "а лимиты расходуются впустую."
        )
    if settings.llm_provider == "openai" and settings.brief_model.startswith("gpt-5") \
            and settings.daily_llm_budget_usd > 1.0:
        warn(
            f"DAILY_LLM_BUDGET_USD={settings.daily_llm_budget_usd} при ожидаемом "
            "расходе около $0.005/сутки. Потолок можно опустить до 0.05."
        )
    if settings.oanda_environment == "live":
        warn("OANDA_ENVIRONMENT=live — используется боевой счёт, а не демо.")


if __name__ == "__main__":
    asyncio.run(main())
