"""Tavily 搜索源。

第一步.md 里点名先接 Tavily：免费额度每月 1000 次，字段最全（标题、网址、摘要、评分），
还能顺带把清洗过的正文带回来。

代码写好了，但这台机器上没有 TAVILY_API_KEY，所以 available() 返回 False、会被跳过。
拿到 key 之后写进 .env 就自动生效，不用改任何别的地方。
"""

from __future__ import annotations

import httpx

from .. import config
from .base import SearchHit


class TavilySearch:
    name = "tavily"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def available(self) -> bool:
        return bool(config.TAVILY_API_KEY)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=10.0),
                headers={
                    "Authorization": f"Bearer {config.TAVILY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        client = await self._get_client()
        resp = await client.post(
            f"{config.TAVILY_BASE_URL.rstrip('/')}/search",
            json={
                "query": query,
                # 上限 20
                "max_results": min(limit, 20),
                # basic 一次算 1 个额度，advanced 算 2 个。
                # 免费额度每月 1000 个，按我们一轮问答 6 条检索词算，够 166 轮。
                "search_depth": config.TAVILY_SEARCH_DEPTH,
                # 传 true 拿回来的是原始 HTML，传 "markdown" 是清洗过的 markdown。
                # 这份东西是要喂给便宜模型抽要点的，markdown 干净得多，也省 token。
                # 按官方说明，带不带正文不额外扣额度，只有 search_depth 影响计费。
                "include_raw_content": "markdown",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits: list[SearchHit] = []
        for item in data.get("results") or []:
            url = item.get("url") or ""
            if not url.startswith("http"):
                continue
            hits.append(
                SearchHit(
                    title=item.get("title") or url,
                    url=url,
                    snippet=item.get("content") or "",
                    score=item.get("score"),
                    engine=self.name,
                    raw_content=item.get("raw_content"),
                )
            )
        return hits
