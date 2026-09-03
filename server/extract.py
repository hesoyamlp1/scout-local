"""把正文按目标抽成要点。原子工具：一次（或几次）模型调用，没有判断空间。

**这是全项目调用最多的一处**，所以用 flash 档。它的活很窄：给定一段正文和
"想从里面看什么"，交出一段要点。它不判断这一页该不该读、够不够、要不要换一篇——
那些是 subagent 的事。

长文分段抽再合并：一篇几万字的文章一次喂不进去，也没必要——要点是给上层做判断用的，
不是给用户读的（用户读的是原文和译文，它们在库里）。
"""

from __future__ import annotations

import asyncio
import logging

from . import config
from .cost import Budget
from .llm import for_agent

log = logging.getLogger("scout.extract")

SYSTEM = """你在从一个网页的正文里摘出要点，给后面的步骤当材料用。

- 只写正文里**真有的东西**，一个字都不许添。正文里没有的就是没有，别补全、别推测。
- 按目标摘：目标问什么就重点摘什么，无关的段落一句带过或者不写。
- 有数字、日期、人名、版本号、专有名词的，原样抄下来，别概括成"最近""几个"。
- 正文要是跟目标完全无关（广告页、导航页、错误页），就直接说"这一页讲的是 X，
  跟目标无关"，别硬凑。
- 用中文写。原文是别的语言也写中文，但专有名词保留原文。
- 不要写"根据正文""这篇文章提到"这类开场白，直接写内容。
- 控制在 {limit} 字以内。"""


async def extract(
    text: str,
    goal: str,
    *,
    title: str = "",
    budget: Budget | None = None,
    limit: int | None = None,
) -> str:
    """从正文里抽要点。抽不出来就返回空字符串，不抛异常。"""
    body = (text or "").strip()
    if not body:
        return ""
    limit = limit or config.EXTRACT_MAX_CHARS
    chunk_max = config.EXTRACT_INPUT_MAX_CHARS
    channel = for_agent("extract")

    async def one(part: str, part_limit: int) -> str:
        try:
            msg = await channel.chat(
                [
                    {"role": "system", "content": SYSTEM.format(limit=part_limit)},
                    {"role": "user", "content":
                        f"目标：{goal or '这一页讲了什么'}\n"
                        f"网页标题：{title or '（无）'}\n\n正文：\n{part}"},
                ],
                agent="extract", purpose="extract", budget=budget, temperature=0.2,
            )
            return (msg.get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001 抽不出来不该让抓取整个失败
            log.warning("抽要点失败：%s", exc)
            return ""

    if len(body) <= chunk_max:
        return (await one(body, limit))[: limit * 2]

    # 长文分段。**段之间并发**，一篇十万字的文章不该串行等三次模型。
    parts = [body[i : i + chunk_max] for i in range(0, len(body), chunk_max)]
    per = max(400, limit // len(parts))
    pieces = await asyncio.gather(*(one(p, per) for p in parts), return_exceptions=True)
    got = [p for p in pieces if isinstance(p, str) and p.strip()]
    return "\n\n".join(got)[: limit * 2]
