"""模型通道：两档，同一家供应商。

两档都是 OpenAI 兼容的 `/chat/completions`，所以共用一个客户端类，只有模型名不同：

- `pro`   —— 判断错了整轮就废的地方：主 agent、搜索、处理整篇
- `flash` —— 高频、判断局部的地方：抓取、找一找、抽要点

**谁用哪档写在 `config.AGENT_MODEL` 里，不写在代码里。** 调档只改配置。

两档都是推理模型（DeepSeek V4 的两个都是），所以：
- 千万别给 `max_tokens` 设小值，推理会把额度吃光、正文返回空（实测 20 就全没了）
- `reasoning_content` 单独记账，它是轨迹里"模型当时在想什么"的来源

只依赖 httpx，不用任何 SDK——这层薄得没必要引依赖，出问题时能直接看到发出去的是什么。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from . import config
from .cost import Budget, Call

log = logging.getLogger("scout.llm")


class LLMError(RuntimeError):
    pass


class Channel:
    def __init__(self, name: str, base_url: str, api_key: str, model: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.LLM_TIMEOUT, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _record(
        self, budget: Budget | None, agent: str, purpose: str, usage: dict, started: float
    ) -> None:
        if budget is None:
            return
        usage = usage or {}
        details = usage.get("prompt_tokens_details") or {}
        comp = usage.get("completion_tokens_details") or {}
        budget.record(
            Call(
                agent=agent,
                model=self.model,
                purpose=purpose,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(
                    usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0
                ),
                reasoning_tokens=int(comp.get("reasoning_tokens") or 0),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        )

    def _payload(self, messages: list[dict], **kw) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kw.get("max_tokens") or config.LLM_MAX_TOKENS,
        }
        for key in ("tools", "tool_choice", "temperature", "response_format"):
            if kw.get(key) is not None:
                payload[key] = kw[key]
        # 推理深度：显式传的优先，其次查配置表，都没有就用模型默认。
        effort = kw.get("reasoning") or config.AGENT_REASONING.get(kw.get("agent") or "")
        if effort:
            payload["reasoning_effort"] = effort
        return payload

    async def chat(
        self,
        messages: list[dict],
        *,
        agent: str = "",
        purpose: str = "",
        budget: Budget | None = None,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        reasoning: str = "",
        retries: int = 2,
    ) -> dict:
        """一次非流式调用，返回 assistant 那条 message（额外带 `reasoning`）。

        通道抖动会重试。**这一层只重试网络和 5xx**，模型说了什么不归它管。
        """
        payload = self._payload(
            messages, tools=tools, tool_choice=tool_choice, temperature=temperature,
            response_format=response_format, max_tokens=max_tokens,
            agent=agent, reasoning=reasoning,
        )
        last: Exception | None = None
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                client = await self.client()
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise LLMError(f"{self.name} 返回 {resp.status_code}")
                if resp.status_code >= 400:
                    raise LLMError(
                        f"{self.name} 返回 {resp.status_code}: {resp.text[:300]}"
                    )
                data = resp.json()
                self._record(budget, agent, purpose, data.get("usage") or {}, started)
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"{self.name} 没有返回 choices: {str(data)[:200]}")
                msg = dict(choices[0].get("message") or {})
                msg["reasoning"] = msg.pop("reasoning_content", "") or ""
                return msg
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
        raise LLMError(f"{self.name} 调用失败：{last}")

    async def stream(
        self,
        messages: list[dict],
        *,
        agent: str = "",
        purpose: str = "",
        budget: Budget | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式，只吐正文，不给工具。出话那一次用它。

        **推理内容不往外吐**：用户要读的是答案，不是模型的草稿纸。
        推理照样记账，也照样进轨迹。
        """
        payload = self._payload(messages, temperature=temperature,
                                max_tokens=max_tokens, agent=agent)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        started = time.monotonic()
        client = await self.client()
        usage: dict = {}
        async with client.stream(
            "POST", f"{self.base_url}/chat/completions", json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "ignore")
                raise LLMError(f"{self.name} 返回 {resp.status_code}: {body[:300]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
        self._record(budget, agent, purpose, usage, started)

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        agent: str = "",
        purpose: str = "",
        budget: Budget | None = None,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """**带工具的流式**。产出两种事件：

            ("text", "增量正文")   收到一段正文就产出一次
            ("message", {...})    流走完了，产出拼好的那条完整 message

        主 agent 用它，这样答案是边生成边出，而不是等整段写完再发。
        工具调用在流里是按 index 分片来的（第一片带 id 和函数名，
        后面每片续一小段 arguments），所以要自己按 index 攒起来。

        **推理内容不往外吐**，但攒下来放进 message["reasoning"]——轨迹要用它。
        """
        payload = self._payload(messages, tools=tools, tool_choice=tool_choice,
                                temperature=temperature, agent=agent)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        started = time.monotonic()
        client = await self.client()
        usage: dict = {}
        text_parts: list[str] = []
        reason_parts: list[str] = []
        acc: dict[int, dict] = {}
        finish = ""
        async with client.stream(
            "POST", f"{self.base_url}/chat/completions", json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "ignore")
                raise LLMError(f"{self.name} 返回 {resp.status_code}: {body[:300]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reason_parts.append(delta["reasoning_content"])
                    piece = delta.get("content")
                    if piece:
                        text_parts.append(piece)
                        yield ("text", piece)
                    for frag in delta.get("tool_calls") or []:
                        idx = int(frag.get("index") or 0)
                        slot = acc.setdefault(idx, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        fn = frag.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        self._record(budget, agent, purpose, usage, started)

        msg: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
            "reasoning": "".join(reason_parts),
            "finish_reason": finish,
        }
        if acc:
            msg["tool_calls"] = [acc[i] for i in sorted(acc)]
        yield ("message", msg)


# ---------------------------------------------------------------- 两档实例

pro = Channel("pro", config.MODEL_BASE_URL, config.MODEL_API_KEY, config.MODEL_PRO)
flash = Channel("flash", config.MODEL_BASE_URL, config.MODEL_API_KEY, config.MODEL_FLASH)

_TIERS = {"pro": pro, "flash": flash}


def for_agent(name: str) -> Channel:
    """这个 agent 该用哪档。配置里没写就用 flash——便宜的那档兜底比贵的安全。"""
    return _TIERS.get(config.AGENT_MODEL.get(name, "flash"), flash)


def reconfigure() -> None:
    """在设置页改了模型或 key 之后，把两个通道更新过来。

    地址和 key 在构造客户端时就抓死在请求头里，所以变了要把旧客户端丢掉。
    丢的时候不 await：这个函数是同步的，旧客户端交给垃圾回收，它上面没有正在跑的请求。
    """
    for ch, model in ((pro, config.MODEL_PRO), (flash, config.MODEL_FLASH)):
        changed = (
            ch.base_url != config.MODEL_BASE_URL.rstrip("/")
            or ch.api_key != config.MODEL_API_KEY
        )
        ch.base_url = config.MODEL_BASE_URL.rstrip("/")
        ch.api_key = config.MODEL_API_KEY
        ch.model = model
        if changed:
            ch._client = None


async def close_all() -> None:
    for ch in _TIERS.values():
        await ch.aclose()


def parse_json_object(text: str) -> dict | None:
    """从模型输出里抠出一个 JSON 对象。带 ```json 围栏或者前后有废话都能处理。"""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None
