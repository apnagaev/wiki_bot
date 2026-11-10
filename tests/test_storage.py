"""Нужен PostgreSQL: PGHOST/PGPORT/PGUSER/PGDATABASE в окружении, иначе тест пропускается."""
import asyncio, os, sys, pathlib
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files/checklistbot/app"))
asyncpg = pytest.importorskip("asyncpg")
from cryptography.fernet import Fernet
from bot.storage import Storage

pytestmark = pytest.mark.skipif(not os.environ.get("PGHOST"), reason="нет PGHOST")

OLD = """CREATE TABLE checklist_users (tg_id BIGINT PRIMARY KEY, wiki_url TEXT NOT NULL,
  root_id TEXT NOT NULL, root_title TEXT NOT NULL DEFAULT '', wiki_user TEXT NOT NULL DEFAULT '',
  api_key BYTEA NOT NULL, hide_done BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""

def test_storage():
    async def run():
        key = Fernet.generate_key().decode()
        pool = await asyncpg.create_pool(min_size=1, max_size=4)
        await pool.execute("DROP TABLE IF EXISTS checklist_roots, checklist_users")
        await pool.execute(OLD)
        await pool.execute("INSERT INTO checklist_users (tg_id, wiki_url, root_id, root_title, api_key) VALUES (5,'https://w','r-old','Старый',$1)",
                           Fernet(key).encrypt(b"ol_api_old"))
        st = Storage(pool, key)
        await asyncio.gather(st.init(), Storage(pool, key).init())   # два узла разом
        await st.init()                                              # повторный старт
        u = await st.get(5)
        assert u.api_key == "ol_api_old" and [r.id for r in u.roots] == ["r-old"] and u.roots[0].title == "Старый"
        assert await st.add_root(5, "r2", "Семья") is True
        assert await st.add_root(5, "r2", "Семья 2") is False
        await st.rename_root(5, "r-old", "Checklists")
        u = await st.get(5)
        assert [(r.id, r.title) for r in u.roots] == [("r-old", "Checklists"), ("r2", "Семья 2")]
        assert u.is_root("r2") and not u.is_root("x")
        await st.save_user(5, "https://w", "A", "ol_api_new")
        assert (await st.get(5)).api_key == "ol_api_new"
        assert await st.remove_root(5, "r2") and not await st.remove_root(5, "r2")
        await st.set_hide(5, True); assert (await st.get(5)).hide_done
        assert await st.delete(5)
        assert await pool.fetchval("SELECT count(*) FROM checklist_roots") == 0   # каскад
        assert await Storage(pool, Fernet.generate_key().decode()).get(5) is None
        await pool.close()
    asyncio.run(run())
