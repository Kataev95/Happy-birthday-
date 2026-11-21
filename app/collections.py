"""Public Iris fundraising cards directly in Telegram group chats (not Mini App).

Payments are user-confirmed Iris deep links. Only confirmed incoming Iris
events count toward a collection. This module never reads users' Iris wallets.
"""
import html
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from app import iris_autocollect as auto

import httpx
from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

log = logging.getLogger(__name__)
router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
API_ROOT = "https://iris-tg.ru/api/v0.5"
MAX_AMOUNT = 1_000_000
INCOMING_TYPES = {"receive", "receive_with"}


def iris_auth(bot_id: int, token: str) -> str:
    # Accept either the raw Iris token or the full {bot_id}_{iris_token}.
    prefix = f"{bot_id}_"
    return token if token.startswith(prefix) else prefix + token

SCHEMA = """
CREATE TABLE IF NOT EXISTS iris_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL,
    owner_name TEXT NOT NULL,
    title TEXT NOT NULL,
    goal_cents INTEGER NOT NULL CHECK(goal_cents > 0),
    raised_cents INTEGER NOT NULL DEFAULT 0,
    code TEXT UNIQUE,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','closed','paying','paid','review')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    payout_tx_id TEXT
);
CREATE INDEX IF NOT EXISTS iris_collections_chat
    ON iris_collections(chat_id,status,id);
CREATE TABLE IF NOT EXISTS iris_collection_events (
    event_id INTEGER PRIMARY KEY,
    collection_id INTEGER REFERENCES iris_collections(id),
    sender_id INTEGER,
    amount_cents INTEGER,
    comment TEXT,
    received_at TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iris_collection_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iris_collection_recovered (
    transaction_id INTEGER PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES iris_collections(id),
    amount_cents INTEGER NOT NULL,
    recovered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iris_collection_manual_audit (
    transaction_id INTEGER PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES iris_collections(id),
    actor_id INTEGER NOT NULL,
    sender_id INTEGER,
    amount_cents INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iris_collection_prompts (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    collection_id INTEGER NOT NULL,
    prompt_message_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,user_id)
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def initialize(db):
    """Add tables without changing birthday or wishlist data."""
    async with db.lock:
        await db.connection.executescript(SCHEMA)
        await db.connection.commit()
    await auto.initialize(db)


def parse_amount(raw):
    """Use whole Iris sweets for chat UI; keep balances in hundredths internally."""
    value = raw.replace(" ", "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,6}", value):
        raise ValueError("Укажи целое число ирисок от 1 до 1 000 000.")
    amount = int(value)
    if amount > MAX_AMOUNT:
        raise ValueError("Максимум: 1 000 000 ирисок.")
    return amount


def inbound_cents(raw):
    try:
        value = Decimal(str(raw)) * 100
        if not value.is_finite() or value <= 0 or value != value.to_integral_value():
            return None
        return int(value)
    except (InvalidOperation, ValueError, TypeError):
        return None


def iris_link(bot_id: int, amount: int, code: str) -> str:
    """Iris confirms every transfer; comment identifies the collection."""
    if not (1 <= amount <= MAX_AMOUNT) or not re.fullmatch(r"c[0-9]+r[a-f0-9]{12}", code):
        raise ValueError("Invalid payment link parameters")
    payload = f"give_bot{bot_id}_{amount}_{code}"
    if len(payload) > 64:
        raise ValueError("Iris deep-link payload too long")
    return "https://t.me/iris_cm_bot?" + urlencode({"start": payload})


def collection_keyboard(row: dict, bot_id: int) -> InlineKeyboardMarkup:
    # These are authenticated callbacks, NOT direct Iris URLs.
    rows = []
    if row["status"] == "open" and row["raised_cents"] < row["goal_cents"]:
        rows.extend([
            [InlineKeyboardButton(text="🍬 1", callback_data=f"ic:amount:{row['id']}:1"),
             InlineKeyboardButton(text="🍬 5", callback_data=f"ic:amount:{row['id']}:5")],
            [InlineKeyboardButton(text="🍬 10", callback_data=f"ic:amount:{row['id']}:10"),
             InlineKeyboardButton(text="🍬 50", callback_data=f"ic:amount:{row['id']}:50")],
            [InlineKeyboardButton(text="💎 100", callback_data=f"ic:amount:{row['id']}:100"),
             InlineKeyboardButton(text="✏️ Своя сумма", callback_data=f"ic:own:{row['id']}")],
        ])
    rows.append([InlineKeyboardButton(text="🔄 Обновить сбор",
                                      callback_data=f"ic:refresh:{row['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_sweets(cents: int) -> str:
    return str(cents // 100) if cents % 100 == 0 else f"{cents / 100:.2f}"


def collection_text(row: dict, donors: int) -> str:
    raised, goal = row["raised_cents"], row["goal_cents"]
    filled = min(10, raised * 10 // goal)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    owner = (f'<a href="tg://user?id={row["owner_id"]}">'
             f'{html.escape(row["owner_name"])}</a>')
    status = {"open": "🟢 Принимает взносы", "closed": "🔒 Закрыт",
              "paying": "⏳ Выплата", "paid": "✅ Выплачен",
              "review": "⚠️ Проверка выплаты"}[row["status"]]
    completed = row["raised_cents"] >= row["goal_cents"]
    completion = "🏆 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n" if completed else ""
    return (
        f"💝 <b>СБОР ИРИСОК №{row['id']}</b>\n\n"
        f"{completion}"
        f"🎯 <b>{html.escape(row['title'])}</b>\n"
        f"👤 Организатор: {owner}\n"
        f"💰 Собрано: <b>{format_sweets(raised)} / {format_sweets(goal)} 🍬</b>\n"
        f"{bar} {min(100, raised * 100 // goal)}%\n"
        f"👥 Поддержали: {donors}\n"
        f"📌 Статус: {status}\n\n"
        ("<i>Сбор завершён. Спасибо участникам! "
         "Ириски находятся в мешке бота до выплаты организатору.</i>"
         if row["status"] != "open" else
         "Нажми на сумму, получи персональную ссылку и подтверди перевод в Ирисе.\n"
         "<i>Бот сверяет ID отправителя, сумму и время подтверждённого перевода. "
         "На сбор поступает сумма после комиссии Ириса.</i>")
    )


async def get_collection(db, collection_id: int, chat_id: int | None = None):
    if chat_id is None:
        row = await db.one("SELECT * FROM iris_collections WHERE id=?", (collection_id,))
    else:
        row = await db.one(
            "SELECT * FROM iris_collections WHERE id=? AND chat_id=?",
            (collection_id, chat_id),
        )
    return row


async def donor_count(db, collection_id: int) -> int:
    result = await db.one(
        """SELECT COUNT(DISTINCT donor_id) AS count FROM (
             SELECT sender_id AS donor_id
               FROM iris_collection_events
              WHERE collection_id=? AND outcome='credited' AND sender_id IS NOT NULL
             UNION
             SELECT sender_id AS donor_id
               FROM iris_collection_manual_audit
              WHERE collection_id=? AND sender_id IS NOT NULL
             UNION
             SELECT payer_id AS donor_id
               FROM iris_payment_intents
              WHERE collection_id=? AND status='matched'
           )""",
        (collection_id, collection_id, collection_id),
    )
    return int(result["count"])


async def update_card(db, bot, row, bot_id):
    if not row or not row["message_id"]:
        return
    try:
        await bot.edit_message_text(
            chat_id=row["chat_id"], message_id=row["message_id"],
            text=collection_text(row, await donor_count(db, row["id"])),
            reply_markup=collection_keyboard(row, bot_id), parse_mode="HTML",
        )
    except TelegramAPIError as exc:
        # "message is not modified" also lands here. Never repeat an Iris transfer.
        log.debug("Could not update collection card %s: %s", row["id"], type(exc).__name__)


async def create_collection(db, chat_id: int, user, title: str, goal: int):
    created = now_iso()
    async with db.lock:
        async with db.connection.execute(
            """SELECT COUNT(*) FROM iris_collections
               WHERE chat_id=? AND owner_id=? AND status='open'""",
            (chat_id, user.id),
        ) as cursor:
            if (await cursor.fetchone())[0] >= 2:
                raise ValueError("У тебя уже есть два открытых сбора в этом чате.")
        async with db.connection.execute(
            """SELECT COUNT(*) FROM iris_collections
               WHERE chat_id=? AND owner_id=? AND created_at>=?""",
            (chat_id, user.id, (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()),
        ) as cursor:
            if (await cursor.fetchone())[0] >= 3:
                raise ValueError("Можно создать не больше трёх сборов за сутки.")
        async with db.connection.execute(
            "SELECT COUNT(*) FROM iris_collections WHERE chat_id=? AND status='open'",
            (chat_id,),
        ) as cursor:
            if (await cursor.fetchone())[0] >= 15:
                raise ValueError("В этом чате уже 15 активных сборов.")
        cursor = await db.connection.execute(
            """INSERT INTO iris_collections
               (chat_id,owner_id,owner_name,title,goal_cents,created_at)
               VALUES(?,?,?,?,?,?)""",
            (chat_id, user.id, (user.full_name or "Участник")[:100],
             title, goal * 100, created),
        )
        collection_id = cursor.lastrowid
        code = f"c{collection_id}r{secrets.token_hex(6)}"
        await db.connection.execute(
            "UPDATE iris_collections SET code=? WHERE id=?", (code, collection_id),
        )
        await db.connection.commit()
    return await get_collection(db, collection_id)


async def send_personal_link(message, db, row, payer, amount: int, bot_id: int):
    """Record a payment intent for the real Telegram user before revealing URL."""
    intent_id = await auto.begin_intent(db, row, payer.id, amount)
    mention = f'<a href="tg://user?id={payer.id}">{html.escape(payer.full_name)}</a>'
    try:
        sent = await message.answer(
            f"💝 {mention}, взнос в сбор <b>№{row['id']}</b>\n"
            f"🍬 Сумма: <b>{amount}</b>\n\n"
            "Подтверди перевод в официальном Ирисе. Бот сам проверит "
            "отправителя, сумму и время, затем обновит сбор.\n"
            "⌛ Ссылка для учёта активна 15 минут. "
            "Ею должен пользоваться только тот, кто запросил перевод.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"💝 Перевести {amount} 🍬 через Ирис",
                    url=iris_link(bot_id, amount, row["code"]))],
                [InlineKeyboardButton(text="❌ Отменить перевод",
                                      callback_data=f"ic:stop:{intent_id}")],
            ]),
        )
    except Exception:
        await auto.cancel_intent(db, intent_id, payer.id)
        raise
    await db.execute(
        "UPDATE iris_payment_intents SET link_message_id=? WHERE id=?",
        (sent.message_id, intent_id),
    )


@router.message(Command("collect"), F.chat.type.in_(GROUPS))
async def create_command(message: Message, db, settings, bot_id: int):
    if not message.from_user:
        return
    if not settings.iris_connect_token:
        await message.answer("💝 Сборы пока не подключены к Iris Connect. Обратись к владельцу бота.")
        return
    content = (message.text or "").partition(" ")[2].strip()
    fields = [part.strip() for part in content.split("|")]
    if len(fields) != 2 or not (3 <= len(fields[0]) <= 100):
        await message.answer(
            "💝 Создать сбор может каждый участник.\n"
            "Формат: <code>/collect На свадьбу | 5000</code>\n"
            "Цель — целое число ирисок.", parse_mode="HTML",
        )
        return
    title = fields[0]
    if any(ord(c) < 32 for c in title):
        await message.answer("Название должно быть одной строкой.")
        return
    try:
        goal = parse_amount(fields[1])
        await db.ensure_chat(message.chat.id, message.chat.title or "Наш чат",
                             settings.default_timezone, settings.default_birthday_time)
        row = await create_collection(db, message.chat.id, message.from_user, title, goal)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    try:
        sent = await message.answer(
            collection_text(row, 0), parse_mode="HTML",
            reply_markup=collection_keyboard(row, bot_id),
        )
    except TelegramAPIError:
        await db.execute("UPDATE iris_collections SET status='closed' WHERE id=?", (row["id"],))
        raise
    await db.execute("UPDATE iris_collections SET message_id=? WHERE id=?",
                     (sent.message_id, row["id"]))


@router.message(Command("collections"), F.chat.type.in_(GROUPS))
async def list_command(message: Message, db):
    rows = await db.all(
        """SELECT id,title,raised_cents,goal_cents FROM iris_collections
           WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 15""",
        (message.chat.id,),
    )
    if not rows:
        await message.answer("Пока нет открытых сборов. Создай: /collect На подарок | 1000")
        return
    body = "\n".join(
        f"#{r['id']} · {html.escape(r['title'])} — "
        f"{format_sweets(r['raised_cents'])}/{format_sweets(r['goal_cents'])} 🍬"
        for r in rows
    )
    await message.answer("💝 <b>Открытые сборы</b>\n\n" + body +
                         "\n\nКарточка: <code>/collect_info ID</code>", parse_mode="HTML")


def command_id(message: Message):
    raw = (message.text or "").partition(" ")[2].strip()
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,9}", raw) else None


@router.message(Command("collect_info"), F.chat.type.in_(GROUPS))
async def info_command(message: Message, db, bot_id: int):
    collection_id = command_id(message)
    row = await get_collection(db, collection_id, message.chat.id) if collection_id else None
    if not row:
        await message.answer("Сбор не найден. Используй /collections.")
        return
    await message.answer(collection_text(row, await donor_count(db, row["id"])),
                         parse_mode="HTML", reply_markup=collection_keyboard(row, bot_id))


async def is_admin(message: Message, user_id: int):
    try:
        member = await message.bot.get_chat_member(message.chat.id, user_id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except TelegramAPIError:
        return False


@router.message(Command("collect_close"), F.chat.type.in_(GROUPS))
async def close_command(message: Message, db, bot_id: int):
    if not message.from_user:
        return
    collection_id = command_id(message)
    row = await get_collection(db, collection_id, message.chat.id) if collection_id else None
    if not row:
        await message.answer("Сбор не найден.")
        return
    if row["owner_id"] != message.from_user.id and not await is_admin(message, message.from_user.id):
        await message.answer("Закрывать сбор может организатор или администратор чата.")
        return
    if row["status"] != "open":
        await message.answer("Этот сбор уже закрыт.")
        return
    await db.execute(
        "UPDATE iris_collections SET status='closed',closed_at=? WHERE id=? AND status='open'",
        (now_iso(), row["id"]),
    )
    await update_card(db, message.bot, await get_collection(db, row["id"]), bot_id)
    await message.answer(
        f"🔒 Сбор #{row['id']} закрыт. Старые ссылки в Ирисе технически остаются "
        "действительными; поздние переводы потребуют ручной сверки. "
        "За выплатой обращайтесь к администратору."
    )


@router.callback_query(F.data.startswith("ic:"))
async def collection_callback(callback: CallbackQuery, db, settings, bot_id: int):
    if (callback.data or "") == "ic:help":
        if callback.message and callback.message.chat.type in GROUPS:
            await callback.message.answer(
                "💝 <b>Сборы ирисок прямо в чате</b>\n\n"
                "Создать сбор может любой участник:\n"
                "<code>/collect На свадьбу | 5000</code>\n\n"
                "📋 Список: /collections\n"
                "🔒 Закрыть свой: /collect_close ID\n"
                "Взносы учитываются автоматически, без ручной привязки.",
                parse_mode="HTML",
            )
            await callback.answer()
        else:
            await callback.answer("Открой меню в группе.", show_alert=True)
        return
    if not callback.message or callback.message.chat.type not in GROUPS:
        await callback.answer("Открой сбор в чате.", show_alert=True)
        return
    pieces = (callback.data or "").split(":")
    if len(pieces) == 3 and pieces[1] == "stop" and pieces[2].isdigit():
        stopped = await auto.cancel_intent(db, int(pieces[2]), callback.from_user.id)
        await callback.answer("Перевод отменён." if stopped else "Ссылка уже использована или устарела.")
        if stopped:
            try:
                await callback.message.delete()
            except TelegramAPIError:
                pass
        return
    if (len(pieces) == 4 and pieces[1] == "amount" and
            pieces[2].isdigit() and pieces[3].isdigit()):
        row = await get_collection(db, int(pieces[2]), callback.message.chat.id)
        if not row or row["status"] != "open":
            await callback.answer("Сбор закрыт.", show_alert=True)
            return
        try:
            member = await callback.bot.get_chat_member(row["chat_id"], callback.from_user.id)
            if (member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
                    or (member.status == ChatMemberStatus.RESTRICTED and not member.is_member)):
                await callback.answer("Только для участников чата.", show_alert=True)
                return
        except TelegramAPIError:
            await callback.answer("Не удалось проверить участие.", show_alert=True)
            return
        try:
            amount = parse_amount(pieces[3])
            await send_personal_link(callback.message, db, row, callback.from_user,
                                     amount, bot_id)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer("Персональная кнопка отправлена в чат.")
        return
    if len(pieces) != 3 or not pieces[2].isdigit():
        await callback.answer("Неверная кнопка.", show_alert=True)
        return
    action, collection_id = pieces[1], int(pieces[2])
    row = await get_collection(db, collection_id, callback.message.chat.id)
    if not row:
        await callback.answer("Сбор не найден.", show_alert=True)
        return
    if action == "refresh":
        await update_card(db, callback.bot, row, bot_id)
        await callback.answer("Данные обновлены.")
        return
    if action != "own" or row["status"] != "open":
        await callback.answer("Сбор закрыт.", show_alert=True)
        return
    try:
        member = await callback.bot.get_chat_member(row["chat_id"], callback.from_user.id)
        if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} or (
            member.status == ChatMemberStatus.RESTRICTED and not member.is_member
        ):
            await callback.answer("Кнопка доступна участникам чата.", show_alert=True)
            return
    except TelegramAPIError:
        await callback.answer("Не удалось проверить участие в чате.", show_alert=True)
        return
    name = html.escape(callback.from_user.full_name or "Участник")
    prompt = await callback.message.answer(
        f'✏️ <a href="tg://user?id={callback.from_user.id}">{name}</a>, '
        "ответь <b>на это сообщение</b> целым числом от 1 до 1 000 000.",
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Количество ирисок"),
    )
    await db.execute(
        """INSERT INTO iris_collection_prompts
           (chat_id,user_id,collection_id,prompt_message_id,expires_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(chat_id,user_id) DO UPDATE SET
           collection_id=excluded.collection_id,
           prompt_message_id=excluded.prompt_message_id,
           expires_at=excluded.expires_at""",
        (row["chat_id"], callback.from_user.id, row["id"], prompt.message_id,
         (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()),
    )
    await callback.answer()


@router.message(F.reply_to_message, F.text, F.chat.type.in_(GROUPS))
async def custom_amount_reply(message: Message, db, bot_id: int):
    if not message.from_user:
        return
    prompt = await db.one(
        "SELECT * FROM iris_collection_prompts WHERE chat_id=? AND user_id=?",
        (message.chat.id, message.from_user.id),
    )
    if not prompt or message.reply_to_message.message_id != prompt["prompt_message_id"]:
        return
    await db.execute(
        "DELETE FROM iris_collection_prompts WHERE chat_id=? AND user_id=?",
        (message.chat.id, message.from_user.id),
    )
    if prompt["expires_at"] < now_iso():
        await message.answer("⌛ Время ввода истекло. Нажми «Своя сумма» снова.")
        return
    row = await get_collection(db, prompt["collection_id"], message.chat.id)
    if not row or row["status"] != "open":
        await message.answer("Сбор уже закрыт.")
        return
    try:
        amount = parse_amount(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    try:
        await send_personal_link(message, db, row, message.from_user, amount, bot_id)
    except ValueError as exc:
        await message.answer(str(exc))


async def ingest_event(db, event: dict) -> int | None:
    """Exactly-once ledger insert. An unmatched transfer is NEVER invented as a donation."""
    event_id = int(event["id"])
    data = event.get("object") if event.get("type") == "sweets_log" else None
    changed = None
    async with db.lock:
        async with db.connection.execute(
            "SELECT 1 FROM iris_collection_events WHERE event_id=?", (event_id,)
        ) as cursor:
            exists = await cursor.fetchone()
        if isinstance(data, dict) and not exists and data.get("type") in INCOMING_TYPES:
            comment = str(data.get("comment") or "")
            cents = inbound_cents(data.get("amount"))
            sender = data.get("peer_id")
            sender = int(sender) if isinstance(sender, int) and sender > 0 else None
            outcome, collection = "unmatched", None
            tx_id = data.get("id")
            if isinstance(tx_id, int):
                async with db.connection.execute(
                    "SELECT 1 FROM iris_collection_recovered WHERE transaction_id=?",
                    (tx_id,),
                ) as cursor:
                    if await cursor.fetchone():
                        outcome = "already_recovered"
            if cents and outcome != "already_recovered":
                async with db.connection.execute(
                    """SELECT * FROM iris_collections WHERE code=? AND status IN ('open','closed')""",
                    (comment,),
                ) as cursor:
                    collection = await cursor.fetchone()
                if collection:
                    # Prevent credits for transfers predating the creation of this collection.
                    event_time = data.get("date") or event.get("date") or 0
                    try:
                        event_epoch = float(event_time)
                    except (ValueError, TypeError, OverflowError):
                        event_epoch = 0
                    if event_epoch > 10**11:
                        event_epoch /= 1000
                    created = datetime.fromisoformat(collection["created_at"]).timestamp()
                    if event_epoch >= created - 120:
                        outcome = "credited"
                    else:
                        collection = None
            await db.connection.execute(
                """INSERT INTO iris_collection_events
                   (event_id,collection_id,sender_id,amount_cents,comment,received_at,outcome)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, collection["id"] if outcome == "credited" else None,
                 sender, cents, comment, now_iso(), outcome),
            )
            if outcome == "credited":
                await db.connection.execute(
                    "UPDATE iris_collections SET raised_cents=raised_cents+? WHERE id=?",
                    (cents, collection["id"]),
                )
                changed = collection["id"]
        # Persist the cursor with the ledger update, so a restart never doubles credits.
        await db.connection.execute(
            """INSERT INTO iris_collection_meta(key,value) VALUES('cursor',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(event_id),),
        )
        await db.connection.commit()
    return changed


async def poll_iris(db, bot, bot_id: int, token: str):
    """Called by the existing background scheduler, not by Mini App."""
    if not token:
        return
    state = await db.one("SELECT value FROM iris_collection_meta WHERE key='cursor'")
    cursor = int(state["value"]) if state else 0
    url = f"{API_ROOT}/{iris_auth(bot_id, token)}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            reply = await client.get(url, params={"offset": cursor, "limit": 100})
            data = reply.json()
        if reply.status_code != 200 or not isinstance(data.get("result"), list):
            # Never log the request URL (it contains the API secret).
            await set_poll_status(db, f"HTTP {reply.status_code} / error")
            log.warning("Iris update request unsuccessful: HTTP %s", reply.status_code)
            return
    except (httpx.HTTPError, ValueError):
        await set_poll_status(db, "network / invalid JSON")
        log.warning("Iris update request failed; will try again")
        return
    await set_poll_status(db, f"OK, events={len(data['result'])}")
    changed_ids = set()
    try:
        for event in sorted(data["result"], key=lambda x: int(x["id"])):
            if int(event["id"]) <= cursor:
                continue
            changed = await ingest_event(db, event)
            cursor = int(event["id"])
            if changed:
                changed_ids.add(changed)
    except (KeyError, TypeError, ValueError):
        log.exception("Invalid Iris event; cursor was not advanced for the failed event")
    # One history request per cycle. Legacy exact-code recovery is preserved,
    # but the NEW receipt matcher never relies on comments being preserved.
    to_delete = []
    try:
        status, history = await get_iris_history(bot_id, token)
        if status == 200:
            legacy = await recover_zero_collections(db, bot_id, token, history=history)
            changed_ids.update(legacy)
            matched, completed_links = await auto.reconcile(db, history)
            changed_ids.update(matched)
            to_delete.extend(completed_links)
            to_delete.extend(await auto.expire_intents(db))
        else:
            log.warning("Iris history API unavailable: HTTP %s", status)
    except (httpx.HTTPError, ValueError, TypeError):
        log.warning("Iris history sync failed; no guessed credits")
    # Close previously completed campaigns created with the old workflow.
    changed_ids.update(await auto.close_completed(db))
    for collection_id in changed_ids:
        await update_card(db, bot, await get_collection(db, collection_id), bot_id)
    for chat_id, message_id in to_delete:
        if not message_id:
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass


async def set_poll_status(db, value: str):
    # Expose only an HTTP result or short status, never a token, URL or payload.
    await db.execute(
        """INSERT INTO iris_collection_meta(key,value) VALUES('poll_status',?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (f"{now_iso()} | {value[:90]}",),
    )


async def get_iris_history(bot_id: int, token: str) -> tuple[int, list]:
    url = f"{API_ROOT}/{iris_auth(bot_id, token)}/pocket/sweets/history"
    async with httpx.AsyncClient(timeout=15) as client:
        reply = await client.get(url, params={"offset": 0, "limit": 200})
        try:
            data = reply.json()
        except ValueError:
            data = None
    if reply.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("result"), list):
        return reply.status_code, []
    return 200, data["result"]


