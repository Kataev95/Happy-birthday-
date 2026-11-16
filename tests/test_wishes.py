"""End-to-end wish API tests with two users and isolated groups."""
import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
import pytest

from app.api import context
from app.db import Database
from app.wishes import WishPayload, initialize, list_for_chat, router


class FakeBot:
    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


def test_wish_privacy_booking_and_pickup(tmp_path):
    async def run():
        db = Database(str(tmp_path / "wishes.sqlite3"))
        await db.connect()
        try:
            await initialize(db)
            await db.ensure_chat(-100123, "Первая группа", "Europe/Moscow", "09:00")
            await db.ensure_chat(-100456, "Вторая группа", "Europe/Moscow", "09:00")
            ctx = {"db":db, "chat":{"chat_id":-100123}, "bot":FakeBot(),
                   "user":{"id":101,"first_name":"Аня"}}
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[context] = lambda: ctx

            async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
                payload = {"title":"Наушники","description":"Хочу хорошие",
                           "product_url":"https://www.ozon.ru/product/123",
                           "price_rub":2500}
                response = await client.post("/api/wishes",params={"chat_id":-100123},json=payload)
                assert response.status_code == 201, response.text
                wish_id = response.json()["id"]
                points = await client.put("/api/pickup",params={"chat_id":-100123},
                    json={"wb":"Грозный, ПВЗ на улице Победы","ozon":"Грозный, ПВЗ Оzon"})
                assert points.status_code == 200, points.text
                mine = (await client.get("/api/wishes",params={"chat_id":-100123})).json()["mine"]
                assert len(mine) == 1
                assert "address" not in str(mine)
                assert "Грозный" not in str(mine)
                assert mine[0]["reserved"] is False
                owner_points = (await client.get("/api/pickup",params={"chat_id":-100123})).json()
                assert owner_points["wb"].startswith("Грозный")

                ctx["user"]={"id":202,"first_name":"Руслан"}
                friendlist=(await client.get("/api/wishes",params={"chat_id":-100123})).json()
                assert len(friendlist["friends"]) == 1
                assert "Грозный" not in str(friendlist)
                assert (await client.get(f"/api/wishes/{wish_id}/delivery",params={"chat_id":-100123})).status_code == 403
                assert (await client.get("/api/pickup",params={"chat_id":-100123})).json() == {}
                claimed=await client.post(f"/api/wishes/{wish_id}/reserve",params={"chat_id":-100123})
                assert claimed.status_code == 200, claimed.text
                again=await client.post(f"/api/wishes/{wish_id}/reserve",params={"chat_id":-100123})
                assert again.json()["already_reserved"] is True
                delivery=await client.get(f"/api/wishes/{wish_id}/delivery",params={"chat_id":-100123})
                assert delivery.status_code == 200, delivery.text
                assert delivery.json()["pickup"]["ozon"] == "Грозный, ПВЗ Оzon"
                assert (await client.get("/api/wishes",params={"chat_id":-100123})).json()["friends"][0]["reserved_by_me"]

                ctx["user"]={"id":303,"first_name":"Магомед"}
                assert (await client.post(f"/api/wishes/{wish_id}/reserve",params={"chat_id":-100123})).status_code == 409
                assert (await client.get(f"/api/wishes/{wish_id}/delivery",params={"chat_id":-100123})).status_code == 403

                ctx["user"]={"id":101,"first_name":"Аня"}
                mine_after = (await client.get("/api/wishes",params={"chat_id":-100123})).json()["mine"]
                assert mine_after[0]["reserved"] is False, "The recipient should not learn a gift has been reserved"
                ctx["chat"]={"chat_id":-100456}
                assert (await client.get("/api/wishes",params={"chat_id":-100456})).json()["friends"] == []
                assert (await client.get("/api/pickup",params={"chat_id":-100456})).json() == {}
                ctx["chat"]={"chat_id":-100123}

                ctx["user"]={"id":202,"first_name":"Руслан"}
                assert (await client.delete(f"/api/wishes/{wish_id}/reserve",params={"chat_id":-100123})).status_code == 200
                ctx["user"]={"id":101,"first_name":"Аня"}
                fulfilled=await client.post(f"/api/wishes/{wish_id}/received",params={"chat_id":-100123})
                assert fulfilled.status_code == 200
                ctx["user"]={"id":202,"first_name":"Руслан"}
                assert (await client.get("/api/wishes",params={"chat_id":-100123})).json()["friends"] == []
                ctx["user"]={"id":101,"first_name":"Аня"}
                assert (await client.delete(f"/api/wishes/{wish_id}",params={"chat_id":-100123})).status_code == 200
                assert (await client.get("/api/wishes",params={"chat_id":-100123})).json()["mine"] == []
        finally:
            await db.close()
    asyncio.run(run())


def test_malicious_or_unsupported_urls_rejected():
    for url in ("http://www.ozon.ru/foo", "https://evil.example/gift",
                "https://ozon.ru.evil.example/item", "https://127.0.0.1/item",
                "javascript:alert(1)", "https://user:pw@ozon.ru/item"):
        with pytest.raises(ValidationError):
            WishPayload(title="Подарок",product_url=url)
    assert WishPayload(title="Наушники",product_url="https://www.wildberries.ru/catalog/12").product_url


def test_wishlist_limit_and_data_survives_reopen(tmp_path):
    async def run():
        path=str(tmp_path / "existing.sqlite3")
        db=Database(path)
        await db.connect()
        await initialize(db)
        await db.ensure_chat(-1004,"Чат","Europe/Moscow","09:00")
        from app.wishes import create_wish
        for i in range(12):
            await create_wish(db,-1004,{"id":42,"first_name":"Я"},WishPayload(title=f"Подарок {i}"))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await create_wish(db,-1004,{"id":42,"first_name":"Я"},WishPayload(title="Тринадцатый"))
        assert exc.value.status_code == 409
        await db.close()
        db2=Database(path)
        await db2.connect()
        try:
            await initialize(db2)
            listing=await list_for_chat(db2,-1004,42)
            assert len(listing["mine"])==12
        finally:
            await db2.close()
    asyncio.run(run())
