"""Unit tests run without Telegram credentials or a network connection."""
import asyncio
import hashlib
import hmac
import json
from datetime import date
from urllib.parse import urlencode

import pytest

from app.auth import AuthError, verify_init_data
from app.dates import day_of_observance, days_until, validate_birthday
from app.db import Database


def signed_data(token: str, now: int, **extra) -> str:
    data = {"auth_date": str(now), "user": json.dumps({"id": 123, "first_name": "Маша"})}
    data.update(extra)
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_init_data_signature_and_expiry():
    raw = signed_data("123:secret", 1700000000)
    assert verify_init_data(raw, "123:secret", now=1700000001)["id"] == 123
    with pytest.raises(AuthError):
        verify_init_data(raw, "different", now=1700000001)
    with pytest.raises(AuthError):
        verify_init_data(raw, "123:secret", now=1700000000 + 86401)
    with pytest.raises(AuthError):
        verify_init_data(raw + "&user=fake", "123:secret", now=1700000001)


def test_dates():
    validate_birthday(29, 2, None)
    with pytest.raises(ValueError):
        validate_birthday(31, 2, None)
    with pytest.raises(ValueError):
        validate_birthday(29, 2, 2001)
    assert day_of_observance(29, 2, 2025) == date(2025, 2, 28)
    assert day_of_observance(29, 2, 2024) == date(2024, 2, 29)
    assert days_until(14, 11, date(2026, 11, 16)) == 363


def test_database_claims_and_privacy(tmp_path):
    async def run():
        db = Database(str(tmp_path / "db.sqlite3"))
        await db.connect()
        try:
            await db.ensure_chat(-10042, "Тест", "Europe/Moscow", "09:00")
            await db.put_birthday(-10042, {"id": 123, "first_name": "Маша"},
                                  14, 11, 1998, False, True, True)
            visible = await db.get_visible_birthdays(-10042)
            assert visible[0]["birth_year"] is None
            assert await db.claim_delivery(-10042, 123, 2026)
            assert not await db.claim_delivery(-10042, 123, 2026)
            await db.sent_delivery(-10042, 123, 2026, 45, None)
            await db.remove_birthday(-10042, 123)
            assert await db.get_birthday(-10042, 123) is None
        finally:
            await db.close()
    asyncio.run(run())



def test_health_url_opened_as_main_mini_app():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True, "service": "happy-birthday"}
    response = client.get("/health?tgWebAppStartParam=c-1004417994455_b")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="shell"' in response.text
    assert client.get("/health?tgWebAppStartParam=nonsense").json()["ok"] is True
    assert client.get("/favicon.ico").status_code == 204


def test_private_start_has_add_to_group_button():
    from app.bot import group_invite_link, private_start_markup
    assert group_invite_link("drmetr_bot") == (
        "https://t.me/drmetr_bot?startgroup&admin=pin_messages"
    )
    markup = private_start_markup("drmetr_bot", "https://example.org")
    add_button = markup.inline_keyboard[0][0]
    assert add_button.text == "➕ Добавить в группу"
    assert add_button.url == group_invite_link("drmetr_bot")
    assert markup.inline_keyboard[1][0].web_app.url == "https://example.org"
