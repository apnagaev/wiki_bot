"""Быстрые заметки: свёрнутый блок (toggle) с заголовком-датой в конце документа."""
from __future__ import annotations

import re
from datetime import datetime

FENCE = re.compile(r"^\s*\+{3,}\s*$")


def stamp(now: datetime) -> str:
    return now.strftime("%Y.%m.%d %H:%M:%S")


def note_markdown(text: str, now: datetime) -> str:
    """Markdown для добавления в конец документа.

    В Outline свёрнутый блок записывается как
        +++
        ## заголовок
        содержимое
        +++
    Строки из одних плюсов в тексте экранируются, чтобы не закрыть блок раньше времени.
    """
    lines = [("\\" + ln) if FENCE.match(ln) else ln for ln in text.strip("\n").split("\n")]
    body = "\n".join(lines).strip() or "—"
    return f"\n\n+++\n## {stamp(now)}\n{body}\n+++\n"
