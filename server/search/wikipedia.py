"""维基百科搜索源：官方 API，不要 key，不会被封。

它是专门源，不是通用搜索引擎——覆盖面窄，但在人物、概念、机构、历史这类问题上
比通用搜索干净得多，而且几乎不会返回垃圾页面。所以在融合排序里给它一个较低的权重，
让它参与但不主导。

查询里有中日韩文字就查中文维基，否则查英文维基。
"""

from __future__ import annotations

import re
import urllib.parse

import httpx

from .. import config
from .base import SearchHit

_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ]")
_TAGS = re.compile(r"<[^>]+>")


class WikipediaSearch:
    name = "wikipedia"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def available(self) -> bool:
        return True

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=10.0),
                headers={"User-Agent": "scout/0.3 (personal research assistant)"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        lang = "zh" if _CJK.search(query) else "en"
        client = await self._get_client()
        resp = await client.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": min(limit, 10),
                "format": "json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits: list[SearchHit] = []
        for item in (data.get("query") or {}).get("search") or []:
            title = item.get("title") or ""
            if not title:
                continue
            url = f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(
                title.replace(" ", "_")
            )
            snippet = _TAGS.sub("", item.get("snippet") or "").replace("&quot;", '"')
            hits.append(
                SearchHit(title=title, url=url, snippet=snippet, engine=self.name)
            )
        return hits
