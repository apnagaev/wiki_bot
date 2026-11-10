"""Сценарий целиком: имитация API Outline + настоящий PostgreSQL.

Запуск: PGHOST=… PGPORT=… PGUSER=… PGDATABASE=… python3 tests/test_flow.py
(база очищается: таблицы checklist_* пересоздаются).
"""
import asyncio, os, sys, pathlib, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files/checklistbot/app"))
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp, asyncpg
from aiohttp import web
from cryptography.fernet import Fernet
from bot.handlers import Handlers, Config, NOTE_BTN, LISTS_BTN
from bot.storage import Storage
from bot import checklist as cl

KEY = "ol_api_" + "x" * 38
BASE = "http://127.0.0.1:8765"
DOCS = {
    "root-uuid": {"id": "root-uuid", "urlId": "tE5hSY73Kc", "title": "Checklists", "text": "", "parentDocumentId": None, "url": "/doc/checklists-tE5hSY73Kc"},
    "trip-uuid": {"id": "trip-uuid", "urlId": "SMQWdQQqCa", "title": "Travel list", "parentDocumentId": "root-uuid", "url": "/doc/travel-list-SMQWdQQqCa",
                  "text": "# Всегда\n\n" + "\n".join(f"- [ ] вещь {i}" for i in range(8)) + "\n\n\\"},
    "fam-uuid": {"id": "fam-uuid", "urlId": "FaMiLy0001", "title": "Семья", "text": "- [ ] молоко", "parentDocumentId": None, "url": "/doc/semya-FaMiLy0001"},
    "notes-uuid": {"id": "notes-uuid", "urlId": "NoTeS00001", "title": "Заметки", "text": "Мои заметки", "parentDocumentId": None, "url": "/doc/zametki-NoTeS00001"},
    "secret-uuid": {"id": "secret-uuid", "urlId": "SeCrEt0001", "title": "Чужое", "text": "", "parentDocumentId": None, "url": "/doc/x-SeCrEt0001", "deny": True},
}
UPDATES = []


def find(i):
    return next((d for d in DOCS.values() if i in (d["id"], d["urlId"])), None)


async def api(req):
    if req.headers.get("Authorization") != f"Bearer {KEY}":
        return web.json_response({"ok": False}, status=401)
    m = req.match_info["m"]; body = await req.json()
    await asyncio.sleep(0.02)          # задержка как у сети — проверяем гонки
    if m == "auth.info":
        return web.json_response({"data": {"user": {"name": "Aleksandr"}}})
    if m == "documents.info":
        d = find(body["id"])
        if not d or d.get("deny"):
            return web.json_response({}, status=404)
        return web.json_response({"data": dict(d)})
    if m == "documents.list":
        kids = [dict(d) for d in DOCS.values() if d["parentDocumentId"] == body["parentDocumentId"]]
        return web.json_response({"data": kids})
    if m == "documents.update":
        d = find(body["id"])
        if d.get("deny"):
            return web.json_response({}, status=404)
        d["text"] = d["text"] + body["text"] if body.get("append") else body["text"]
        UPDATES.append(body["id"])
        return web.json_response({"data": dict(d)})
    return web.json_response({}, status=400)


class Chat:
    """Личный чат: ответы бота и правки сообщения с кнопками."""
    def __init__(s): s.out = []; s.edits = []; s.deleted = 0

    def msg(s, text):
        async def answer(t, reply_markup=None): s.out.append((t, reply_markup))
        async def delete(): s.deleted += 1
        return types.SimpleNamespace(text=text, from_user=types.SimpleNamespace(id=1), answer=answer, delete=delete)

    def cb(s, data):
        toasts = []
        async def edit_text(t, reply_markup=None): s.edits.append((t, reply_markup))
        async def answer(t=None, show_alert=False): toasts.append(t)
        async def send(t, reply_markup=None): s.out.append((t, reply_markup))
        return types.SimpleNamespace(data=data, message=types.SimpleNamespace(edit_text=edit_text, answer=send),
                                     from_user=types.SimpleNamespace(id=1), answer=answer, toasts=toasts)


def labels(kb):
    return [b.text for r in kb.inline_keyboard for b in r]


def cbs(kb):
    return {b.text: b.callback_data for r in kb.inline_keyboard for b in r}


