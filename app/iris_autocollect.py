"""Automated in-chat Iris contribution intents and conservative ledger matching.

Iris deep links sometimes lose their comments. A Telegram callback creates an
authenticated intent BEFORE revealing the payment link. A real incoming Iris
history receipt is associated only when peer_id, amount and time agree.
Unverifiable receipts are never silently allocated to a fundraiser.
"""
import logging
from datetime import datetime, timedelta, timezone

from app import collections as col

log = logging.getLogger(__name__)
INTENT_TTL_MINUTES = 15
AMOUNT_FEE_TOLERANCE_CENTS = 100  # observed Iris fee: 5 sent -> 4 received

SCHEMA = """
CREATE TABLE IF NOT EXISTS iris_payment_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  collection_id INTEGER NOT NULL REFERENCES iris_collections(id),
  payer_id INTEGER NOT NULL,
  expected_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','matched','expired','canceled')),
  transaction_id INTEGER UNIQUE,
  received_cents INTEGER,
  link_message_id INTEGER,
  matched_at TEXT
);
CREATE INDEX IF NOT EXISTS iris_intents_pending_idx
 ON iris_payment_intents(status,payer_id,created_at);
"""


async def initialize(db):
    async with db.lock:
        await db.connection.executescript(SCHEMA)
        await db.connection.commit()


