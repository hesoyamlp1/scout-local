"""搜索 subagent：去外面把一件事弄清楚。

**判断权在它手上**：搜什么词、十几条结果里哪几条值得打开、读完够不够、
要不要换个角度再搜一轮。

旧版本这里是"按融合排序的名次读前 N 条"——打开哪个网址是按排名定的，不是按内容。
现在它看着标题、域名、摘要自己挑，官方文档会排在三年前的博客前面。

**并发在 `读` 这个工具里**：给它几个编号，它同时派出几个抓取 subagent，
墙钟时间等于最慢那一个，不是几个之和。一个失败不阻塞其它，
失败的那条变成一句回执交给它，由它判断够不够、要不要补几篇。
"""

from __future__ import annotations

import asyncio
import logging

from ... import config, content
from ...cost import Budget
from ...search import search_many
from ..runner import Emit, Result, Tool, run_agent
from .fetch import run_fetch

log = logging.getLogger("scout.agent.research")

SYSTEM = """你负责去互联网上把一件事弄清楚，然后把材料交回去。**你不写最终答案**，
交回去的是材料和一段说明。

两个工具：

- `search`：一次给一到三条检索词，它们会同时发出去。
  **几条词的角度要明显错开**，不能只是在同一个词组后面换最后一个词。
  技术、科学、开源项目、国际产品这类问题，至少配一条纯英文的检索词
  （用这个领域通行的英文说法，比如「异步运行时」写成 async runtime）——
  中英混排在英文索引里搜不到东西。
  小众内容、日文来源，就用它本来的语言去搜。
- `read`：给几个编号，同时把这几个网页真正打开读。**一次多给几个**（三四个），
  它们是并发的，读三个跟读一个花的时间差不多。

怎么挑要读哪几条：看标题、域名和摘要。官方文档、原始出处、一手资料优先；
内容农场、聚合站、明显过时的东西往后放。**别按顺序读前几条**，
搜索引擎的排名跟你要找的东西不是一回事。

**读到的页面标着"还有下一页"时，不要自己去翻，也不要去搜那篇的后半截。**
把这一篇交回去就行——后面有专门的一步会顺着分页读到末页。
分页正文搜索引擎多半没收录，搜"全文""続き"这类词只会白费力气。

什么时候收尾：材料够回答那个目标了就停。**不要为同一件事反复搜**——
一次搜索加一轮阅读通常就够，除非读回来的东西明确说明没查到、
或者冒出了一个必须再查的新角度。

用户给了明确网址的时候更简单：直接 `read` 那一个，读到了就交回去，别再搜别的。

结论怎么写：一段话说清**查到了什么**，直接回答那个目标。
不用复述每一篇的内容（材料会一起交上去），但要点出关键事实和分歧。
要是没查到，就直接说没查到、试过哪些角度，别硬凑。"""


async def run_research(
    goal: str,
    *,
    budget: Budget,
    emit: Emit | None = None,
) -> Result:
    """去查清楚一件事。返回结论 + 素材 + 遇到的问题。"""
    pool: dict[int, dict] = {}      # 编号 → 搜索结果
    picked: list[content.Item] = []
    problems: list[str] = []
    seen_urls: set[str] = set()
    counter = {"n": 0}

    async def do_search(queries: list[str]) -> str:
        qs = [q.strip() for q in (queries or []) if isinstance(q, str) and q.strip()][:3]
        if not qs:
            return "搜索需要至少一条检索词。"
        if emit:
            await emit("search_start", {"queries": qs})
        per_query, failed = await search_many(qs)
        lines: list[str] = []
        for q, hits in per_query:
            fresh = [h for h in hits if content.normalize_url(h.url) not in seen_urls]
            if not fresh:
                lines.append(f"「{q}」：没有新结果")
                continue
            lines.append(f"「{q}」：")
            for h in fresh:
                counter["n"] += 1
                num = counter["n"]
                seen_urls.add(content.normalize_url(h.url))
                pool[num] = {"url": h.url, "title": h.title, "snippet": h.snippet}
                snippet = (h.snippet or "").strip().replace("\n", " ")[:160]
                lines.append(
                    f"  {num}. {h.title}  —— {content.host_of(h.url)}\n"
                    f"     {snippet}"
                )
        if failed:
            lines.append(f"（这几条没搜出东西：{'、'.join(failed)}）")
        if emit:
            await emit("search_done", {"queries": qs, "results": len(pool)})
        if not pool:
            return "一条结果都没有。换个说法，或者换一种语言再试一次。"
        return "\n".join(lines) + "\n\n挑几条真正可能有答案的，用 `read` 一次性打开。"

    async def do_read(nums: list, goal_text: str = "") -> tuple[str, Result]:
        want = []
        for raw in (nums or [])[:6]:
            try:
                num = int(str(raw).strip().strip("[]"))
            except ValueError:
                continue
            if num in pool:
                want.append(num)
        if not want:
            return ("这些编号我这儿没有。只能读 `搜索` 结果里列出的编号。",
                    Result(text=""))

        # **并发派抓取。** 预算按这一批的个数平分，避免第一个把额度吃光。
        subs = [
            run_fetch(
                pool[n]["url"], goal_text or goal,
                budget=budget.child(config.SUBAGENT_BUDGET_SHARE["fetch"],
                                    label=f"fetch:{n}", of=len(want)),
                emit=emit,
            )
            for n in want
        ]
        got = await asyncio.gather(*subs, return_exceptions=True)

        merged = Result()
        lines: list[str] = []
        for num, res in zip(want, got):
            if isinstance(res, BaseException):
                problems.append(f"[{num}] {pool[num]['url']} 读的时候整个出错了")
                lines.append(f"{num}. 出错了：{type(res).__name__}")
                continue
            merged.steps.extend(res.steps)
            if res.items:
                picked.extend(res.items)
                lines.append(f"{num}. {res.text.strip() or '读到了'}")
            else:
                problems.extend(res.problems)
                lines.append(f"{num}. 没读下来：{res.text.strip() or '；'.join(res.problems)}")
        merged.text = "\n".join(lines)
        return merged.text, merged

    tools = [
        Tool(
            name="search",
            description="一次发一到三条检索词，同时搜。几条词的角度要错开。",
            parameters={
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "一到三条检索词，角度明显错开",
                    }
                },
                "required": ["queries"],
            },
            run=do_search,
        ),
        Tool(
            name="read",
            description=(
                "把几个搜索结果真正打开读。给编号数组，一次多给几个——它们是并发的，"
                "读三个跟读一个花的时间差不多。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "nums": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "搜索结果前面那些编号，一次三四个",
                    },
                    "goal_text": {
                        "type": "string",
                        "description": "打开它们想看什么（不填就用这次调研的目标）",
                    },
                },
                "required": ["nums"],
            },
            run=do_read,
            spawns="fetch",
        ),
    ]

    out = await run_agent(
        name="research", system=SYSTEM,
        task=f"要弄清楚的事：{goal}",
        tools=tools, budget=budget, emit=emit,
    )
    out.items = picked
    out.problems.extend(problems)
    return out
