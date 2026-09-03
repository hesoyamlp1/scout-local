"""搜索层：注册表、并发、去重、融合排序。

对上只暴露一个函数 `search_many(queries)`：给几条检索词，回来一批去过重、排过序的结果。
换搜索源、加搜索源只动这个包里的文件，上层一个字不用改。

**两种工作方式**（`SCOUT_SEARCH_MODE`）：

- `fanout`（默认）：几家搜索源同时查，结果用加权的倒数排名融合（RRF）合并。
  同一个网址被好几家都排在前面，融合后就会排到最上面——不同来源的相互印证本身就是信号。
- `fallback`：按配置顺序一家一家试，第一个出结果的就用它。省请求，但只有一家的视角。

**为什么要融合排序而不是简单拼接。**几家搜索源各有各的打分，分数之间没有可比性，
直接按分数排会被打分最大方的那家主导。倒数排名融合只看名次不看分数，
公式是 每家的权重 ÷ (k + 这家给它的名次)，几家的分加起来，k 见 config.SEARCH_RRF_K。

专门源（维基百科、Hacker News、Stack Overflow）覆盖面窄但结果干净，
所以给较低的权重：它们参与排序，但不会把通用搜索的结果挤下去。
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse

from .. import config
from .base import SearchHit
from .hackernews import HackerNewsSearch
from .mojeek import MojeekSearch
from .searxng import SearxngSearch
from .serper import SerperSearch
from .stackexchange import StackExchangeSearch
from .tavily import TavilySearch
from .wikipedia import WikipediaSearch
from .yahoo_japan import YahooJapanSearch

log = logging.getLogger("scout.search")

_REGISTRY = {
    # Yahoo Japan 只有配置专用出口时才启用，避免无意改变用户的默认网络路径。
    "yahoo_japan": YahooJapanSearch(),
    # 自建的，无 key 无配额，排在最前
    "searxng": SearxngSearch(),
    "serper": SerperSearch(),
    "tavily": TavilySearch(),
    "mojeek": MojeekSearch(),
    "wikipedia": WikipediaSearch(),
    "hackernews": HackerNewsSearch(),
    "stackexchange": StackExchangeSearch(),
    # **知识库和对话历史不在这里。**它们是"召回"，归「找一找」subagent，
    # 跟联网搜索分开呈现、不共用排序——混在一起排序正是旧版本
    # "问什么是幂等，召回一篇讲书法的东西并挤进前三"的机制。
}

def active_providers() -> list:
    """按配置顺序返回真正能用的搜索源。没配 key、配额用完、正在歇着的都跳过。"""
    out = []
    for name in config.SEARCH_PROVIDERS:
        p = _REGISTRY.get(name)
        if p is None:
            log.warning("配置里写了不认识的搜索源：%s", name)
            continue
        if _resting(name):
            continue
        if not p.available():
            log.info("搜索源 %s 现在不可用，跳过", name)
            continue
        out.append(p)
    return out


def normalize_url(url: str) -> str:
    """去重用的规范化：丢掉 fragment 和常见的跟踪参数，末尾斜杠统一。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    drop = {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "ref",
        "fbclid",
        "gclid",
    }
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in drop]
    )
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urllib.parse.urlunsplit((parts.scheme, host, path, query, ""))


# 连着挂几次就先歇一会儿。**这是物理约束不是判断**：一个源的配额用完了
# （Tavily 返 432）或者被限流（Mojeek 返 403），这一轮里再打它只是白等超时。
# 实测评测集跑到一半时两个源都挂了，每条检索词都要多等它们几秒。
_COOLDOWN = 600.0
_FAIL_MAX = 3
_fails: dict[str, int] = {}
_paused: dict[str, float] = {}


def _resting(name: str) -> bool:
    until = _paused.get(name, 0.0)
    if until and time.monotonic() < until:
        return True
    if until:
        _paused.pop(name, None)
        _fails.pop(name, None)
    return False


async def _search_one(provider, query: str, limit: int) -> list[SearchHit]:
    """单条检索词打一个搜索源。失败就返回空，绝不往上抛——单个来源失败不能拖垮整轮。"""
    if _resting(provider.name):
        return []
    try:
        got = await asyncio.wait_for(
            provider.search(query, limit=limit), timeout=config.SEARCH_TIMEOUT
        )
        _fails.pop(provider.name, None)
        return got
    except Exception as exc:  # noqa: BLE001 —— 这里就是要吞掉一切
        n = _fails.get(provider.name, 0) + 1
        _fails[provider.name] = n
        if n >= _FAIL_MAX:
            _paused[provider.name] = time.monotonic() + _COOLDOWN
            log.warning("搜索源 %s 连着挂了 %d 次，歇 %d 秒再用（最后一次：%s）",
                        provider.name, n, int(_COOLDOWN), exc)
        else:
            log.warning("搜索源 %s 查「%s」失败：%s", provider.name, query, exc)
        return []


