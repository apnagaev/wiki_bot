"""Telegram: подключение вики, показ списков, отметки."""
from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from . import checklist as cl
from . import notes as nt
from .outline import (API_KEY_RE, AuthError, NotFound, Outline, OutlineError,
                      parse_doc_link)
from .storage import MAX_ROOTS, Storage, User

log = logging.getLogger("checklistbot")

PAGE_ROWS = 40          # строк с пунктами и заголовками на странице
MAX_CHILDREN = 30       # вложенных списков в меню
BTN_LEN = 60            # Telegram обрезает длинные кнопки сам, но так аккуратнее


LISTS_BTN = "📋 Списки"
NOTE_BTN = "📝 Заметка"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=LISTS_BTN), KeyboardButton(text=NOTE_BTN)]],
    resize_keyboard=True, is_persistent=True)


@dataclass
class Config:
    wiki_urls: list[str]
    allowed_ids: set[int] = field(default_factory=set)
    timezone: str = "Europe/Moscow"


def _cut(s: str, n: int = BTN_LEN) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _doc_key(doc: dict) -> str:
    return doc.get("urlId") or doc["id"]


def _progress(doc: dict) -> tuple[int, int]:
    t = doc.get("tasks")
    if isinstance(t, dict) and "total" in t:
        return int(t.get("completed", 0)), int(t.get("total", 0))
    return cl.progress(doc.get("text") or "")


def build_view(doc: dict, kids: list[dict], *, hide: bool, page: int, is_root: bool,
               web_url: str) -> tuple[str, InlineKeyboardMarkup]:
    """Сообщение и клавиатура для документа: его пункты + вложенные списки."""
    key = _doc_key(doc)
    entries = cl.parse(doc.get("text") or "")
    its = cl.items(entries)
    done = sum(1 for i in its if i.done)

    shown: list[cl.Item | cl.Heading] = [e for e in entries
                                         if not (hide and isinstance(e, cl.Item) and e.done)]
    # заголовок без пунктов под ним не показываем
    shown = [e for n, e in enumerate(shown)
             if isinstance(e, cl.Item)
             or any(isinstance(x, cl.Item) for x in _section(shown, n))]
    pages = max(1, -(-len(shown) // PAGE_ROWS))
    page = min(max(page, 0), pages - 1)
    chunk = shown[page * PAGE_ROWS:(page + 1) * PAGE_ROWS]

    lines = [f"<b>{html.escape(doc.get('title') or 'Без названия')}</b>"]
    if its:
        status = "🎉 всё отмечено" if done == len(its) else f"отмечено {done} из {len(its)}"
        if hide and done:
            status += " · отмеченные скрыты"
        if pages > 1:
            status += f" · стр. {page + 1}/{pages}"
        lines.append(status)
    elif kids:
        lines.append("Выберите список")
    else:
        lines.append("Здесь нет пунктов. Пункт в вики — строка «- [ ] текст».")

    rows: list[list[InlineKeyboardButton]] = []
    for e in chunk:
        if isinstance(e, cl.Heading):
            rows.append([InlineKeyboardButton(text=_cut(f"▸ {e.text}"), callback_data="n")])
        else:
            mark = "✅" if e.done else "⬜️"
            pad = "↳ " * e.level
            rows.append([InlineKeyboardButton(
                text=_cut(f"{mark} {pad}{e.text}"),
                callback_data=f"t|{key}|{e.index}|{e.key}|{page}")])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"o|{key}|{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="n"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"o|{key}|{page + 1}"))
        rows.append(nav)

    for k in kids[:MAX_CHILDREN]:
        d, t = _progress(k)
        icon = "📁" if not t else ("✅" if d == t else "📋")
        label = f"{icon} {k.get('title') or 'Без названия'}" + (f" · {d}/{t}" if t else "")
        rows.append([InlineKeyboardButton(text=_cut(label), callback_data=f"o|{_doc_key(k)}|0")])

    if its:
        ctl = [InlineKeyboardButton(
            text="👁 Показать все" if hide else "🙈 Скрыть отмеченные",
            callback_data=f"h|{key}|{page}")]
        if done:
            ctl.append(InlineKeyboardButton(text="↺ Снять все", callback_data=f"r|{key}"))
        rows.append(ctl)

    bottom = []
    parent = doc.get("parentDocumentId")
    if is_root:
        bottom.append(InlineKeyboardButton(text="📚 Разделы", callback_data="m"))
    elif parent:
        bottom.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"o|{parent}|0"))
    bottom.append(InlineKeyboardButton(text="🔄", callback_data=f"o|{key}|{page}"))
    bottom.append(InlineKeyboardButton(text="🌐 В вики", url=web_url))
    rows.append(bottom)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _section(entries: list, n: int) -> list:
    """Элементы под заголовком n до следующего заголовка того же или старшего уровня."""
    head = entries[n]
    if not isinstance(head, cl.Heading):
        return []
    out = []
    for e in entries[n + 1:]:
        if isinstance(e, cl.Heading) and e.level <= head.level:
            break
        out.append(e)
    return out


