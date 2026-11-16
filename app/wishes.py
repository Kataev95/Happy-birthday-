"""Opt-in New Year wishlists: no chat posts, no secret PВЗ addresses in public APIs."""
import ipaddress
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api import context
from app.scheduler import is_current_member
from aiogram.exceptions import TelegramAPIError

router = APIRouter(prefix="/api", tags=["wishes"])

WISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS wishes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
  owner_id INTEGER NOT NULL,
  owner_name TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  product_url TEXT,
  image_url TEXT,
  price_rub INTEGER,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','fulfilled')),
  reserved_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wishes_chat_idx ON wishes(chat_id, status, created_at);
CREATE INDEX IF NOT EXISTS wishes_owner_idx ON wishes(chat_id, owner_id);
CREATE TABLE IF NOT EXISTS pickup_points (
  chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL,
  marketplace TEXT NOT NULL CHECK(marketplace IN ('wb','ozon')),
  address TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(chat_id, user_id, marketplace)
);
"""

MARKET_DOMAINS = {"wildberries.ru", "wb.ru", "ozon.ru", "ozon.com"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_https_url(value: str | None, *, marketplace: bool = False) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > 600:
        raise ValueError("Ссылка слишком длинная")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or not host or parts.username or parts.password:
            raise ValueError()
        if parts.port not in (None, 443):
            raise ValueError()
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError:
            # Reject numeric IPs; ordinary public DNS hostnames are allowed.
            if re.fullmatch(r"[0-9.:]+", host):
                raise ValueError()
        if marketplace and not any(host == suffix or host.endswith("." + suffix)
                                   for suffix in MARKET_DOMAINS):
            raise ValueError()
        if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(
            "Используй ссылку HTTPS" + (" с Wildberries или Ozon" if marketplace else "")
        ) from None
    return value


def clean_text(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()


class WishPayload(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=600)
    product_url: str | None = None
    image_url: str | None = None
    price_rub: int | None = Field(default=None, ge=1, le=10000000)

    @field_validator("title", "description")
    @classmethod
    def normalize_text(cls, value: str):
        result = clean_text(value)
        if len(result) > 1000:
            raise ValueError("Слишком длинный текст")
        return result

    @field_validator("product_url")
    @classmethod
    def product_link(cls, value):
        return valid_https_url(value, marketplace=True)

    @field_validator("image_url")
    @classmethod
    def image_link(cls, value):
        return valid_https_url(value)


class PickupPayload(BaseModel):
    wb: str | None = Field(default=None, max_length=220)
    ozon: str | None = Field(default=None, max_length=220)

    @field_validator("wb", "ozon")
    @classmethod
    def clean_address(cls, value):
        if value is None:
            return None
        value = clean_text(value)
        return value or None


async def initialize(db):
    """Create additive tables. Existing birthdays and delivery history stay untouched."""
    async with db.lock:
        await db.connection.executescript(WISH_SCHEMA)
        await db.connection.commit()


async def list_for_chat(db, chat_id: int, viewer_id: int):
    rows = await db.all(
        """SELECT id, chat_id, owner_id, owner_name, title, description, product_url,
                  image_url, price_rub, status, reserved_by, created_at
           FROM wishes WHERE chat_id=? ORDER BY created_at DESC, id DESC LIMIT 500""",
        (chat_id,),
    )
    mine, friends = [], []
    for row in rows:
        owner = row["owner_id"] == viewer_id
        # Never tell the wish owner which item was reserved, to preserve the surprise.
        item = {key: row[key] for key in
                ("id", "owner_id", "owner_name", "title", "description", "product_url",
                 "image_url", "price_rub", "status")}
        item["reserved"] = row["reserved_by"] is not None if not owner else False
        item["reserved_by_me"] = row["reserved_by"] == viewer_id if not owner else False
        if owner:
            mine.append(item)
        elif row["status"] == "active":
            friends.append(item)
    return {"mine": mine, "friends": friends, "max_active": 12}


async def create_wish(db, chat_id: int, user: dict, payload: WishPayload):
    if not payload.title:
        raise HTTPException(422, "Укажи название подарка")
    async with db.lock:
        async with db.connection.execute(
            "SELECT COUNT(*) FROM wishes WHERE chat_id=? AND owner_id=? AND status='active'",
            (chat_id, user["id"]),
        ) as cur:
            active_count = (await cur.fetchone())[0]
        if active_count >= 12:
            raise HTTPException(409, "Можно иметь не больше 12 активных желаний")
        async with db.connection.execute(
            "SELECT COUNT(*) FROM wishes WHERE chat_id=? AND owner_id=?", (chat_id, user["id"])
        ) as cur:
            total = (await cur.fetchone())[0]
        if total >= 50:
            raise HTTPException(409, "Архив заполнен. Удали старые желания")
        created = now_utc()
        cursor = await db.connection.execute(
            """INSERT INTO wishes
               (chat_id,owner_id,owner_name,title,description,product_url,image_url,
                price_rub,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (chat_id, user["id"], (user.get("first_name") or "Участник")[:128],
             payload.title, payload.description, payload.product_url, payload.image_url,
             payload.price_rub, created, created),
        )
        await db.connection.commit()
        return cursor.lastrowid


