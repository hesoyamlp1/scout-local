"""两个后台 subagent：记结论、记关于我。

**它们不由主 agent 派，主 agent 甚至不知道它们存在。** 触发方式也不一样：

- 记结论：每轮出话之后跑，看的是这一轮
- 记关于我：定时任务每小时扫一次闲置的会话，看的是**整段对话**

节奏不同是有理由的。关于用户的事变化慢，每三轮抽一次的话，模型只看得见那三轮，
会把"这会儿在问 Rust"抽成"用户是 Rust 开发者"。等一整段聊完再看，
它知道那只是其中一个话题——同样一次调用，判断准得多。

两个都跑在用户的等待路径之外，慢一点没人等。
"""

from __future__ import annotations

import logging

from ... import content
from ...cost import Budget
from ..runner import Emit, Result, Tool, run_agent

log = logging.getLogger("scout.agent.memory")

FACT_SYSTEM = """你在决定这一轮对话里有什么值得记进长期记忆。

**判据三条，都要满足才记：**

1. 这句话**离开这次对话还成立吗**？
   「Tokio 是目前 Rust 最主流的 async 运行时」——成立，记。
   「他刚才说的第三点很重要」——离开这次对话就没意义，不记。
2. **过一段时间还成立吗**？
   「Tokio 用 work-stealing 调度」——长期成立，记。
   「Tokio 最新版是 1.42」——下个月就过期。要记就把日期写进去。
3. **以后可能还会问到吗**？

**大多数轮次什么都不该记。** 闲聊、改写、翻译、算术这类轮次没有新事实，
直接调 `skip`。硬凑几条出来只会把记忆库塞满垃圾。

我会把库里相关的旧结论一起给你看。同一件事已经有记录时：

- 说的是同一件事、但情况变了 —— 用 `update_fact` 改那一条，**不要新增**。
  库里留着两条互相矛盾的记录，比没有记录更糟。
- 说的是同一件事、内容也一样 —— 什么都不做。
- 是新的一件事 —— `note` 一条新的。

一条结论就是一句能独立看懂的话，不许有"它""这个""上面说的"这类指代。
每条给一个 `subject`（两到六个字的主题名），以后按主题能点名取。"""

PROFILE_SYSTEM = """你在维护一份"关于这个用户"的记录。

可以从稳定行为归纳的：长期阅读偏好、交互偏好和明确反复出现的习惯。
**身份、工作、住址、关系等个人事实，只有用户在对话里明确说“记住”时才允许保存。**
明确要求记住的记录标成 `confirmed=true`；行为归纳出的偏好标成 `confirmed=false`。
**不值得记的**：他这会儿在问什么（那是话题不是他这个人）、一次性的请求、
从一两句话推测出来的性格。

**宁可少记。** 一条"用户对 X 感兴趣"这种当时成立、过两天就没用的东西，
比空着更糟——它会一直出现在每一轮的上下文里。

我会把现有的记录一起给你。三个动作：

- `add`：确实是新的、长期成立的一条
- `edit`：情况变了（他换了工作、改了偏好）
- `remove`：这条已经不成立了，或者当初就不该记

大多数时候一条都不用动，那就直接说"没什么要改的"。"""


