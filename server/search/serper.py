"""Serper 搜索源：拿 Google 的结果，要 API key。

**它自己没有索引**，给的是 Google 的搜索结果包装成 JSON。这是它和 Mojeek、Brave
最根本的区别——那两家自己爬自己建索引，Serper 是把 Google 的结果转手给你。

**为什么值得当主力**：长尾内容（老个人博客、地方站、まとめ站、小语种亚文化）
几乎只有 Google 的索引覆盖得到。为 LLM 优化的那类搜索（Tavily、Exa）会替你挑
「语义清晰、质量高」的页面，查技术资料是优点，查这类东西反而把真正有料的页面筛掉了。

**三件要知道的**：

1. 一条查询要 10 条以内结果算 1 个额度，要 11 到 100 条算 2 个。所以默认卡在 10 条，
   想多要得在配置里明确放开——不然成本悄悄翻倍。
2. 它的文档站 docs.serper.dev 解析不了、官网 /pricing 返回 404（实测）。
   接口本身好用，但出问题别指望有文档可查。
3. 官方没公开速率限制。这里不做节流，只在撞到 429 时退避。

**检索词的语言会自动认。**日文查询自动带上 gl=jp、hl=ja，韩文、俄文、中文同理。
这不是锦上添花：查日本本地的东西时，不指定地区语言，Google 给的结果会明显偏英文站。
"""

from __future__ import annotations

import logging
import re

import httpx

from .. import config
from .base import SearchHit

log = logging.getLogger("scout.search.serper")

# 靠字符集猜检索词是什么语言。假名和谚文是独有的，先判它们；
# 只有汉字时既可能是中文也可能是日文，归到中文（日文句子几乎必然带假名）。
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_HAN = re.compile(r"[一-鿿]")

# 语言 → (gl 国家, hl 界面语言)
_LOCALE = {
    "ja": ("jp", "ja"),
    "ko": ("kr", "ko"),
    "ru": ("ru", "ru"),
    "zh": ("cn", "zh-cn"),
    "en": ("us", "en"),
}


# site: 限定里的域名后缀，能反过来说明这条查询要的是哪个地方的内容
_SITE = re.compile(r"site:(\S+)", re.I)
_TLD_LANG = {".jp": "ja", ".kr": "ko", ".ru": "ru", ".cn": "zh", ".tw": "zh", ".hk": "zh"}
# 少数几个没有 .jp 后缀但确实是日文圈的站
_JP_HOSTS = ("fc2.com", "ameblo", "hatena", "livedoor", "note.com", "togetter",
             "pixiv", "nicovideo", "2ch", "5ch", "fumibako")


def guess_locale(query: str) -> tuple[str, str]:
    """按检索词的文字猜国家和界面语言。

    假名和谚文是各自独有的，先判它们。只有汉字时既可能是中文也可能是日文——
    日文句子几乎必然带假名，所以归到中文。

    但有个例外要处理：`怪談 site:fumibako.com` 这种全是汉字的日文站定向搜索，
    按上面的规则会被当成中文，Google 就去给你找中文站了。所以先看 site: 里的域名。
    """
    m = _SITE.search(query)
    if m:
        host = m.group(1).lower().rstrip("/")
        for tld, lang in _TLD_LANG.items():
            if host.endswith(tld):
                return _LOCALE[lang]
        if any(h in host for h in _JP_HOSTS):
            return _LOCALE["ja"]

    if _KANA.search(query):
        return _LOCALE["ja"]
    if _HANGUL.search(query):
        return _LOCALE["ko"]
    if _CYRILLIC.search(query):
        return _LOCALE["ru"]
    if _HAN.search(query):
        return _LOCALE["zh"]
    return _LOCALE["en"]


