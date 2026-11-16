"""Simple persistent SQLite repository; WAL and serialized writes."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS chats (
  chat_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  tz TEXT NOT NULL,
  greeting_time TEXT NOT NULL DEFAULT '09:00',
  pin_enabled INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  panel_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS birthdays (
  chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL,
  first_name TEXT NOT NULL,
  username TEXT,
  day INTEGER NOT NULL,
  month INTEGER NOT NULL,
  birth_year INTEGER,
  show_age INTEGER NOT NULL DEFAULT 0,
  visible INTEGER NOT NULL DEFAULT 1,
  celebrate INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS birthdays_chat_idx ON birthdays(chat_id, month, day);
CREATE TABLE IF NOT EXISTS birthday_deliveries (
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  occurrence_year INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'sending',
  message_id INTEGER,
  sent_at TEXT,
  unpin_after TEXT,
  unpinned_at TEXT,
  PRIMARY KEY(chat_id, user_id, occurrence_year)
);
"""


def item(row):
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: str):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def one(self, sql: str, params=()):
        async with self.connection.execute(sql, params) as cursor:
            return item(await cursor.fetchone())

    async def all(self, sql: str, params=()):
        async with self.connection.execute(sql, params) as cursor:
            return [dict(x) for x in await cursor.fetchall()]

    async def execute(self, sql: str, params=()) -> None:
        async with self.lock:
            await self.connection.execute(sql, params)
            await self.connection.commit()

    async def ensure_chat(self, chat_id: int, title: str, tz: str, greeting_time: str) -> None:
        await self.execute(
            """INSERT INTO chats(chat_id,title,tz,greeting_time,active) VALUES(?,?,?,?,1)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=1""",
            (chat_id, title, tz, greeting_time),
        )

    async def get_chat(self, chat_id: int):
        return await self.one("SELECT * FROM chats WHERE chat_id=? AND active=1", (chat_id,))

    async def get_chats(self):
        return await self.all("SELECT * FROM chats WHERE active=1")

    async def set_chat_active(self, chat_id: int, active: bool):
        await self.execute("UPDATE chats SET active=? WHERE chat_id=?", (int(active), chat_id))

    async def set_panel(self, chat_id: int, message_id: int):
        await self.execute("UPDATE chats SET panel_message_id=? WHERE chat_id=?", (message_id, chat_id))

    async def set_settings(self, chat_id: int, tz: str, greeting_time: str, pin_enabled: bool):
        await self.execute(
            "UPDATE chats SET tz=?, greeting_time=?, pin_enabled=? WHERE chat_id=?",
            (tz, greeting_time, int(pin_enabled), chat_id),
        )

    async def get_birthday(self, chat_id: int, user_id: int):
        return await self.one(
            "SELECT * FROM birthdays WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )

    async def put_birthday(
        self, chat_id: int, user: dict, day: int, month: int,
        birth_year: int | None, show_age: bool, visible: bool, celebrate: bool
    ):
        await self.execute(
            """INSERT INTO birthdays
                (chat_id,user_id,first_name,username,day,month,birth_year,show_age,visible,celebrate,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                first_name=excluded.first_name, username=excluded.username,
                day=excluded.day, month=excluded.month, birth_year=excluded.birth_year,
                show_age=excluded.show_age, visible=excluded.visible,
                celebrate=excluded.celebrate, updated_at=excluded.updated_at""",
            (
                chat_id, user["id"], user.get("first_name", "Участник")[:128],
                (user.get("username") or "")[:64] or None, day, month, birth_year,
                int(show_age), int(visible), int(celebrate),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def remove_birthday(self, chat_id: int, user_id: int):
        await self.execute("DELETE FROM birthdays WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def get_visible_birthdays(self, chat_id: int):
        return await self.all(
            """SELECT user_id, first_name, username, day, month,
                      CASE WHEN show_age=1 THEN birth_year ELSE NULL END AS birth_year
               FROM birthdays WHERE chat_id=? AND visible=1 ORDER BY month,day,first_name""",
            (chat_id,),
        )

    async def get_celebrants(self, chat_id: int):
        return await self.all(
            "SELECT * FROM birthdays WHERE chat_id=? AND celebrate=1", (chat_id,)
        )

    async def claim_delivery(self, chat_id: int, user_id: int, year: int) -> bool:
        async with self.lock:
            cursor = await self.connection.execute(
                """INSERT OR IGNORE INTO birthday_deliveries
                    (chat_id,user_id,occurrence_year,state) VALUES(?,?,?,'sending')""",
                (chat_id, user_id, year),
            )
            await self.connection.commit()
            return cursor.rowcount == 1

    async def sent_delivery(self, chat_id: int, user_id: int, year: int, message_id: int,
                            unpin_after: str | None):
        await self.execute(
            """UPDATE birthday_deliveries
               SET state='sent', message_id=?, sent_at=?, unpin_after=?
               WHERE chat_id=? AND user_id=? AND occurrence_year=?""",
            (message_id, datetime.now(timezone.utc).isoformat(), unpin_after, chat_id, user_id, year),
        )

    async def discard_claim(self, chat_id: int, user_id: int, year: int):
        await self.execute(
            """DELETE FROM birthday_deliveries
               WHERE chat_id=? AND user_id=? AND occurrence_year=? AND state='sending'""",
            (chat_id, user_id, year),
        )

    async def due_unpins(self, now_utc: str):
        return await self.all(
            """SELECT * FROM birthday_deliveries
               WHERE state='sent' AND unpin_after IS NOT NULL
                     AND unpinned_at IS NULL AND unpin_after<=?""",
            (now_utc,),
        )

    async def mark_unpinned(self, chat_id: int, user_id: int, year: int):
        await self.execute(
            """UPDATE birthday_deliveries SET unpinned_at=?
               WHERE chat_id=? AND user_id=? AND occurrence_year=?""",
            (datetime.now(timezone.utc).isoformat(), chat_id, user_id, year),
        )
