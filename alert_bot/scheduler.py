"""Фоновые циклы.

Уровни задают сами пользователи — бот их не вычисляет. На тике идёт только
сравнение цены с уровнями, которые люди поставили сами, поэтому LLM здесь не
вызывается вообще и стоимость тика равна нулю.

Раз в час пересчитывается ATR: от него зависит, за какое расстояние
предупреждать, и он же переводит «далеко» и «близко» в единицы, сопоставимые
между инструментами с разной волатильностью.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from alert_bot import alerts
from alert_bot.bot.notifier import Notifier, Outgoing
from alert_bot.config import get_settings
from alert_bot.health import touch_heartbeat
from alert_bot.db.models import Instrument
from alert_bot.market import registry, store, user_levels
from alert_bot.market.detector import Subscriber, evaluate_level
from alert_bot.market.indicators import atr
from alert_bot.market.providers.base import get_provider
from alert_bot.llm.brief import make_brief
from alert_bot.news import extract, ingest
from alert_bot.news.context import build_context

log = logging.getLogger(__name__)

CANDLE_TF = "1h"
HISTORY_LIMIT = 200  # H1-свечей на расчёт ATR(14) с запасом
TICK_LIMIT = 3
RECOMPUTE_INTERVAL = timedelta(hours=1)  # как часто пересчитывать ATR

# Сколько инструментов обрабатывается одновременно. На холодном старте всем
# нужен пересчёт сразу, и «все разом» означает пачку тяжёлых запросов: сотни
# свечей на инструмент, часть через синхронный yfinance в потоках. Событийный
# цикл при этом голодает, и HTTP-запросы к бирже успевают выйти по таймауту —
# наблюдалось ровно это: первые два тика падали по всем семи инструментам.
TICK_CONCURRENCY = 3


class InstrumentRuntime:
    """Кэш между тиками: ATR и уровни живут дольше одной итерации."""

    __slots__ = ("atr", "last_recompute", "last_price", "price_history", "market_closed")

    def __init__(self) -> None:
        self.atr: float = float("nan")
        self.last_recompute: datetime | None = None
        self.last_price: float | None = None
        self.price_history: list[float] = []
        self.market_closed: bool = False

    def push_price(self, price: float, keep: int = 10) -> None:
        self.price_history.append(price)
        if len(self.price_history) > keep:
            self.price_history.pop(0)
        self.last_price = price


class PriceLoop:
    def __init__(self, notifier: Notifier | None = None) -> None:
        self._runtime: dict[int, InstrumentRuntime] = {}
        self._stopping = asyncio.Event()
        self._notifier = notifier

    def runtime(self, instrument_id: int) -> InstrumentRuntime:
        return self._runtime.setdefault(instrument_id, InstrumentRuntime())

    def _needs_recompute(self, instrument_id: int, now: datetime) -> bool:
        state = self.runtime(instrument_id)
        if state.last_recompute is None:
            return True
        if now - state.last_recompute >= RECOMPUTE_INTERVAL:
            return True
        # Закрытие дневной свечи: пивоты и PDH/PDL меняются именно здесь.
        return state.last_recompute.date() != now.date()

    async def process_instrument(self, instrument: Instrument, now: datetime) -> None:
        state = self.runtime(instrument.id)
        provider = get_provider(instrument.provider, exchange=instrument.exchange)

        if not provider.is_market_open(now):
            # Форекс стоит с вечера пятницы до вечера воскресенья. Дёргать
            # источник бессмысленно, а застывшую цену легко принять за сбой и
            # записать инструменту ошибку, которой нет.
            state.market_closed = True
            return
        if state.market_closed:
            # После выходных история цен протухла: между последними двумя
            # значениями лежит гэп, и сравнивать их как соседние тики нельзя.
            state.price_history.clear()
            state.market_closed = False

        recompute = self._needs_recompute(instrument.id, now)

        df = await provider.fetch_ohlcv(
            instrument.symbol, CANDLE_TF, limit=HISTORY_LIMIT if recompute else TICK_LIMIT
        )
        if df.empty:
            raise RuntimeError("биржа вернула пустой набор свечей")

        await store.upsert_candles(instrument.id, CANDLE_TF, df)

        price = float(df["c"].iloc[-1])
        state.push_price(price)

        if recompute:
            state.atr = atr(df, 14)
            state.last_recompute = now
            log.info(
                "%s: ATR=%.6g, цена=%.6g, уровней у пользователей=%s",
                instrument.symbol,
                state.atr,
                price,
                await user_levels.count_active(instrument.id),
            )

        await store.touch_instrument(
            instrument.id, price, error=None, atr_value=state.atr if recompute else None
        )

        await self.run_user_levels(instrument, state, now)

    async def run_user_levels(
        self, instrument: Instrument, state: InstrumentRuntime, now: datetime
    ) -> None:
        """Ручные уровни — отдельная ветка.

        Они принадлежат конкретному человеку, поэтому получателем может быть
        только автор, и min_score к ним не применяется: фильтровать по
        значимости уровень, который человек поставил сам, бессмысленно. Порог
        расстояния остаётся — это настройка того, за сколько предупреждать.

        Ограничение «один алерт за тик» здесь считается на пользователя, а не
        на инструмент: чужой сработавший уровень не должен глушить ваш.
        """
        if self._notifier is None or state.last_price is None:
            return

        rows = await user_levels.active_levels_for_instrument(instrument.id)
        if not rows:
            return

        settings = get_settings()
        cooldown = timedelta(hours=settings.cooldown_hours)
        thresholds = {s.tg_id: s for s in await alerts.load_subscribers(instrument.id)}
        personal = await alerts.cooldown_by_user(list(thresholds))

        rows.sort(key=lambda r: abs(r.price - state.last_price))
        fired_for: set[int] = set()

        for row in rows:
            owner = thresholds.get(row.tg_id)
            if owner is None:
                continue  # автор отписался от инструмента — молчим, но уровень храним

            # min_score обнуляется: фильтровать по значимости уровень, который
            # человек поставил сам, бессмысленно. Остальные настройки — его.
            subscriber = Subscriber(
                tg_id=owner.tg_id,
                min_score=0.0,
                atr_k=owner.atr_k,
                muted_until=owner.muted_until,
                unit=owner.unit,
                threshold_pct=owner.threshold_pct,
                direction=owner.direction,
            )

            decision = evaluate_level(
                user_levels.to_snapshot(row),
                state.last_price,
                state.price_history,
                state.atr,
                [subscriber],
                now,
                personal.get(row.tg_id, cooldown),
            )

            if decision.event is not None and row.tg_id in fired_for:
                continue

            await user_levels.persist_decision(row.id, decision)

            if decision.event is None:
                continue

            # Объект уровня прочитан до инкремента — синхронизируем счётчик,
            # иначе в тексте алерта окажется предыдущее значение.
            row.trigger_count = await user_levels.record_trigger(
                row.id,
                decision.event.kind,
                decision.event.price,
                decision.event.level_price,
                decision.event.distance_atr,
                now,
            )
            brief = await self._make_brief(instrument, decision.event)
            await alerts.dispatch_event(
                instrument,
                decision.event,
                self._notifier,
                brief=brief,
                now=now,
                user_level=row,
            )
            fired_for.add(row.tg_id)

    async def _make_brief(self, instrument: Instrument, event) -> str | None:  # noqa: ANN001
        """Сводка к алерту. Её отсутствие не должно задерживать сам алерт."""
        try:
            context = await build_context(instrument.symbol)
            return await make_brief(instrument, event, context)
        except Exception:  # noqa: BLE001 — событие на графике важнее сводки о нём
            log.exception("Сводка для %s не получена, шлём алерт без неё", instrument.symbol)
            return None

    async def tick(self) -> None:
        instruments = await registry.list_instruments(enabled_only=True)
        if not instruments:
            return

        now = datetime.now(tz=UTC)

        async def guarded(instrument: Instrument) -> None:
            try:
                await self.process_instrument(instrument, now)
            except Exception as exc:  # noqa: BLE001 — падение одного не валит цикл
                log.warning("%s: тик не удался: %s", instrument.symbol, exc)
                await store.touch_instrument(instrument.id, None, error=str(exc)[:300])

        # Ограничение общее на тик, а не на провайдера: тяжесть создаёт сумма
        # всех источников, а не каждый по отдельности.
        semaphore = asyncio.Semaphore(TICK_CONCURRENCY)

        async def bounded(instrument: Instrument) -> None:
            async with semaphore:
                await guarded(instrument)

        await asyncio.gather(*(bounded(i) for i in instruments))

    async def run(self) -> None:
        settings = get_settings()
        log.info("Цикл цены запущен, интервал %s c", settings.price_poll_seconds)

        # Пульс ставится до первого тика, а не после. Холодный старт с
        # несколькими инструментами занимает десятки секунд — за это время
        # проверка живости успевала бы решить, что цикл мёртв, и платформа
        # перезапускала бы контейнер по кругу.
        touch_heartbeat()

        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — цикл обязан пережить любую ошибку
                log.exception("Ошибка в цикле цены")
            # Пульс ставится и после сбоя: цикл жив и продолжит работу, а вот
            # если он застрянет внутри tick(), отметка перестанет обновляться —
            # ровно это healthcheck и должен поймать.
            touch_heartbeat()
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=settings.price_poll_seconds
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()


class NewsLoop:
    """Новостной контур: опрос источников и разбор моделью.

    Идёт отдельно от цикла цены и на порядок реже — ленты обновляются раз в
    десятки минут, и опрашивать их каждые 30 секунд значит платить трафиком
    и лимитами за одни и те же записи.
    """

    def __init__(self, admin_notifier: Notifier | None = None) -> None:
        self._stopping = asyncio.Event()
        self._notifier = admin_notifier

    async def tick(self) -> None:
        instruments = await registry.list_instruments(enabled_only=True)
        if not instruments:
            return

        stats = await ingest.ingest_once(instruments)
        if stats.fetched or stats.stored:
            log.info("Новости: %s", stats.summary())

        await self._report_disabled_sources(stats)

        pending = await ingest.pending_articles()
        if not pending:
            return

        processed, halted = await extract.process_pending(pending, instruments)
        if processed:
            log.info("Разобрано материалов: %s", processed)
        if halted:
            log.warning("Разбор остановлен: %s", halted)

    async def _report_disabled_sources(self, stats: ingest.IngestStats) -> None:
        """Отключённый источник — то, о чём админ должен узнать сразу."""
        if not stats.disabled_sources or self._notifier is None:
            return

        settings = get_settings()
        names = ", ".join(stats.disabled_sources)
        self._notifier.enqueue(
            Outgoing(
                chat_id=settings.admin_tg_id,
                text=(
                    f"⚠️ Источники отключены после серии ошибок: <b>{names}</b>\n\n"
                    "Подробности и повторное включение: /sources"
                ),
            )
        )

    async def run(self) -> None:
        settings = get_settings()
        log.info("Новостной цикл запущен, интервал %s c", settings.news_poll_seconds)
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — цикл обязан пережить любую ошибку
                log.exception("Ошибка в новостном цикле")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=settings.news_poll_seconds
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()