HELP = (
    "Бот показывает чек-листы из вики и даёт отмечать пункты. "
    "Отметки сохраняются прямо в документах вики, их видно и там.\n\n"
    "<b>Как подключить</b>\n"
    "1. Пришлите ссылку на раздел чек-листов в вики (документ, внутри которого лежат списки), "
    "например <code>{example}/doc/checklists-…</code>. Пункт списка в вики — строка "
    "«- [ ] текст» (чек-бокс).\n"
    "2. Пришлите свой API-ключ вики: в вики «Настройки → API» → новый ключ, он начинается с "
    "<code>ol_api_</code>. Сообщение с ключом бот сразу удалит, ключ хранится зашифрованным. "
    "Бот видит в вики ровно то, что видите вы.\n\n"
    "Разделов может быть несколько (свои, семейные, рабочие): чтобы добавить ещё один, "
    "просто пришлите ссылку на него.\n\n"
    "<b>Быстрые заметки</b>: кнопка «📝 Заметка» или <code>/note текст</code> — заметка "
    "добавится в конец выбранной страницы вики свёрнутым блоком с датой в заголовке.\n\n"
    "/lists — мои списки\n/sections — разделы: добавить, убрать\n"
    "/note — заметка\n/notes_page — сменить страницу заметок\n"
    "/setup — сменить ключ\n/forget — удалить мои данные из бота"
)

ADD_HINT = ("Чтобы добавить раздел, пришлите ссылку на него: откройте документ-раздел в вики "
            "и скопируйте адрес вида <code>{example}/doc/…</code>.")


def sections_view(u: User) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[InlineKeyboardButton(text=_cut(f"📁 {r.title or 'Без названия'}"),
                                  callback_data=f"o|{r.id}|0")] for r in u.roots]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="a")]
                + ([InlineKeyboardButton(text="✖️ Убрать", callback_data="s")] if u.roots else []))
    rows.append([InlineKeyboardButton(text=NOTE_BTN, callback_data="N")])
    text = "<b>Мои разделы</b>\n" + ("Выберите раздел" if u.roots else
                                     "Разделов нет. Пришлите ссылку на раздел чек-листов в вики.")
    if u.notes_id:
        text += f"\nЗаметки → «{html.escape(u.notes_title or 'Без названия')}»"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def remove_view(u: User) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[InlineKeyboardButton(text=_cut(f"✖️ {r.title or 'Без названия'}"),
                                  callback_data=f"d|{r.id}")] for r in u.roots]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m")])
    return ("Какой раздел убрать из бота? В вики ничего не удаляется.",
            InlineKeyboardMarkup(inline_keyboard=rows))


