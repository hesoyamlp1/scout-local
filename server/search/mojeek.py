"""Mojeek 搜索源：不要 API key，解析它的搜索结果页。

选它的原因：它有自己的爬虫和索引（不是去代理谷歌），同一批探测里 Bing 直接给验证码、
DuckDuckGo 给「选出所有含鸭子的方格」、Brave 返回 429，只有 Mojeek 正常出结果。

**它有限流，这是实测出来的：**间隔 1.5 秒连发，第 9 条还正常，第 10 条开始返回 403；
歇 60 秒之后恢复。所以这里有一个全局节流器（同一时刻只发一条，两条之间至少隔一段时间）
和一个 403 退避重试。即便如此它也只适合低频使用。

**这是权宜之计，不是长久方案。**设计里写得很清楚：搜索要买现成的，自己抓搜索结果页
早晚被封。有 Tavily 或者别家的 key 之后应该切过去，切换只要改 SCOUT_SEARCH_PROVIDERS
这个环境变量，代码一行不用动。
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import SearchHit

log = logging.getLogger("scout.search.mojeek")


class RateLimiter:
    """全局节流：同一时刻只放一条请求过去，且两条之间至少隔 min_interval 秒。"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._blocked_until = 0.0

    def blocked_for(self) -> float:
        """还要多久才出惩罚窗口。0 表示现在就能发。"""
        return max(0.0, self._blocked_until - time.monotonic())

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(self._last + self.min_interval - now, self._blocked_until - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def penalize(self, seconds: float) -> None:
        """撞到 403 之后，让接下来一段时间内的请求都先等着。"""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)


class MojeekSearch:
    name = "mojeek"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._limiter = RateLimiter(config.MOJEEK_MIN_INTERVAL)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=10.0),
                headers={
                    "User-Agent": config.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _parse(html: str, limit: int) -> list[SearchHit]:
        soup = BeautifulSoup(html, "lxml")
        hits: list[SearchHit] = []
        for li in soup.select("ul.results-standard > li"):
            a = li.select_one("h2 a.title") or li.select_one("a.title")
            if a is None:
                continue
            href = a.get("href") or ""
            if not href.startswith("http"):
                continue
            snippet_el = li.select_one("p.s")
            hits.append(
                SearchHit(
                    title=a.get_text(" ", strip=True),
                    url=href,
                    snippet=snippet_el.get_text(" ", strip=True) if snippet_el else "",
                    engine="mojeek",
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def available(self) -> bool:
        """正在惩罚窗口里就先别用它。

        傻等 8 到 16 秒换一家可能也没结果的搜索源不划算：另外三家是并发查的，
        Mojeek 缺席这一次，那三家的结果照样回来。这就是「单个来源失败不能拖垮整轮」
        用在搜索层上——只不过这里拖垮的不是正确性，是首字时间。
        """
        wait = self._limiter.blocked_for()
        if wait > config.MOJEEK_SKIP_IF_BLOCKED_OVER:
            log.info("Mojeek 还在限流窗口里（还要 %.0f 秒），这次跳过它", wait)
            return False
        return True

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        client = await self._get_client()
        # 只传 q。之前多传了一个 t 参数，结果页会返回 200 但一条结果都没有。
        params = {"q": query}
        for attempt in range(config.MOJEEK_MAX_RETRY + 1):
            await self._limiter.acquire()
            resp = await client.get("https://www.mojeek.com/search", params=params)
            if resp.status_code == 200:
                return self._parse(resp.text, limit)
            if resp.status_code in (403, 429):
                backoff = config.MOJEEK_BACKOFF * (attempt + 1)
                self._limiter.penalize(backoff)
                log.warning(
                    "Mojeek 限流（HTTP %d），等 %.0f 秒后重试（第 %d 次）",
                    resp.status_code,
                    backoff,
                    attempt + 1,
                )
                continue
            resp.raise_for_status()
        log.warning("Mojeek 连续被限流，放弃这条检索词：%s", query)
        return []