def fuse(per_provider: list[tuple[str, list[SearchHit]]], limit: int) -> list[SearchHit]:
    """加权倒数排名融合。同一个网址被多家命中时，几家的分加起来。"""
    scores: dict[str, float] = {}
    best: dict[str, SearchHit] = {}
    engines: dict[str, list[str]] = {}

    for name, hits in per_provider:
        weight = config.SEARCH_WEIGHTS.get(name, 1.0)
        for rank, hit in enumerate(hits, 1):
            key = normalize_url(hit.url)
            scores[key] = scores.get(key, 0.0) + weight / (config.SEARCH_RRF_K + rank)
            engines.setdefault(key, []).append(name)
            # 保留信息最全的那一份：优先有正文的，其次片段最长的
            cur = best.get(key)
            if (
                cur is None
                or (hit.raw_content and not cur.raw_content)
                or len(hit.snippet or "") > len(cur.snippet or "")
            ):
                best[key] = hit

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[SearchHit] = []
    for key, score in ordered[:limit]:
        hit = best[key]
        srcs = sorted(set(engines[key]))
        out.append(
            SearchHit(
                title=hit.title,
                url=hit.url,
                snippet=hit.snippet,
                score=round(score, 6),
                engine="+".join(srcs),
                raw_content=hit.raw_content,
            )
        )
    return out


_cache: dict[str, tuple[float, list[SearchHit]]] = {}


def _cache_get(key: str) -> list[SearchHit] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, hits = entry
    if time.monotonic() - ts > config.SEARCH_CACHE_TTL:
        _cache.pop(key, None)
        return None
    return hits


async def search_query(query: str, limit: int) -> list[SearchHit]:
    """一条检索词。

    结果按检索词缓存一段时间。多轮对话里同一个词经常被查好几次，
    命中缓存既省一次外网请求，也少撞一次搜索源的限流。
    """
    key = f"{config.SEARCH_MODE}:{limit}:{query.strip().lower()}"
    cached = _cache_get(key)
    if cached is not None:
        log.debug("检索词命中缓存：%s", query)
        return cached

    providers = active_providers()
    if not providers:
        log.error("一个可用的搜索源都没有")
        return []

    if config.SEARCH_MODE == "fallback":
        for provider in providers:
            hits = await _search_one(provider, query, limit)
            if hits:
                _cache[key] = (time.monotonic(), hits)
                return hits
        return []

    results = await asyncio.gather(
        *(_search_one(p, query, limit) for p in providers)
    )
    per_provider = [(p.name, r) for p, r in zip(providers, results)]
    got = [(n, len(r)) for n, r in per_provider]
    log.info("检索词「%s」各源结果：%s", query, got)
    fused = fuse(per_provider, limit)
    if fused:
        _cache[key] = (time.monotonic(), fused)
    return fused


async def search_many(
    queries: list[str], *, limit_per_query: int | None = None
) -> tuple[list[tuple[str, list[SearchHit]]], list[str]]:
    """并发打几条检索词。

    返回两样：
    - 每条检索词各自的结果（保持顺序，界面要按检索词分组显示）
    - 失败的检索词列表
    """
    limit = limit_per_query or config.SEARCH_RESULTS_PER_QUERY
    sem = asyncio.Semaphore(config.SEARCH_CONCURRENCY)

    async def one(q: str) -> list[SearchHit]:
        async with sem:
            return await search_query(q, limit)

    results = await asyncio.gather(*(one(q) for q in queries), return_exceptions=True)

    per_query: list[tuple[str, list[SearchHit]]] = []
    failed: list[str] = []
    seen: set[str] = set()
    for q, res in zip(queries, results):
        if isinstance(res, Exception) or not res:
            failed.append(q)
            per_query.append((q, []))
            continue
        # 几条检索词之间也要去重，同一个网址只留在第一次出现的那条下面
        deduped: list[SearchHit] = []
        for hit in res:
            k = normalize_url(hit.url)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(hit)
        per_query.append((q, deduped))
    return per_query, failed


async def close_all() -> None:
    for p in _REGISTRY.values():
        close = getattr(p, "aclose", None)
        if close:
            await close()
