"""Админка источников: /add_source, /sources, /toggle_source, /rm_source.

Добавление идёт через превью: показать три свежих заголовка до записи в БД —
единственный способ убедиться, что по ссылке живая лента, а не HTML-страница,
редирект на капчу или фид, переставший обновляться полгода назад.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from alert_bot.bot.access import AdminOnlyMiddleware, fmt_dt
from alert_bot.config import get_settings
from alert_bot.db.models import User
from alert_bot.news import registry
from alert_bot.news.fetch import probe_source

log = logging.getLogger(__name__)

router = Router(name="admin_sources")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class AddSource(StatesGroup):
    confirming = State()


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data="src:confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="src:cancel"),
            ]
        ]
    )


def _default_name(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return host.split(".")[0].capitalize() if host else "Источник"


@router.message(Command("add_source"))
async def cmd_add_source(
    message: Message, command: CommandObject, state: FSMContext, user: User
) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/add_source ТИП URL [название]</code>\n\n"
            f"Доступные типы: <code>{'</code>, <code>'.join(registry.SUPPORTED_KINDS)}</code>\n"
            "Например: <code>/add_source rss https://example.com/feed</code>\n\n"
            "<i>Произвольный JSON-API добавить нельзя: под каждую схему ответа "
            "нужен парсер, то есть код. Для лент подходит любой валидный RSS/Atom.</i>"
        )
        return

    kind, url = args[0].lower(), args[1]
    name = " ".join(args[2:]) if len(args) > 2 else _default_name(url)

    if kind not in registry.SUPPORTED_KINDS:
        await message.answer(
            f"❌ Тип <code>{kind}</code> не поддерживается. "
            f"Доступны: <code>{'</code>, <code>'.join(registry.SUPPORTED_KINDS)}</code>."
        )
        return

    status = await message.answer(f"⏳ Проверяю {url}…")

    settings = get_settings()
    result = await probe_source(kind, url, settings.cryptopanic_token)

    if not result.ok:
        await status.edit_text(f"❌ Источник не прошёл проверку: {result.error}")
        return

    preview = "\n".join(
        f"• <i>{a.title[:90]}</i>\n  <code>{fmt_dt(a.published_at)}</code>"
        for a in result.articles[:3]
    )
    conditional = "да" if (result.etag or result.last_modified) else "нет"

    await state.set_state(AddSource.confirming)
    await state.update_data(name=name, kind=kind, url=url)

    await status.edit_text(
        f"<b>{name}</b> · {kind}\n<code>{url}</code>\n\n"
        f"Записей в ленте: {len(result.articles)}\n"
        f"Поддерживает условные запросы: {conditional}\n\n"
        f"<b>Свежие заголовки</b>\n{preview}\n\n"
        "Добавить источник?",
        reply_markup=_confirm_kb(),
    )


@router.callback_query(AddSource.confirming, F.data == "src:confirm")
async def cb_confirm(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    await state.clear()

    settings = get_settings()
    try:
        source = await registry.add_source(
            name=data["name"],
            kind=data["kind"],
            url=data["url"],
            added_by=user.tg_id,
            poll_interval=settings.news_poll_seconds,
        )
    except registry.SourceError as exc:
        assert callback.message is not None
        await callback.message.edit_text(f"❌ {exc}")
        await callback.answer()
        return

    assert callback.message is not None
    await callback.message.edit_text(
        f"✅ Источник <b>{source.name}</b> добавлен (id={source.id}).\n"
        "Новостной цикл подхватит его на следующей итерации."
    )
    await callback.answer()


@router.callback_query(F.data == "src:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    assert callback.message is not None
    await callback.message.edit_text("Добавление отменено.")
    await callback.answer()


@router.message(Command("sources"))
async def cmd_sources(message: Message) -> None:
    sources = await registry.list_sources()
    if not sources:
        await message.answer("Источников нет.")
        return

    lines = ["<b>Источники</b>\n"]
    for source in sources:
        lines.append(
            f"<code>{source.id}</code> <b>{source.name}</b> · {source.kind}\n"
            f"    {registry.health_label(source)} · последний успех: {fmt_dt(source.last_ok_at)}"
        )
        if source.last_error:
            lines.append(f"    ⚠️ {source.last_error[:120]}")

    lines.append(
        "\n<code>/toggle_source ID</code> · <code>/rm_source ID</code>"
    )
    await message.answer("\n".join(lines))


@router.message(Command("toggle_source"))
async def cmd_toggle_source(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: <code>/toggle_source ID</code>")
        return

    current = await registry.get_source(int(raw))
    if current is None:
        await message.answer(f"Источник <code>{raw}</code> не найден.")
        return

    source = await registry.set_enabled(current.id, not current.enabled)
    assert source is not None
    state = "включён" if source.enabled else "отключён"
    await message.answer(f"Источник <b>{source.name}</b> {state}.")


@router.message(Command("rm_source"))
async def cmd_rm_source(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: <code>/rm_source ID</code>")
        return

    name = await registry.delete_source(int(raw))
    if name is None:
        await message.answer(f"Источник <code>{raw}</code> не найден.")
        return

    await message.answer(f"🗑 Источник <b>{name}</b> удалён вместе с его статьями.")


@router.callback_query(F.data == "menu:sources")
async def cb_sources(callback: CallbackQuery) -> None:
    from alert_bot.bot.menu import admin_menu_kb

    sources = await registry.list_sources()
    if not sources:
        text = "Источников нет."
    else:
        lines = ["<b>📰 Источники</b>\n"]
        for source in sources:
            lines.append(
                f"<code>{source.id}</code> <b>{source.name}</b> · "
                f"{registry.health_label(source)}\n"
                f"    последний успех: {fmt_dt(source.last_ok_at)}"
            )
            if source.last_error:
                lines.append(f"    ⚠️ {source.last_error[:100]}")
        lines.append("\n<code>/add_source rss URL</code> · <code>/toggle_source ID</code>")
        text = "\n".join(lines)

    assert callback.message is not None
    await callback.message.edit_text(text, reply_markup=admin_menu_kb())
    await callback.answer()