async def get_wish(db, chat_id: int, wish_id: int):
    return await db.one("SELECT * FROM wishes WHERE chat_id=? AND id=?", (chat_id, wish_id))


@router.get("/wishes")
async def get_wishes(ctx=Depends(context)):
    return await list_for_chat(ctx["db"], ctx["chat"]["chat_id"], ctx["user"]["id"])


@router.post("/wishes", status_code=201)
async def post_wish(payload: WishPayload, ctx=Depends(context)):
    wish_id = await create_wish(ctx["db"], ctx["chat"]["chat_id"], ctx["user"], payload)
    return {"ok": True, "id": wish_id}


@router.put("/wishes/{wish_id}")
async def edit_wish(wish_id: int, payload: WishPayload, ctx=Depends(context)):
    db = ctx["db"]
    async with db.lock:
        row = await get_wish(db, ctx["chat"]["chat_id"], wish_id)
        if not row or row["owner_id"] != ctx["user"]["id"]:
            raise HTTPException(404, "Желание не найдено")
        if row["status"] != "active":
            raise HTTPException(409, "Исполненное желание нельзя изменить")
        if not payload.title:
            raise HTTPException(422, "Укажи название подарка")
        await db.connection.execute(
            """UPDATE wishes SET title=?,description=?,product_url=?,image_url=?,
               price_rub=?,updated_at=? WHERE id=? AND chat_id=?""",
            (payload.title,payload.description,payload.product_url,payload.image_url,
             payload.price_rub,now_utc(),wish_id,ctx["chat"]["chat_id"]),
        )
        await db.connection.commit()
    return {"ok": True}


@router.delete("/wishes/{wish_id}")
async def delete_wish(wish_id: int, ctx=Depends(context)):
    db = ctx["db"]
    async with db.lock:
        cursor = await db.connection.execute(
            "DELETE FROM wishes WHERE id=? AND chat_id=? AND owner_id=?",
            (wish_id,ctx["chat"]["chat_id"],ctx["user"]["id"]),
        )
        await db.connection.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "Желание не найдено")
    return {"ok": True}


@router.post("/wishes/{wish_id}/received")
async def wish_received(wish_id: int, ctx=Depends(context)):
    db = ctx["db"]
    async with db.lock:
        cursor = await db.connection.execute(
            """UPDATE wishes SET status='fulfilled', reserved_by=NULL, updated_at=?
               WHERE id=? AND chat_id=? AND owner_id=? AND status='active'""",
            (now_utc(),wish_id,ctx["chat"]["chat_id"],ctx["user"]["id"]),
        )
        await db.connection.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "Желание не найдено или уже исполнено")
    return {"ok": True}


