"""记账和预算。

**一轮问答只有一本账。** 主 agent 持有总预算，派一个 subagent 就从剩余额度里
划走一块（`Budget.child`），subagent 花完自己收尾。划走的是上限不是预付——
它没花完，剩下的自动回到父任务手里，因为大家花的是同一个 `Ledger`。

旧版本有两套平行的上限（BRAIN_MAX_ROUNDS / RESEARCH_MAX_ROUNDS），必须手工隔离，
不隔离就两层相乘、跑穿超时。那种东西在这里从结构上就不存在了。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# 每百万 token 多少美元。2026-08-16 查的 DeepSeek 官方价。
# **两档都是推理模型**，推理 token 算在 completion 里，按输出价计费。
PRICING = {
    "deepseek-v4-flash": {
        "input_cache_hit": 0.0028,
        "input_cache_miss": 0.14,
        "output": 0.28,
    },
    "deepseek-v4-pro": {
        "input_cache_hit": 0.003625,
        "input_cache_miss": 0.435,
        "output": 0.87,
    },
}


@dataclass
class Call:
    """一次模型调用的记账条目。"""

    agent: str = ""          # 哪个 agent 调的：main / research / fetch / find / ...
    model: str = ""
    purpose: str = ""        # 干什么用的，同一个 agent 里可以细分
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    # 推理模型的思考过程。它已经算在 completion_tokens 里了，这里单独记一份是为了
    # 看清楚「一次回答里有多大比例花在推理上」——实测一句话的回答能有四分之三是推理。
    reasoning_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def usd(self) -> float:
        price = PRICING.get(self.model)
        if price is None:
            return 0.0
        hit = min(self.cached_tokens, self.prompt_tokens)
        miss = self.prompt_tokens - hit
        return (
            hit * price["input_cache_hit"]
            + miss * price["input_cache_miss"]
            + self.completion_tokens * price["output"]
        ) / 1_000_000


@dataclass
class Ledger:
    """一轮问答的全部模型调用。父 agent 和它派出去的所有 subagent 共用一本。"""

    calls: list[Call] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, call: Call) -> None:
        with self._lock:
            self.calls.append(call)

    def total_tokens(self) -> int:
        with self._lock:
            return sum(c.total_tokens for c in self.calls)

    def summary(self) -> dict:
        with self._lock:
            calls = list(self.calls)
        by_agent: dict[str, dict] = {}
        for c in calls:
            slot = by_agent.setdefault(
                c.agent or "?",
                {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0,
                 "usd": 0.0, "ms": 0},
            )
            slot["calls"] += 1
            slot["prompt"] += c.prompt_tokens
            slot["completion"] += c.completion_tokens
            slot["reasoning"] += c.reasoning_tokens
            slot["usd"] += c.usd
            slot["ms"] += c.latency_ms
        return {
            "calls": len(calls),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "reasoning_tokens": sum(c.reasoning_tokens for c in calls),
            "total_tokens": sum(c.total_tokens for c in calls),
            "usd": round(sum(c.usd for c in calls), 6),
            "by_agent": {k: {**v, "usd": round(v["usd"], 6)} for k, v in by_agent.items()},
        }


class Budget:
    """一份额度：能花多少 token、能花到什么时候。

    **每份额度有自己的账本，花费逐级冒泡到父额度。** 这一条是并发的要求：
    四个抓取同时跑时，如果它们共用一本账、各自拿"账本总量减去起点"当自己的花费，
    那么每一个都会把另外三个的花费算成自己的，四个一起提前收尾。
    所以子任务记在自己账上，同时往上冒泡——父任务的账里自然含着全部子孙。

    **墙钟是全局的**：`deadline` 从根一路传下去，子任务不会因为自己"刚开始"
    就多拿时间。
    """

    def __init__(
        self,
        token_cap: int,
        deadline: float,
        *,
        parent: "Budget | None" = None,
        label: str = "root",
    ) -> None:
        self.ledger = Ledger()
        self.parent = parent
        self.token_cap = int(token_cap)
        self.deadline = deadline
        self.label = label

    @classmethod
    def root(cls, token_cap: int, wall_seconds: float) -> "Budget":
        return cls(token_cap, time.monotonic() + wall_seconds)

    def record(self, call: Call) -> None:
        """记一次模型调用。自己记一笔，同时往上冒泡到每一级父额度。"""
        self.ledger.add(call)
        if self.parent is not None:
            self.parent.record(call)

    def spent(self) -> int:
        """这份额度上已经花掉的 token（含它派出去的所有子任务）。"""
        return self.ledger.total_tokens()

    def remaining(self) -> int:
        return max(0, self.token_cap - self.spent())

    def seconds_left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def exhausted(self) -> str:
        """花完了吗。没花完返回空字符串，花完了返回原因（进轨迹和回执）。"""
        if self.seconds_left() <= 0:
            return "wall_clock"
        if self.remaining() <= 0:
            return "token_budget"
        return ""

    def child(self, share: float, *, label: str, of: int = 1) -> "Budget":
        """从剩下的额度里划一块给子任务。

        划的是**当前剩余**的一个比例，不是总额的比例——所以派到第三个子任务时，
        它拿到的自然比第一个少。这就是"两层相乘"不会发生的原因：
        子任务的上限永远小于父任务此刻的剩余量。

        `of` 是"这一批同时派几个"。并发派四个时，四个是同一时刻建的、
        看到的剩余量一样，不除一下就会各自以为能花掉父任务的一大半。
        """
        share = max(0.05, min(1.0, share)) / max(1, of)
        cap = max(2000, int(self.remaining() * share))
        return Budget(cap, self.deadline, parent=self, label=label)
