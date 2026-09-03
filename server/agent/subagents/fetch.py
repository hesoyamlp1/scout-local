"""抓取 subagent：把一个网址真正拿下来。

**判断权在它手上**：抓回来的是不是想要的东西、要不要换个方法再来一次、
这页是登录墙还是本来就是篇短文、这是文章还是榜单。

旧版本这些是写死的：重试固定次数、正文短于某个字数就判失败、
403 或挑战页才降级到浏览器。于是六种情况归成一句"正文太短，多半是空页或登录墙"，
然后放弃。现在它看得见事实（状态码、字数、页面特征、正文开头一段），自己决定下一步。

程序在这里只剩三件事：物理约束（并发、超时、重试硬顶）、把正文存进库、
把分页关系记下来。**一个判断都不做。**
"""

from __future__ import annotations

import logging

from ... import config, content, net
from ...cost import Budget
from ...extract import extract
from ..runner import Emit, Result, Tool, run_agent

log = logging.getLogger("scout.agent.fetch")

SYSTEM = """你负责把一个网页真正拿下来。

你有两种方法：

- `http`：直接发 HTTP 请求。快、便宜，绝大多数页面够用。**先试它。**
- `browser`：跑一个真的浏览器把页面渲染出来。慢十几倍，但能对付
  靠 JavaScript 渲染的页面和一部分反爬。

每次取回来，你会看到这些事实：HTTP 状态码、正文抽出多少字、页面里有没有
反爬挑战或登录墙的字样、有没有下一页、以及**正文开头一段**。
看着这些自己判断：

- 正文正常，内容也对得上目标 —— **就说结论，别再取了。**
- **`http` 已经拿回正常正文的，不要再用 `browser` 试一遍。** 同一个网址，
  两种方法拿到的是同一个页面；只有 `http` 那次几乎抽不出东西（页面靠 JS 渲染）
  或者被明确挡住时，`browser` 才可能给出不一样的结果。
  浏览器一次要十几二十秒，白试一次就是白等这么久。
- 有反爬挑战的字样、或者正文几乎为空而页面明显不空 —— 换 `browser` 再来一次。
- 有登录墙或付费墙的字样 —— **别再试了**，换方法也进不去。直接说明情况。
- 正文很短但读起来是完整的一小段（比如一首诗、一条公告）—— 那就是这一页的全部内容，
  别当失败。
- **正文不长、但页面有「下一页」链接** —— 那是分页文章里的一页，这一页本来就该这么短。
  **别用浏览器再试一遍**，换方法拿到的还是这一页。直接交回去，
  后面有专门的一步会顺着分页读到末页。
- 抽出来的是一串榜单条目而不是文章 —— 如实说这是个榜单页/目录页，
  以及它列了些什么。**这不是失败**，很多时候榜单正是要找的东西。
- 超时或连不上 —— 可以再试一次，但同一个方法失败两次就别再试了。

结论怎么写：一两句话说清**拿到了什么**（是文章还是榜单、讲的是什么、
跟目标对不对得上），或者**为什么没拿到**（登录墙、连不上、页面是空的）。
不要复述正文内容，后面的步骤会拿到要点。"""


