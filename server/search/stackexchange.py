"""Stack Exchange 搜索源：官方 API，不带 key 时每天 300 次配额。

专门源，只在编程和运维类问题上有用。配额用完会返回错误，那时它自动被当成失败跳过，
不影响别的搜索源——单个来源失败绝不能拖垮整轮。

**正文从 API 直接拿，不去抓网页。**实测 Stack Overflow 的网页对我们返回 403，
无头浏览器也过不去；一轮里模型挑了 5 个 SO 页面去读，5 个全失败。
它的 API 本来就能连问题正文一起返回（`filter=withbody`），走 API 既是它规定的用法，
也顺便省掉一次抓取。返回的正文塞进 SearchHit.raw_content，
读网页那一步看到有正文就不再发 HTTP 请求。
"""

from __future__ import annotations

import logging
import re

import httpx

from bs4 import BeautifulSoup

from .. import config
from .base import SearchHit

log = logging.getLogger("scout.search.stackexchange")


def _html_to_text(html: str) -> str:
    """把 API 返回的 HTML 片段转成纯文本。代码块用反引号围起来，别丢掉缩进。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for pre in soup.find_all("pre"):
        pre.replace_with("\n```\n" + pre.get_text() + "\n```\n")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


class StackExchangeSearch:
    name = "stackexchange"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._quota_left: int | None = None

    def available(self) -> bool:
        # 配额见底就别再打了，省得每次都白等一个超时
        return self._quota_left is None or self._quota_left > 5

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
            "https://api.stackexchange.com/2.3/search/advanced",
            params={
                "order": "desc",
                "sort": "relevance",
                "q": query,
                "site": "stackoverflow",
                "pagesize": min(limit, 10),
                "filter": "withbody",
            },
            headers={"Accept-Encoding": "gzip"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._quota_left = data.get("quota_remaining")
        if self._quota_left is not None and self._quota_left < 20:
            log.warning("Stack Exchange 配额只剩 %s 次", self._quota_left)
        hits: list[SearchHit] = []
        by_qid: dict[int, SearchHit] = {}
        for item in data.get("items") or []:
            link = item.get("link") or ""
            title = item.get("title") or ""
            if not link or not title:
                continue
            answered = "已采纳答案" if item.get("is_answered") else "无采纳答案"
            meta = (
                f"Stack Overflow 提问，得分 {item.get('score', 0)}，"
                f"{item.get('answer_count', 0)} 个回答，{answered}，"
                f"标签 {'、'.join(item.get('tags') or [])}"
            )
            body = _html_to_text(item.get("body") or "")
            hit = SearchHit(
                title=title,
                url=link,
                snippet=meta + (f"。问题内容：{body[:300]}" if body else ""),
                engine=self.name,
                raw_content=(f"{title}\n\n{meta}\n\n{body}" if body else None),
            )
            qid = item.get("question_id")
            if qid:
                by_qid[int(qid)] = hit
            hits.append(hit)

        # 光有问题正文往往不够，答案才是要的东西。
        # 一次批量取回这一批问题的答案（用分号连 id），只多花一次配额。
        if by_qid:
            await self._attach_answers(client, by_qid)
        return hits

    async def _attach_answers(self, client: httpx.AsyncClient, by_qid: dict) -> None:
        ids = ";".join(str(q) for q in list(by_qid)[:20])
        try:
            resp = await client.get(
                f"https://api.stackexchange.com/2.3/questions/{ids}/answers",
                params={
                    "order": "desc",
                    "sort": "votes",
                    "site": "stackoverflow",
                    "pagesize": 60,
                    "filter": "withbody",
                },
                headers={"Accept-Encoding": "gzip"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 —— 取不到答案不影响这一批结果本身
            log.info("取 Stack Overflow 答案失败，只用问题正文：%s", exc)
            return
        self._quota_left = data.get("quota_remaining", self._quota_left)

        grouped: dict[int, list[dict]] = {}
        for a in data.get("items") or []:
            grouped.setdefault(int(a.get("question_id") or 0), []).append(a)

        for qid, answers in grouped.items():
            hit = by_qid.get(qid)
            if hit is None:
                continue
            answers.sort(
                key=lambda a: (bool(a.get("is_accepted")), a.get("score") or 0), reverse=True
            )
            chunks = []
            for a in answers[: config.STACKEXCHANGE_ANSWERS_PER_QUESTION]:
                mark = "（已采纳）" if a.get("is_accepted") else ""
                chunks.append(
                    f"回答{mark}，得分 {a.get('score', 0)}：\n"
                    + _html_to_text(a.get("body") or "")
                )
            if chunks:
                hit.raw_content = (hit.raw_content or hit.title) + "\n\n" + "\n\n".join(chunks)
