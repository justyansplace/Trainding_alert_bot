"""Схема БД.

SQLite хранит datetime без таймзоны, поэтому все временные колонки идут через
``UtcDateTime``: наружу всегда aware-UTC, внутрь — naive-UTC. Без этого сравнения
"сейчас vs cooldown_until" ломаются молча, а именно на них держится анти-спам.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """Aware-UTC снаружи, naive-UTC в SQLite."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime не принимается: {value!r}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Доступ
# --------------------------------------------------------------------------- #


class Role(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class ThresholdUnit(str, enum.Enum):
    """В чём задан порог срабатывания."""

    ATR = "atr"
    PERCENT = "percent"


class Direction(str, enum.Enum):
    """Какие подходы к уровню интересуют."""

    ANY = "any"
    UP = "up"       # цена идёт вверх, к уровню сверху
    DOWN = "down"   # цена идёт вниз, к уровню снизу


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.USER.value)
    granted_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    invite_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tz: Mapped[str] = mapped_column(String(64), default="UTC")
    quiet_from: Mapped[int | None] = mapped_column(Integer, nullable=True)  # час 0-23
    quiet_to: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Глобальные дефолты юзера; пер-инструментные оверрайды — в Subscription.
    def_min_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    def_atr_k: Mapped[float | None] = mapped_column(Float, nullable=True)

    # В чём мерить расстояние до уровня. ATR подстраивается под волатильность
    # инструмента, процент проще понять — но один и тот же процент для BTC и
    # для валютной пары означает совершенно разную близость.
    def_threshold_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    def_threshold_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Персональные предохранители вместо общих на всех.
    def_cooldown_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_alerts_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Какие подходы интересуют: сверху, снизу или любые.
    direction_filter: Mapped[str | None] = mapped_column(String(8), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    used_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def is_usable(self, now: datetime | None = None) -> bool:
        return self.used_by is None and (now or utcnow()) < self.expires_at


# --------------------------------------------------------------------------- #
# Инструменты и подписки
# --------------------------------------------------------------------------- #


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (Index("ix_instruments_symbol_exchange", "symbol", "exchange", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32), default="ccxt")
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Шаг круглых уровней. Выводится из порядка цены при добавлении: для монеты
    # за $0.40 шаг 500 был бы бессмыслицей.
    round_step: Mapped[float] = mapped_column(Float)
    price_precision: Mapped[int] = mapped_column(Integer, default=2)

    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)

    added_by: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_tick_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ATR(14, H1) с последнего пересчёта. Хранится, а не оценивается на лету:
    # расстояние до уровня в ATR — то, по чему принимается торговое решение,
    # и приблизительное значение здесь хуже, чем отсутствие значения.
    atr: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self) -> str:
        return f"<Instrument {self.symbol}@{self.exchange}>"


class Subscription(Base):
    __tablename__ = "subscriptions"

    tg_id: Mapped[int] = mapped_column(
        ForeignKey("users.tg_id", ondelete="CASCADE"), primary_key=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )

    # NULL = взять дефолт юзера, а тот при NULL — дефолт из конфига.
    min_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_k: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    threshold_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    muted_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- #
# Источники новостей
# --------------------------------------------------------------------------- #


class SourceKind(str, enum.Enum):
    RSS = "rss"
    CRYPTOPANIC = "cryptopanic"


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    poll_interval: Mapped[int] = mapped_column(Integer, default=600)

    # Conditional GET — без этого фиды отдают 429 или банят.
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Источники ломаются молча: фид переезжает, отдаёт 403, меняет формат.
    last_ok_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    added_by: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# Рыночные данные
# --------------------------------------------------------------------------- #


class Candle(Base):
    __tablename__ = "candles"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    tf: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, primary_key=True)

    o: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    l: Mapped[float] = mapped_column(Float)  # noqa: E741
    c: Mapped[float] = mapped_column(Float)
    v: Mapped[float] = mapped_column(Float)


class LevelState(str, enum.Enum):
    ARMED = "armed"
    TRIGGERED = "triggered"


