"""Инструменты заводит любой, а роль администратора живёт в БД.

Реестр общий: одна строка на символ и площадку, один опрос на всех. Поэтому
добавление безопасно отдать всем, а отключение — нет: оно бьёт по чужим
подпискам и чужим уровням.
"""

from __future__ import annotations

import pytest

from alert_bot import config
from alert_bot.bot import instruments
from alert_bot.bot.access import RedeemError, grant_admin, list_admins, revoke_admin
from alert_bot.db.models import Instrument, Role, User, utcnow
from alert_bot.db.session import init_db, session_scope
from alert_bot.market import registry
from alert_bot.market.providers.base import SymbolMeta


async def make_user(tg_id: int, role: str = Role.USER.value) -> User:
    async with session_scope() as session:
        user = User(tg_id=tg_id, role=role, granted_at=utcnow(), active=True)
        session.add(user)
        return user


async def add_instrument(symbol: str, added_by: int, exchange: str = "bybit") -> Instrument:
    return await registry.add_instrument(
        meta=SymbolMeta(symbol=symbol, last_price=100.0, price_precision=2),
        exchange=exchange,
        keywords=["x"],
        added_by=added_by,
    )


# --------------------------------------------------------------------------- #
# Квота на добавление
# --------------------------------------------------------------------------- #


async def test_user_may_add_up_to_the_personal_cap(db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_INSTRUMENTS_PER_USER", "2")
    config._settings = None

    user = await make_user(1)
    assert await instruments.quota_left(user) == 2

    await add_instrument("AAA/USDT", added_by=1)
    assert await instruments.quota_left(user) == 1

    await add_instrument("BBB/USDT", added_by=1)
    assert await instruments.quota_left(user) == 0


async def test_cap_is_personal_not_shared(db, monkeypatch) -> None:
    """Иначе первый пришедший занимает все слоты, и следующему добавить нечего."""
    monkeypatch.setenv("MAX_INSTRUMENTS_PER_USER", "2")
    config._settings = None

    first, second = await make_user(1), await make_user(2)
    await add_instrument("AAA/USDT", added_by=1)
    await add_instrument("BBB/USDT", added_by=1)

    assert await instruments.quota_left(first) == 0
    assert await instruments.quota_left(second) == 2


async def test_admin_has_no_cap(db) -> None:
    admin = await make_user(9, role=Role.ADMIN.value)
    assert await instruments.quota_left(admin) is None


async def test_disabled_instruments_free_the_slot(db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_INSTRUMENTS_PER_USER", "1")
    config._settings = None

    user = await make_user(1)
    await add_instrument("AAA/USDT", added_by=1)
    assert await instruments.quota_left(user) == 0

    await registry.disable_instrument("AAA/USDT", "bybit")
    assert await instruments.quota_left(user) == 1


# --------------------------------------------------------------------------- #
# Уже существующий инструмент
# --------------------------------------------------------------------------- #


async def test_existing_instrument_subscribes_instead_of_duplicating(db) -> None:
    await make_user(1)
    user = await make_user(2)
    existing = await add_instrument("BTC/USDT", added_by=1)

    adopted = await instruments.adopt_existing(user, "BTC/USDT", "bybit")

    assert adopted is not None and adopted.id == existing.id
    subs = await registry.list_subscriptions(2)
    assert subs[existing.id].enabled is True
    assert len(await registry.list_instruments()) == 1


async def test_adopting_does_not_spend_the_personal_cap(db, monkeypatch) -> None:
    """Подписка на чужой инструмент слот не занимает — новой строки не появилось."""
    monkeypatch.setenv("MAX_INSTRUMENTS_PER_USER", "1")
    config._settings = None

    await make_user(1)
    user = await make_user(2)
    await add_instrument("BTC/USDT", added_by=1)

    await instruments.adopt_existing(user, "BTC/USDT", "bybit")
    assert await instruments.quota_left(user) == 1


async def test_disabled_instrument_is_not_adopted(db) -> None:
    """Отключённый инструмент проходит обычный путь с превью и реактивацией."""
    user = await make_user(1)
    await add_instrument("BTC/USDT", added_by=1)
    await registry.disable_instrument("BTC/USDT", "bybit")

    assert await instruments.adopt_existing(user, "BTC/USDT", "bybit") is None


# --------------------------------------------------------------------------- #
# Кто что может
# --------------------------------------------------------------------------- #


def test_adding_is_open_and_removing_is_not() -> None:
    def commands(router) -> set[str]:  # noqa: ANN001
        found = set()
        for handler in router.message.handlers:
            for f in handler.filters:
                found |= set(getattr(f.callback, "commands", []) or [])
        return found

    assert {"add_instrument", "add_many"} <= commands(instruments.router)
    assert {"rm_instrument", "instruments"} <= commands(instruments.admin_router)
    assert not commands(instruments.router) & {"rm_instrument"}


# --------------------------------------------------------------------------- #
# Второй администратор
# --------------------------------------------------------------------------- #


async def test_role_can_be_granted_and_taken_back(db, admin_id) -> None:
    await make_user(5)

    promoted = await grant_admin(5)
    assert promoted.is_admin
    assert {a.tg_id for a in await list_admins()} == {admin_id, 5}

    await revoke_admin(5, by=admin_id)
    assert {a.tg_id for a in await list_admins()} == {admin_id}


async def test_role_is_not_handed_to_a_stranger(db) -> None:
    """Сначала обычный путь через инвайт — иначе роль уходит по номеру наугад."""
    with pytest.raises(RedeemError):
        await grant_admin(777)


async def test_demoting_keeps_access_to_the_bot(db, admin_id) -> None:
    await make_user(5)
    await grant_admin(5)
    await revoke_admin(5, by=admin_id)

    async with session_scope() as session:
        user = await session.get(User, 5)
    assert user.active is True and not user.is_admin


async def test_admins_cannot_lock_each_other_out(db, admin_id) -> None:
    await make_user(5)
    await grant_admin(5)

    # Владельца из конфига не разжаловать...
    with pytest.raises(RedeemError):
        await revoke_admin(admin_id, by=5)
    # ...и себя тоже: иначе последний администратор запирает себя снаружи.
    with pytest.raises(RedeemError):
        await revoke_admin(5, by=5)


async def test_extra_admins_from_config_are_seeded(tmp_path, monkeypatch) -> None:
    """Второй администратор должен появляться вместе с первым, а не после."""
    from alert_bot.db import session as db_session

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ADMIN_TG_ID", "100")
    monkeypatch.setenv("ADMIN_TG_IDS", "200, 300")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CRYPTOPANIC_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "seed.db"))
    config._settings = None
    db_session._engine = None
    db_session._sessionmaker = None

    await init_db()
    try:
        assert {a.tg_id for a in await list_admins()} == {100, 200, 300}
    finally:
        await db_session.dispose_engine()
        config._settings = None
        db_session._engine = None
        db_session._sessionmaker = None