class SerperSearch:
    name = "serper"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._credits_left: int | None = None
        self._dead_reason = ""

    def available(self) -> bool:
        if not config.SERPER_API_KEY:
            return False
        if self._dead_reason:
            # key 不对或者额度用完了，别每次都白打一次请求
            return False
        return True

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.SEARCH_TIMEOUT, connect=10.0),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ 解析

    @staticmethod
    def _from_answer_box(data: dict) -> SearchHit | None:
        """Google 的答案框。有链接才要——没有链接就没法给它分配引用编号。"""
        box = data.get("answerBox") or {}
        link = box.get("link") or ""
        if not link.startswith("http"):
            return None
        body = (
            box.get("answer")
            or box.get("snippet")
            or " ".join(box.get("snippetHighlighted") or [])
        )
        if not body:
            return None
        return SearchHit(
            title=box.get("title") or link,
            url=link,
            snippet="（Google 答案框）" + str(body).replace("\n", " "),
            engine="serper",
        )

    @staticmethod
    def _from_knowledge_graph(data: dict) -> SearchHit | None:
        kg = data.get("knowledgeGraph") or {}
        link = kg.get("descriptionLink") or kg.get("website") or ""
        if not link.startswith("http"):
            return None
        bits = []
        if kg.get("type"):
            bits.append(str(kg["type"]))
        if kg.get("description"):
            bits.append(str(kg["description"]))
        for k, v in (kg.get("attributes") or {}).items():
            bits.append(f"{k}：{v}")
        if not bits:
            return None
        return SearchHit(
            title=kg.get("title") or link,
            url=link,
            snippet="（Google 知识卡片）" + "；".join(bits),
            engine="serper",
        )

    @classmethod
    def parse(cls, data: dict, limit: int) -> list[SearchHit]:
        """把返回的 JSON 转成统一的结果列表。答案框和知识卡片排在自然结果前面。"""
        hits: list[SearchHit] = []
        seen: set[str] = set()

        for extra in (cls._from_answer_box(data), cls._from_knowledge_graph(data)):
            if extra is not None and extra.url not in seen:
                seen.add(extra.url)
                hits.append(extra)

        for item in data.get("organic") or []:
            link = item.get("link") or ""
            title = item.get("title") or ""
            if not link.startswith("http") or not title or link in seen:
                continue
            seen.add(link)
            snippet = item.get("snippet") or ""
            # 有些结果带 attributes（比如价格、发布时间），拼进片段里
            for k, v in (item.get("attributes") or {}).items():
                snippet += f"　{k}：{v}"
            if item.get("date"):
                snippet = f"[{item['date']}] {snippet}"
            hits.append(
                SearchHit(title=title, url=link, snippet=snippet.strip(), engine="serper")
            )
            if len(hits) >= limit:
                break
        return hits

    # ------------------------------------------------------------ 请求

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        client = await self._get_client()
        gl, hl = (
            guess_locale(query)
            if config.SERPER_AUTO_LOCALE
            else (config.SERPER_GL, config.SERPER_HL)
        )
        # 10 条以内 1 个额度，11 到 100 条 2 个。默认卡在 10，别让成本悄悄翻倍。
        num = min(limit, config.SERPER_MAX_RESULTS)

        payload = {"q": query, "num": num, "gl": gl, "hl": hl}
        if config.SERPER_TBS:
            payload["tbs"] = config.SERPER_TBS

        resp = await client.post(
            f"{config.SERPER_BASE_URL.rstrip('/')}/search",
            json=payload,
            headers={"X-API-KEY": config.SERPER_API_KEY},
        )

        if resp.status_code in (401, 403):
            # key 不对，或者额度用完。标记成不可用，别每条检索词都再撞一次。
            self._dead_reason = f"HTTP {resp.status_code}: {resp.text[:120]}"
            log.warning("Serper 不可用（%s），本次进程内不再调用它", self._dead_reason)
            return []
        if resp.status_code == 429:
            log.warning("Serper 限流（429），这条检索词放弃")
            return []
        resp.raise_for_status()

        data = resp.json()
        left = data.get("credits")
        if isinstance(left, int):
            self._credits_left = left
        log.info(
            "Serper 查「%s」（gl=%s hl=%s num=%d），自然结果 %d 条",
            query, gl, hl, num, len(data.get("organic") or []),
        )
        return self.parse(data, limit)

    def revive(self) -> None:
        """换了 key 之后把不可用的标记清掉。设置页保存时会调它。"""
        self._dead_reason = ""