class Level(Base):
    __tablename__ = "levels"
    __table_args__ = (Index("ix_levels_instrument", "instrument_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    price: Mapped[float] = mapped_column(Float)
    kinds: Mapped[list[str]] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float)
    touches: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    state: Mapped[str] = mapped_column(String(16), default=LevelState.ARMED.value)
    cooldown_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Кому уже отправлен алерт за текущий подход к уровню. Состояние уровня общее,
    # а пороги у каждого свои: пользователь с широким atr_k получает алерт раньше,
    # с узким — позже, но оба за один подход получают ровно по одному сообщению.
    # Очищается, когда цена уходит от уровня и он перезаряжается.
    notified_users: Mapped[list[int]] = mapped_column(JSON, default=list)


class UserLevel(Base):
    """Уровень, заданный человеком вручную.

    Отдельная таблица, а не флаг в levels, по двум причинам. Во-первых,
    посчитанные уровни целиком перезаписываются при каждом часовом пересчёте —
    ручной уровень там просто не пережил бы первый час. Во-вторых, он
    принадлежит конкретному пользователю: алерт по нему уходит только автору,
    тогда как посчитанные уровни общие для всех подписчиков инструмента.

    Уровни не удаляются, а архивируются: снятый уровень вместе со статистикой
    срабатываний остаётся историей наблюдений, к которой можно вернуться.
    """

    __tablename__ = "user_levels"
    __table_args__ = (Index("ix_user_levels_owner", "tg_id", "instrument_id", "active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(ForeignKey("users.tg_id", ondelete="CASCADE"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))

    price: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    archive_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Состояние живёт здесь же: ручных уровней немного, отдельная таблица
    # состояний только усложнила бы чтение.
    state: Mapped[str] = mapped_column(String(16), default=LevelState.ARMED.value)
    cooldown_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    notified_users: Mapped[list[int]] = mapped_column(JSON, default=list)

    # История наблюдений по уровню.
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    last_triggered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_distance_atr: Mapped[float | None] = mapped_column(Float, nullable=True)


class UserLevelEvent(Base):
    """Журнал срабатываний ручного уровня.

    Хранится отдельно от alerts и переживает архивацию самого уровня: смысл
    ручного уровня в том, чтобы потом посмотреть, как рынок его отрабатывал.
    """

    __tablename__ = "user_level_events"
    __table_args__ = (Index("ix_user_level_events_level", "user_level_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_level_id: Mapped[int] = mapped_column(
        ForeignKey("user_levels.id", ondelete="CASCADE")
    )
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    level_price: Mapped[float] = mapped_column(Float)
    distance_atr: Mapped[float] = mapped_column(Float)


# --------------------------------------------------------------------------- #
# Новости
# --------------------------------------------------------------------------- #


class Article(Base):
    __tablename__ = "articles"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    simhash: Mapped[int] = mapped_column(Integer)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class Extraction(Base):
    __tablename__ = "extractions"
    __table_args__ = (Index("ix_extractions_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(ForeignKey("articles.url_hash", ondelete="CASCADE"))

    sentiment: Mapped[float] = mapped_column(Float)
    impact: Mapped[int] = mapped_column(Integer)
    horizon: Mapped[str] = mapped_column(String(16))
    thesis: Mapped[str] = mapped_column(Text)

    relevant_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    mentioned_levels: Mapped[list[float]] = mapped_column(JSON, default=list)
    topics: Mapped[list[str]] = mapped_column(JSON, default=list)

    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# Алерты и расход
# --------------------------------------------------------------------------- #


class AlertKind(str, enum.Enum):
    APPROACH = "approach"
    BREAKOUT = "breakout"


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # SET NULL, а не CASCADE: уровни перезаписываются каждый час, и каскад стирал
    # бы всю историю отправленного вместе с ними — вместе с ней и дневной потолок
    # алертов, который по этой истории и считается. Всё нужное (цена уровня,
    # расстояние, инструмент) продублировано в самой строке, поэтому потеря
    # ссылки на пересчитанный уровень ничего не ломает.
    level_id: Mapped[int | None] = mapped_column(
        ForeignKey("levels.id", ondelete="SET NULL"), nullable=True
    )
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))

    kind: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    level_price: Mapped[float] = mapped_column(Float)
    distance_atr: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    brief_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_to: Mapped[list[int]] = mapped_column(JSON, default=list)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
