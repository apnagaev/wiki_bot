import asyncio, sys, pathlib, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files/checklistbot/app"))
import aiohttp
from aiohttp import web
from bot.handlers import Handlers, Config
from bot.storage import User
from bot import checklist as cl

KEY = "ol_api_" + "x" * 38
DOCS = {
    "root-uuid": {"id": "root-uuid", "urlId": "tE5hSY73Kc", "title": "Checklists", "text": "", "parentDocumentId": None, "url": "/doc/checklists-tE5hSY73Kc"},
    "trip-uuid": {"id": "trip-uuid", "urlId": "SMQWdQQqCa", "title": "Travel list", "parentDocumentId": "root-uuid", "url": "/doc/travel-list-SMQWdQQqCa",
                  "text": "# Всегда\n\n" + "\n".join(f"- [ ] вещь {i}" for i in range(8)) + "\n\n\\"},
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
        return web.json_response({"data": dict(d)}) if d else web.json_response({}, status=404)
    if m == "documents.list":
        kids = [dict(d) for d in DOCS.values() if d["parentDocumentId"] == body["parentDocumentId"]]
        return web.json_response({"data": kids})
    if m == "documents.update":
        d = find(body["id"]); d["text"] = body["text"]; UPDATES.append(body["id"])
        return web.json_response({"data": dict(d)})
    return web.json_response({}, status=400)

class St:
    def __init__(s, u): s.u = u
    async def get(s, _): return s.u
    async def set_hide(s, _, h): s.u.hide_done = h

class Msg:
    def __init__(s): s.edits = []
    async def edit_text(s, text, reply_markup=None): s.edits.append((text, reply_markup))

def cb(data, msg):
    toasts = []
    async def answer(t=None, show_alert=False): toasts.append(t)
    return types.SimpleNamespace(data=data, message=msg, from_user=types.SimpleNamespace(id=1), answer=answer, toasts=toasts)

async def main():
    app = web.Application(); app.router.add_post("/api/{m}", api)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8765); await site.start()
    base = "http://127.0.0.1:8765"
    u = User(1, base, "root-uuid", "Checklists", "A", KEY, False)
    async with aiohttp.ClientSession() as http:
        h = Handlers(St(u), http, Config([base]))
        msg = Msg()
        # корень: кнопка списка с прогрессом
        await h.on_callback(cb("o|root-uuid|0", msg))
        labels = [b.text for r in msg.edits[-1][1].inline_keyboard for b in r]
        assert "📋 Travel list · 0/8" in labels, labels
        # открыть список и быстро отметить 5 пунктов параллельно
        await h.on_callback(cb("o|SMQWdQQqCa|0", msg))
        its = cl.items(cl.parse(DOCS["trip-uuid"]["text"]))
        cbs = [cb(f"t|SMQWdQQqCa|{i.index}|{i.key}|0", msg) for i in its[:5]]
        await asyncio.gather(*(h.on_callback(c) for c in cbs))
        assert cl.progress(DOCS["trip-uuid"]["text"]) == (5, 8), DOCS["trip-uuid"]["text"]
        assert DOCS["trip-uuid"]["text"].endswith("\n\n\\")
        assert "отмечено 5 из 8" in msg.edits[-1][0] and "⬅️ Назад" in [b.text for r in msg.edits[-1][1].inline_keyboard for b in r]
        # снять отметку
        await h.on_callback(cb(f"t|SMQWdQQqCa|0|{its[0].key}|0", msg))
        assert cl.progress(DOCS["trip-uuid"]["text"]) == (4, 8)
        # скрыть отмеченные
        await h.on_callback(cb("h|SMQWdQQqCa|0", msg))
        assert u.hide_done and "скрыты" in msg.edits[-1][0]
        # снять все
        await h.on_callback(cb("r|SMQWdQQqCa", msg)); await h.on_callback(cb("R|SMQWdQQqCa", msg))
        assert cl.progress(DOCS["trip-uuid"]["text"]) == (0, 8)
        # пункт удалили в вики — бот не отмечает чужой пункт
        stale = cl.items(cl.parse(DOCS["trip-uuid"]["text"]))[7]
        DOCS["trip-uuid"]["text"] = DOCS["trip-uuid"]["text"].replace("- [ ] вещь 7\n", "")
        c = cb(f"t|SMQWdQQqCa|7|{stale.key}|0", msg); n = len(UPDATES)
        await h.on_callback(c)
        assert len(UPDATES) == n and "изменился" in c.toasts[-1]
        # плохой ключ
        u.api_key = "ol_api_bad"
        c = cb("o|SMQWdQQqCa|0", msg); await h.on_callback(c)
        assert "ключ" in c.toasts[-1]
    await runner.cleanup()
    print("flow ok, updates:", len(UPDATES))

asyncio.run(main())
