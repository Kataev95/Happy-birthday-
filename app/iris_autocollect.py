"""In-chat Iris payment intents and verified, idempotent receipt matching.

Iris can omit comments from inbound history. Never attribute an anonymous
transfer by its amount or time alone: the counterparty identity must match
the Telegram user who explicitly requested a payment link.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app import collections as col

log = logging.getLogger(__name__)
INTENT_TTL_MINUTES = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS iris_payment_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  collection_id INTEGER NOT NULL REFERENCES iris_collections(id),
  payer_id INTEGER NOT NULL,
  payer_name TEXT NOT NULL DEFAULT 'Участник',
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
CREATE UNIQUE INDEX IF NOT EXISTS iris_intents_unique_tx
 ON iris_payment_intents(transaction_id) WHERE transaction_id IS NOT NULL;
"""


async def initialize(db):
    async with db.lock:
        await db.connection.executescript(SCHEMA)
        # Existing deployments already have this table but not payer_name.
        async with db.connection.execute("PRAGMA table_info(iris_payment_intents)") as cursor:
            names = {r["name"] for r in await cursor.fetchall()}
        if "payer_name" not in names:
            await db.connection.execute(
                "ALTER TABLE iris_payment_intents "
                "ADD COLUMN payer_name TEXT NOT NULL DEFAULT 'Участник'"
            )
        await db.connection.commit()


async def begin_intent(db, row: dict, payer_id: int, amount: int, payer_name: str = "Участник"):
    """A payer can have only one pending payment across ALL fundraising chats."""
    now = datetime.now(timezone.utc)
    created, expires = now.isoformat(), (
        now + timedelta(minutes=INTENT_TTL_MINUTES)).isoformat()
    safe_name = (payer_name or "Участник").strip()[:70] or "Участник"
    async with db.lock:
        # Keep a newly requested intent separate from older expired ones.
        await db.connection.execute(
            "UPDATE iris_payment_intents SET status='expired' "
            "WHERE payer_id=? AND status='pending' AND expires_at<?",
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
                "У тебя уже есть активный перевод. Заверши его либо нажми "
                "«Отменить» под предыдущей персональной ссылкой."
            )
        async with db.connection.execute(
            "SELECT status,goal_cents,raised_cents FROM iris_collections WHERE id=?",
            (row["id"],),
        ) as cur:
            live = await cur.fetchone()
        if (not live or live["status"] != "open" or
                live["raised_cents"] >= live["goal_cents"]):
            await db.connection.commit()
            raise ValueError("Этот сбор уже закрыт или достиг цели.")
        cur = await db.connection.execute(
            """INSERT INTO iris_payment_intents
               (chat_id,collection_id,payer_id,payer_name,expected_cents,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?)""",
            (row["chat_id"], row["id"], payer_id, safe_name, amount * 100,
             created, expires),
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


def int_id(value):
    """Handle int or integer-like string, without silently accepting floats/bools."""
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value.strip()):
        return int(value)
    return None


def cents_or_zero(value):
    if value is None:
        return None
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            return None
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        return None


def parsed_epoch(receipt):
    try:
        value = float(receipt.get("date") or 0)
        if value > 1e11:  # Iris history is usually in Unix milliseconds.
            value /= 1000
        return value if 1e9 < value <= datetime.now(timezone.utc).timestamp() + 120 else None
    except (ValueError, TypeError, OverflowError):
        return None


def receipt_identity(receipt):
    if not isinstance(receipt, dict):
        return None
    return int_id(receipt.get("peer_id"))


def receipt_gross_cents(receipt):
    """Reconstruct intended sent amount from Iris's transaction details, if present.

    The API calls details.total the overall amount, and details.fee its fee.
    A missing details object means we can only use a conservative fallback.
    """
    details = receipt.get("details")
    if not isinstance(details, dict):
        return None
    net = col.inbound_cents(receipt.get("amount"))
    if net is None:
        return None
    total = cents_or_zero(details.get("total"))
    fee = cents_or_zero(details.get("fee"))
    # -1 means malformed or contradictory payment details. Crucially this is
    # NOT the same as absent details: we must not fall back to a guess.
    if ("total" in details and total is None) or ("fee" in details and fee is None):
        return -1
    if total is not None and total > 0:
        if total < net:
            return -1
        # Some Iris responses omit fees; don't reject a valid disclosed total.
        if fee is not None and abs(total - net - fee) > 1:
            return -1
        return total
    if fee is not None:
        return net + fee
    return None


def amount_matches(intent, receipt):
    """Confirm exact gross when supplied; never infer from rounded amounts."""
    received = col.inbound_cents(receipt.get("amount"))
    expected = intent["expected_cents"]
    if received is None or received > expected:
        return False
    gross = receipt_gross_cents(receipt)
    if gross is not None:
        return gross == expected
    # Older Iris responses have no details. Fall back only when sender and
    # narrow time-window can ALSO be verified.
    fee = expected - received
    # Fee can be fractional. Never infer a bigger deduction than 1 sweet or
    # over half the sent amount when details are missing.
    return fee <= 100 and 2 * fee <= expected