async def recover_zero_collections(db, bot_id: int, token: str, history=None) -> set[int]:
    """Backfill exact-comment receipts where no event was previously credited.

    The previous version skipped some incoming types while advancing its event
    cursor. Reconcile only collections whose TOTAL IS ZERO, to avoid counting
    existing (legacy) credits twice. Never attribute transfers without a code.
    """
    rows = await db.all(
        """SELECT * FROM iris_collections
           WHERE raised_cents=0 AND status IN ('open','closed')
           ORDER BY id DESC LIMIT 25"""
    )
    if not rows:
        return set()
    if history is None:
        status, history = await get_iris_history(bot_id, token)
        if status != 200:
            return set()
    by_code = {r["code"]: r for r in rows}
    recovered = set()
    for item in history:
        if not isinstance(item, dict) or item.get("type") not in INCOMING_TYPES:
            continue
        row = by_code.get(str(item.get("comment") or ""))
        cents = inbound_cents(item.get("amount"))
        transaction_id = item.get("id")
        if not row or not cents or not isinstance(transaction_id, int) or transaction_id <= 0:
            continue
        try:
            event_epoch = float(item.get("date") or 0)
            if event_epoch > 10**11:
                event_epoch /= 1000
            if event_epoch < datetime.fromisoformat(row["created_at"]).timestamp() - 120:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        # Only campaigns with no earlier credits, and only transaction IDs
        # not used in previous recoveries, are safe to backfill.
        async with db.lock:
            async with db.connection.execute(
                "SELECT raised_cents FROM iris_collections WHERE id=?", (row["id"],)
            ) as cursor:
                current = await cursor.fetchone()
            # A campaign with history already recovered in this pass can
            # accept additional distinct records, but legacy credits cannot.
            if not current or (current["raised_cents"] and row["id"] not in recovered):
                continue
            cursor = await db.connection.execute(
                """INSERT OR IGNORE INTO iris_collection_recovered
                   (transaction_id,collection_id,amount_cents,recovered_at)
                   VALUES(?,?,?,?)""",
                (transaction_id, row["id"], cents, now_iso()),
            )
            if cursor.rowcount:
                await db.connection.execute(
                    "UPDATE iris_collections SET raised_cents=raised_cents+? WHERE id=?",
                    (cents, row["id"]),
                )
                recovered.add(row["id"])
            await db.connection.commit()
    return recovered


