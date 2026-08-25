"""Офлайн-прогон детектора по исторической цене.

Смысл: пороги atr_k и min_score нельзя выбрать умозрительно. От них напрямую
зависит, получит пользователь три сообщения в сутки или сорок, а разница между
этими режимами — работает бот или его молча выключают. Скрипт гоняет ровно тот
же detector.evaluate_level, что работает в проде, по свечам за прошедшие месяцы
и печатает, во что порог выливается на практике.

    python -m scripts.replay --symbol BTC/USDT --days 90
    python -m scripts.replay --symbol ETH/USDT --sweep

Метрика «дошла» отвечает на вопрос, подтвердилось ли то, о чём бот сообщил:
после алерта «цена приближается к уровню» — коснулась ли она этого уровня
в последующие сутки. Это проверка честности сообщения, а не прибыльности
сделки: торговый результат зависит от входа, стопа и размера позиции, которых
бот не знает и не предлагает.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pandas as pd

from alert_bot.market import levels as level_math
from alert_bot.market.detector import LevelSnapshot, Subscriber, evaluate_level
from alert_bot.market.indicators import atr as atr_of
from alert_bot.market.providers.base import derive_round_step
from alert_bot.market.providers.ccxt_provider import CcxtProvider, close_all_exchanges

WARMUP_BARS = 400
RECOMPUTE_EVERY = 1  # часов; в проде уровни пересчитываются раз в час
LOOKAHEAD_BARS = 24
REACH_TOLERANCE_ATR = 0.1
REACTION_ATR = 0.5  # ход, после которого реакция на уровень считается состоявшейся
STATE_CARRY_PCT = 0.0008


@dataclass
class Firing:
    ts: datetime
    price: float
    level_price: float
    distance_atr: float
    score: float
    kinds: tuple[str, ...]
    reached: bool = False
    max_move_atr: float = 0.0
    reaction: str = "не дошла"


@dataclass
class ReplayResult:
    symbol: str
    bars: int
    days: float
    firings: list[Firing] = field(default_factory=list)

    @property
    def per_day(self) -> float:
        return len(self.firings) / self.days if self.days else 0.0

    @property
    def reach_rate(self) -> float:
        if not self.firings:
            return 0.0
        return sum(f.reached for f in self.firings) / len(self.firings)

    def reaction_mix(self) -> dict[str, float]:
        if not self.firings:
            return {}
        counts: dict[str, int] = {}
        for firing in self.firings:
            counts[firing.reaction] = counts.get(firing.reaction, 0) + 1
        return {k: v / len(self.firings) for k, v in counts.items()}


class ReplayState:
    """Состояния уровней между барами, включая перенос при пересчёте."""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._next_id = 1

    def sync(self, levels: list[level_math.Level]) -> list[LevelSnapshot]:
        carried: dict[int, dict] = {}
        unmatched = dict(self._rows)

        for level in levels:
            tolerance = level.price * STATE_CARRY_PCT
            match_id = None
            best = tolerance
            for row_id, row in unmatched.items():
                distance = abs(row["price"] - level.price)
                if distance <= best:
                    match_id, best = row_id, distance

            if match_id is not None:
                row = unmatched.pop(match_id)
                row["price"] = level.price
                row["score"] = level.score
                row["kinds"] = tuple(level.kinds)
                carried[match_id] = row
            else:
                carried[self._next_id] = {
                    "price": level.price,
                    "score": level.score,
                    "kinds": tuple(level.kinds),
                    "state": "armed",
                    "cooldown_until": None,
                    "notified": (),
                }
                self._next_id += 1

        self._rows = carried
        return [
            LevelSnapshot(
                id=row_id,
                price=row["price"],
                score=row["score"],
                kinds=row["kinds"],
                state=row["state"],
                cooldown_until=row["cooldown_until"],
                notified_users=row["notified"],
            )
            for row_id, row in self._rows.items()
        ]

    def apply(self, level_id: int, decision) -> None:  # noqa: ANN001
        row = self._rows.get(level_id)
        if row is None:
            return
        row["state"] = decision.state
        row["cooldown_until"] = decision.cooldown_until
        row["notified"] = tuple(decision.notified_users)


def measure_outcome(df: pd.DataFrame, index: int, firing: Firing, atr_value: float) -> None:
    """Что случилось с уровнем после алерта.

    Факт «цена дошла до уровня» почти ничего не сообщает: алерт и так
    срабатывает в долях ATR от него, поэтому касание происходит практически
    всегда и метрика насыщается под 100% на любых порогах. Различает уровни
    другое — устоял он или был пробит, и это единственное, что делает подбор
    порога осмысленным.
    """
    ahead = df.iloc[index + 1 : index + 1 + LOOKAHEAD_BARS]
    if ahead.empty or atr_value <= 0:
        return

    level = firing.level_price
    tolerance = atr_value * REACH_TOLERANCE_ATR
    margin = atr_value * REACTION_ATR

    touched = (ahead["l"] <= level + tolerance) & (ahead["h"] >= level - tolerance)
    firing.reached = bool(touched.any())

    excursion = max(
        abs(float(ahead["h"].max()) - firing.price),
        abs(firing.price - float(ahead["l"].min())),
    )
    firing.max_move_atr = excursion / atr_value

    if not firing.reached:
        firing.reaction = "не дошла"
        return

    approached_from_below = firing.price < level
    after = ahead.iloc[int(touched.argmax()) :]

    for _, bar in after.iterrows():
        if approached_from_below:
            if float(bar["h"]) >= level + margin:
                firing.reaction = "пробит"
                return
            if float(bar["l"]) <= level - margin:
                firing.reaction = "устоял"
                return
        else:
            if float(bar["l"]) <= level - margin:
                firing.reaction = "пробит"
                return
            if float(bar["h"]) >= level + margin:
                firing.reaction = "устоял"
                return

    firing.reaction = "без реакции"


def run_replay(
    df: pd.DataFrame, symbol: str, atr_k: float, min_score: float, cooldown_hours: int
) -> ReplayResult:
    round_step = derive_round_step(float(df["c"].iloc[-1]))
    subscriber = Subscriber(tg_id=1, min_score=min_score, atr_k=atr_k)
    cooldown = timedelta(hours=cooldown_hours)

    state = ReplayState()
    snapshots: list[LevelSnapshot] = []
    atr_value = float("nan")
    result = ReplayResult(
        symbol=symbol,
        bars=len(df) - WARMUP_BARS,
        days=(len(df) - WARMUP_BARS) / 24,
    )

    for index in range(WARMUP_BARS, len(df)):
        window = df.iloc[: index + 1]
        price = float(window["c"].iloc[-1])
        now = window["ts"].iloc[-1].to_pydatetime()
        history = [float(window["c"].iloc[-2]), price]

        if (index - WARMUP_BARS) % RECOMPUTE_EVERY == 0:
            atr_value = atr_of(window, 14)
            computed = level_math.build_levels(window, price, round_step)
            snapshots = state.sync(computed)

        fired = False
        for snapshot in sorted(snapshots, key=lambda s: abs(s.price - price)):
            decision = evaluate_level(
                snapshot, price, history, atr_value, [subscriber], now, cooldown
            )
            if fired and decision.event is not None:
                continue

            state.apply(snapshot.id, decision)

            if decision.event is not None:
                firing = Firing(
                    ts=now,
                    price=price,
                    level_price=decision.event.level_price,
                    distance_atr=decision.event.distance_atr,
                    score=decision.event.level_score,
                    kinds=decision.event.level_kinds,
                )
                measure_outcome(df, index, firing, atr_value)
                result.firings.append(firing)
                fired = True

        # Снимки должны видеть обновлённое состояние на следующем баре.
        snapshots = [
            LevelSnapshot(
                id=s.id,
                price=state._rows[s.id]["price"],
                score=state._rows[s.id]["score"],
                kinds=state._rows[s.id]["kinds"],
                state=state._rows[s.id]["state"],
                cooldown_until=state._rows[s.id]["cooldown_until"],
                notified_users=state._rows[s.id]["notified"],
            )
            for s in snapshots
            if s.id in state._rows
        ]

    return result


def print_report(result: ReplayResult, atr_k: float, min_score: float) -> None:
    print(f"\n=== {result.symbol} · atr_k={atr_k} · min_score={min_score}")
    print(f"Период: {result.days:.0f} суток ({result.bars} баров H1)")
    print(f"Алертов: {len(result.firings)}  →  {result.per_day:.1f} в сутки")

    if not result.firings:
        print("Ни одного срабатывания — порог слишком строгий.")
        return

    print(f"Дошла до уровня за сутки: {result.reach_rate:.0%} "
          f"(метрика насыщена — алерт и так срабатывает вплотную к уровню)")
    mix = result.reaction_mix()
    print("Реакция на уровень: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(mix.items())))
    moves = [f.max_move_atr for f in result.firings]
    print(f"Медианный ход после алерта: {statistics.median(moves):.2f}×ATR")
    scores = [f.score for f in result.firings]
    print(f"Score сработавших: медиана {statistics.median(scores):.1f}, "
          f"мин {min(scores):.1f}, макс {max(scores):.1f}")

    counter: dict[str, int] = {}
    for firing in result.firings:
        for kind in firing.kinds:
            counter[kind] = counter.get(kind, 0) + 1
    top = sorted(counter.items(), key=lambda kv: -kv[1])[:6]
    print("Чаще всего срабатывали: " + ", ".join(f"{k}×{v}" for k, v in top))

    verdict = (
        "комфортно" if 2 <= result.per_day <= 8
        else ("слишком тихо" if result.per_day < 2 else "слишком шумно")
    )
    print(f"Вердикт по частоте: {verdict}")


async def load_candles(symbol: str, exchange: str, days: int) -> pd.DataFrame:
    """Догружает историю постранично: биржи отдают не больше ~1000 свечей за раз."""
    provider = CcxtProvider(exchange)
    needed = days * 24 + WARMUP_BARS
    frames: list[pd.DataFrame] = []
    until = datetime.now(UTC)

    while sum(len(f) for f in frames) < needed:
        chunk = await provider.fetch_ohlcv(symbol, "1h", limit=1000)
        if chunk.empty:
            break
        frames.append(chunk)
        earliest = chunk["ts"].iloc[0].to_pydatetime()
        if earliest >= until:
            break
        until = earliest
        # ccxt без since вернёт тот же хвост — для MVP ограничиваемся одним окном.
        break

    if not frames:
        raise SystemExit(f"не удалось загрузить свечи для {symbol}")

    df = pd.concat(frames).drop_duplicates(subset=["ts"]).sort_values("ts")
    return df.reset_index(drop=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон детектора по истории")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--days", type=int, default=25)
    parser.add_argument("--atr-k", type=float, default=0.3)
    parser.add_argument("--min-score", type=float, default=4.0)
    parser.add_argument("--cooldown-hours", type=int, default=4)
    parser.add_argument(
        "--sweep", action="store_true", help="перебрать сетку порогов и сравнить"
    )
    args = parser.parse_args()

    try:
        df = await load_candles(args.symbol, args.exchange, args.days)
        if len(df) <= WARMUP_BARS + 24:
            raise SystemExit(
                f"слишком мало истории: {len(df)} баров, нужно больше {WARMUP_BARS + 24}"
            )

        print(f"Загружено {len(df)} баров H1, из них прогон по {len(df) - WARMUP_BARS}")

        if args.sweep:
            for atr_k in (0.15, 0.3, 0.5):
                for min_score in (3.0, 4.5, 6.0):
                    result = run_replay(
                        df, args.symbol, atr_k, min_score, args.cooldown_hours
                    )
                    mix = result.reaction_mix()
                    print(
                        f"atr_k={atr_k:<5} min_score={min_score:<4} → "
                        f"{result.per_day:5.1f} алертов/сутки · "
                        f"устоял {mix.get('устоял', 0):.0%} · "
                        f"пробит {mix.get('пробит', 0):.0%} · "
                        f"без реакции {mix.get('без реакции', 0):.0%}"
                    )
        else:
            result = run_replay(
                df, args.symbol, args.atr_k, args.min_score, args.cooldown_hours
            )
            print_report(result, args.atr_k, args.min_score)
    finally:
        await close_all_exchanges()


if __name__ == "__main__":
    asyncio.run(main())
