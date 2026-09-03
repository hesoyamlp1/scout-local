"""搜索源的统一接口。

设计里的硬要求：搜索源一开始就别绑死任何一家。进去是检索词，出来是一串
标题加链接加片段，底下换成别家只改一个文件。哪天涨价或者挂了就换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    score: float | None = None
    engine: str = ""
    # 有些搜索源（比如 Tavily）能顺带把清洗过的正文带回来。
    # 带回来了就省一次抓取，但它一样不进对话层的上下文，只当抽取的输入。
    raw_content: str | None = field(default=None, repr=False)
    # 这条来自本机的哪条记忆（现在只有结论记忆填它）。网络搜索源不填。
    # 一轮结束后拿它记一笔「这条被召回过」，淘汰时要用这个数。
    memory_id: str | None = None


class SearchProvider(Protocol):
    name: str

    def available(self) -> bool:
        """key 配了没有、能不能用。不能用的会被跳过。"""
        ...

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        ...
