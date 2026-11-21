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


def test_callback_buttons_generate_intents_instead_of_direct_links(tmp_path):
    from app.collections import collection_keyboard
    row = {"id": 12, "status": "open", "raised_cents": 0, "goal_cents": 500,
           "code": "c12rabcdef123456"}
    kb = collection_keyboard(row, 999)
    assert kb.inline_keyboard[0][0].callback_data == "ic:amount:12:1"
    assert kb.inline_keyboard[0][0].url is None
    assert kb.inline_keyboard[2][1].callback_data == "ic:own:12"
    row["status"] = "closed"
    assert len(collection_keyboard(row, 999).inline_keyboard) == 1


def test_auto_intent_reconciliation_and_idempotency(tmp_path):
    from app import iris_autocollect as auto
    async def scenario():
        db = Database(str(tmp_path / "auto.sqlite3"))
        await db.connect()
        try:
            await initialize(db)
            await db.ensure_chat(-10010, "Чат А", "Europe/Moscow", "09:00")
            await db.ensure_chat(-10020, "Чат Б", "Europe/Moscow", "09:00")
            user = SimpleNamespace(id=101, full_name="Плательщик")
            first = await create_collection(db, -10010, user, "Свадьба", 5)
            second = await create_collection(db, -10020, user, "Подарок", 5)
            intent_id = await auto.begin_intent(db, first, 101, 5)
            assert isinstance(intent_id, int)
            # Same user must NOT have two payments in flight (even in another group).
            try:
                await auto.begin_intent(db, second, 101, 5)
                assert False, "Concurrent payment intent must be refused"
            except ValueError:
                pass
            stamp = int(datetime.now(timezone.utc).timestamp())
            wrong_user = {"id": 1200, "type": "receive", "amount": 4,
                          "peer_id": 102, "date": stamp, "comment": ""}
            no_id = {"id": 1201, "type": "receive", "amount": 4,
                     "peer_id": None, "date": stamp, "comment": ""}
            unrelated = {"id": 1202, "type": "receive", "amount": 4,
                         "peer_id": 101, "date": stamp, "comment": "elsewhere"}
            nonfee = {"id": 1203, "type": "receive", "amount": 2,
                      "peer_id": 101, "date": stamp, "comment": ""}
            changed, _ = await auto.reconcile(db, [wrong_user, no_id, unrelated, nonfee])
            assert changed == set()
            assert (await get_collection(db, first["id"]))["raised_cents"] == 0
            # Iris may deduct 1 sweet from a 5-sweet payment; credit ACTUAL 4.
            receipt = {"id": 1204, "type": "receive", "amount": 4,
                       "peer_id": 101, "date": stamp, "comment": ""}
            changed, _ = await auto.reconcile(db, [receipt])
            assert changed == {first["id"]}
            assert (await get_collection(db, first["id"]))["raised_cents"] == 400
            assert await auto.reconcile(db, [receipt]) == (set(), [])
            assert await col_donors(db, first["id"]) == 1
            assert (await get_collection(db, second["id"]))["raised_cents"] == 0
            # A new 1-sweet contribution completes goal 5 and auto-closes.
            next_id = await auto.begin_intent(db, first, 101, 1)
            next_receipt = {"id": 1205, "type": "receive_with", "amount": 1,
                            "peer_id": 101, "date": stamp, "comment": ""}
            changed, _ = await auto.reconcile(db, [receipt, next_receipt])
            assert changed == {first["id"]}
            complete = await get_collection(db, first["id"])
            assert complete["raised_cents"] == 500
            assert complete["status"] == "closed"
            assert complete["closed_at"]
            assert "ЦЕЛЬ ДОСТИГНУТА" in collection_text(complete, 1)
            assert len(collection_keyboard_for_test(complete)) == 1
            assert await auto.reconcile(db, [next_receipt]) == (set(), [])
            assert (await get_collection(db, first["id"]))["raised_cents"] == 500
            assert await auto.close_completed(db) == set()
        finally:
            await db.close()

    from app.collections import donor_count as col_donors, collection_keyboard
    collection_keyboard_for_test = lambda row: collection_keyboard(row, 1).inline_keyboard
    asyncio.run(scenario())


def test_intent_cancel_and_expiry(tmp_path):
    from app import iris_autocollect as auto
    async def scenario():
        db = Database(str(tmp_path / "expire.sqlite3"))
        await db.connect()
        try:
            await initialize(db)
            await db.ensure_chat(-10011, "Чат", "Europe/Moscow", "09:00")
            row = await create_collection(
                db, -10011, SimpleNamespace(id=90, full_name="Саша"), "Подарок", 5
            )
            intent = await auto.begin_intent(db, row, 90, 1)
            assert not await auto.cancel_intent(db, intent, 91)
            assert await auto.cancel_intent(db, intent, 90)
            assert not await auto.cancel_intent(db, intent, 90)
            second = await auto.begin_intent(db, row, 90, 5)
            assert second != intent
            # Deliberately age the intent to check that it stops matching.
            expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            await db.execute(
                "UPDATE iris_payment_intents SET expires_at=? WHERE id=?",
                (expired, second),
            )
            await auto.expire_intents(db)
            result = await db.one(
                "SELECT status FROM iris_payment_intents WHERE id=?", (second,)
            )
            assert result["status"] == "expired"
            created = datetime.now(timezone.utc)
            again = await auto.begin_intent(db, row, 90, 5)
            assert again > second
        finally:
            await db.close()
    asyncio.run(scenario())