def valid_match(intent, receipt):
    """Validated sender + amount/fee + narrow time window + safe memo."""
    if not isinstance(receipt, dict) or receipt.get("type") not in col.INCOMING_TYPES:
        return False
    if int_id(receipt.get("id")) is None:
        return False
    sender = receipt_identity(receipt)
    if sender is None or sender != intent["payer_id"]:
        return False
    comment = str(receipt.get("comment") or "").strip()
    if comment and comment != intent.get("collection_code"):
        # Never steal a transaction labelled for another fundraiser.
        return False
    if not amount_matches(intent, receipt):
        return False
    ts = parsed_epoch(receipt)
    if ts is None:
        return False
    created = datetime.fromisoformat(intent["created_at"]).timestamp()
    expires = datetime.fromisoformat(intent["expires_at"]).timestamp()
    return created - 5 <= ts <= expires + 60


async def match_summary(db, receipts):
    """Safe aggregate diagnostics: no raw user IDs, tokens or transaction texts."""
    intents = await db.all(
        """SELECT p.*,c.code AS collection_code FROM iris_payment_intents p
           JOIN iris_collections c ON c.id=p.collection_id
           WHERE p.status='pending'""")
    summary = Counter()
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("type") not in col.INCOMING_TYPES:
            continue
        summary["incoming"] += 1
        if receipt_identity(receipt) is None:
            summary["missing_sender"] += 1
            continue
        summary["has_sender"] += 1
        if (receipt_gross_cents(receipt) is not None and
                receipt_gross_cents(receipt) >= 0):
            summary["has_gross"] += 1
        if any(p["payer_id"] == receipt_identity(receipt) for p in intents):
            summary["sender_has_intent"] += 1
        if any(valid_match(p, receipt) for p in intents):
            summary["valid_candidates"] += 1
    return dict(summary)


async def reconcile(db, history: list):
    """Exactly-once automatic crediting for UNAMBIGUOUS confirmed receipts.

    A missing peer_id, unexpected amount, or more than one receipt matching
    the same intent is not safe to attribute automatically. No manual credit.
    """
    # Recover a transfer already paid while the matching logic was outdated,
    # even if its 10-minute intent has since expired. Only transactions whose
    # actual Iris timestamp falls WITHIN that original window may be credited.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    intents = await db.all(
        """SELECT p.*,c.code AS collection_code FROM iris_payment_intents p
           JOIN iris_collections c ON c.id=p.collection_id
           WHERE p.status IN ('pending','expired') AND p.created_at>=?
           ORDER BY p.created_at,p.id""",
        (cutoff,),
    )
    if not intents:
        return set(), []
    candidates = {}
    receipt_matches = Counter()
    for receipt in history:
        if not isinstance(receipt, dict):
            continue
        valid_intents = [p for p in intents if valid_match(p, receipt)]
        if len(valid_intents) != 1:
            continue
        intent = valid_intents[0]
        candidates.setdefault(intent["id"], []).append(receipt)
        receipt_matches[int_id(receipt["id"])] += 1
    changed = set()
    links_to_delete = []
    for pending in intents:
        receipts = candidates.get(pending["id"], [])
        if len(receipts) != 1:
            # Two matching transactions from the same payer in the same window
            # cannot be assigned uniquely without a real payment reference.
            continue
        receipt = receipts[0]
        tx_id = int_id(receipt["id"])
        if receipt_matches[tx_id] != 1:
            continue
        cents = col.inbound_cents(receipt["amount"])
        async with db.lock:
            async with db.connection.execute(
                "SELECT 1 FROM iris_collection_recovered WHERE transaction_id=?",
                (tx_id,),
            ) as cur:
                previously_counted = await cur.fetchone()
            async with db.connection.execute(
                "SELECT p.*,c.code AS collection_code FROM iris_payment_intents p "
                "JOIN iris_collections c ON c.id=p.collection_id WHERE p.id=?",
                (pending["id"],),
            ) as cur:
                current = await cur.fetchone()
            async with db.connection.execute(
                "SELECT * FROM iris_collections WHERE id=?", (pending["collection_id"],)
            ) as cur:
                fundraiser = await cur.fetchone()
            if (previously_counted or not current or current["status"] not in ("pending","expired")
                    or not fundraiser or fundraiser["status"] not in ("open", "closed")):
                continue
            if not valid_match(current, receipt):
                continue
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
                   WHERE id=? AND status IN ('pending','expired')""",
                (tx_id, cents, col.now_iso(), current["id"]),
            )
            if cur.rowcount != 1:
                await db.connection.rollback()
                continue
            await db.connection.execute(
                """UPDATE iris_collections SET raised_cents=raised_cents+?
                   WHERE id=? AND status IN ('open','closed')""",
                (cents, current["collection_id"]),
            )
            await db.connection.execute(
                """UPDATE iris_collections SET status='closed',closed_at=?
                   WHERE id=? AND status='open' AND raised_cents>=goal_cents""",
                (col.now_iso(), current["collection_id"]),
            )
            await db.connection.commit()
            changed.add(current["collection_id"])
            links_to_delete.append((current["chat_id"], current["link_message_id"]))
    return changed, links_to_delete


async def close_completed(db):
    """Close earlier completed test collections after deploying this stage."""
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
    """Never expire pending payments until after a successful history fetch."""
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
