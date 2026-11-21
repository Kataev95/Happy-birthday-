"""Group-chat display of fundraisers' confirmed contributors.

Uses only recorded, confirmed payments. Names come from users who clicked
Telegram payment buttons; older transactions without names remain anonymous.
"""
import html
from collections import defaultdict

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 10
TOP_SIZE = 5


async def donor_totals(db, collection_id: int):
    events = await db.all(
        """SELECT payer_id AS uid,payer_name AS name,received_cents AS cents,
                  matched_at AS paid_at
           FROM iris_payment_intents
           WHERE collection_id=? AND status='matched'
           UNION ALL
           SELECT sender_id AS uid, NULL AS name, amount_cents AS cents,
                  received_at AS paid_at
           FROM iris_collection_events
           WHERE collection_id=? AND outcome='credited'
           UNION ALL
           SELECT sender_id AS uid, NULL AS name, amount_cents AS cents,
                  recorded_at AS paid_at
           FROM iris_collection_manual_audit
           WHERE collection_id=?
           """,
        (collection_id, collection_id, collection_id),
    )
    users = {}
    for event in events:
        uid = event["uid"]
        if type(uid) is not int or uid <= 0 or not event["cents"]:
            # A verified anonymous receipt affects the aggregate collection
            # total but cannot be confidently credited to a particular user.
            continue
        entry = users.setdefault(uid, {"id": uid, "name": "", "total": 0, "count": 0})
        label = (event["name"] or "").strip()
        if label and label != "Участник":
            entry["name"] = label[:70]
        entry["total"] += int(event["cents"])
        entry["count"] += 1
    return sorted(users.values(), key=lambda x: (-x["total"], -x["count"], x["id"]))


def sweets(cents: int):
    if cents % 100 == 0:
        return str(cents // 100)
    return f"{cents // 100},{cents % 100:02d}"


def mention(entry, maxlen=30):
    name = entry["name"][:maxlen] if entry["name"] else "Участник"
    name = html.escape(name)
    return f'<a href="tg://user?id={entry["id"]}">{name}</a>'


async def top_donors_text(db, collection_id: int):
    donors = await donor_totals(db, collection_id)
    if not donors:
        return ""
    lines = ["", "💝 <b>Участники сбора:</b>"]
    for idx, user in enumerate(donors[:TOP_SIZE], 1):
        lines.append(f"{idx}. {mention(user, 24)} — <b>{sweets(user['total'])} 🍬</b>")
    if len(donors) > TOP_SIZE:
        lines.append(f"<i>И ещё {len(donors)-TOP_SIZE} участников. Нажми «Все участники».</i>")
    return "\n".join(lines) + "\n"


def donors_markup(collection_id: int, page: int, count: int):
    total_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    controls = []
    if page > 0:
        controls.append(InlineKeyboardButton(
            text="◀️ Назад", callback_data=f"ic:donorpage:{collection_id}:{page-1}"
        ))
    if page + 1 < total_pages:
        controls.append(InlineKeyboardButton(
            text="Далее ▶️", callback_data=f"ic:donorpage:{collection_id}:{page+1}"
        ))
    return InlineKeyboardMarkup(inline_keyboard=[controls] if controls else [
        [InlineKeyboardButton(
            text="🔄 Обновить список",
            callback_data=f"ic:donorpage:{collection_id}:0",
        )]
    ])


def donors_page_text(row, donors, page):
    total_pages = max(1, (len(donors) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    lines = [
        f"👥 <b>Участники сбора №{row['id']}</b>",
        f"🎯 {html.escape(row['title'])}",
        f"Всего участников с установленным ID: {len(donors)}",
        "",
    ]
    chunk = donors[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    for pos, donor in enumerate(chunk, page * PAGE_SIZE + 1):
        lines.append(
            f"{pos}. {mention(donor, 38)} — <b>{sweets(donor['total'])} 🍬</b> "
            f"({donor['count']} взн.)"
        )
    if not chunk:
        lines.append("Пока нет подтверждённых взносов с известным отправителем.")
    lines.append(f"\nСтраница {page+1}/{total_pages}")
    return "\n".join(lines), page
