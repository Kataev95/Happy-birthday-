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
from app.db import Database
from app.scheduler import scheduler_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    db = Database(settings.database_path)
    await db.connect()
    bot = Bot(settings.bot_token)
    try:
        info = await bot.get_me()
        app.state.db = db
        app.state.bot = bot
        app.state.settings = settings
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть праздничное приложение"),
            BotCommand(command="holidays", description="Панель праздников в группе (администратор)"),
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
        dp.include_router(bot_router)
        poll_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        schedule_task = asyncio.create_task(
            scheduler_loop(db, bot, settings.check_interval_seconds)
        )
        log.info("Happy Birthday started as @%s", info.username)
        try:
            yield
        finally:
            for task in (poll_task, schedule_task):
                task.cancel()
            await asyncio.gather(poll_task, schedule_task, return_exceptions=True)
    finally:
        await bot.session.close()
        await db.close()


app = FastAPI(
    title="Happy Birthday Mini App",
    docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=lifespan,
)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    # BotHost starts the package with `python -m app.main`.
    # The built-in process must serve HTTP instead of exiting immediately.
    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, proxy_headers=True)
