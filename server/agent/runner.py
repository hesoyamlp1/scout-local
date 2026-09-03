"""唯一的 agent loop。

**全项目只有这一个循环。** 主 agent 用它，搜索用它，抓取用它，翻译用它。
不同的只有三样：提示词、工具集、预算。旧版本有两份几乎一样的 loop 代码
（brain 6 个工具 / research 4 个工具），预算必须手工隔离，不隔离就两层相乘
跑穿超时——那种东西在这里从结构上就不存在。

**并发在两个层面发生**，这是这一版最要紧的性质：

1. 模型一次返回好几个工具调用时，它们**并发执行**（`asyncio.gather`）。
2. 工具自己可以接数组（比如"读这几个网址"），内部再派一批 subagent 并发跑。

一个失败不阻塞其它——`gather(return_exceptions=True)`，失败的那条变成一句
"这个没成"的回执交给模型，由**它**判断够不够、要不要补派。程序不替它决定
"失败几个算整体失败"。

**预算耗尽不抛异常**：改写最后一条消息逼它用手上的东西收尾。抛异常等于整轮白跑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .. import config
from ..content import Item
from ..cost import Budget
from ..llm import LLMError, for_agent

log = logging.getLogger("scout.agent")

Emit = Callable[[str, dict], Awaitable[None]]


async def _noop(_type: str, _data: dict) -> None:
    return None


@dataclass
class Step:
    """轨迹里的一步。**它同时是三样东西**：界面上给人看的、排查用的、评测读的。"""

    agent: str
    n: int
    tool: str = ""
    args: dict = field(default_factory=dict)
    # 模型这一步在想什么。推理模型自己吐出来的，不是我们编的。
    # 用户那条"看不懂它在干什么"的痛点靠这个字段解决。
    thinking: str = ""
    say: str = ""                       # 模型这一步说的话（没调工具时就是结论）
    result: str = ""                    # 回执摘要
    ok: bool = True
    ms: int = 0
    tokens: int = 0
    batch: int = 0                      # 同一批并发的步骤标同一个号
    children: list["Step"] = field(default_factory=list)

    def to_client(self) -> dict:
        return {
            "agent": self.agent, "n": self.n, "tool": self.tool, "args": _short(self.args),
            "thinking": self.thinking[:600], "say": self.say[:600],
            "result": self.result[:600], "ok": self.ok, "ms": self.ms,
            "tokens": self.tokens, "batch": self.batch,
            "children": [c.to_client() for c in self.children],
        }


@dataclass
class Result:
    """一个 agent 干完活交回来的东西。

    **结构化，不是一段文字。** 旧版本的 research 交回一段话，主 agent 不知道
    读了几篇、哪篇失败了、为什么失败。四部分里前三部分进父 agent 的上下文，
    账只进轨迹。
    """

    text: str = ""                       # 结论
    items: list[Item] = field(default_factory=list)   # 素材
    problems: list[str] = field(default_factory=list)  # 遇到的问题
    steps: list[Step] = field(default_factory=list)    # 轨迹
    tokens: int = 0
    stopped: str = ""                    # done / budget / max_steps / error / silent
    extra: dict = field(default_factory=dict)

    def receipt(self, *, with_items: bool = True, start_num: int = 1) -> str:
        """给父 agent 看的回执。素材带编号，编号由父 agent 统一分配。"""
        parts = [self.text.strip()] if self.text.strip() else []
        if with_items and self.items:
            lines = [it.line(start_num + i) for i, it in enumerate(self.items)]
            parts.append("拿到这些素材：\n\n" + "\n\n".join(lines))
        if self.problems:
            parts.append("遇到的问题：\n" + "\n".join(f"- {p}" for p in self.problems))
        if self.stopped in ("budget", "max_steps"):
            parts.append(f"（这一步是因为{'预算用完' if self.stopped == 'budget' else '步数到顶'}收的尾）")
        return "\n\n".join(parts) or "这一步没有产出。"


def _short(obj: Any, limit: int = 300) -> Any:
    """参数放进轨迹之前削一刀，别把整篇正文塞进事件流。"""
    if isinstance(obj, dict):
        return {k: _short(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_short(v, limit) for v in obj[:8]]
    if isinstance(obj, str) and len(obj) > limit:
        return obj[:limit] + f"…（共 {len(obj)} 字）"
    return obj


@dataclass
class Tool:
    """一个工具。`run` 拿到参数，返回一段给模型看的回执。

    `run` 可以返回字符串，也可以返回 `(回执, Result)` —— 后者用于 subagent 类工具，
    父 agent 借此把子 agent 的素材和轨迹收进来。
    """

    name: str
    description: str
    parameters: dict
    run: Callable[..., Awaitable[Any]]
    # 这个工具是不是派一个 subagent。只影响轨迹怎么显示。
    spawns: str = ""

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


CEILING = (
    "预算到顶了，不要再调用任何工具。用你手上已经有的东西，现在就把结论给出来。"
)
SILENT = "你既没有调用工具，也没有说话。要么调工具继续，要么直接把结论说出来。"


async def _stream_step(
    channel, messages: list[dict], name: str, n: int, budget: Budget,
    tools: list[Tool], temperature: float,
    on_text: Callable[[str], Awaitable[None]],
) -> tuple[dict, bool]:
    """边生成边发的一步。返回（完整 message，有没有真发出去过字）。

    **先攒够 `STREAM_HOLDBACK` 个字再往外送。** 攒着的这一段还没出门，
    期间要是冒出工具调用，就把它扔了当无事发生——字一旦发出去就收不回来，
    界面那边是往上累加的、不清屏。
    """
    buf: list[str] = []
    holding = True
    msg: dict = {}
    async for kind, payload in channel.stream_chat(
        messages, agent=name, purpose=f"{name}:step{n}", budget=budget,
        tools=[t.schema() for t in tools] if tools else None,
        tool_choice="auto" if tools else None, temperature=temperature,
    ):
        if kind == "message":
            # 这是最后一个事件，**不 break**：让生成器自己跑完，
            # 它里头那个 `async with client.stream(...)` 才会当场退出。
            msg = payload
            continue
        buf.append(payload)
        if not holding:
            await on_text(payload)
        elif sum(len(x) for x in buf) >= config.STREAM_HOLDBACK:
            await on_text("".join(buf))
            holding = False

    calls = msg.get("tool_calls") or []
    if not calls and holding and buf:
        # 太短，还没攒够阈值就说完了。一次发掉。
        await on_text("".join(buf))
        holding = False
    return msg, (not holding)


async def run_agent(
    *,
    name: str,
    system: str,
    task: str,
    tools: list[Tool],
    budget: Budget,
    emit: Emit | None = None,
    history: list[dict] | None = None,
    max_steps: int | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    collect: Callable[[Result, str, dict], None] | None = None,
    temperature: float = 0.3,
    stream: bool = False,
) -> Result:
    """跑一个 agent 直到它交出结论。

    `collect` 是父 agent 用来收子 agent 产出的钩子：每次某个工具返回了 `Result`，
    就调一次 `collect(子结果, 工具名, 参数)`。素材归拢、编号分配都在那里做。
    """
    emit = emit or _noop
    steps_cap = int(max_steps or config.AGENT_MAX_STEPS.get(name, 8))
    channel = for_agent(name)
    by_name = {t.name: t for t in tools}

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": task})

    out = Result()
    silent = 0
    batch = 0
    started_tokens = budget.spent()

    await emit("agent_start", {"agent": name, "task": _short(task, 200),
                               "budget": budget.token_cap, "model": channel.model})

    for n in range(1, steps_cap + 1):
        why = budget.exhausted()
        if why:
            await emit("agent_ceiling", {"agent": name, "why": why})
            messages.append({"role": "user", "content": CEILING})

        t0 = time.monotonic()
        tok0 = budget.spent()
        sent_live = False        # 这一步的正文已经边生成边发出去了
        try:
            if stream and on_text is not None:
                msg, sent_live = await _stream_step(
                    channel, messages, name, n, budget, tools, temperature, on_text
                )
            else:
                msg = await channel.chat(
                    messages,
                    agent=name,
                    purpose=f"{name}:step{n}",
                    budget=budget,
                    tools=[t.schema() for t in tools] if tools else None,
                    tool_choice="auto" if tools else None,
                    temperature=temperature,
                )
        except LLMError as exc:
            log.warning("[%s] 第 %d 步模型调用失败：%s", name, n, exc)
            out.stopped = "error"
            out.problems.append(f"模型调用失败：{exc}")
            await emit("agent_error", {"agent": name, "error": str(exc)[:200]})
            break

        calls = msg.get("tool_calls") or []
        say = (msg.get("content") or "").strip()
        thinking = (msg.get("reasoning") or "").strip()
        step = Step(agent=name, n=n, thinking=thinking, say=say,
                    tokens=budget.spent() - tok0, ms=int((time.monotonic() - t0) * 1000))
        if calls and sent_live:
            # 混流：说了几句又要调工具，而那几句已经出门了。
            # **照常执行工具，别把那几句当答案。**混出来的多半是"我来查一下"
            # 这类过渡语，把它当答案等于让用户白等一场；丢掉的工具调用才是真要做的事。
            out.extra["mixed_stream"] = out.extra.get("mixed_stream", 0) + 1
            log.warning("[%s] 同一条流里既吐了正文又要调工具，字照发、工具照跑", name)

        # 没有工具调用 = 它说完了，这就是结论
        if not calls:
            if say:
                out.text = say
                out.stopped = out.stopped or "done"
                out.steps.append(step)
                out.extra["streamed"] = sent_live
                await emit("agent_step", {"agent": name, **step.to_client()})
                if on_text and not sent_live:
                    await on_text(say)
                break
            silent += 1
            out.steps.append(step)
            await emit("agent_step", {"agent": name, **step.to_client()})
            if silent >= 2:
                out.stopped = "silent"
                break
            messages.append({"role": "assistant", "content": ""})
            messages.append({"role": "user", "content": SILENT})
            continue

        silent = 0
        if why:
            # 已经喊过收尾还在调工具，不给它继续了
            out.stopped = "budget"
            out.text = say or out.text
            out.steps.append(step)
            break

        messages.append({k: v for k, v in msg.items() if k != "reasoning"})
        await emit("agent_step", {"agent": name, **step.to_client()})

        # ---- 并发执行这一批工具调用 ----
        batch += 1
        wanted = [c for c in calls if (c.get("function") or {}).get("name") in by_name]
        unknown = [c for c in calls if c not in wanted]

        async def one(call: dict) -> tuple[dict, Any, Step]:
            fn = (call.get("function") or {}).get("name") or ""
            raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {}
            sub = Step(agent=name, n=n, tool=fn, args=args, batch=batch)
            s0 = time.monotonic()
            await emit("tool_start", {"agent": name, "tool": fn, "args": _short(args),
                                      "batch": batch})
            try:
                got = await by_name[fn].run(**args)
            except Exception as exc:  # noqa: BLE001 单个工具失败不拖垮整轮
                log.warning("[%s] 工具 %s 出错：%s", name, fn, exc)
                got = f"{fn} 这次没跑成（{type(exc).__name__}: {exc}）。换个做法，或者把这一步的情况告诉用户。"
                sub.ok = False
            sub.ms = int((time.monotonic() - s0) * 1000)
            return call, got, sub

        results = await asyncio.gather(*(one(c) for c in wanted), return_exceptions=True)

        for item in results:
            if isinstance(item, BaseException):
                log.warning("[%s] 工具批次里有一条整个挂了：%s", name, item)
                continue
            call, got, sub = item
            payload: Result | None = None
            if isinstance(got, tuple):
                text, payload = got
            else:
                text = got
            if payload is not None:
                sub.children = payload.steps
                sub.tokens = payload.tokens
                if collect:
                    collect(payload, sub.tool, sub.args)
                    text = payload.receipt(with_items=False) if not isinstance(text, str) else text
            sub.result = str(text)[:800]
            step.children.append(sub)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or sub.tool,
                "content": str(text),
            })
            await emit("tool_done", {"agent": name, "tool": sub.tool, "ms": sub.ms,
                                     "ok": sub.ok, "batch": batch,
                                     "result": str(text)[:300]})

        for call in unknown:
            fn = (call.get("function") or {}).get("name") or "?"
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or fn,
                "content": f"没有叫 {fn} 的工具。能用的是：{'、'.join(by_name)}。",
            })

        out.steps.append(step)
    else:
        out.stopped = out.stopped or "max_steps"

    out.tokens = budget.spent() - started_tokens
    await emit("agent_done", {"agent": name, "stopped": out.stopped,
                              "tokens": out.tokens, "steps": len(out.steps),
                              "items": len(out.items)})
    return out