async def main():
    app = web.Application(); app.router.add_post("/api/{m}", api)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8765).start()
    pool = await asyncpg.create_pool(min_size=1, max_size=4)
    await pool.execute("DROP TABLE IF EXISTS checklist_roots, checklist_users")
    st = Storage(pool, Fernet.generate_key().decode()); await st.init()
    c = Chat()
    async with aiohttp.ClientSession() as http:
        h = Handlers(st, http, Config([BASE]))

        # подключение: ссылка, потом ключ
        await h.on_text(c.msg(f"вот {BASE}/doc/checklists-tE5hSY73Kc"))
        assert "пришлите API-ключ" in c.out[-1][0]
        await h.on_text(c.msg(KEY))
        assert c.deleted == 1
        assert "Раздел «Checklists» подключён" in c.out[-2][0], c.out
        assert "📋 Travel list · 0/8" in labels(c.out[-1][1]) and "📚 Разделы" in labels(c.out[-1][1])

        # один раздел: /lists сразу открывает его
        u = await st.get(1); await h.send_lists(c.msg("/lists"), u)
        assert c.out[-1][0].startswith("<b>Checklists</b>")

        # второй раздел — одной ссылкой, ключ не нужен
        await h.on_text(c.msg(f"{BASE}/doc/semya-FaMiLy0001"))
        assert "Раздел «Семья» подключён" in c.out[-2][0]
        assert "⬜️ молоко" in labels(c.out[-1][1]) and "📚 Разделы" in labels(c.out[-1][1])
        await h.on_text(c.msg(f"{BASE}/doc/semya-FaMiLy0001"))
        assert "уже был подключён" in c.out[-2][0]

        # нет доступа — раздел не добавляется
        await h.on_text(c.msg(f"{BASE}/doc/x-SeCrEt0001"))
        assert "нет доступа" in c.out[-1][0]
        # чужая вики
        await h.on_text(c.msg("https://evil.example/doc/x-SeCrEt0001"))
        assert "только на вики" in c.out[-1][0]

        # несколько разделов: /lists — меню разделов
        u = await st.get(1); await h.send_lists(c.msg("/lists"), u)
        assert labels(c.out[-1][1]) == ["📁 Checklists", "📁 Семья", "➕ Добавить", "✖️ Убрать", NOTE_BTN]

        # список: параллельные отметки не теряются
        await h.on_callback(c.cb("o|SMQWdQQqCa|0"))
        its = cl.items(cl.parse(DOCS["trip-uuid"]["text"]))
        await asyncio.gather(*(h.on_callback(c.cb(f"t|SMQWdQQqCa|{i.index}|{i.key}|0")) for i in its[:5]))
        assert cl.progress(DOCS["trip-uuid"]["text"]) == (5, 8)
        assert DOCS["trip-uuid"]["text"].endswith("\n\n\\")
        assert "отмечено 5 из 8" in c.edits[-1][0] and "⬅️ Назад" in labels(c.edits[-1][1])
        # «Назад» из списка ведёт в раздел, из раздела — «Разделы»
        await h.on_callback(c.cb(cbs(c.edits[-1][1])["⬅️ Назад"]))
        assert c.edits[-1][0].startswith("<b>Checklists</b>") and "📋 Travel list · 5/8" in labels(c.edits[-1][1])
        await h.on_callback(c.cb("m"))
        assert "Мои разделы" in c.edits[-1][0]

        # скрыть отмеченные, снять все
        await h.on_callback(c.cb("h|SMQWdQQqCa|0"))
        assert (await st.get(1)).hide_done and "скрыты" in c.edits[-1][0]
        await h.on_callback(c.cb("r|SMQWdQQqCa")); await h.on_callback(c.cb("R|SMQWdQQqCa"))
        assert cl.progress(DOCS["trip-uuid"]["text"]) == (0, 8)

        # пункт удалили в вики — бот не отмечает другой пункт
        stale = cl.items(cl.parse(DOCS["trip-uuid"]["text"]))[7]
        DOCS["trip-uuid"]["text"] = DOCS["trip-uuid"]["text"].replace("- [ ] вещь 7\n", "")
        cb = c.cb(f"t|SMQWdQQqCa|7|{stale.key}|0"); n = len(UPDATES)
        await h.on_callback(cb)
        assert len(UPDATES) == n and "изменился" in cb.toasts[-1]

        # раздел переименовали в вики — название в меню обновится
        DOCS["fam-uuid"]["title"] = "Семья и дача"
        await h.on_callback(c.cb("o|fam-uuid|0")); await h.on_callback(c.cb("m"))
        assert "📁 Семья и дача" in labels(c.edits[-1][1])

        # убрать раздел
        await h.on_callback(c.cb("s")); await h.on_callback(c.cb("d|fam-uuid"))
        assert "Семья и дача" in c.edits[-1][0]
        cb = c.cb("D|fam-uuid"); await h.on_callback(cb)
        assert labels(c.edits[-1][1]) == ["📁 Checklists", "➕ Добавить", "✖️ Убрать", NOTE_BTN] and cb.toasts[-1] == "Раздел убран"
        assert "Семья" in DOCS["fam-uuid"]["title"]          # в вики ничего не тронуто

        # смена ключа: разделы остаются
        await h.on_text(c.msg(KEY))
        assert "Ключ обновлён" in c.out[-2][0] and len((await st.get(1)).roots) == 1

        # заметки: страницы ещё нет — бот спрашивает ссылку, потом текст
        await h.on_text(c.msg(NOTE_BTN))
        assert "Куда складывать заметки" in c.out[-1][0]
        await h.on_text(c.msg("просто текст"))                      # не ссылка — переспросит
        assert "Нужна ссылка" in c.out[-1][0]
        await h.on_text(c.msg(f"{BASE}/doc/zametki-NoTeS00001"))
        assert "в конец «Заметки»" in c.out[-2][0] and "напишите заметку" in c.out[-1][0]
        before = datetime.now(ZoneInfo("Europe/Moscow")).replace(microsecond=0)
        await h.on_text(c.msg(f"Купить молоко\n+++\nи хлеб {BASE}/doc/x-SeCrEt0001"))   # ссылка внутри — это текст заметки
        t = DOCS["notes-uuid"]["text"]
        m = re.search(r"\n\n\+\+\+\n## (\d{4}\.\d\d\.\d\d \d\d:\d\d:\d\d)\nКупить молоко\n\\\+\+\+\nи хлеб .*\n\+\+\+\n$", t)
        assert m, t
        stamp = datetime.strptime(m.group(1), "%Y.%m.%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Moscow"))
        assert 0 <= (stamp - before).total_seconds() < 5            # время московское
        assert t.startswith("Мои заметки") and c.out[-1][0].startswith("📝 Сохранено в «Заметки»")
        assert len((await st.get(1)).roots) == 1                     # ссылка не стала разделом
        # /note текст — сразу, без вопросов; кнопка «Ещё заметка»
        await h.start_note(c.msg("/note"), 1, "вторая")
        assert DOCS["notes-uuid"]["text"].endswith("\nвторая\n+++\n")
        await h.on_callback(c.cb("N")); assert "Напишите заметку" in c.out[-1][0]
        # ключ во время ожидания заметки — не попадает в заметку
        n = len(DOCS["notes-uuid"]["text"])
        await h.on_text(c.msg(KEY))
        assert len(DOCS["notes-uuid"]["text"]) == n and "Ключ обновлён" in c.out[-2][0]
        # меню разделов показывает страницу заметок
        await h.on_callback(c.cb("m")); assert "Заметки → «Заметки»" in c.edits[-1][0]
        # страница заметок пропала — бот просит новую и сохраняет туда же текст
        DOCS["notes-uuid"]["deny"] = True
        await h.start_note(c.msg("/note"), 1, "потерянная")
        assert "недоступна" in c.out[-1][0]
        DOCS["fam-uuid"]["text"] = "Семейное"
        await h.on_text(c.msg(f"{BASE}/doc/semya-FaMiLy0001"))
        assert DOCS["fam-uuid"]["text"].endswith("\npotерянная\n+++\n".replace("potерянная", "потерянная"))
        assert (await st.get(1)).notes_title == "Семья и дача"
        # /cancel
        await h.on_text(c.msg(NOTE_BTN)); h.pending.pop(1, None)
        await h.on_text(c.msg("что-то"))
        assert "Не понял" in c.out[-1][0]

        # плохой ключ в базе
        await pool.execute("UPDATE checklist_users SET api_key=$1", st.f.encrypt(b"ol_api_bad"))
        cb = c.cb("o|SMQWdQQqCa|0"); await h.on_callback(cb)
        assert "ключ" in cb.toasts[-1]

    await pool.execute("DROP TABLE checklist_roots, checklist_users")
    await pool.close(); await runner.cleanup()
    print("flow ok, updates:", len(UPDATES))


if __name__ == "__main__":
    if not os.environ.get("PGHOST"):
        sys.exit("нужен PostgreSQL: задайте PGHOST, PGPORT, PGUSER, PGDATABASE")
    asyncio.run(main())