@router.message(Command("collect_status"), F.chat.type.in_(GROUPS))
async def collection_status(message: Message, db, settings, bot_id: int):
    """Admin-only diagnostics with no token, URL, user IDs or secret comments."""
    if not message.from_user or not await is_admin(message, message.from_user.id):
        await message.answer("Диагностика доступна администраторам чата.")
        return
    state = await db.one("SELECT value FROM iris_collection_meta WHERE key='poll_status'")
    cursor = await db.one("SELECT value FROM iris_collection_meta WHERE key='cursor'")
    stats = await db.all(
        """SELECT outcome,COUNT(*) AS n FROM iris_collection_events
           GROUP BY outcome"""
    )
    recovered = await db.one(
        "SELECT COUNT(*) AS n FROM iris_collection_recovered"
    )
    lines = [
        "🛠 <b>Диагностика сборов Ириса</b>",
        f"Токен: {'✅ указан' if settings.iris_connect_token else '❌ отсутствует'}",
        f"Синхронизация: {html.escape(state['value'] if state else 'нет попыток')}",
        f"Последнее событие: {cursor['value'] if cursor else 'нет'}",
        "События: " + (", ".join(
            f"{html.escape(r['outcome'])}={r['n']}" for r in stats
        ) or "нет"),
        f"Восстановлено из истории: {recovered['n'] if recovered else 0}",
    ]
    collection_id = command_id(message)
    if collection_id:
        row = await get_collection(db, collection_id, message.chat.id)
        if not row:
            lines.append("Указанный сбор не найден в этом чате.")
        else:
            lines.append(f"Сбор #{row['id']}: {format_sweets(row['raised_cents'])} 🍬")
            if settings.iris_connect_token:
                try:
                    code, history = await get_iris_history(bot_id, settings.iris_connect_token)
                    inbound = [
                        x for x in history if isinstance(x, dict)
                        and x.get("type") in INCOMING_TYPES
                    ]
                    matches = [
                        x for x in inbound if str(x.get("comment") or "") == row["code"]
                    ]
                    lines.append(
                        f"История Ириса: HTTP {code}, входящих={len(inbound)}, "
                        f"с кодом этого сбора={len(matches)}"
                    )
                    if inbound and not matches:
                        lines.append(
                            "ℹ️ Iris не передал код сбора. Новые платежи "
                            "автоматически связываются с персональной заявкой "
                            "по ID отправителя, сумме и времени."
                        )
                    pending = await db.one(
                        """SELECT COUNT(*) AS n FROM iris_payment_intents
                           WHERE collection_id=? AND status='pending'""",
                        (row["id"],),
                    )
                    lines.append(f"Ожидающих переводов: {pending['n'] if pending else 0}")
                except (httpx.HTTPError, ValueError, TypeError):
                    lines.append("История Ириса: ошибка соединения")
    await message.answer("\n".join(lines), parse_mode="HTML")



