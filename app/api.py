"""Mini App HTTP API. Never trust a user ID or chat ID supplied by the client."""
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app.auth import AuthError, verify_init_data
from app.config import validate_time
from app.dates import days_until, validate_birthday
from app.scheduler import is_current_member


router = APIRouter()


class BirthdayPayload(BaseModel):
    day: int = Field(ge=1, le=31)
    month: int = Field(ge=1, le=12)
    birth_year: int | None = None
    show_age: bool = False
    visible: bool = True
    celebrate: bool = True

    @field_validator("birth_year")
    @classmethod
    def year_range(cls, value):
        if value is not None and not 1900 <= value <= date.today().year:
            raise ValueError("Incorrect year")
        return value


class ChatSettingsPayload(BaseModel):
    tz: str = Field(min_length=2, max_length=80)
    greeting_time: str
    pin_enabled: bool

    @field_validator("tz")
    @classmethod
    def timezone_known(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Unknown time zone") from None
        return value

    @field_validator("greeting_time")
    @classmethod
    def time_known(cls, value):
        validate_time(value)
        return value


async def context(
    request: Request,
    chat_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    if chat_id >= 0:
        raise HTTPException(400, "Only Telegram groups are supported")
    try:
        user = verify_init_data(x_telegram_init_data, request.app.state.settings.bot_token)
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    db = request.app.state.db
    chat = await db.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "Group not initialized. Ask an admin to send /holidays")
    try:
        member = await request.app.state.bot.get_chat_member(chat_id, user["id"])
    except TelegramAPIError:
        raise HTTPException(403, "Cannot confirm chat membership. Give the bot admin rights") from None
    if not is_current_member(member):
        raise HTTPException(403, "You are not a member of this chat")
    is_admin = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    return {"user": user, "chat": chat, "admin": is_admin, "db": db}


def birthday_public(row):
    if not row:
        return None
    return {
        "day": row["day"],
        "month": row["month"],
        "birth_year": row["birth_year"],
        "show_age": bool(row["show_age"]),
        "visible": bool(row["visible"]),
        "celebrate": bool(row["celebrate"]),
    }


@router.get("/health")
async def health():
    return {"ok": True, "service": "happy-birthday"}


@router.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/bootstrap")
async def bootstrap(ctx=Depends(context)):
    chat = ctx["chat"]
    user = ctx["user"]
    current = date.today()
    birthdays = await ctx["db"].get_visible_birthdays(chat["chat_id"])
    for item in birthdays:
        item["days_until"] = days_until(item["day"], item["month"], current)
    birthdays.sort(key=lambda x: (x["days_until"], x["first_name"].casefold()))
    my_birthday = await ctx["db"].get_birthday(chat["chat_id"], user["id"])
    return {
        "me": {"id": user["id"], "first_name": user.get("first_name", "Друг"),
               "username": user.get("username"), "photo_url": user.get("photo_url")},
        "chat": {"id": chat["chat_id"], "title": chat["title"]},
        "birthday": birthday_public(my_birthday),
        "birthdays": birthdays,
        "is_admin": ctx["admin"],
        "settings": {
            "tz": chat["tz"], "greeting_time": chat["greeting_time"],
            "pin_enabled": bool(chat["pin_enabled"])
        },
    }


@router.put("/api/birthday")
async def save_birthday(payload: BirthdayPayload, ctx=Depends(context)):
    try:
        validate_birthday(payload.day, payload.month, payload.birth_year)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await ctx["db"].put_birthday(
        ctx["chat"]["chat_id"], ctx["user"], payload.day, payload.month,
        payload.birth_year, payload.show_age, payload.visible, payload.celebrate
    )
    return {"ok": True}


@router.delete("/api/birthday")
async def delete_birthday(ctx=Depends(context)):
    await ctx["db"].remove_birthday(ctx["chat"]["chat_id"], ctx["user"]["id"])
    return {"ok": True}


@router.put("/api/settings")
async def save_settings(payload: ChatSettingsPayload, ctx=Depends(context)):
    if not ctx["admin"]:
        raise HTTPException(403, "Admin rights required")
    await ctx["db"].set_settings(ctx["chat"]["chat_id"], payload.tz,
                                 payload.greeting_time, payload.pin_enabled)
    return {"ok": True}
