"""Single-process FastAPI + aiogram polling + daily job; no separate worker required."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.bot import router as bot_router
from app.config import Settings
from app.collections import router as collections_router, initialize as initialize_collections, poll_iris
from app.iris_autocollect import initialize as initialize_iris_intents
from app.db import Database
from app.scheduler import scheduler_loop
from app.wishes import router as wishes_router, initialize as initialize_wishes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)
# httpx INFO may print Iris API URL including the API credential.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def iris_poll_loop(db, bot, bot_id: int, token: str):
    while True:
        if token:
            try:
                await poll_iris(db, bot, bot_id, token)
            except Exception:
                # A temporary Iris API problem must not interrupt birthdays.
                log.exception("Iris collection polling failed")
        await asyncio.sleep(45)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    db = Database(settings.database_path)
    await db.connect()
    await initialize_wishes(db)
    await initialize_collections(db)
    await initialize_iris_intents(db)
    bot = Bot(settings.bot_token)
    try:
        info = await bot.get_me()
        app.state.db = db
        app.state.bot = bot
        app.state.settings = settings
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть праздничное приложение"),
            BotCommand(command="holidays", description="Вызвать кнопки праздников в группе"),
            BotCommand(command="collect", description="Создать сбор ирисок в чате"),
            BotCommand(command="collections", description="Список сборов ирисок"),
            BotCommand(command="collect_status", description="Диагностика сборов (для админа)"),
        ])
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🎉 Праздники", web_app=WebAppInfo(url=settings.base_url)
            )
        )
        dp = Dispatcher()
        dp["db"] = db
        dp["settings"] = settings
        dp["username"] = info.username
        dp["bot_id"] = info.id
        dp.include_router(bot_router)
        dp.include_router(collections_router)
        poll_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        schedule_task = asyncio.create_task(
            scheduler_loop(db, bot, info.username, settings.check_interval_seconds)
        )
        iris_task = asyncio.create_task(
            iris_poll_loop(db, bot, info.id, settings.iris_connect_token)
        )
        log.info("Happy Birthday started as @%s", info.username)
        try:
            yield
        finally:
            for task in (poll_task, schedule_task, iris_task):
                task.cancel()
            await asyncio.gather(poll_task, schedule_task, iris_task, return_exceptions=True)
    finally:
        await bot.session.close()
        await db.close()


app = FastAPI(
    title="Happy Birthday Mini App",
    docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=lifespan,
)
app.include_router(api_router)
app.include_router(wishes_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    # BotHost starts the package with `python -m app.main`.
    # The built-in process must serve HTTP instead of exiting immediately.
    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, proxy_headers=True)
