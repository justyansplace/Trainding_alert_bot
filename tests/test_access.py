"""Тесты доступа: инвайт-коды и отзыв."""

from __future__ import annotations

from datetime import timedelta

import pytest

from alert_bot.bot.access import (
    RedeemError,
    create_invite,
    list_invites,
    redeem_invite,
    revoke_user,
)
from sqlalchemy import select

from alert_bot.db.models import Invite, Role, User, utcnow
from alert_bot.db.session import session_scope


async def test_invite_single_use(db, admin_id: int) -> None:
    invite = await create_invite(admin_id)

    user = await redeem_invite(invite.code, 1001, "alice")
    assert user.role == Role.USER.value
    assert user.active

    with pytest.raises(RedeemError, match="уже использован"):
        await redeem_invite(invite.code, 1002, "bob")


async def test_invite_rejects_unknown_code(db, admin_id: int) -> None:
    with pytest.raises(RedeemError, match="не существует"):
        await redeem_invite("totally-made-up", 1003, "carol")


async def test_expired_invite_rejected(db, admin_id: int) -> None:
    invite = await create_invite(admin_id)

    async with session_scope() as session:
        stored = await session.get(Invite, invite.code)
        stored.expires_at = utcnow() - timedelta(seconds=1)

    with pytest.raises(RedeemError, match="истёк"):
        await redeem_invite(invite.code, 1004, "dave")


async def test_expired_invite_not_listed_as_active(db, admin_id: int) -> None:
    fresh = await create_invite(admin_id)
    stale = await create_invite(admin_id)

    async with session_scope() as session:
        stored = await session.get(Invite, stale.code)
        stored.expires_at = utcnow() - timedelta(hours=1)

    codes = {i.code for i in await list_invites(only_unused=True)}
    assert fresh.code in codes
    assert stale.code not in codes


async def test_revoke_blocks_and_preserves_row(db, admin_id: int) -> None:
    invite = await create_invite(admin_id)
    await redeem_invite(invite.code, 1005, "erin")

    assert await revoke_user(1005) is True

    async with session_scope() as session:
        user = await session.get(User, 1005)
    assert user is not None and not user.active

    assert await revoke_user(1005) is False


async def test_admin_cannot_be_revoked(db, admin_id: int) -> None:
    """Отзыв админа заблокировал бы управление ботом без доступа к БД."""
    with pytest.raises(RedeemError, match="администратора"):
        await revoke_user(admin_id)


async def test_revoked_user_can_return_with_new_code(db, admin_id: int) -> None:
    first = await create_invite(admin_id)
    await redeem_invite(first.code, 1006, "frank")
    await revoke_user(1006)

    second = await create_invite(admin_id)
    user = await redeem_invite(second.code, 1006, "frank")
    assert user.active
    assert user.invite_code == second.code


async def test_concurrent_redeem_lets_exactly_one_in(db, admin_id: int) -> None:
    """Один код — один человек, даже если жмут одновременно.

    Связка «прочитать → проверить → записать» такую гарантию даёт только за счёт
    того, как SQLite сериализует записи. Проверяем саму гарантию, а не поведение
    конкретной СУБД.
    """
    import asyncio

    invite = await create_invite(admin_id)
    candidates = list(range(9100, 9110))

    results = await asyncio.gather(
        *[redeem_invite(invite.code, tg_id, f"u{tg_id}") for tg_id in candidates],
        return_exceptions=True,
    )

    granted = [r for r in results if isinstance(r, User)]
    assert len(granted) == 1

    async with session_scope() as session:
        rows = (
            await session.scalars(select(User).where(User.tg_id.in_(candidates)))
        ).all()
    assert [u.tg_id for u in rows if u.active] == [granted[0].tg_id]
