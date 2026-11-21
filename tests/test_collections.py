"""Offline regression tests: receipts are credited once and never across chats."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from app.collections import (
    create_collection, collection_text, get_collection, inbound_cents, ingest_event,
    initialize, iris_link, parse_amount,
)
from app.db import Database


def test_link_validation():
    link = iris_link(123456789, 10, "c12rabcdef123456")
    assert urlsplit(link).hostname == "t.me"
    assert parse_qs(urlsplit(link).query)["start"] == [
        "give_bot123456789_10_c12rabcdef123456"
    ]
    for raw in ("", "0", "-5", "2.5", "1000001", "1a", "0.1"):
        try:
            parse_amount(raw)
            assert False, f"{raw!r} must be invalid"
        except ValueError:
            pass
    assert parse_amount("1 000") == 1000
    assert inbound_cents(1.5) == 150
    assert inbound_cents("3.01") == 301
    assert inbound_cents(-3) is None


def test_confirmed_receipts_and_chat_isolation(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "collections.sqlite3"))
        await db.connect()
        try:
            await initialize(db)
            await db.ensure_chat(-100111, "Чат 1", "Europe/Moscow", "09:00")
            await db.ensure_chat(-100222, "Чат 2", "Europe/Moscow", "09:00")
            user = SimpleNamespace(id=12345, full_name="<Свадьба>")
            fund = await create_collection(db, -100111, user, "На свадьбу", 5000)
            assert (await get_collection(db, fund["id"], -100222)) is None
            assert fund["raised_cents"] == 0
            assert "&lt;Свадьба&gt;" in collection_text(fund, 0)
            timestamp = int(datetime.now(timezone.utc).timestamp())
            event = {
                "id": 1, "type": "sweets_log", "date": timestamp,
                "object": {"type": "receive", "amount": 5.0,
                           "comment": fund["code"], "peer_id": 5566,
                           "date": timestamp},
            }
            assert await ingest_event(db, event) == fund["id"]
            assert (await get_collection(db, fund["id"]))["raised_cents"] == 500
            # A replay from Iris must not duplicate the donation.
            assert await ingest_event(db, event) is None
            assert (await get_collection(db, fund["id"]))["raised_cents"] == 500
            # A different comment cannot be assigned to the fundraising campaign.
            unknown = {
                "id": 2, "type": "sweets_log", "date": timestamp,
                "object": {"type": "receive", "amount": 50,
                           "comment": "something_else", "peer_id": 5566,
                           "date": timestamp},
            }
            assert await ingest_event(db, unknown) is None
            assert (await get_collection(db, fund["id"]))["raised_cents"] == 500
            # A receive_with event (often sent with donate score) is also a
            # confirmed incoming transfer. Missing peer_id may affect donor
            # count but must not drop confirmed funds.
            with_donate = {
                "id": 3, "type": "sweets_log", "date": timestamp,
                "object": {"id": 991, "type": "receive_with", "amount": 10,
                           "comment": fund["code"], "peer_id": None,
                           "date": timestamp},
            }
            assert await ingest_event(db, with_donate) == fund["id"]
            assert (await get_collection(db, fund["id"]))["raised_cents"] == 1500
            # Fractional amounts beyond supported precision and ancient
            # transactions must not silently inflate a collection.
            for eid, sender, amount, date in [
                (4, 5566, 0.001, timestamp),
                (5, 5566, 10, timestamp - 86400 * 5),
            ]:
                bad = {"id": eid, "type": "sweets_log", "date": date,
                       "object": {"type": "receive", "amount": amount,
                                  "comment": fund["code"], "peer_id": sender,
                                  "date": date}}
                assert await ingest_event(db, bad) is None
            assert (await get_collection(db, fund["id"]))["raised_cents"] == 1500
            result = await db.one("SELECT COUNT(*) AS n FROM iris_collection_events")
            assert result["n"] == 5
            cursor = await db.one(
                "SELECT value FROM iris_collection_meta WHERE key='cursor'"
            )
            assert cursor["value"] == "5"
            # The existing birthdays table stays available.
            assert await db.get_chat(-100111)
            assert await db.all("SELECT * FROM birthdays") == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_history_recovers_skipped_receipt_without_double_count(tmp_path, monkeypatch):
    async def scenario():
        from app import collections as col
        db = Database(str(tmp_path / "history.sqlite3"))
        await db.connect()
        try:
            await initialize(db)
            await db.ensure_chat(-100333, "Чат", "Europe/Moscow", "09:00")
            campaign = await create_collection(
                db, -100333, SimpleNamespace(id=77, full_name="Тест"), "На свадьбу", 5
            )
            timestamp = int(datetime.now(timezone.utc).timestamp())
            async def fake_history(bot_id, token):
                return 200, [
                    {"id": 901, "type": "receive_with", "amount": 1.0,
                     "peer_id": 22, "date": timestamp, "comment": campaign["code"]},
                    {"id": 902, "type": "receive", "amount": 1,
                     "peer_id": 23, "date": timestamp, "comment": campaign["code"]},
                ]
            monkeypatch.setattr(col, "get_iris_history", fake_history)
            assert await col.recover_zero_collections(db, 1234, "token") == {campaign["id"]}
            assert (await get_collection(db, campaign["id"]))["raised_cents"] == 200
            assert await col.recover_zero_collections(db, 1234, "token") == set()
            assert (await get_collection(db, campaign["id"]))["raised_cents"] == 200
            # A later delivery of the same update cannot count the receipt again.
            event = {"id": 50, "type": "sweets_log", "date": timestamp,
                     "object": {"id": 901, "type": "receive_with", "amount": 1,
                                "peer_id": 22, "date": timestamp,
                                "comment": campaign["code"]}}
            assert await ingest_event(db, event) is None
            assert (await get_collection(db, campaign["id"]))["raised_cents"] == 200
        finally:
            await db.close()
    asyncio.run(scenario())


def test_iris_auth_accepts_full_or_raw_token():
    from app.collections import iris_auth
    assert iris_auth(123, "abc") == "123_abc"
    assert iris_auth(123, "123_abc") == "123_abc"
