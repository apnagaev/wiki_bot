"""Минимальный клиент API Outline: только то, что нужно боту."""
from __future__ import annotations

import re

import aiohttp

DOC_LINK_RE = re.compile(
    r"(?P<base>https?://[^/\s]+)(?P<prefix>/[^\s]*?)?/doc/(?:[^/\s?#]*-)?(?P<id>[A-Za-z0-9]{10})(?=$|[/?#\s])"
)
API_KEY_RE = re.compile(r"\bol_api_[A-Za-z0-9]{20,}\b")


class OutlineError(Exception):
    pass


class AuthError(OutlineError):
    """Ключ не принят: истёк, отозван или неверный."""


class NotFound(OutlineError):
    """Документа нет или к нему нет доступа."""


def parse_doc_link(text: str, allowed: list[str]) -> tuple[str, str] | None:
    """(базовый адрес вики, urlId документа) или None.

    Принимаются только адреса из allowed: бот работает во внутренней сети и не
    должен ходить по произвольным ссылкам.
    """
    for m in DOC_LINK_RE.finditer(text or ""):
        base = (m.group("base") + (m.group("prefix") or "")).rstrip("/")
        for a in allowed:
            if base.lower() == a.rstrip("/").lower():
                return a.rstrip("/"), m.group("id")
    return None


class Outline:
    def __init__(self, session: aiohttp.ClientSession, base: str, key: str):
        self.s = session
        self.base = base.rstrip("/")
        self.key = key

    async def call(self, method: str, **payload) -> dict:
        try:
            async with self.s.post(
                f"{self.base}/api/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 401:
                    raise AuthError(method)
                if r.status in (403, 404):
                    raise NotFound(method)
                if r.status >= 400:
                    body = (await r.text())[:200]
                    raise OutlineError(f"{method}: HTTP {r.status} {body}")
                return await r.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise OutlineError(f"{method}: {e.__class__.__name__}") from e

    async def whoami(self) -> str:
        data = (await self.call("auth.info"))["data"]
        return data.get("user", {}).get("name") or "?"

    async def info(self, doc_id: str) -> dict:
        return (await self.call("documents.info", id=doc_id))["data"]

    async def children(self, parent_id: str) -> list[dict]:
        docs: list[dict] = []
        offset = 0
        while True:
            res = await self.call("documents.list", parentDocumentId=parent_id,
                                  limit=100, offset=offset, sort="title", direction="ASC")
            page = res.get("data") or []
            docs += page
            if len(page) < 100:
                return docs
            offset += 100

    async def update_text(self, doc_id: str, text: str) -> dict:
        return (await self.call("documents.update", id=doc_id, text=text))["data"]

    async def prepend_text(self, doc_id: str, text: str) -> dict:
        """Добавить markdown в начало документа (сервер вставляет сам, без перезаписи)."""
        return (await self.call("documents.update", id=doc_id, text=text, editMode="prepend"))["data"]

    def web_url(self, doc: dict) -> str:
        url = doc.get("url") or ""
        return url if url.startswith("http") else self.base + url
