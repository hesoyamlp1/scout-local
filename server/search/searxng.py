"""自建的 SearXNG。**无 key、无配额**，聚合几十个上游搜索引擎。

这是为了摆脱 key 方案的配额加进来的：2026 年那些搜索 API 的免费档都很紧
（Google Custom Search 100/天且 2027 年 1 月关停、Serper 2500 次一次性试用、
Exa 1000/月、SerpApi 250/月、Brave 的免费档 2025 年底撤了），
Tavily 的 1000/月已经算最大方的，照样一个月就用满。

自建的代价是它跑在你自己的 IP 上，上游引擎会限流、会挡；
好处是没有配额这回事，而且**日文小站的覆盖比 Mojeek 好得多**——
它背后能用上 DuckDuckGo 和 Google CSE。

起法见 `docs/searxng.md`。没起的时候 `available()` 返回 False，自动跳过。
"""

from __future__ import annotations

import logging

import httpx

from .. import config
from .base import SearchHit

log = logging.getLogger("scout.search.searxng")


class SearxngSearch:
    name = "searxng"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 探活结果缓存：起没起来不必每条检索词都试一次。
        self._alive: bool | None = None

    def available(self) -> bool:
        # 没配地址就当没有。真正能不能连上在 search 里试，失败了由上层熔断接管。
        return bool(config.SEARXNG_BASE_URL)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=config.SEARXNG_BASE_URL.rstrip("/"),
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=5.0),
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def reset(self) -> None:
        self._alive = None

    async def search(self, query: str, limit: int = 8) -> list[SearchHit]:
        client = await self._get_client()
        params = {"q": query, "format": "json", "safesearch": "0"}
        if config.SEARXNG_ENGINES:
            params["engines"] = config.SEARXNG_ENGINES
        resp = await client.get("/search", params=params)
        if resp.status_code == 403:
            # limiter 开着的时候 JSON API 会被挡。这是配置问题不是限流，说清楚。
            raise RuntimeError(
                "SearXNG 返回 403：多半是 settings.yml 里 server.limiter 没关，"
                "或者 search.formats 里没加 json"
            )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchHit] = []
        for r in (data.get("results") or [])[:limit]:
            url = (r.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            out.append(SearchHit(
                title=(r.get("title") or url).strip(),
                url=url,
                snippet=(r.get("content") or "").strip(),
                # SearXNG 自己做过一轮融合排序，它的分拿来当名次的参考；
                # scout 这边还会再融合一次（RRF 只看名次不看分），所以不冲突。
                score=float(r.get("score") or 0.0),
                engine="searxng",
            ))
        if out:
            engines = sorted({e for r in (data.get("results") or [])
                              for e in (r.get("engines") or [])})
            log.info("SearXNG 查「%s」拿到 %d 条（上游：%s）",
                     query, len(out), "、".join(engines[:6]))
        return out