@router.message(Command("collect_pay"), F.chat.type.in_(GROUPS))
async def admin_payout(message: Message, db, settings, bot_id: int):
    """Admin-only, one-shot payout; uncertain API failures never auto-retry."""
    if not message.from_user or not await is_admin(message, message.from_user.id):
        await message.answer("Выплату может запускать только администратор чата.")
        return
    collection_id = command_id(message)
    row = await get_collection(db, collection_id, message.chat.id) if collection_id else None
    if not row:
        await message.answer("Сбор не найден.")
        return
    if row["status"] != "closed" or row["raised_cents"] < 100:
        await message.answer("Нужен закрытый сбор с подтверждёнными ирисками; "
                             "повторную выплату бот не допускает.")
        return
    if datetime.fromisoformat(row["closed_at"]) + timedelta(hours=24) > datetime.now(timezone.utc):
        await message.answer("Для сверки переводов выплата доступна через 24 часа после закрытия.")
        return
    async with db.lock:
        result = await db.connection.execute(
            "UPDATE iris_collections SET status='paying' WHERE id=? AND status='closed'",
            (row["id"],),
        )
        await db.connection.commit()
        if result.rowcount != 1:
            await message.answer("Выплата уже была начата.")
            return
    amount = Decimal(row["raised_cents"]) / 100
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            reply = await client.get(
                f"{API_ROOT}/{iris_auth(bot_id, settings.iris_connect_token)}/pocket/sweets/give",
                params={"user_id": row["owner_id"], "amount": str(amount),
                        "comment": f"collection_{row['id']}"},
            )
            payload = reply.json()
        tx_id = payload.get("result")
        if reply.status_code != 200 or not isinstance(tx_id, int):
            raise ValueError("Unconfirmed Iris payout")
    except (httpx.HTTPError, ValueError, TypeError):
        # The request could have succeeded despite a timeout. Never auto-retry.
        await db.execute("UPDATE iris_collections SET status='review' WHERE id=? AND status='paying'",
                         (row["id"],))
        await message.answer(
            f"⚠️ Выплата по сбору #{row['id']} требует ручной проверки в мешке Ириса. "
            "Не повторяй перевод, пока не сверишь историю операций."
        )
    else:
        await db.execute(
            "UPDATE iris_collections SET status='paid',payout_tx_id=? WHERE id=? AND status='paying'",
            (str(tx_id), row["id"]),
        )
        await message.answer(f"✅ По сбору #{row['id']} отправлено {format_sweets(row['raised_cents'])} 🍬 "
                             "организатору. Операция записана.")
    await update_card(db, message.bot, await get_collection(db, row["id"]), bot_id)