async def begin_intent(db, row: dict, payer_id: int, amount: int):
    """Require one active payment intent per Telegram ID across all groups."""
    now = datetime.now(timezone.utc)
    created = now.isoformat()
    expires = (now + timedelta(minutes=INTENT_TTL_MINUTES)).isoformat()
    async with db.lock:
        await db.connection.execute(
            "UPDATE iris_payment_intents SET status='expired' "
            "WHERE payer_id=? AND status='pending' AND expires_at < ?",
            (payer_id, created),
        )
        async with db.connection.execute(
            "SELECT id FROM iris_payment_intents WHERE payer_id=? AND status='pending'",
            (payer_id,),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            await db.connection.commit()
            raise ValueError(
                "У тебя уже открыт перевод. Закончи его или нажми «Отменить перевод» "
                "в предыдущем сообщении."
            )
        async with db.connection.execute(
            "SELECT status,goal_cents,raised_cents FROM iris_collections WHERE id=?",
            (row["id"],),
        ) as cur:
            live = await cur.fetchone()
        if (not live or live["status"] != "open"
                or live["raised_cents"] >= live["goal_cents"]):
            await db.connection.commit()
            raise ValueError("Сбор уже закрыт или его цель достигнута.")
        cur = await db.connection.execute(
            """INSERT INTO iris_payment_intents
               (chat_id,collection_id,payer_id,expected_cents,created_at,expires_at)
               VALUES(?,?,?,?,?,?)""",
            (row["chat_id"], row["id"], payer_id, amount * 100, created, expires),
        )
        intent_id = cur.lastrowid
        await db.connection.commit()
    return intent_id


async def cancel_intent(db, intent_id: int, payer_id: int):
    async with db.lock:
        cur = await db.connection.execute(
            """UPDATE iris_payment_intents SET status='canceled'
               WHERE id=? AND payer_id=? AND status='pending'""",
            (intent_id, payer_id),
        )
        await db.connection.commit()
        return cur.rowcount == 1


def parsed_epoch(receipt):
    try:
        value = float(receipt.get("date") or 0)
        if value > 1e11:
            value /= 1000
        return value if 1e9 < value <= datetime.now(timezone.utc).timestamp() + 120 else None
    except (ValueError, TypeError, OverflowError):
        return None


def valid_match(intent, receipt):
    """Conservative payer + timestamp + amount match; no guess from balance."""
    if not isinstance(receipt, dict):
        return False
    if receipt.get("type") not in col.INCOMING_TYPES:
        return False
    if not isinstance(receipt.get("id"), int) or receipt["id"] <= 0:
        return False
    sender = receipt.get("peer_id")
    if type(sender) is not int or sender <= 0 or sender != intent["payer_id"]:
        return False
    if str(receipt.get("comment") or "").strip():
        # Nonempty comments are handled by existing exact-code logic, not
        # by this timing-based fallback. Avoid unrelated paid transfers.
        return False
    amount = col.inbound_cents(receipt.get("amount"))
    expected = intent["expected_cents"]
    if amount is None or not (
        amount == expected or (expected >= 200 and amount == expected - AMOUNT_FEE_TOLERANCE_CENTS)
    ):
        return False
    ts = parsed_epoch(receipt)
    if ts is None:
        return False
    created = datetime.fromisoformat(intent["created_at"]).timestamp()
    expires = datetime.fromisoformat(intent["expires_at"]).timestamp()
    return created - 5 <= ts <= expires + 60


async def reconcile(db, history: list):
    """Credit only real, unique confirmed history receipts against valid intents.

    Called after a successful Iris API fetch. Atomic ledger and card balance updates
    prevent replay and cross-campaign attribution; refunds remain out of scope.
    """
    intents = await db.all(
        """SELECT * FROM iris_payment_intents WHERE status='pending'
           ORDER BY created_at,id"""
    )
    if not intents:
        return set(), []
    changed = set()
    links_to_delete = []
    for receipt in history:
        matches = [p for p in intents if valid_match(p, receipt)]
        if len(matches) != 1:
            continue
        pending = matches[0]
        tx_id = receipt["id"]
        cents = col.inbound_cents(receipt["amount"])
        async with db.lock:
            async with db.connection.execute(
                "SELECT 1 FROM iris_collection_recovered WHERE transaction_id=?",
                (tx_id,),
            ) as cur:
                seen = await cur.fetchone()
            async with db.connection.execute(
                "SELECT * FROM iris_payment_intents WHERE id=?", (pending["id"],)
            ) as cur:
                current = await cur.fetchone()
            async with db.connection.execute(
                "SELECT * FROM iris_collections WHERE id=?", (pending["collection_id"],)
            ) as cur:
                fundraiser = await cur.fetchone()
            if (seen or not current or current["status"] != "pending"
                    or not fundraiser or fundraiser["status"] != "open"):
                continue
            if not valid_match(current, receipt):
                continue
            # Every external transaction has a single global owner, regardless
            # of intent, chat or collection. Do not reuse historic manual records.
            cur = await db.connection.execute(
                """INSERT OR IGNORE INTO iris_collection_recovered
                   (transaction_id,collection_id,amount_cents,recovered_at)
                   VALUES(?,?,?,?)""",
                (tx_id, current["collection_id"], cents, col.now_iso()),
            )
            if cur.rowcount != 1:
                await db.connection.commit()
                continue
            cur = await db.connection.execute(
                """UPDATE iris_payment_intents
                   SET status='matched',transaction_id=?,received_cents=?,matched_at=?
                   WHERE id=? AND status='pending'""",
                (tx_id, cents, col.now_iso(), current["id"]),
            )
            if cur.rowcount != 1:
                await db.connection.rollback()
                continue
            await db.connection.execute(
                """UPDATE iris_collections SET raised_cents=raised_cents+?
                   WHERE id=? AND status='open'""",
                (cents, current["collection_id"]),
            )
            # Close when at or above goal. Do NOT initiate payout without a
            # separately reviewed settlement process.
            await db.connection.execute(
                """UPDATE iris_collections SET status='closed',closed_at=?
                   WHERE id=? AND status='open' AND raised_cents>=goal_cents""",
                (col.now_iso(), current["collection_id"]),
            )
            await db.connection.commit()
            changed.add(current["collection_id"])
            links_to_delete.append((current["chat_id"], current["link_message_id"]))
        # Prevent processing a second receipt against an already matched intent.
        intents = [p for p in intents if p["id"] != pending["id"]]
    return changed, links_to_delete


async def close_completed(db):
    """Also close existing completed test collections after deploying this stage."""
    async with db.lock:
        async with db.connection.execute(
            "SELECT id FROM iris_collections WHERE status='open' AND raised_cents>=goal_cents"
        ) as cur:
            ids = [row["id"] for row in await cur.fetchall()]
        if ids:
            await db.connection.execute(
                """UPDATE iris_collections SET status='closed',closed_at=?
                   WHERE status='open' AND raised_cents>=goal_cents""",
                (col.now_iso(),),
            )
            await db.connection.commit()
    return set(ids)


async def expire_intents(db):
    """Expiry only after successfully examining Iris history that cycle."""
    async with db.lock:
        async with db.connection.execute(
            """SELECT chat_id,link_message_id FROM iris_payment_intents
               WHERE status='pending' AND expires_at<?""",
            (col.now_iso(),),
        ) as cur:
            to_delete = [(r["chat_id"], r["link_message_id"]) for r in await cur.fetchall()]
        await db.connection.execute(
            """UPDATE iris_payment_intents SET status='expired'
               WHERE status='pending' AND expires_at<?""",
            (col.now_iso(),),
        )
        await db.connection.commit()
    return to_delete
