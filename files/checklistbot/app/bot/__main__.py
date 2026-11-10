"""Запуск. На каждом узле работает свой экземпляр; Telegram опрашивает только тот,
кто держит advisory lock в PostgreSQL. Остальные ждут и подхватывают работу,
если активный узел пропал (его соединение с базой рвётся, блокировка снимается)."""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
import time
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from .handlers import Config, Handlers, set_commands, setup_router
from .storage import Storage

log = logging.getLogger("checklistbot")
HEARTBEAT = pathlib.Path(os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat"))
LOCK_ID = int(os.environ.get("LOCK_ID", "1667787628"))   # "chkl"


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None or v == "":
        sys.exit(f"не задана переменная {name}")
    return v


async def heartbeat() -> None:
    while True:
        HEARTBEAT.write_text(str(int(time.time())))
        await asyncio.sleep(20)


async def hold_lock(conn: asyncpg.Connection) -> None:
    """Проверяет соединение с блокировкой; при обрыве — выход, Docker перезапустит."""
    while True:
        await asyncio.sleep(15)
        try:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), 10)
        except Exception as e:  # noqa: BLE001
            log.error("соединение с блокировкой потеряно (%s), выхожу", e)
            os._exit(1)


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    token = env("BOT_TOKEN")
    cfg = Config(
        wiki_urls=[u.strip().rstrip("/") for u in env("WIKI_URLS").split(",") if u.strip()],
        allowed_ids={int(x) for x in os.environ.get("ALLOWED_TG_IDS", "").replace(" ", "").split(",") if x},
        timezone=os.environ.get("TIMEZONE") or "Europe/Moscow",
    )
    ZoneInfo(cfg.timezone)   # опечатка в поясе — ошибка сразу при старте
    # подключение к базе — из стандартных переменных PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
    pool = await asyncpg.create_pool(min_size=1, max_size=5)
    storage = Storage(pool, env("SECRET_KEY"))
    await storage.init()

    asyncio.create_task(heartbeat())

    lock = await asyncpg.connect()
    waiting = False
    while not await lock.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID):
        if not waiting:
            log.info("резерв: другой узел опрашивает Telegram, жду")
            waiting = True
        await asyncio.sleep(10)
    log.info("активный узел: опрашиваю Telegram")
    asyncio.create_task(hold_lock(lock))

    proxy = os.environ.get("TELEGRAM_PROXY") or None
    bot = Bot(token, session=AiohttpSession(proxy=proxy),
              default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    async with aiohttp.ClientSession() as http:
        dp = Dispatcher()
        dp.include_router(setup_router(Handlers(storage, http, cfg)))
        await bot.delete_webhook(drop_pending_updates=False)
        await set_commands(bot)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"],
                               handle_signals=True)


if __name__ == "__main__":
    asyncio.run(main())
