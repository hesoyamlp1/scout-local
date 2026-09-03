"""Yahoo! Japan 搜索源，通过专用日本 SOCKS 出口访问。

SearXNG 的通用 Yahoo/Brave 适配器在当前版本会解析失败，但 Yahoo! Japan
原始搜索页在日本出口上稳定返回结果。这里直接取页面，只做确定性的 HTML 解析；
结果是否值得打开仍由 research subagent 判断。
"""

from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import SearchHit

log = logging.getLogger("scout.search.yahoo_japan")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class YahooJapanSearch:
    name = "yahoo_japan"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def available(self) -> bool:
        # 必须显式配置日本代理。没有代理时绝不从 Scout 所在机器直接爬搜索页。
        return bool(config.YAHOO_JAPAN_PROXY_URL)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                proxy=config.YAHOO_JAPAN_PROXY_URL,
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=8.0),
                follow_redirects=True,
                headers={
                    "User-Agent": _UA,
                    "Accept-Language": "ja,en-US;q=0.7,en;q=0.5",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def parse(html: str, limit: int) -> list[SearchHit]:
        soup = BeautifulSoup(html, "lxml")
        anchors = list(soup.select("a.sw-Card__titleInner[href]"))
        if not anchors:
            # Yahoo 偶尔改 CSS hash；结果标题仍是 h3，留一个结构性回退。
            anchors = [a for a in soup.select("a[href]") if a.find("h3")]

        hits: list[SearchHit] = []
        seen: set[str] = set()
        for anchor in anchors:
            url = (anchor.get("href") or "").strip()
            heading = anchor.find("h3")
            title = heading.get_text(" ", strip=True) if heading else ""
            if not url.startswith("http") or not title or url in seen:
                continue
            seen.add(url)
            card = anchor.find_parent(class_="sw-Card")
            summary = card.select_one(".sw-Card__summary") if card else None
            snippet = summary.get_text(" ", strip=True) if summary else ""
            hits.append(SearchHit(
                title=" ".join(title.split()),
                url=url,
                snippet=" ".join(snippet.split()),
                engine="yahoo_japan",
            ))
            if len(hits) >= limit:
                break
        return hits

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        client = await self._get_client()
        resp = await client.get(
            config.YAHOO_JAPAN_BASE_URL.rstrip("/") + "/search",
            params={"p": query, "ei": "UTF-8", "x": "wrt"},
        )
        resp.raise_for_status()
        hits = self.parse(resp.text, limit)
        if not hits and "検索結果" not in resp.text:
            raise RuntimeError("Yahoo! Japan 返回了非搜索结果页，可能触发了反爬")
        log.info("Yahoo! Japan 查「%s」拿到 %d 条", query, len(hits))
        return hits
