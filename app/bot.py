"""Telegram group panel and private start; all birthdays edited in Mini App."""
import logging

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
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


def panel_markup(username: str, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎂 Мой день рождения", url=app_link(username, chat_id, "b"))],
        [InlineKeyboardButton(text="📅 Календарь", url=app_link(username, chat_id, "c"))],
        [InlineKeyboardButton(text="🎄 Желания (скоро)", url=app_link(username, chat_id, "w"))],
    ])


PANEL_TEXT = (
    "🎉 <b>ПРАЗДНИКИ НАШЕГО ЧАТА</b>\n\n"
    "🎂 Сохрани день рождения, чтобы друзья не забыли поздравить!\n"
    "📅 Посмотри ближайшие праздники в календаре.\n"
    "🎄 Новогодние желания и подарки появятся в следующем обновлении.\n\n"
    "🔕 <i>Без лишних сообщений: всё управление внутри приложения.</i>"
)


@router.message(Command("holidays"), F.chat.type.in_(GROUPS))
async def holidays_panel(message: Message, db, settings, username: str):
    bot = message.bot
    if not message.from_user:
        return
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        return
    await db.ensure_chat(message.chat.id, message.chat.title or "Наш чат",
                         settings.default_timezone, settings.default_birthday_time)
    chat = await db.get_chat(message.chat.id)
    keyboard = panel_markup(username, message.chat.id)
    panel_id = chat.get("panel_message_id")
    if panel_id:
        try:
            await bot.edit_message_text(
                text=PANEL_TEXT, chat_id=message.chat.id,
                message_id=panel_id, reply_markup=keyboard, parse_mode="HTML"
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            log.info("Previous panel cannot be updated: %s", exc)
    sent = await message.answer(PANEL_TEXT, reply_markup=keyboard, parse_mode="HTML")
    await db.set_panel(message.chat.id, sent.message_id)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def private_start(message: Message, settings):
    await message.answer(
        "🎉 <b>Праздники нашего чата</b>\n\n"
        "Открой приложение из закреплённой панели своей группы, "
        "чтобы сохранить день рождения и посмотреть календарь.\n\n"
        "🎄 Новогодние желания добавим на следующем этапе.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✨ Открыть приложение",
                                 web_app=WebAppInfo(url=settings.base_url))
        ]]),
    )


@router.my_chat_member(F.chat.type.in_(GROUPS))
async def bot_membership(event: ChatMemberUpdated, db, settings):
    status = event.new_chat_member.status
    if status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
        await db.ensure_chat(event.chat.id, event.chat.title or "Наш чат",
                             settings.default_timezone, settings.default_birthday_time)
    elif status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        await db.set_chat_active(event.chat.id, False)
