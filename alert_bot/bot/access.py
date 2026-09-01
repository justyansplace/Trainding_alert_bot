"""Контроль доступа: whitelist, роли, инвайт-коды.

Middleware отсекает всех, кого нет в users, до любого хендлера — кроме /start и
/redeem, иначе новый человек не смог бы активировать код.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser
from sqlalchemy import select, update

from alert_bot.config import get_settings
from alert_bot.db.models import Invite, Role, User, utcnow
from alert_bot.db.session import invite_lifetime, session_scope

log = logging.getLogger(__name__)

# Команды, доступные тому, кого ещё нет в whitelist.
PUBLIC_COMMANDS = ("/start", "/redeem", "/help")

DENIED_TEXT = (
    "🔒 Доступ к боту закрыт.\n\n"
    "Если у вас есть инвайт-код, активируйте его: <code>/redeem КОД</code>"
)


class AccessMiddleware(BaseMiddleware):
    """Кладёт User из БД в data['user'] либо отбивает запрос."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return None

        async with session_scope() as session:
            user = await session.get(User, tg_user.id)

        if user is not None and user.active:
            data["user"] = user
            return await handler(event, data)

        # Незнакомцу разрешены только публичные команды.
        if isinstance(event, Message) and (event.text or "").split()[:1]:
            command = (event.text or "").split()[0].split("@")[0]
            if command in PUBLIC_COMMANDS:
                data["user"] = None
                return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(DENIED_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ закрыт", show_alert=True)
        return None


class AdminOnlyMiddleware(BaseMiddleware):
    """Вешается на админский роутер поверх AccessMiddleware."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("user")
        if user is None or not user.is_admin:
            if isinstance(event, Message):
                await event.answer("⛔ Команда доступна только администратору.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Только для администратора", show_alert=True)
            return None
        return await handler(event, data)


# --------------------------------------------------------------------------- #
# Инвайт-коды
# --------------------------------------------------------------------------- #


async def create_invite(created_by: int) -> Invite:
    code = secrets.token_urlsafe(9)
    async with session_scope() as session:
        invite = Invite(
            code=code,
            created_by=created_by,
            created_at=utcnow(),
            expires_at=utcnow() + invite_lifetime(),
        )
        session.add(invite)
    return invite


class RedeemError(Exception):
    pass


async def redeem_invite(code: str, tg_id: int, username: str | None) -> User:
    """Активирует код и заводит пользователя. Бросает RedeemError с причиной.

    Код занимается одним условным UPDATE, а не связкой «прочитать → проверить →
    записать»: условие целиком в WHERE, решение принимает СУБД, ноль изменённых
    строк означает, что код заняли раньше нас.

    На SQLite вариант с раздельными чтением и записью тоже держится — движок
    сериализует пишущие транзакции. Но держится он на поведении конкретной СУБД,
    а не на самой конструкции, и переезд на Postgres это молча сломает. Здесь
    цена корректности — одна строка, поэтому она уплачена сразу.
    """
    code = code.strip()
    now = utcnow()

    async with session_scope() as session:
        existing = await session.get(User, tg_id)
        if existing is not None and existing.active:
            raise RedeemError("У вас уже есть доступ.")

        invite = await session.get(Invite, code)
        if invite is None:
            raise RedeemError("Такого кода не существует.")
        if invite.expires_at <= now:
            raise RedeemError("Срок действия кода истёк.")

        claimed = await session.execute(
            update(Invite)
            .where(
                Invite.code == code,
                Invite.used_by.is_(None),
                Invite.expires_at > now,
            )
            .values(used_by=tg_id, used_at=now)
        )
        if claimed.rowcount != 1:
            raise RedeemError("Код уже использован.")

        if existing is not None:
            existing.active = True
            existing.invite_code = code
            existing.username = username
            user = existing
        else:
            user = User(
                tg_id=tg_id,
                role=Role.USER.value,
                granted_at=utcnow(),
                invite_code=code,
                username=username,
            )
            session.add(user)

    log.info("Доступ выдан tg_id=%s по коду %s", tg_id, code)
    return user


async def list_invites(only_unused: bool = True) -> list[Invite]:
    async with session_scope() as session:
        stmt = select(Invite).order_by(Invite.created_at.desc())
        if only_unused:
            stmt = stmt.where(Invite.used_by.is_(None), Invite.expires_at > utcnow())
        return list((await session.scalars(stmt)).all())


async def revoke_user(tg_id: int) -> bool:
    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if user is None or not user.active:
            return False
        if user.is_admin:
            raise RedeemError("Нельзя отозвать доступ у администратора.")
        user.active = False
        return True


async def grant_admin(tg_id: int) -> User:
    """Делает администратором того, у кого уже есть доступ.

    Именно «у кого уже есть»: роль не выдаётся незнакомцу по одному номеру.
    Сначала человек проходит обычный путь через инвайт — так администратор
    видит, что номер настоящий и принадлежит тому, кому он думает.
    """
    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if user is None or not user.active:
            raise RedeemError(
                "Сначала выдайте доступ: /gen_invite, человек активирует код, "
                "и только потом роль."
            )
        if user.is_admin:
            raise RedeemError("Уже администратор.")
        user.role = Role.ADMIN.value
        log.info("tg_id=%s стал администратором", tg_id)
        return user


async def revoke_admin(tg_id: int, by: int) -> User:
    """Снимает роль администратора. Доступ к боту при этом остаётся."""
    settings = get_settings()

    if tg_id == by:
        raise RedeemError("Снять роль с себя нельзя — попросите второго администратора.")
    # Владелец из конфига неприкосновенен: роль ему всё равно вернётся на
    # ближайшем старте, а до старта бот остался бы без администратора вовсе.
    if tg_id == settings.admin_tg_id:
        raise RedeemError("Это владелец из конфига, роль с него не снимается.")

    async with session_scope() as session:
        user = await session.get(User, tg_id)
        if user is None:
            raise RedeemError("Пользователь не найден.")
        if not user.is_admin:
            raise RedeemError("Он и так не администратор.")
        user.role = Role.USER.value
        log.info("tg_id=%s больше не администратор (снял %s)", tg_id, by)
        return user


async def list_admins() -> list[User]:
    async with session_scope() as session:
        return list(
            (
                await session.scalars(
                    select(User).where(User.role == Role.ADMIN.value, User.active.is_(True))
                )
            ).all()
        )


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"
