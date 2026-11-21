"""Regression coverage for the real Iris API's amount, fee and history shapes."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import collections as c
from app import iris_autocollect as a
from app import iris_donors as d
from app.db import Database


async def setup_db(tmp_path):
    db = Database(str(tmp_path / "iris2.sqlite3"))
    await db.connect()
    await c.initialize(db)
    await db.ensure_chat(-1005, "Чат", "Europe/Moscow", "09:00")
    row = await c.create_collection(
        db, -1005, SimpleNamespace(id=55, full_name="Организатор"), "Свадьба", 10
    )
    return db, row


def receipt(tx_id, uid, amount, *, total=None, fee=None, comment="", time=None):
    result = {
        "id": tx_id, "type": "receive", "peer_id": uid, "amount": amount,
        "date": time or int(datetime.now(timezone.utc).timestamp() * 1000),
        "comment": comment,
    }
    if total is not None or fee is not None:
        result["details"] = {}
        if total is not None:
            result["details"]["total"] = total
        if fee is not None:
            result["details"]["fee"] = fee
    return result


def test_details_total_and_fee_exact_match(tmp_path):
    async def scenario():
        db, row = await setup_db(tmp_path)
        try:
            await a.begin_intent(db, row, 101, 5, payer_name="Роза & <Лиза>")
            pending = await db.one("SELECT * FROM iris_payment_intents")
            assert pending["payer_name"] == "Роза & <Лиза>"
            good = receipt(12001, "101", 4.75, total=5, fee=0.25)
            assert a.amount_matches(pending, good)
            assert a.valid_match({**pending, "collection_code": row["code"]}, good)
            bad_gross = receipt(12002, 101, 4.75, total=6, fee=1.25)
            assert not a.amount_matches(pending, bad_gross)
            bad_fee = receipt(12003, 101, 4.75, total=5, fee=0.99)
            assert not a.amount_matches(pending, bad_fee)
            unrelated = receipt(12004, 101, 4.75, total=5, fee=0.25,
                                comment="other_project")
            assert not a.valid_match(
                {**pending, "collection_code": row["code"]}, unrelated)
            outcome, _ = await a.reconcile(db, [bad_gross, unrelated, good])
            assert outcome == {row["id"]}
            assert (await c.get_collection(db, row["id"]))["raised_cents"] == 475
            contributors = await d.donor_totals(db, row["id"])
            assert len(contributors) == 1
            assert contributors[0]["total"] == 475
            top = await d.top_donors_text(db, row["id"])
            assert "Роза &amp; &lt;Лиза&gt;" in top
            assert "4,75" in top
            assert await a.reconcile(db, [good]) == (set(), [])
        finally:
            await db.close()
    asyncio.run(scenario())


def test_ambiguous_receipts_never_credited(tmp_path):
    async def scenario():
        db, row = await setup_db(tmp_path)
        try:
            await a.begin_intent(db, row, 101, 5, payer_name="Мага")
            candidates = [
                receipt(301, 101, 4, total=5, fee=1),
                receipt(302, 101, 4, total=5, fee=1),
            ]
            matched, to_delete = await a.reconcile(db, candidates)
            assert matched == set() and not to_delete
            assert (await c.get_collection(db, row["id"]))["raised_cents"] == 0
        finally:
            await db.close()
    asyncio.run(scenario())


def test_retroactive_credit_of_expired_intent(tmp_path):
    async def scenario():
        db, row = await setup_db(tmp_path)
        try:
            intent_id = await a.begin_intent(db, row, 101, 1, payer_name="Майя")
            created = datetime.now(timezone.utc) - timedelta(minutes=8)
            expired = datetime.now(timezone.utc) - timedelta(minutes=4)
            await db.execute(
                """UPDATE iris_payment_intents
                   SET created_at=?,expires_at=?,status='expired' WHERE id=?""",
                (created.isoformat(), expired.isoformat(), intent_id),
            )
            paid_at = int((created + timedelta(minutes=2)).timestamp() * 1000)
            r = receipt(999, 101, .9, total=1, fee=.1, time=paid_at)
            matched, _ = await a.reconcile(db, [r])
            assert matched == {row["id"]}
            saved = await db.one(
                "SELECT status,received_cents FROM iris_payment_intents WHERE id=?",
                (intent_id,),
            )
            assert saved == {"status": "matched", "received_cents": 90}
            assert (await c.get_collection(db, row["id"]))["raised_cents"] == 90
        finally:
            await db.close()
    asyncio.run(scenario())


def test_exact_comment_does_not_double_credit_between_endpoints(tmp_path):
    async def scenario():
        db, row = await setup_db(tmp_path)
        try:
            await a.begin_intent(db, row, 101, 5, payer_name="Амина")
            r = receipt(901, 101, 4, total=5, fee=1, comment=row["code"])
            event = {"id": 200, "date": r["date"], "type": "sweets_log",
                     "object": r}
            assert await c.ingest_event(db, event) == row["id"]
            assert (await c.get_collection(db, row["id"]))["raised_cents"] == 400
            assert await a.reconcile(db, [r]) == (set(), [])
            assert (await c.get_collection(db, row["id"]))["raised_cents"] == 400
        finally:
            await db.close()
    asyncio.run(scenario())


def test_card_refresh_replaces_old_urls_and_shows_contributors(tmp_path):
    async def scenario():
        db, row = await setup_db(tmp_path)
        try:
            await a.begin_intent(db, row, 101, 1, payer_name="Анзор")
            await a.reconcile(db, [receipt(310, 101, .95, total=1, fee=.05)])
            await db.execute(
                "UPDATE iris_collections SET message_id=99 WHERE id=?", (row["id"],)
            )
            class StubBot:
                def __init__(self):
                    self.calls = []
                async def edit_message_text(self, **kwargs):
                    self.calls.append(kwargs)
            bot = StubBot()
            await c.refresh_existing_cards(db, bot, 42)
            assert len(bot.calls) == 1
            kwargs = bot.calls[0]
            assert kwargs["message_id"] == 99
            assert "Анзор" in kwargs["text"]
            assert "0.95" in kwargs["text"] or "0,95" in kwargs["text"]
            buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
            assert any(b.callback_data == f"ic:amount:{row['id']}:1" for b in buttons)
            assert not any(b.url is not None for b in buttons)
            assert any(b.callback_data == f"ic:donorpage:{row['id']}:0" for b in buttons)
        finally:
            await db.close()
    asyncio.run(scenario())
