"""Разбор и правка чек-листов в markdown Outline.

Пункт — строка вида "- [ ] текст" / "- [x] текст" (также *, +, "1."),
заголовок — "# текст". Всё остальное в документе не трогается: при отметке
меняется ровно один символ внутри квадратных скобок.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

TASK_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:[-*+]|\d+[.)])[ \t]+\[(?P<mark>[ xX])\](?P<rest>.*)$"
)
HEAD_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
ESC_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~<>&=])")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
EMPH_RE = re.compile(r"(\*\*|__|~~|==)(.+?)\1")


@dataclass
class Item:
    index: int      # порядковый номер пункта в документе
    line: int       # номер строки
    text: str       # текст для показа
    done: bool
    level: int      # вложенность
    key: str        # короткий хеш текста: защита от сдвига пунктов


@dataclass
class Heading:
    text: str
    level: int


def clean(raw: str) -> str:
    s = raw.strip()
    s = LINK_RE.sub(r"\1", s)
    s = EMPH_RE.sub(r"\2", s)
    s = ESC_RE.sub(r"\1", s)
    s = s.rstrip("\\").strip()
    return s


def item_key(raw: str) -> str:
    return format(zlib.crc32(raw.strip().encode("utf-8")) & 0xFFFFFFFF, "08x")[:6]


def _level(indent: str) -> int:
    width = len(indent.replace("\t", "    "))
    return min(width // 2, 3)


def parse(text: str) -> list[Item | Heading]:
    entries: list[Item | Heading] = []
    in_code = False
    idx = 0
    for n, line in enumerate((text or "").split("\n")):
        if FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = TASK_RE.match(line)
        if m:
            raw = m.group("rest")
            entries.append(Item(idx, n, clean(raw) or "—", m.group("mark") != " ",
                                _level(m.group("indent")), item_key(raw)))
            idx += 1
            continue
        h = HEAD_RE.match(line)
        if h and clean(h.group(2)):
            entries.append(Heading(clean(h.group(2)), len(h.group(1))))
    return entries


def items(entries: list[Item | Heading]) -> list[Item]:
    return [e for e in entries if isinstance(e, Item)]


def progress(text: str) -> tuple[int, int]:
    it = items(parse(text))
    return sum(1 for i in it if i.done), len(it)


def find(text: str, index: int, key: str) -> Item | None:
    """Пункт по номеру; если документ изменился — по хешу текста (если он однозначен)."""
    it = items(parse(text))
    if 0 <= index < len(it) and it[index].key == key:
        return it[index]
    same = [i for i in it if i.key == key]
    return same[0] if len(same) == 1 else None


def _set_mark(line: str, done: bool) -> str:
    m = TASK_RE.match(line)
    if not m:
        return line
    s, e = m.span("mark")
    return line[:s] + ("x" if done else " ") + line[e:]


def set_item(text: str, item: Item, done: bool) -> str:
    lines = text.split("\n")
    lines[item.line] = _set_mark(lines[item.line], done)
    return "\n".join(lines)


def set_all(text: str, done: bool) -> tuple[str, int]:
    lines = text.split("\n")
    changed = 0
    for i in items(parse(text)):
        if i.done != done:
            lines[i.line] = _set_mark(lines[i.line], done)
            changed += 1
    return "\n".join(lines), changed