@router.post("/wishes/{wish_id}/reserve")
async def reserve_wish(wish_id: int, ctx=Depends(context)):
    db=ctx["db"]
    chat_id=ctx["chat"]["chat_id"]
    uid=ctx["user"]["id"]
    async with db.lock:
        row=await get_wish(db,chat_id,wish_id)
        if not row or row["status"] != "active" or row["owner_id"] == uid:
            raise HTTPException(404, "Желание недоступно")
        if row["reserved_by"] == uid:
            return {"ok": True, "already_reserved": True}
        if row["reserved_by"] is not None:
            raise HTTPException(409, "Этот подарок уже выбрал другой участник")
        # An atomic update prevents two gifters reserving the same gift simultaneously.
        cursor=await db.connection.execute(
            """UPDATE wishes SET reserved_by=?,updated_at=?
               WHERE id=? AND chat_id=? AND status='active'
                 AND reserved_by IS NULL AND owner_id!=?""",
            (uid,now_utc(),wish_id,chat_id,uid),
        )
        await db.connection.commit()
        if cursor.rowcount != 1:
            raise HTTPException(409, "Этот подарок уже выбрали")
    return {"ok": True, "already_reserved": False}


@router.delete("/wishes/{wish_id}/reserve")
async def cancel_reservation(wish_id: int, ctx=Depends(context)):
    db=ctx["db"]
    async with db.lock:
        cursor=await db.connection.execute(
            """UPDATE wishes SET reserved_by=NULL,updated_at=?
               WHERE id=? AND chat_id=? AND status='active' AND reserved_by=?""",
            (now_utc(),wish_id,ctx["chat"]["chat_id"],ctx["user"]["id"]),
        )
        await db.connection.commit()
        if cursor.rowcount != 1:
            raise HTTPException(404, "Твоё бронирование не найдено")
    return {"ok": True}


@router.get("/pickup")
async def my_pickup(ctx=Depends(context)):
    rows = await ctx["db"].all(
        "SELECT marketplace,address FROM pickup_points WHERE chat_id=? AND user_id=?",
        (ctx["chat"]["chat_id"],ctx["user"]["id"]),
    )
    return {row["marketplace"]:row["address"] for row in rows}


@router.put("/pickup")
async def save_pickup(payload: PickupPayload, ctx=Depends(context)):
    db=ctx["db"]
    chat_id=ctx["chat"]["chat_id"]
    uid=ctx["user"]["id"]
    async with db.lock:
        for marketplace, value in (("wb",payload.wb),("ozon",payload.ozon)):
            if value:
                await db.connection.execute(
                    """INSERT INTO pickup_points(chat_id,user_id,marketplace,address,updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(chat_id,user_id,marketplace) DO UPDATE SET
                       address=excluded.address,updated_at=excluded.updated_at""",
                    (chat_id,uid,marketplace,value,now_utc()),
                )
            else:
                await db.connection.execute(
                    "DELETE FROM pickup_points WHERE chat_id=? AND user_id=? AND marketplace=?",
                    (chat_id,uid,marketplace),
                )
        await db.connection.commit()
    return {"ok": True}


@router.get("/wishes/{wish_id}/delivery")
async def delivery_for_reserver(wish_id: int, ctx=Depends(context)):
    db=ctx["db"]
    chat_id=ctx["chat"]["chat_id"]
    viewer=ctx["user"]["id"]
    wish=await get_wish(db,chat_id,wish_id)
    if not wish or not (wish["owner_id"] == viewer or (
        wish["status"] == "active" and wish["reserved_by"] == viewer
    )):
        raise HTTPException(403, "Адрес доступен только автору или дарителю, выбравшему подарок")
    # A recipient who left the group should not have their pickup address disclosed.
    if wish["owner_id"] != viewer:
        try:
            member=await ctx["bot"].get_chat_member(chat_id,wish["owner_id"])
        except TelegramAPIError:
            raise HTTPException(403,"Не удалось подтвердить участие получателя в чате") from None
        if not is_current_member(member):
            raise HTTPException(403,"Получатель больше не состоит в чате")
    rows=await db.all(
        "SELECT marketplace,address FROM pickup_points WHERE chat_id=? AND user_id=?",
        (chat_id,wish["owner_id"]),
    )
    return {
        "owner_name": wish["owner_name"],
        "pickup": {row["marketplace"]: row["address"] for row in rows},
        "note": "Адрес ПВЗ — ориентир. Для получения заказа может понадобиться код/QR от покупателя. Не публикуй коды в общем чате.",
    }