async def run_fetch(
    url: str,
    goal: str,
    *,
    budget: Budget,
    emit: Emit | None = None,
    series_hint: str = "",
) -> Result:
    """抓一个网址。返回的 `items` 里最多一条：抓到的那一页。

    `series_hint` 是"这一页属于哪一篇"，翻页时由调用方传进来——
    分页归组是事实记录，不是判断，所以由程序保证。
    """
    state: dict = {"tries": 0, "doc_id": "", "item": None, "raw": None, "done": {}}

    async def take(method: str = "http") -> str:
        if state["tries"] >= config.FETCH_HARD_RETRY_CAP:
            return "这一页已经试了太多次，别再试了，直接说明情况。"
        # 同一个方法已经成功过一次，就别再发一次请求了。
        # **这不是替它做判断**：它可以换方法、可以收尾，只是"用同样的方法再抓一遍
        # 同一个网址"不会有不同的结果。实测它拿到一页 1300 字的首页后觉得内容少，
        # 用 http 又抓了两遍，每遍都重新抽一次要点——白花二十多秒。
        if method in state["done"]:
            return (state["done"][method] +
                    "\n\n（这次没有重新请求：同样的方法刚才已经抓过，结果就是上面这些。"
                    "**这就是这一页的全部内容**——要么换个方法，要么用它收尾。）")
        state["tries"] += 1
        await emit_safe(emit, "fetch_try", {"url": url, "method": method,
                                            "n": state["tries"]})
        raw = await (net.browser_get(url) if method == "browser" else net.http_get(url))
        state["raw"] = raw
        body, kind = raw.best_text()

        # ---- 以下都是"把活干对"，不含判断 ----
        if body.strip():
            doc_id = content.save_doc(
                url=url, text=body, title=raw.title or url, kind=kind, via=raw.via,
                has_next=bool(raw.next_links),
                series_id=series_hint or "",
                page_no=0,
            )
            state["doc_id"] = doc_id
            # 这一页的下一页登记进库：它属于同一篇、页码加一。
            for nxt, _label in raw.next_links[:1]:
                content.link_page(url, nxt)
            content.remember_host(url, method=raw.via, ok=True)
            points = await extract(body, goal, title=raw.title,
                                   budget=budget)
            st = content.series_status(doc_id)
            state["item"] = content.Item(
                id=doc_id, kind="doc", title=raw.title or url, url=url,
                summary=points or content.preview(body, goal),
                when="刚读到", source=content.host_of(url),
                complete=content.complete_label(st), chars=len(body),
                extra={"series_id": st.get("series"), "via": raw.via},
            )
            await emit_safe(emit, "fetch_ok", {
                "url": url, "id": doc_id, "chars": len(body), "via": raw.via,
                "title": raw.title, "kind": kind,
                "next": [u for u, _ in raw.next_links],
            })
            report = raw.report() + f"\n\n按目标抽出来的要点：\n{points or '（没抽出来）'}"
            # 两种方法都试过时，如实说一句结果差多少——**这是报告事实不是替它判断**，
            # 但它下次就知道在这个站上白试了一次浏览器。
            other = "browser" if method == "http" else "http"
            if other in state["done"] and state.get("chars", {}).get(other):
                a, b = len(body), state["chars"][other]
                if abs(a - b) < max(60, min(a, b) * 0.1):
                    report += (f"\n\n（注意：`{other}` 那次拿到的是 {b} 字，这次 {a} 字，"
                               "**两种方法拿到的是同一个页面**。别再换方法试了。）")
            state.setdefault("chars", {})[method] = len(body)
            state["done"][method] = report
            return report

        content.remember_host(url, note=("挑战页" if raw.challenge else
                                         "登录墙" if raw.wall else ""), ok=False)
        await emit_safe(emit, "fetch_fail", {"url": url, "via": raw.via,
                                             "error": raw.error or "没有正文"})
        report = raw.report()
        # 失败的也记：同一个方法再试一遍，结果不会变。
        # （超时和连不上例外——那可能是一时的，值得再试一次。）
        if not raw.error or ("超时" not in raw.error and "连不上" not in raw.error):
            state["done"][method] = report
        return report

    tools = [
        Tool(
            name="fetch_page",
            description="把这个网址取回来。method 填 http 或 browser。",
            parameters={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["http", "browser"],
                        "description": "先试 http，被挡住或者页面是 JS 渲染的再用 browser",
                    }
                },
                "required": ["method"],
            },
            run=take,
        )
    ]

    prior = content.host_note(url)
    task = (
        f"把这个网址取下来：{url}\n"
        f"要从里面看什么：{goal or '这一页讲了什么'}\n"
        + (f"这个站以前的情况：{prior}\n" if prior else "")
    )

    out = await run_agent(
        name="fetch", system=SYSTEM, task=task, tools=tools,
        budget=budget, emit=emit,
    )
    if state["item"] is not None:
        out.items = [state["item"]]
        out.extra["doc_id"] = state["doc_id"]
        raw = state["raw"]
        out.extra["next_links"] = [u for u, _ in (raw.next_links if raw else [])]
    else:
        raw = state["raw"]
        why = (raw.error if raw and raw.error else
               "有登录墙" if raw and raw.wall else
               "有反爬挑战" if raw and raw.challenge else "没有抽出正文")
        out.problems.append(f"{url} 没读下来：{why}")
    return out


async def emit_safe(emit: Emit | None, kind: str, data: dict) -> None:
    if emit is not None:
        await emit(kind, data)
