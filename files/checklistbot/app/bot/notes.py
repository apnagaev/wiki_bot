"""Быстрые заметки: свёрнутый блок (toggle) с заголовком-датой в начале документа."""
from __future__ import annotations

import re
from datetime import datetime

FENCE = re.compile(r"^\s*\+{3,}\s*$")


def stamp(now: datetime) -> str:
    return now.strftime("%Y.%m.%d %H:%M:%S")


def note_markdown(text: str, now: datetime) -> str:
    """Markdown для добавления в начало документа (новые заметки сверху).

    В Outline свёрнутый блок записывается как
        +++
        ## заголовок
        содержимое
        +++
    Строки из одних плюсов в тексте экранируются, чтобы не закрыть блок раньше времени.
    """
    lines = [("\\" + ln) if FENCE.match(ln) else ln for ln in text.strip("\n").split("\n")]
    body = "\n".join(lines).strip() or "—"
    return f"+++\n## {stamp(now)}\n{body}\n+++\n\n"


def looks_replaced(old: str, new: str | None, added: str) -> bool:
    """Сервер без editMode (Outline старше 2026) молча заменяет документ целиком.

    Тогда в ответе только добавленный блок: текст короче, чем старый документ плюс заметка.
    Выгрузка Outline только удлиняет markdown (+++ → +++++, пустые строки), так что
    после настоящего prepend ответ не короче суммы.
    """
    if new is None or not old.strip():
        return False
    return len(new) < len(added.strip()) + len(old.strip()) // 2
