"""Hacker News 搜索源：官方的 Algolia 接口，不要 key。

也是专门源。它的价值不在于覆盖面，而在于技术类问题上能直接找到讨论帖，
里面往往有通用搜索翻不到的一手经验和反面意见。融合排序里给较低权重。

外链帖返回它指向的那个网址（那才是正文）；Ask HN 这种没有外链的返回帖子本身。
"""

from __future__ import annotations

import httpx

from .. import config
from .base import SearchHit


class HackerNewsSearch:
    name = "hackernews"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def available(self) -> bool:
        return True

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=10.0),
                headers={"User-Agent": config.USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        client = await self._get_client()
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": query,
                "tags": "story",
                "hitsPerPage": min(limit, 10),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for h in data.get("hits") or []:
            title = h.get("title") or h.get("story_title") or ""
            url = h.get("url") or ""
            oid = h.get("objectID")
            if not url and oid:
                url = f"https://news.ycombinator.com/item?id={oid}"
            if not title or not url or url in seen:
                continue
            seen.add(url)
            points = h.get("points") or 0
            comments = h.get("num_comments") or 0
            date = (h.get("created_at") or "")[:10]
            hits.append(
                SearchHit(
                    title=title,
                    url=url,
                    snippet=f"Hacker News 讨论帖，{points} 分，{comments} 条评论，{date}",
                    engine=self.name,
                )
            )
        return hits
