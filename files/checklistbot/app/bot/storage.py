"""Пользователи бота в PostgreSQL. Ключ API вики хранится зашифрованным (Fernet)."""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS checklist_users (
    tg_id       BIGINT PRIMARY KEY,
    wiki_url    TEXT NOT NULL,
    root_id     TEXT NOT NULL,
    root_title  TEXT NOT NULL DEFAULT '',
    wiki_user   TEXT NOT NULL DEFAULT '',
    api_key     BYTEA NOT NULL,
    hide_done   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class User:
    tg_id: int
    wiki_url: str
    root_id: str
    root_title: str
    wiki_user: str
    api_key: str
    hide_done: bool


class Storage:
    def __init__(self, pool: asyncpg.Pool, secret: str):
        self.pool = pool
        self.f = Fernet(secret.encode())

    async def init(self) -> None:
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)

    async def get(self, tg_id: int) -> User | None:
        r = await self.pool.fetchrow("SELECT * FROM checklist_users WHERE tg_id=$1", tg_id)
        if not r:
            return None
        try:
            key = self.f.decrypt(bytes(r["api_key"])).decode()
        except InvalidToken:
            # сменили SECRET_KEY: ключ не расшифровать, пользователь пришлёт новый
            return None
        return User(r["tg_id"], r["wiki_url"], r["root_id"], r["root_title"],
                    r["wiki_user"], key, r["hide_done"])

    async def save(self, tg_id: int, wiki_url: str, root_id: str, root_title: str,
                   wiki_user: str, api_key: str) -> None:
        await self.pool.execute(
            """INSERT INTO checklist_users (tg_id, wiki_url, root_id, root_title, wiki_user, api_key)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (tg_id) DO UPDATE SET wiki_url=$2, root_id=$3, root_title=$4,
                   wiki_user=$5, api_key=$6, updated_at=now()""",
            tg_id, wiki_url, root_id, root_title, wiki_user, self.f.encrypt(api_key.encode()))

    async def set_hide(self, tg_id: int, hide: bool) -> None:
        await self.pool.execute(
            "UPDATE checklist_users SET hide_done=$2, updated_at=now() WHERE tg_id=$1", tg_id, hide)

    async def delete(self, tg_id: int) -> bool:
        res = await self.pool.execute("DELETE FROM checklist_users WHERE tg_id=$1", tg_id)
        return res.endswith(" 1")
