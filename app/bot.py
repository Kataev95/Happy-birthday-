"""Telegram group panel and private start; all birthdays edited in Mini App."""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonWebApp, Message, WebAppInfo,
)

router = Router()
log = logging.getLogger(__name__)
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


def app_link(bot_username: str, chat_id: int, tab: str = "") -> str:
    # A main Mini App must first be configured in BotFather.
    postfix = f"_{tab}" if tab else ""
    return f"https://t.me/{bot_username}?startapp=c{chat_id}{postfix}"


def group_invite_link(bot_username: str) -> str:
    """Open Telegram's group picker, requesting only the pin right we need."""
    return f"https://t.me/{bot_username.lstrip('@')}?startgroup&admin=pin_messages"


def private_start_markup(username: str, app_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить в группу",
            url=group_invite_link(username),
        )],
        [InlineKeyboardButton(
            text="✨ Открыть приложение",
            web_app=WebAppInfo(url=app_url),
        )],
    ])


def panel_markup(username: str, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎂 Мой день рождения", url=app_link(username, chat_id, "b"))],
        [InlineKeyboardButton(text="📅 Календарь", url=app_link(username, chat_id, "c"))],
        [InlineKeyboardButton(text="🎄 Новогодние желания", url=app_link(username, chat_id, "w"))],
    ])


PANEL_TEXT = (
    "🎉 <b>ПРАЗДНИКИ НАШЕГО ЧАТА</b>\n\n"
    "🎂 Сохрани день рождения, чтобы друзья не забыли поздравить!\n"
    "📅 Посмотри ближайшие праздники в календаре.\n"
    "🎄 Составь список желаний и укажи удобный ПВЗ по желанию.\n\n"
    "🔕 <i>Без лишних сообщений: всё управление внутри приложения.</i>"
)


QUICK_MENU_TEXT = (
    "🎉 <b>Днюхометр — меню праздников</b>\n\n"
    "🎂 Добавь свой день рождения.\n"
    "📅 Посмотри ближайшие праздники.\n"
    "🎄 Узнай желания друзей.\n\n"
    "✨ <i>Меню появилось по запросу и автоматически исчезнет через 15 минут.</i>"
)


@router.message(Command("holidays"), F.chat.type.in_(GROUPS))
async def holidays_panel(message: Message, db, settings, username: str):
    """The first admin call creates a permanent panel; every next call summons a fresh menu."""
    if not message.from_user:
        return

    # The original panel is never deleted or moved, including when an admin pinned it.
    await db.ensure_chat(
        message.chat.id, message.chat.title or "Наш чат",
        settings.default_timezone, settings.default_birthday_time
    )
    chat = await db.get_chat(message.chat.id)
    keyboard = panel_markup(username, message.chat.id)

    if chat.get("panel_message_id") is None:
        # Only the first permanent panel requires an admin; other participants
        # always get a temporary menu, even if admin rights cannot be checked.
        is_admin = False
        try:
            member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
            is_admin = member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
        except TelegramAPIError:
            log.warning("Could not check role for permanent holidays panel in %s", message.chat.id)
        if is_admin:
            sent = await message.answer(PANEL_TEXT, reply_markup=keyboard, parse_mode="HTML")
            await db.set_panel(message.chat.id, sent.message_id)
            return

    # /holidays can be called by ANY member at ANY time, without changing
    # the original panel. Only one transient menu is kept per chat.
    sent = await message.answer(
        QUICK_MENU_TEXT, reply_markup=keyboard, parse_mode="HTML"
    )
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    previous_id = await db.replace_quick_menu(message.chat.id, sent.message_id, expires_at)
    if previous_id and previous_id != sent.message_id:
        try:
            await message.bot.delete_message(
                chat_id=message.chat.id, message_id=previous_id
            )
        except TelegramAPIError as exc:
            log.info("Could not remove old temporary menu in %s: %s", message.chat.id, exc)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def private_start(message: Message, settings, username: str):
    await message.answer(
        "🎉 <b>Днюхометр — праздники вашего чата!</b>\n\n"
        "🎂 Сохраняю дни рождения участников и поздравляю их автоматически.\n"
        "📅 Показываю ближайшие праздники в календаре.\n"
        "🎄 Помогаю делиться новогодними желаниями и подарками.\n\n"
        "🛠 <b>Как подключить (3 шага):</b>\n"
        "1️⃣ Нажми <b>«➕ Добавить в группу»</b> и выбери свой чат.\n"
        "2️⃣ Разреши боту закреплять сообщения, чтобы поздравления "
        "могли появляться в закрепе.\n"
        "3️⃣ <b>В самом групповом чате</b> отправь команду "
        "<code>/holidays</code>. При первом запуске это должен сделать администратор группы.\n\n"
        "✨ Бот отправит одну праздничную панель с кнопками: "
        "участники откроют Mini App, укажут свои дни рождения и желания. "
        "При желании закрепи эту панель в чате, чтобы всем было удобно.\n\n"
        "🔄 Позже <b>любой участник</b> сможет снова написать "
        "<code>/holidays</code>, чтобы вызвать кнопки. Повторное меню "
        "автоматически исчезает через 15 минут.\n\n"
        "🔕 Никаких лишних сообщений в беседе.",
        parse_mode="HTML",
        reply_markup=private_start_markup(username, settings.base_url),
    )


@router.my_chat_member(F.chat.type.in_(GROUPS))
async def bot_membership(event: ChatMemberUpdated, db, settings):
    status = event.new_chat_member.status
    if status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
        await db.ensure_chat(event.chat.id, event.chat.title or "Наш чат",
                             settings.default_timezone, settings.default_birthday_time)
    elif status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        await db.set_chat_active(event.chat.id, False)
