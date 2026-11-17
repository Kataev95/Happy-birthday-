"""One chat message per celebrant per year; unpin only own pinned message."""
import asyncio
import html
import logging
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from aiogram.enums import ChatMemberStatus
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramAPIError

from app.bot import app_link
from app.dates import day_of_observance

log = logging.getLogger(__name__)


def is_current_member(member) -> bool:
    if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return True


def greeting_text(person: dict, current_year: int) -> str:
    name = html.escape(person["first_name"] or "Наш друг")
    mention = f'<a href="tg://user?id={person["user_id"]}">{name}</a>'
    age = ""
    if person["show_age"] and person["birth_year"]:
        years = current_year - person["birth_year"]
        age = f"\n🎈 Сегодня исполняется <b>{years}</b>!" if years > 0 else ""
    return (
        f"🎉 <b>С ДНЁМ РОЖДЕНИЯ!</b> 🎂\n\n"
        f"Сегодня свой особенный день отмечает {mention}!{age}\n\n"
        "✨ Желаем счастья, здоровья, тёплых встреч и исполнения самых "
        "заветных желаний!\n\n"
        "💌 Друзья, давайте поздравим именинника!"
    )


def birthday_markup(bot_username: str, chat_id: int) -> InlineKeyboardMarkup:
    """A group-scoped Mini App link: registering from this greeting edits this chat."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎂 Добавить свой ДР",
            url=app_link(bot_username, chat_id, "b"),
        )
    ]])


async def process_birthdays(db, bot, bot_username: str) -> None:
    for chat in await db.get_chats():
        try:
            now_local = datetime.now(ZoneInfo(chat["tz"]))
            hh, mm = map(int, chat["greeting_time"].split(":"))
            if (now_local.hour, now_local.minute) < (hh, mm):
                continue
            for person in await db.get_celebrants(chat["chat_id"]):
                if day_of_observance(person["day"], person["month"], now_local.year) != now_local.date():
                    continue
                try:
                    member = await bot.get_chat_member(chat["chat_id"], person["user_id"])
                    if not is_current_member(member):
                        continue
                except TelegramAPIError:
                    log.warning("Cannot check membership in chat %s; not greeting", chat["chat_id"])
                    continue
                claimed = await db.claim_delivery(chat["chat_id"], person["user_id"], now_local.year)
                if not claimed:
                    continue
                try:
                    sent = await bot.send_message(
                        chat_id=chat["chat_id"],
                        text=greeting_text(person, now_local.year),
                        parse_mode="HTML",
                        reply_markup=birthday_markup(bot_username, chat["chat_id"]),
                    )
                    # Record delivery immediately; pin errors must NOT result in duplicate greetings.
                    await db.sent_delivery(chat["chat_id"], person["user_id"], now_local.year,
                                           sent.message_id, None)
                except Exception:
                    await db.discard_claim(chat["chat_id"], person["user_id"], now_local.year)
                    log.exception("Failed birthday greeting in chat %s", chat["chat_id"])
                    continue
                if chat["pin_enabled"]:
                    try:
                        await bot.pin_chat_message(
                            chat_id=chat["chat_id"], message_id=sent.message_id,
                            disable_notification=True
                        )
                        tomorrow = datetime.combine(
                            now_local.date() + timedelta(days=1),
                            time.min, tzinfo=ZoneInfo(chat["tz"])
                        ).astimezone(timezone.utc).isoformat()
                        await db.execute(
                            """UPDATE birthday_deliveries SET unpin_after=?
                               WHERE chat_id=? AND user_id=? AND occurrence_year=?""",
                            (tomorrow, chat["chat_id"], person["user_id"], now_local.year),
                        )
                    except TelegramAPIError:
                        log.warning("Bot could not pin birthday in chat %s", chat["chat_id"])
        except Exception:
            log.exception("Birthday scheduler error in chat %s", chat["chat_id"])


async def process_unpins(db, bot):
    now_utc = datetime.now(timezone.utc).isoformat()
    for delivery in await db.due_unpins(now_utc):
        try:
            await bot.unpin_chat_message(
                chat_id=delivery["chat_id"], message_id=delivery["message_id"]
            )
        except TelegramAPIError:
            # The admin may have already unpinned the message, or revoked rights.
            log.info("Could not unpin message %s in %s",
                     delivery["message_id"], delivery["chat_id"])
        finally:
            # Prevent endless attempts and avoid touching unrelated pins.
            await db.mark_unpinned(
                delivery["chat_id"], delivery["user_id"], delivery["occurrence_year"]
            )


async def process_quick_menus(db, bot):
    """Clean up only bot-created temporary menus; never touch the saved panel."""
    now_utc = datetime.now(timezone.utc).isoformat()
    for menu in await db.due_quick_menus(now_utc):
        try:
            await bot.delete_message(chat_id=menu["chat_id"], message_id=menu["message_id"])
        except TelegramAPIError as exc:
            log.info("Could not expire temporary menu in chat %s: %s", menu["chat_id"], exc)
        finally:
            await db.forget_quick_menu(menu["chat_id"], menu["message_id"])


async def scheduler_loop(db, bot, bot_username: str, interval: int = 45):
    while True:
        try:
            await process_birthdays(db, bot, bot_username)
            await process_unpins(db, bot)
            await process_quick_menus(db, bot)
        except Exception:
            log.exception("Unexpected scheduler error")
        await asyncio.sleep(interval)
