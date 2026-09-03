"""逐段翻译。原子工具：切段、逐段调模型、把结果配对交回去。

**产出是「原文段↔译文段」的配对，不是一整块文本。** 界面默认只渲染译文
（用户要读的是一篇干净的中文），但配对里的原文段是"想看这一段原文"
和"在这一段上写批注"两个交互的全部依据，所以必须一起带出来。

**段之间并发。** 一篇一万字切成五段，五段同时翻，墙钟时间等于最慢那一段。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from . import config
from .cost import Budget
from .llm import for_agent

log = logging.getLogger("scout.translate")

SYSTEM = """你在把一段原文翻译成中文。

- **逐字翻译，不许省略、不许概括、不许总结。**原文有多少句，译文就有多少句。
- **逐行对齐**：原文每个非空行对应译文一个非空行，非空行数必须完全一致；
  保持原文的段落划分和换行，不得合并或拆分行。
- 人名、地名、作品名、专有名词：常见的用通行译名，没有通行译名的**保留原文**，
  第一次出现时可以在后面用括号补一个音译。
- 对话保持对话的口气，叙述保持叙述的口气。原文是恐怖故事就别翻得像说明书。
- 原文里的注释、编者按、投票提示这类**不属于正文的东西**照样翻，
  但不要自己添加任何解释、评论或者"译者注"。
- **只输出译文本身**，不要写"以下是翻译"这类话，不要加标题。

{purpose}"""


@dataclass
class Segment:
    idx: int
    total: int
    source: str
    target: str = ""
    ok: bool = True
    error: str = ""


def split_segments(text: str, target: int | None = None) -> list[str]:
    """把整篇切成一段一段。**这个粒度就是对照阅读的粒度。**

    先按自然段（空行）切；一段本身太长就按行累积到目标长度再断，
    **绝不在句子中间断开**。太粗则对照没法看，太细则上下文断裂、
    人物称呼前后不一致——所以还给每段配了前后文（见 translate_all）。
    """
    target = target or config.TRANSLATE_SEGMENT_CHARS
    hard = config.TRANSLATE_CHUNK_CHARS
    out: list[str] = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        para = para.strip()
        if not para:
            continue
        # 判据是**目标粒度**不是硬上限：短于目标的一段整个留下，
        # 超过的按行累积切开。用硬上限判会切不动——实测一段 683 字
        # （小于 900 的硬上限）整段留着，对照视图还是一大坨。
        if len(para) <= target * 1.25:
            out.append(para)
            continue
        # 长自然段：按行累积；单行还超长就按句号断
        buf: list[str] = []
        for line in para.split("\n"):
            if len(line) > hard:
                if buf:
                    out.append("\n".join(buf))
                    buf = []
                cur = ""
                for piece in re.split(r"(?<=[。！？.!?」』])\s*", line):
                    if len(cur) + len(piece) > hard and cur:
                        out.append(cur)
                        cur = piece
                    else:
                        cur += piece
                if cur.strip():
                    out.append(cur)
                continue
            buf.append(line)
            if sum(len(x) for x in buf) >= target:
                out.append("\n".join(buf))
                buf = []
        if buf:
            out.append("\n".join(buf))
    return out or ([text.strip()] if text.strip() else [])


async def translate_all(
    text: str,
    purpose: str = "",
    *,
    budget: Budget | None = None,
    on_segment=None,
) -> list[Segment]:
    """把整篇翻完。段之间并发，每段翻好就回调一次 `on_segment`（前端逐段显示）。"""
    parts = split_segments(text)
    total = len(parts)
    channel = for_agent("process")
    sem = asyncio.Semaphore(config.TRANSLATE_CONCURRENCY)
    system = SYSTEM.format(purpose=(f"这次的具体要求：{purpose}" if purpose else ""))

    async def one(i: int, src: str) -> Segment:
        seg = Segment(idx=i, total=total, source=src)
        # **前后各给一段原文当参考，但只翻中间那段。**段切细了对照才好看，
        # 代价是上下文断裂（人称、称呼前后不一致），这一条把代价补回来。
        # 只给原文不给译文：段之间是并发的，拿不到邻段的译文。
        around = ""
        if i > 0:
            around += f"\n\n【前一段原文，只作参考，不要翻译它】\n{parts[i - 1][-300:]}"
        if i + 1 < total:
            around += f"\n\n【后一段原文，只作参考，不要翻译它】\n{parts[i + 1][:300]}"
        async with sem:
            for attempt in range(config.TRANSLATE_SEGMENT_RETRY + 1):
                try:
                    msg = await channel.chat(
                        [{"role": "system", "content": system},
                         {"role": "user", "content":
                             (f"【要翻译的就是下面这段】\n{src}" + around) if around else src}],
                        agent="process", purpose=f"translate:{i + 1}/{total}",
                        budget=budget, temperature=0.3,
                        # 逐段翻译是转换不是推断，关掉推理：实测开着时
                        # 输出 token 有九成花在"想"上，一篇短文要多等一两分钟。
                        reasoning="none",
                    )
                    got = (msg.get("content") or "").strip()
                    if got:
                        seg.target = got
                        return seg
                except Exception as exc:  # noqa: BLE001
                    seg.error = str(exc)[:120]
                if attempt < config.TRANSLATE_SEGMENT_RETRY:
                    await asyncio.sleep(0.6 * (attempt + 1))
            seg.ok = False
            seg.target = f"（这一段没翻成：{seg.error or '模型没有返回内容'}）"
            log.warning("第 %d/%d 段翻译失败：%s", i + 1, total, seg.error)
            return seg

    tasks = [asyncio.create_task(one(i, p)) for i, p in enumerate(parts)]
    done: list[Segment] = []
    for task in tasks:
        seg = await task
        done.append(seg)
        if on_segment is not None:
            await on_segment(seg)
    return done


def segments_to_text(segments: list[Segment], note: str = "") -> str:
    body = "\n\n".join(s.target for s in segments if s.target)
    return f"{note}\n\n{body}".strip() if note else body
