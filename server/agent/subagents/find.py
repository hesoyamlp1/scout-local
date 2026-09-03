"""找一找 subagent：在自己的记忆里翻。

**判断权在它手上**：查回来这批跟问题是不是一回事、够不够、要不要换个说法再查。

旧版本这一步**没有模型**：拿问题去 FTS 查一遍，命中什么给什么，然后跟联网搜索结果
混在同一个池子里融合排序。于是问"什么是幂等"能命中一篇讲书法的东西，
八条里没有一条相关，全部塞进主 agent 的上下文，整轮被这批素材毁掉。

这一版有两处结构性的改动：

1. **三层分开呈现，不共用排序。** 网页、结论、以前聊过的各是各的，
   每条都带出处和时间，模型看得出"这是三个月前记的一条书法笔记"。
2. **必须显式采用。** 只有它调 `采用` 点名的条目才会进主 agent 的上下文。
   查回来但没采用的，死在这个 subagent 自己的上下文里——这正是 subagent 存在的意义。
"""

from __future__ import annotations

import logging

from ... import content
from ...cost import Budget
from ..runner import Emit, Result, Tool, run_agent

log = logging.getLogger("scout.agent.find")

SYSTEM = """你在自己的记忆库里找东西。库里有三层，各查各的：

- **网页**：以前读过并存下来的网页正文
- **结论**：以前回答时记下来的一句话事实，每条带着记录日期
- **以前聊过**：过去几轮对话的问答
- **我的批注**：用户自己在某一篇的某一段上写下的话。
  **这是用户亲手写的，比任何抽取物都值钱**——命中了几乎一定要采用。

三个工具：

- `query`：给一到三条检索词。**字面匹配很死板**——库里写着「Bun.s3」，
  拿「对象存储」去找是找不到的。第一次没找到，换个说法再试一次。
- `catalog`：列出记过哪些主题、最近读过哪些网页。**字面没匹配上不等于记忆里没有**，
  在目录里认出哪个主题跟问题是一回事，比换检索词反复试准得多。
- `adopt`：把真正跟问题相关的几条点名交上去。**只有你采用的才会被用到。**

怎么判断一条相关不相关：**看它有没有回答问题本身**，不是看有没有出现相同的词。
一条讲书法的笔记里出现了「幂等」两个字，跟「什么是幂等」不是一回事，别采用它。
结论带着日期，太老的东西要在结论里说一句"这是几月记的，可能过时了"。

收尾：
- 找到了 —— 先 `adopt` 那几条，再用一段话说清记忆里有什么。
- 没找到 —— **直接说"记忆里没有关于 X 的东西"，不要采用任何东西。**
  硬凑几条沾边的上去，比说没有更糟。"""


async def run_find(
    query: str,
    *,
    budget: Budget,
    emit: Emit | None = None,
    session_id: str = "",
) -> Result:
    """在记忆里找。返回它**采用**的那几条。"""
    pool: dict[int, content.Item] = {}
    counter = {"n": 0}
    taken: list[content.Item] = []

    def _register(items: list[content.Item]) -> list[str]:
        lines = []
        for it in items:
            counter["n"] += 1
            pool[counter["n"]] = it
            lines.append(it.line(counter["n"]))
        return lines

    async def do_query(queries: list[str]) -> str:
        qs = [q.strip() for q in (queries or []) if isinstance(q, str) and q.strip()][:3]
        if not qs:
            return "查询需要至少一条检索词。"
        if emit:
            await emit("find_query", {"queries": qs})
        blocks: list[str] = []
        for q in qs:
            docs = content.search_docs(q, limit=4)
            facts = content.search_facts(q, limit=4)
            dialogs = content.search_dialogs(q, limit=3)
            notes = content.search_notes(q, limit=4)
            if not (docs or facts or dialogs or notes):
                blocks.append(f"「{q}」：三层都没查到。")
                continue
            part = [f"「{q}」查到："]
            if docs:
                part.append("— 读过的网页 —")
                part.extend(_register(docs))
            if facts:
                part.append("— 记下的结论 —")
                part.extend(_register(facts))
            if dialogs:
                part.append("— 以前聊过 —")
                part.extend(_register(dialogs))
            if notes:
                part.append("— 用户自己写的批注 —")
                part.extend(_register(notes))
            blocks.append("\n".join(part))
        return "\n\n".join(blocks)

    async def do_catalog() -> str:
        cat = content.catalog()
        recent = content.recent_dialogs(4, exclude_session=session_id)
        parts = [cat or "记忆库还是空的。"]
        if recent:
            parts.append("最近聊过：")
            parts.extend(_register(recent))
        return "\n".join(parts)

    async def do_take(nums: list) -> str:
        got = []
        for raw in (nums or [])[:8]:
            try:
                num = int(str(raw).strip().strip("[]"))
            except ValueError:
                continue
            it = pool.get(num)
            if it is not None and it not in taken:
                taken.append(it)
                got.append(f"[{num}] {it.title}")
        if not got:
            return "这些编号我这儿没有。只能采用前面列出来的编号。"
        if emit:
            await emit("find_take", {"count": len(taken)})
        return "已采用：" + "；".join(got) + "。现在用一段话说清记忆里有什么。"

    tools = [
        Tool(
            name="query",
            description="用检索词在三层记忆里找。一次一到三条词。",
            parameters={
                "type": "object",
                "properties": {
                    "queries": {"type": "array", "items": {"type": "string"},
                                "description": "一到三条检索词"},
                },
                "required": ["queries"],
            },
            run=do_query,
        ),
        Tool(
            name="catalog",
            description="列出记过哪些主题、最近读过哪些网页、最近聊过什么。不花钱。",
            parameters={"type": "object", "properties": {}},
            run=do_catalog,
        ),
        Tool(
            name="adopt",
            description="把真正相关的几条点名交上去。只有采用的才会被用到。",
            parameters={
                "type": "object",
                "properties": {
                    "nums": {"type": "array", "items": {"type": "integer"},
                             "description": "要采用的条目编号"},
                },
                "required": ["nums"],
            },
            run=do_take,
        ),
    ]

    out = await run_agent(
        name="find", system=SYSTEM, task=f"要找的东西：{query}",
        tools=tools, budget=budget, emit=emit,
    )
    out.items = taken
    # 被采用的结论记一笔"被召回过"，淘汰时按这个排。
    content.mark_recalled([i.id for i in taken if i.kind == "fact"])
    return out
