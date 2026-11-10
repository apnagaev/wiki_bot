import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files/checklistbot/app"))
from bot import checklist as cl
from bot.handlers import build_view
from bot.outline import parse_doc_link, API_KEY_RE

TRAVEL = """# Всегда

- [x] Паспорт
- [x] Линзы  
- [ ] Зубная щетка
- [x] Зубная паста 
- [x] Продукты маме (кофе, рыба, сыр, колбаса
- [ ] Горелка автоген
- [x] Мишина пижама


\\"""

def test_parse_real():
    e = cl.parse(TRAVEL)
    assert isinstance(e[0], cl.Heading) and e[0].text == "Всегда"
    it = cl.items(e)
    assert len(it) == 7 and sum(i.done for i in it) == 5
    assert it[4].text == "Продукты маме (кофе, рыба, сыр, колбаса"

def test_toggle_changes_one_char():
    it = cl.items(cl.parse(TRAVEL))
    new = cl.set_item(TRAVEL, it[2], True)
    diff = [(a, b) for a, b in zip(TRAVEL, new) if a != b]
    assert diff == [(" ", "x")] and len(new) == len(TRAVEL)
    assert cl.progress(new) == (6, 7)

def test_find_after_shift():
    it = cl.items(cl.parse(TRAVEL))
    shifted = "- [ ] Новый пункт\n" + TRAVEL
    f = cl.find(shifted, it[2].index, it[2].key)
    assert f and f.text == "Зубная щетка" and f.index == 3
    assert cl.find(TRAVEL.replace("Зубная щетка", "Щётка"), 2, it[2].key) is None

def test_reset_and_code_block():
    md = "- [x] a\n```\n- [x] not a task\n```\n  - [X] b \\(c\\) **жирный** [ссылка](http://x)\n1. [x] num"
    it = cl.items(cl.parse(md))
    assert [i.text for i in it] == ["a", "b (c) жирный ссылка", "num"]
    assert it[1].level == 1
    new, n = cl.set_all(md, False)
    assert n == 3 and "- [x] not a task" in new and cl.progress(new) == (0, 3)

def test_view_hide_and_pages():
    doc = {"id": "u1", "urlId": "SMQWdQQqCa", "title": "Travel list", "text": TRAVEL, "parentDocumentId": "root"}
    text, kb = build_view(doc, [], hide=True, page=0, is_root=False, web_url="https://w/doc/x")
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert "⬜️ Зубная щетка" in labels and "✅ Паспорт" not in labels and "▸ Всегда" in labels
    assert "5 из 7" in text
    for r in kb.inline_keyboard:
        for b in r:
            assert b.callback_data is None or len(b.callback_data.encode()) <= 64
    big = "\n".join(f"- [ ] пункт {i}" for i in range(95))
    text, kb = build_view({**doc, "text": big}, [], hide=False, page=2, is_root=True, web_url="https://w")
    assert "стр. 3/3" in text and sum(len(r) for r in kb.inline_keyboard) <= 100
    assert not any(b.text == "⬅️ Назад" for r in kb.inline_keyboard for b in r)

def test_all_done_hidden_heading_dropped():
    md = "# A\n- [x] one\n# B\n- [ ] two"
    _, kb = build_view({"id": "1", "title": "t", "text": md}, [], hide=True, page=0, is_root=True, web_url="https://w")
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert "▸ A" not in labels and "▸ B" in labels

def test_links():
    allowed = ["https://wiki.example.com"]
    assert parse_doc_link("вот https://wiki.example.com/doc/checklists-tE5hSY73Kc", allowed) == ("https://wiki.example.com", "tE5hSY73Kc")
    assert parse_doc_link("https://wiki.example.com/doc/tE5hSY73Kc/edit", allowed)[1] == "tE5hSY73Kc"
    assert parse_doc_link("https://evil.example/doc/checklists-tE5hSY73Kc", allowed) is None
    assert API_KEY_RE.search("ключ ol_api_" + "a1B2" * 10)