async def run_remember(
    *,
    question: str,
    answer: str,
    used_ids: list[str],
    session_id: str,
    turn_id: str,
    budget: Budget,
    emit: Emit | None = None,
) -> Result:
    """每轮出话之后跑：这一轮有什么值得记的。"""
    saved = {"new": 0, "updated": 0}

    async def note(text: str, subject: str = "") -> str:
        fid = content.save_fact(text, subject=subject, source_ids=used_ids,
                                session_id=session_id, turn_id=turn_id)
        saved["new"] += 1
        return f"记下了（id {fid}）。"

    async def update(fact_id: str, text: str, subject: str = "") -> str:
        fid = content.save_fact(text, subject=subject, source_ids=used_ids,
                                session_id=session_id, turn_id=turn_id, fact_id=fact_id)
        saved["updated"] += 1
        return f"更新了 {fid}。"

    async def skip(why: str = "") -> str:
        return "好，这一轮不记。"

    tools = [
        Tool(name="note", description="记一条新结论。",
             parameters={"type": "object", "properties": {
                 "text": {"type": "string", "description": "一句能独立看懂的话，不许有指代"},
                 "subject": {"type": "string", "description": "两到六个字的主题名"}},
                 "required": ["text"]}, run=note),
        Tool(name="update_fact", description="库里那条说的是同一件事但情况变了，改它。",
             parameters={"type": "object", "properties": {
                 "fact_id": {"type": "string"}, "text": {"type": "string"},
                 "subject": {"type": "string"}},
                 "required": ["fact_id", "text"]}, run=update),
        Tool(name="skip", description="这一轮没有值得记的新事实。",
             parameters={"type": "object", "properties": {
                 "why": {"type": "string", "description": "一句话说明为什么"}}},
             run=skip),
    ]

    # 把相关的旧结论一起给它看——"该更新还是该新增"要有依据才判得了。
    related = content.search_facts(f"{question} {answer[:200]}", limit=6)
    old = "\n".join(f"- [{i.id}] {i.summary}（{i.when}）" for i in related) or "（没有相关的旧记录）"

    out = await run_agent(
        name="remember", system=FACT_SYSTEM,
        task=(f"这一轮的问题：{question}\n\n这一轮的回答：\n{answer[:4000]}\n\n"
              f"库里相关的旧结论：\n{old}"),
        tools=tools, budget=budget, emit=emit,
    )
    out.extra.update(saved)
    if emit and (saved["new"] or saved["updated"]):
        await emit("memory_written", saved)
    return out


async def run_profile(
    *,
    conversation: str,
    budget: Budget,
    emit: Emit | None = None,
) -> Result:
    """定时任务跑：看完一整段对话，维护"关于用户"。"""
    changed = {"added": 0, "updated": 0, "deleted": 0}

    async def add(text: str, confirmed: bool = False) -> str:
        content.save_profile(text, confirmed=confirmed)
        changed["added"] += 1
        return "加上了。"

    async def edit(pid: str, text: str, confirmed: bool = False) -> str:
        content.save_profile(text, pid=pid, confirmed=confirmed)
        changed["updated"] += 1
        return "改好了。"

    async def drop(pid: str) -> str:
        content.delete_profile(pid)
        changed["deleted"] += 1
        return "删了。"

    tools = [
        Tool(name="add", description="加一条关于用户的长期记录。",
             parameters={"type": "object", "properties": {
                 "text": {"type": "string"},
                 "confirmed": {"type": "boolean", "description": "用户是否明确要求记住"}},
                 "required": ["text", "confirmed"]}, run=add),
        Tool(name="edit", description="改一条已有的记录。",
             parameters={"type": "object", "properties": {
                 "pid": {"type": "string"}, "text": {"type": "string"},
                 "confirmed": {"type": "boolean", "description": "用户是否明确要求记住"}},
                 "required": ["pid", "text", "confirmed"]}, run=edit),
        Tool(name="remove", description="删一条不再成立的记录。",
             parameters={"type": "object", "properties": {
                 "pid": {"type": "string"}}, "required": ["pid"]}, run=drop),
    ]

    now = content.profile_all()
    have = "\n".join(
        f"- [{r['id']}] ({'明确记忆' if r['confirmed'] else '归纳偏好'}) {r['text']}"
        for r in now
    ) or "（还没有任何记录）"
    out = await run_agent(
        name="profile", system=PROFILE_SYSTEM,
        task=f"现有的记录：\n{have}\n\n这一段对话：\n{conversation[:12000]}",
        tools=tools, budget=budget, emit=emit,
    )
    out.extra.update(changed)
    return out