class Handlers:
    def __init__(self, storage: Storage, session: aiohttp.ClientSession, cfg: Config):
        self.st = storage
        self.http = session
        self.cfg = cfg
        self.pending: dict[int, dict] = defaultdict(dict)   # незавершённая настройка
        self.doc_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.router = Router()
        self.router.message.filter(F.chat.type == "private")
        self._register()

    # ---------- общее ----------

    @property
    def example(self) -> str:
        return html.escape(self.cfg.wiki_urls[0])

    def allowed(self, uid: int) -> bool:
        return not self.cfg.allowed_ids or uid in self.cfg.allowed_ids

    def client(self, u: User) -> Outline:
        return Outline(self.http, u.wiki_url, u.api_key)

    async def view(self, u: User, doc_id: str, page: int = 0,
                   doc: dict | None = None) -> tuple[str, InlineKeyboardMarkup]:
        oc = self.client(u)
        if doc is None:
            doc = await oc.info(doc_id)
        kids = await oc.children(doc["id"])
        is_root = u.is_root(doc["id"])
        if is_root and doc.get("title"):
            await self.st.rename_root(u.tg_id, doc["id"], doc["title"])   # переименовали в вики
        return build_view(doc, kids, hide=u.hide_done, page=page,
                          is_root=is_root, web_url=oc.web_url(doc))

    @staticmethod
    async def edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise

    @staticmethod
    def error_text(e: Exception) -> str:
        if isinstance(e, AuthError):
            return "Вики не принимает ваш API-ключ (истёк или отозван). Пришлите новый ключ."
        if isinstance(e, NotFound):
            return "Документ не найден или к нему нет доступа."
        return "Вики сейчас недоступна, попробуйте позже."

    # ---------- команды и сообщения ----------

    def _register(self) -> None:
        r = self.router

        @r.message(CommandStart())
        @r.message(Command("help"))
        async def start(m: Message):
            if not self.allowed(m.from_user.id):
                return await m.answer("Бот закрытый.")
            self.pending.pop(m.from_user.id, None)
            u = await self.st.get(m.from_user.id)
            if u and m.text.startswith("/start"):
                await m.answer("Кнопки «📋 Списки» и «📝 Заметка» — внизу.", reply_markup=MAIN_KB)
                if u.roots:
                    return await self.send_lists(m, u)
            await m.answer(HELP.format(example=self.example))

        @r.message(Command("lists"))
        async def lists(m: Message):
            self.pending.get(m.from_user.id, {}).pop("await", None)
            u = await self.st.get(m.from_user.id)
            if not u:
                return await m.answer(HELP.format(example=self.example))
            await self.send_lists(m, u)

        @r.message(Command("sections"))
        async def sections(m: Message):
            u = await self.st.get(m.from_user.id)
            if not u:
                return await m.answer(HELP.format(example=self.example))
            text, kb = sections_view(u)
            await m.answer(text, reply_markup=kb)

        @r.message(Command("note"))
        async def note(m: Message, command: CommandObject):
            await self.start_note(m, m.from_user.id, command.args)

        @r.message(Command("notes_page"))
        async def notes_page(m: Message):
            u = await self.st.get(m.from_user.id)
            if not u:
                return await m.answer(HELP.format(example=self.example))
            p = self.pending[m.from_user.id]
            p.pop("note", None)
            p["await"], p["then"] = "notes_link", None
            cur = f"Сейчас заметки идут в «{html.escape(u.notes_title)}». " if u.notes_id else ""
            await m.answer(cur + "Пришлите ссылку на страницу вики для заметок. /cancel — отмена")

        @r.message(Command("cancel"))
        async def cancel(m: Message):
            self.pending.pop(m.from_user.id, None)
            await m.answer("Отменено.", reply_markup=MAIN_KB)

        @r.message(Command("setup"))
        async def setup(m: Message):
            self.pending.pop(m.from_user.id, None)
            await m.answer("Пришлите новый API-ключ вики — он заменит старый, разделы останутся. "
                           "Новый раздел добавляется просто ссылкой на него, убрать — /sections.")

        @r.message(Command("forget"))
        async def forget(m: Message):
            self.pending.pop(m.from_user.id, None)
            gone = await self.st.delete(m.from_user.id)
            await m.answer("Разделы и ключ удалены из бота. Сам ключ отзовите в вики: «Настройки → API»."
                           if gone else "Данных о вас в боте нет.")

        @r.message(F.text)
        async def text(m: Message):
            await self.on_text(m)

    async def on_text(self, m: Message) -> None:
        uid = m.from_user.id
        if not self.allowed(uid):
            return await m.answer("Бот закрытый.")
        p = self.pending[uid]
        if m.text == LISTS_BTN:
            p.pop("await", None)
            u = await self.st.get(uid)
            return await (self.send_lists(m, u) if u else m.answer(HELP.format(example=self.example)))
        if m.text == NOTE_BTN:
            return await self.start_note(m, uid, None)
        km = API_KEY_RE.search(m.text)
        link = parse_doc_link(m.text, self.cfg.wiki_urls)
        wait = p.get("await")
        if wait == "note_text" and not km:          # ключ никогда не попадает в заметку
            return await self.save_note(m, uid, m.text)
        if wait == "notes_link" and not km:
            if link:
                return await self.set_notes_page(m, uid, link)
            return await m.answer("Нужна ссылка на страницу вики, например "
                                  f"<code>{self.example}/doc/zametki-…</code>. /cancel — отмена")
        if km:
            p["key"] = km.group(0)
            try:
                await m.delete()
            except TelegramBadRequest:
                pass
        if link:
            p["link"] = link
        if not km and not link:
            if "/doc/" in m.text:
                return await m.answer("Принимаю ссылки только на вики: "
                                      + ", ".join(self.cfg.wiki_urls))
            self.pending.pop(uid, None)
            return await m.answer("Не понял. /lists — списки, /help — как подключить.")
        await self.try_setup(m, uid)

    # ---------- заметки ----------

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.cfg.timezone))

    async def start_note(self, m: Message, uid: int, text: str | None) -> None:
        if not self.allowed(uid):
            return await m.answer("Бот закрытый.")
        u = await self.st.get(uid)
        if not u:
            return await m.answer(HELP.format(example=self.example))
        p = self.pending[uid]
        if text and text.strip():
            p["note"] = text
        if not u.notes_id:
            p["await"], p["then"] = "notes_link", "note"
            return await m.answer(
                "Куда складывать заметки? Пришлите ссылку на страницу вики (например, документ "
                "«Заметки»): каждая заметка добавится в её конец свёрнутым блоком с датой. "
                "/cancel — отмена")
        if p.get("note"):
            return await self.save_note(m, uid, p.pop("note"))
        p["await"] = "note_text"
        await m.answer(f"Напишите заметку — добавлю её в конец «{html.escape(u.notes_title)}». "
                       "/cancel — отмена")

    async def set_notes_page(self, m: Message, uid: int, link: tuple[str, str]) -> None:
        u = await self.st.get(uid)
        p = self.pending[uid]
        if link[0] != u.wiki_url:
            return await m.answer("Страница заметок должна быть в той же вики, что и разделы.")
        try:
            doc = await self.client(u).info(link[1])
        except OutlineError as e:
            return await m.answer(self.error_text(e))
        title = doc.get("title") or ""
        await self.st.set_notes(uid, doc["id"], title)
        p.pop("await", None)
        then = p.pop("then", None)
        await m.answer(f"Заметки будут добавляться в конец «{html.escape(title)}» ✅", reply_markup=MAIN_KB)
        if p.get("note"):
            await self.save_note(m, uid, p.pop("note"))
        elif then == "note":
            p["await"] = "note_text"
            await m.answer("Теперь напишите заметку. /cancel — отмена")

    async def save_note(self, m: Message, uid: int, text: str) -> None:
        u = await self.st.get(uid)
        p = self.pending[uid]
        if not text.strip():
            return await m.answer("Пустая заметка. Напишите текст или /cancel.")
        now = self.now()
        oc = self.client(u)
        try:
            async with self.doc_locks[u.notes_id]:
                doc = await oc.append_text(u.notes_id, nt.note_markdown(text, now))
        except OutlineError as e:
            log.info("note %s: %s", uid, e)
            if isinstance(e, NotFound):
                p["await"], p["then"], p["note"] = "notes_link", None, text
                return await m.answer("Страница заметок недоступна (удалена или нет доступа). "
                                      "Пришлите ссылку на новую — заметка сохранится туда. /cancel — отмена")
            return await m.answer(self.error_text(e) + " Заметка не сохранена, текст выше — пришлите его ещё раз.")
        p.pop("await", None)
        title = doc.get("title") or u.notes_title
        if title != u.notes_title:
            await self.st.set_notes(uid, u.notes_id, title)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🌐 Открыть", url=oc.web_url(doc) if doc.get("url") else u.wiki_url),
            InlineKeyboardButton(text="📝 Ещё заметка", callback_data="N")]])
        await m.answer(f"📝 Сохранено в «{html.escape(title)}» · {nt.stamp(now)}", reply_markup=kb)

    async def try_setup(self, m: Message, uid: int) -> None:
        """Ссылка добавляет раздел, ключ заменяет ключ. Порядок любой."""
        p = self.pending[uid]
        old = await self.st.get(uid)
        link = p.get("link")
        key = p.get("key") or (old.api_key if old else None)
        base = link[0] if link else (old.wiki_url if old else None)

        if old and old.roots and link and base != old.wiki_url:
            self.pending.pop(uid, None)
            return await m.answer("Ваши разделы подключены из другой вики. Разделы из разных вики "
                                  "одновременно не поддерживаются: сначала /forget.")
        if not key:
            return await m.answer("Ссылка получена ✅. Теперь пришлите API-ключ вики "
                                  "(«Настройки → API» → новый ключ).")
        if not base:
            return await m.answer("Ключ получен ✅. Теперь пришлите ссылку на раздел чек-листов.")
        if link and old and not old.is_root(link[1]) and len(old.roots) >= MAX_ROOTS:
            self.pending.pop(uid, None)
            return await m.answer(f"Подключено максимум разделов ({MAX_ROOTS}). Уберите лишние: /sections.")

        oc = Outline(self.http, base, key)
        try:
            who = await oc.whoami()
            root = await oc.info(link[1]) if link else None
        except OutlineError as e:
            if isinstance(e, AuthError):
                p.pop("key", None)
            elif isinstance(e, NotFound):
                p.pop("link", None)
            log.info("setup %s: %s", uid, e.__class__.__name__)
            return await m.answer(self.error_text(e))

        await self.st.save_user(uid, base, who, key)
        self.pending.pop(uid, None)
        notes = []
        if p.get("key") and old:
            notes.append("Ключ обновлён ✅")
        if root:
            title = html.escape(root.get("title") or "")
            if await self.st.add_root(uid, root["id"], root.get("title") or ""):
                log.info("user %s added root %s", uid, root["id"])
                notes.append(f"Раздел «{title}» подключён ✅")
            else:
                notes.append(f"Раздел «{title}» уже был подключён")
        elif not old:
            notes.append("Ключ получен ✅. Теперь пришлите ссылку на раздел чек-листов.")
        await m.answer(f"{html.escape(who)}: " + ". ".join(notes), reply_markup=MAIN_KB)

        u = await self.st.get(uid)
        if root:
            await self.send_doc(m, u, root["id"], root)
        elif u.roots:
            await self.send_lists(m, u)

    async def send_doc(self, m: Message, u: User, doc_id: str, doc: dict | None = None) -> None:
        try:
            text, kb = await self.view(u, doc_id, doc=doc)
        except OutlineError as e:
            return await m.answer(self.error_text(e))
        await m.answer(text, reply_markup=kb)

    async def send_lists(self, m: Message, u: User) -> None:
        if len(u.roots) == 1:
            return await self.send_doc(m, u, u.roots[0].id)
        text, kb = sections_view(u)
        await m.answer(text, reply_markup=kb)

    # ---------- кнопки ----------

    async def on_callback(self, cb: CallbackQuery) -> None:
        data = (cb.data or "").split("|")
        if data[0] == "n":
            return await cb.answer()
        u = await self.st.get(cb.from_user.id)
        if not u or not self.allowed(cb.from_user.id):
            return await cb.answer("Сначала подключите вики: /start", show_alert=True)
        try:
            await self._dispatch(cb, u, data)
        except OutlineError as e:
            log.info("callback %s %s: %s", cb.from_user.id, data[0], e)
            await cb.answer(self.error_text(e), show_alert=True)

    async def _dispatch(self, cb: CallbackQuery, u: User, data: list[str]) -> None:
        op = data[0]
        if op == "m":
            await self.edit(cb, *sections_view(u))
            return await cb.answer()

        if op == "N":
            await cb.answer()
            return await self.start_note(cb.message, u.tg_id, None)

        if op == "a":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Назад", callback_data="m")]])
            await self.edit(cb, ADD_HINT.format(example=self.example), kb)
            return await cb.answer()

        if op == "s":
            await self.edit(cb, *remove_view(u))
            return await cb.answer()

        if op == "d":
            title = next((r.title for r in u.roots if r.id == data[1]), "")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, убрать", callback_data=f"D|{data[1]}"),
                InlineKeyboardButton(text="Отмена", callback_data="s")]])
            await self.edit(cb, f"Убрать раздел «{html.escape(title)}» из бота? "
                                "Документы в вики останутся как есть.", kb)
            return await cb.answer()

        if op == "D":
            await self.st.remove_root(u.tg_id, data[1])
            u.roots = [r for r in u.roots if r.id != data[1]]
            await self.edit(cb, *sections_view(u))
            return await cb.answer("Раздел убран")

        if op == "o":
            text, kb = await self.view(u, data[1], int(data[2]))
            await self.edit(cb, text, kb)
            return await cb.answer()

        if op == "h":
            u.hide_done = not u.hide_done
            await self.st.set_hide(u.tg_id, u.hide_done)
            text, kb = await self.view(u, data[1], int(data[2]))
            await self.edit(cb, text, kb)
            return await cb.answer()

        if op == "r":
            doc = await self.client(u).info(data[1])
            d, _ = _progress(doc)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Да, снять ({d})", callback_data=f"R|{data[1]}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"o|{data[1]}|0")]])
            await self.edit(cb, f"Снять все отметки в «{html.escape(doc.get('title') or '')}»?", kb)
            return await cb.answer()

        if op in ("t", "R"):
            oc = self.client(u)
            async with self.doc_locks[data[1]]:        # быстрые нажатия подряд не теряются
                doc = await oc.info(data[1])
                text = doc.get("text") or ""
                if op == "t":
                    item = cl.find(text, int(data[2]), data[3])
                    page = int(data[4])
                    if item is None:
                        v = await self.view(u, doc["id"], page, doc)
                        await self.edit(cb, *v)
                        return await cb.answer("Список изменился в вики — показываю актуальный.")
                    new = cl.set_item(text, item, not item.done)
                    toast = ("✅ " if not item.done else "⬜️ ") + _cut(item.text, 40)
                else:
                    page = 0
                    new, n = cl.set_all(text, False)
                    toast = f"Снято отметок: {n}"
                await oc.update_text(doc["id"], new)
            doc = {**doc, "text": new}
            doc.pop("tasks", None)          # прогресс пересчитаем по тексту
            v = await self.view(u, doc["id"], page, doc)
            await self.edit(cb, *v)
            return await cb.answer(toast)

        await cb.answer()


def setup_router(h: Handlers) -> Router:
    h.router.callback_query.register(h.on_callback)
    return h.router


async def set_commands(bot: Bot) -> None:
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="lists", description="Мои списки"),
        BotCommand(command="note", description="Быстрая заметка"),
        BotCommand(command="sections", description="Разделы: добавить, убрать"),
        BotCommand(command="notes_page", description="Сменить страницу заметок"),
        BotCommand(command="setup", description="Сменить ключ API"),
        BotCommand(command="forget", description="Удалить мои данные"),
        BotCommand(command="help", description="Как подключить"),
    ])
