"""Пользователи бота и их разделы в PostgreSQL. Ключ API вики хранится зашифрованным (Fernet)."""
from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

MAX_ROOTS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS checklist_users (
    tg_id       BIGINT PRIMARY KEY,
    wiki_url    TEXT NOT NULL,
    wiki_user   TEXT NOT NULL DEFAULT '',
    api_key     BYTEA NOT NULL,
    hide_done   BOOLEAN NOT NULL DEFAULT FALSE,
    notes_id    TEXT NOT NULL DEFAULT '',
    notes_title TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS checklist_roots (
    tg_id       BIGINT NOT NULL REFERENCES checklist_users(tg_id) ON DELETE CASCADE,
    root_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, root_id)
);
"""

# первая версия хранила один раздел в checklist_users.root_id
MIGRATE = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='checklist_users' AND column_name='root_id') THEN
    INSERT INTO checklist_roots (tg_id, root_id, title)
      SELECT tg_id, root_id, root_title FROM checklist_users WHERE root_id <> ''
      ON CONFLICT DO NOTHING;
    ALTER TABLE checklist_users DROP COLUMN root_id, DROP COLUMN IF EXISTS root_title;
  END IF;
END $$;
ALTER TABLE checklist_users ADD COLUMN IF NOT EXISTS notes_id TEXT NOT NULL DEFAULT '';
ALTER TABLE checklist_users ADD COLUMN IF NOT EXISTS notes_title TEXT NOT NULL DEFAULT '';
"""


@dataclass
class Root:
    id: str
    title: str


@dataclass
class User:
    tg_id: int
    wiki_url: str
    wiki_user: str
    api_key: str
    hide_done: bool
    roots: list[Root] = field(default_factory=list)
    notes_id: str = ""
    notes_title: str = ""

    def is_root(self, doc_id: str) -> bool:
        return any(r.id == doc_id for r in self.roots)


class Storage:
    def __init__(self, pool: asyncpg.Pool, secret: str):
        self.pool = pool
        self.f = Fernet(secret.encode())

    async def init(self) -> None:
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(1667787629)")  # оба узла стартуют разом
                await c.execute(SCHEMA)
                await c.execute(MIGRATE)

    async def get(self, tg_id: int) -> User | None:
        r = await self.pool.fetchrow("SELECT * FROM checklist_users WHERE tg_id=$1", tg_id)
        if not r:
            return None
        try:
            key = self.f.decrypt(bytes(r["api_key"])).decode()
        except InvalidToken:
            # сменили SECRET_KEY: ключ не расшифровать, пользователь пришлёт новый
            return None
        roots = await self.pool.fetch(
            "SELECT root_id, title FROM checklist_roots WHERE tg_id=$1 ORDER BY added_at, title", tg_id)
        return User(r["tg_id"], r["wiki_url"], r["wiki_user"], key, r["hide_done"],
                    [Root(x["root_id"], x["title"]) for x in roots],
                    r["notes_id"], r["notes_title"])

    async def save_user(self, tg_id: int, wiki_url: str, wiki_user: str, api_key: str) -> None:
        await self.pool.execute(
            """INSERT INTO checklist_users (tg_id, wiki_url, wiki_user, api_key)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (tg_id) DO UPDATE SET wiki_url=$2, wiki_user=$3, api_key=$4, updated_at=now()""",
            tg_id, wiki_url, wiki_user, self.f.encrypt(api_key.encode()))

    async def add_root(self, tg_id: int, root_id: str, title: str) -> bool:
        """True — раздел добавлен, False — уже был (название обновлено)."""
        res = await self.pool.fetchval(
            """INSERT INTO checklist_roots (tg_id, root_id, title) VALUES ($1,$2,$3)
               ON CONFLICT (tg_id, root_id) DO UPDATE SET title=$3
               RETURNING (xmax = 0)""", tg_id, root_id, title)
        return bool(res)

    async def rename_root(self, tg_id: int, root_id: str, title: str) -> None:
        await self.pool.execute(
            "UPDATE checklist_roots SET title=$3 WHERE tg_id=$1 AND root_id=$2 AND title<>$3",
            tg_id, root_id, title)

    async def remove_root(self, tg_id: int, root_id: str) -> bool:
        res = await self.pool.execute(
            "DELETE FROM checklist_roots WHERE tg_id=$1 AND root_id=$2", tg_id, root_id)
        return res.endswith(" 1")

    async def set_notes(self, tg_id: int, doc_id: str, title: str) -> None:
        await self.pool.execute(
            "UPDATE checklist_users SET notes_id=$2, notes_title=$3, updated_at=now() WHERE tg_id=$1",
            tg_id, doc_id, title)

    async def set_hide(self, tg_id: int, hide: bool) -> None:
        await self.pool.execute(
            "UPDATE checklist_users SET hide_done=$2, updated_at=now() WHERE tg_id=$1", tg_id, hide)

    async def delete(self, tg_id: int) -> bool:
        res = await self.pool.execute("DELETE FROM checklist_users WHERE tg_id=$1", tg_id)
        return res.endswith(" 1")
