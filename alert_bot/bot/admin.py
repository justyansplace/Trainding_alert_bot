"""Админка доступа и расхода: /gen_invite, /users, /revoke, /usage."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from alert_bot.bot.access import (
    AdminOnlyMiddleware,
    RedeemError,
    create_invite,
    fmt_dt,
    grant_admin,
    list_admins,
    list_invites,
    revoke_admin,
    revoke_user,
)
from alert_bot.config import get_settings
from alert_bot.db.models import User
from alert_bot.db.session import session_scope
from alert_bot.bot.menu import admin_menu_kb
from alert_bot.llm.client import daily_spend_usd, usage_report

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.message(Command("gen_invite"))
async def cmd_gen_invite(message: Message, user: User) -> None:
    invite = await create_invite(user.tg_id)
    await message.answer(
        f"🎟 Инвайт-код (одноразовый, до {fmt_dt(invite.expires_at)} UTC):\n\n"
        f"<code>{invite.code}</code>\n\n"
        f"Активация получателем: <code>/redeem {invite.code}</code>"
    )


@router.message(Command("invites"))
async def cmd_invites(message: Message) -> None:
    invites = await list_invites(only_unused=True)
    if not invites:
        await message.answer("Активных неиспользованных кодов нет.")
        return
    lines = ["<b>Активные коды</b>\n"]
    lines += [f"<code>{i.code}</code> — до {fmt_dt(i.expires_at)}" for i in invites]
    await message.answer("\n".join(lines))


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    async with session_scope() as session:
        users = list((await session.scalars(select(User).order_by(User.granted_at))).all())

    lines = [f"<b>Пользователи</b> ({sum(u.active for u in users)} активных)\n"]
    for u in users:
        mark = "👑" if u.is_admin else ("🟢" if u.active else "⚪")
        handle = f"@{u.username}" if u.username else "—"
        lines.append(f"{mark} <code>{u.tg_id}</code> {handle} · с {fmt_dt(u.granted_at)}")
    await message.answer("\n".join(lines))


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: <code>/revoke TG_ID</code>")
        return

    try:
        ok = await revoke_user(int(raw))
    except RedeemError as exc:
        await message.answer(f"❌ {exc}")
        return

    await message.answer(
        f"⚪ Доступ для <code>{raw}</code> отозван." if ok else f"Пользователь <code>{raw}</code> не найден."
    )


@router.message(Command("grant_admin"))
async def cmd_grant_admin(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer(
            "Использование: <code>/grant_admin TG_ID</code>\n\n"
            "Номер видно в <code>/users</code>. Администратор может всё то же, "
            "что и вы: выдавать доступ, отзывать его и управлять инструментами "
            "и лентами."
        )
        return

    try:
        promoted = await grant_admin(int(raw))
    except RedeemError as exc:
        await message.answer(f"❌ {exc}")
        return

    handle = f"@{promoted.username}" if promoted.username else f"<code>{raw}</code>"
    await message.answer(
        f"👑 {handle} теперь администратор.\n\n"
        "<i>Подсказки команд у него обновятся при следующем перезапуске бота — "
        "Telegram кэширует их на стороне клиента. Сами команды работают сразу.</i>"
    )


@router.message(Command("revoke_admin"))
async def cmd_revoke_admin(message: Message, command: CommandObject, user: User) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer(
            "Использование: <code>/revoke_admin TG_ID</code>\n\n"
            "<i>Доступ к боту при этом остаётся — снимается только роль.</i>"
        )
        return

    try:
        demoted = await revoke_admin(int(raw), by=user.tg_id)
    except RedeemError as exc:
        await message.answer(f"❌ {exc}")
        return

    handle = f"@{demoted.username}" if demoted.username else f"<code>{raw}</code>"
    await message.answer(f"⚪ {handle} больше не администратор. Доступ к боту остался.")


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    settings = get_settings()
    admins = await list_admins()
    lines = [f"<b>Администраторы</b> ({len(admins)})\n"]
    for a in admins:
        handle = f"@{a.username}" if a.username else "—"
        owner = " · владелец" if a.tg_id == settings.admin_tg_id else ""
        lines.append(f"👑 <code>{a.tg_id}</code> {handle}{owner}")
    lines.append("\n<code>/grant_admin TG_ID</code> · <code>/revoke_admin TG_ID</code>")
    await message.answer("\n".join(lines))


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    settings = get_settings()
    spent_24h = await daily_spend_usd()
    rows_24h = await usage_report(24)
    rows_30d = await usage_report(24 * 30)

    budget_bar = f"${spent_24h:.3f} / ${settings.daily_llm_budget_usd:.2f}"
    pct = spent_24h / settings.daily_llm_budget_usd * 100 if settings.daily_llm_budget_usd else 0
    mark = "🟢" if pct < 70 else ("🟡" if pct < 100 else "🔴")

    lines = [f"<b>Расход на LLM</b>\n\n{mark} За 24ч: {budget_bar} ({pct:.0f}%)\n"]

    if rows_24h:
        lines.append("<b>Разбивка за 24ч</b>")
        for model, purpose, inp, out, cost in rows_24h:
            lines.append(f"• {model} / {purpose}: {inp}→{out} tok · ${cost:.4f}")
    else:
        lines.append("За 24ч вызовов не было.")

    total_30d = sum(c for *_, c in rows_30d)
    lines.append(f"\nЗа 30 дней: <b>${total_30d:.2f}</b>")
    lines.append(
        f"Прогноз по темпу за сутки: <b>${spent_24h * 30:.2f}</b>/мес\n"
        f"<i>Потолок ${settings.daily_llm_budget_usd * 30:.2f}/мес — при его "
        "достижении разбор новостей встаёт, алерты продолжают идти без сводки.</i>"
    )

    await message.answer("\n".join(lines))


# --------------------------------------------------------------------------- #
# Кнопки админского меню
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "menu:invite")
async def cb_invite(callback: CallbackQuery, user: User) -> None:
    invite = await create_invite(user.tg_id)
    assert callback.message is not None
    await callback.message.edit_text(
        f"🎟 Одноразовый код, действует до {fmt_dt(invite.expires_at)} UTC:\n\n"
        f"<code>{invite.code}</code>\n\n"
        f"Получателю: <code>/redeem {invite.code}</code>",
        reply_markup=admin_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:users")
async def cb_users(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        users = list((await session.scalars(select(User).order_by(User.granted_at))).all())

    lines = [f"<b>👥 Люди</b> ({sum(u.active for u in users)} активных)\n"]
    for u in users:
        mark = "👑" if u.is_admin else ("🟢" if u.active else "⚪")
        handle = f"@{u.username}" if u.username else "—"
        lines.append(f"{mark} <code>{u.tg_id}</code> {handle} · с {fmt_dt(u.granted_at)}")

    assert callback.message is not None
    await callback.message.edit_text("\n".join(lines), reply_markup=admin_menu_kb())
    await callback.answer()
