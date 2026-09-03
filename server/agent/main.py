"""主 agent：唯一跟用户说话的那个。

**它自己出话**，不再有第二个模型把答案重写一遍。

手里两条路：

- **直接取内容库** —— 程序化、零判断、几毫秒、不花钱。
  你知道要哪一条（前面出现过、目录里点到名的），就直接取，
  不派人去转述一遍。这条路还让历史压缩变成无损的：旧轮次只留结论和 ID，
  正文一直在库里等着被叫号。
- **派 subagent** —— 要判断的活：找一找、搜索、处理整篇。

**它不知道翻页、白名单、存档不全、分页归组这些东西存在**，那些全在 subagent 内部。
旧版本的系统提示词有 90 行，其中至少 60 行讲的是这类内部机制。
"""

from __future__ import annotations

import logging
import re

from .. import config, content
from ..cost import Budget
from .runner import Emit, Result, Tool, run_agent
from .subagents.find import run_find
from .subagents.process import run_process
from .subagents.research import run_research

log = logging.getLogger("scout.agent.main")

SYSTEM = """你是一个联网阅读助手。今天是 {date}。

{profile}

用户找你，多半是这几件事之一：找点东西看、把找到的东西读懂（翻译、摘要）、
或者就着读过的内容聊两句。你自己判断这一轮是哪一种。

## 你能做什么

**直接答。** 闲聊、改写、算术、根据前面聊过的内容就能答的、你自己知道的概念和原理——
直接说，一个工具都别调。用户明说"不用查""凭你自己的理解说"的时候更是如此。

**`take`**：按编号把某一条材料的内容取回来。**几毫秒，不花钱。**
前面几轮出现过的编号、`目录` 里看到的编号，想看就直接取，别派人去找。

**`catalog`**：看看库里都有什么——记过哪些主题、最近读过哪些东西。

**`remember`**：只有用户明确说“记住”“以后都这样”时，保存一条关于他的长期信息。
普通闲聊、一次性的要求不要记；他没有明确要求时也不要擅自调用。

**`recall`**：在记忆里找。你**不知道**有没有、是哪一条的时候用它
（"我以前是不是查过 CRDT"）。它会自己判断查回来的东西跟问题是不是一回事，
只把真相关的交给你。

**`research`**：去互联网上查。要新东西、要外部事实、要近况的时候用它。
它自己想检索词、自己挑哪几个网页打开、自己读，回来把材料交给你。
**通常一次就够**——除非它明说没查到，或者用户问的是明显不同的另一件事。

**`process`**：把某一条材料整篇翻译或者摘要。用户要"翻译这篇""给我全文的中文"
"这篇讲了什么"的时候，把编号交给它。
它自己会检查那一篇在库里全不全、缺页自己去补。
**译文会直接交给用户，不经过你**——你只拿到一张回执，不用也没法复述译文内容。

## 判断材料够不够

材料会带着**出处、时间、完整性**摆在你面前，像这样：

    [3] 网页｜怪談朗読・第七夜
        kikikaikai.jp · 刚读到 · 不完整：有 1 页，还缺 6 页没抓
    [5] 结论｜幂等
        记忆 · 记于 2026-05-12
        同一个操作执行一次和多次，结果完全相同。

看着这些自己判断：

- **判据是材料里有没有答案本身**，不是有没有出现相关的词。
  只是提到过这个词、说了它被谁用过，却没讲它到底是什么——那就是答不了。
- 材料答不了，而这是概念、原理、术语解释类的问题，**你自己就知道**——
  直接把答案说出来，别理那些材料，也别去查。现查一遍又慢又没必要。
- 材料答不了，而这问的是外部事实、近况、某个东西现在什么样——去 `research`。
- **别拿沾边的材料硬凑出一段"材料显示……但没有说明……"**，那不是回答。
- 材料上写着"不完整""还缺几页"，而用户要的是全文——交给 `process`，它会去补。

## 怎么说话

- 用中文，除非用户用别的语言问。
- **用了材料就在句末带角标**，形如 `[3]`，只写编号，绝不写网址或域名。
  没用材料（自己知道的、闲聊）就不要带角标。
- **派 `process` 处理过的篇目，在答案里提到它就要带上它的角标。**
  译文本身不经过你，但那一篇是有编号的材料——带上角标，用户才点得开去读，
  这段话也才有据可查。说了三篇的内容却一个角标都没有，看起来就像是你编的。
- 不要写"根据搜索结果""以下是我的回答"这类开场白，直接说内容。
- 自然、简短，像个说话正常的熟人。别端着，也别热情过头。
- **不要预设情绪。**用户说"我想回家""不知道"这类模糊的、随口的话，就正常接一句——
  他多半真的就是随口说说。别追着反问，更不要做安全排查。
  只有他明确说了自己难受、遇到了具体麻烦、或者开口求助，才认真接住。
- 拿不准他想说什么，就顺着说一句你的理解，或者问一个具体的小问题，
  别一次抛三个问题让他做选择题。"""


_CITE = re.compile(r"\[(\d{1,3})\]")


class Materials:
    """本轮的材料池：短号 ↔ 内容库 ID。

    模型看到的是 `[3]` 这种短号，库里的 ID 是 16 位哈希——太长，放进上下文纯属浪费。
    所以本轮维护一张映射表，程序负责转换。**这跟旧版本的"两套编号"不是一回事**：
    那两套是各自独立分配、语义不同的东西（本轮素材序号 / 内容哈希），
    到处互相查找、转错就出 bug；这里只是一层显示层映射，一一对应。
    """

    def __init__(self) -> None:
        self.by_num: dict[int, content.Item] = {}
        self.by_id: dict[str, int] = {}

    def add(self, items: list[content.Item]) -> list[int]:
        nums = []
        for it in items:
            if it.id in self.by_id:
                nums.append(self.by_id[it.id])
                continue
            num = len(self.by_num) + 1
            self.by_num[num] = it
            self.by_id[it.id] = num
            nums.append(num)
        return nums

    def get(self, raw) -> content.Item | None:
        try:
            return self.by_num.get(int(str(raw).strip().strip("[]")))
        except ValueError:
            return None

    def render(self, items: list[content.Item]) -> str:
        return "\n\n".join(it.line(self.by_id[it.id]) for it in items if it.id in self.by_id)

    def used(self, text: str) -> list[content.Item]:
        """答案里真正引用到的那几条，按出现顺序。"""
        out, seen = [], set()
        for m in _CITE.finditer(text or ""):
            it = self.by_num.get(int(m.group(1)))
            if it is not None and it.id not in seen:
                seen.add(it.id)
                out.append(it)
        return out


def strip_bad_citations(text: str, mats: Materials) -> str:
    """把指向不存在编号的角标去掉。模型偶尔会写 `[9]` 而手上只有三条材料。"""
    return _CITE.sub(lambda m: m.group(0) if mats.by_num.get(int(m.group(1))) else "", text)


async def run_main(
    question: str,
    *,
    budget: Budget,
    emit: Emit,
    history: list[dict] | None = None,
    on_text=None,
    session_id: str = "",
    today: str = "",
) -> tuple[Result, Materials]:
    """跑完主 agent 这一轮。返回（结果，本轮材料池）。"""
    mats = Materials()
    translations: list[dict] = []
    sub_steps: list = []

    async def take(nums: list) -> str:
        """直接取内容库。零判断、几毫秒。"""
        got = []
        for raw in (nums or [])[:6]:
            it = mats.get(raw)
            if it is None:
                got.append(f"没有编号 {raw} 这条材料。")
                continue
            doc = content.get_doc(it.id)
            if doc is None:
                got.append(f"[{raw}] {it.title}\n{it.summary}")
                continue
            st = content.series_status(it.id)
            body, pages = content.series_text(doc["series_id"] or it.id)
            notes = content.notes_block(doc["series_id"] or it.id)
            got.append(
                f"[{raw}] {doc['title']}（{content.host_of(doc['url'])}）"
                f" · {content.complete_label(st)}\n"
                f"{content.preview(body, question, 1200)}"
                + (f"\n\n{notes}" if notes else "")
            )
        if emit:
            await emit("take", {"nums": nums})
        return "\n\n".join(got) or "没取到东西。"

    async def quote(num, seg: int = 0) -> str:
        """按段把译文调出来。**这是译文进上下文的唯一入口，而且只进一段。**"""
        it = mats.get(num)
        series = (it.extra.get("series_id") or it.id) if it else str(num)
        got = content.translation_segment(series, int(seg) - 1 if int(seg) > 0 else 0)
        if got is None:
            tr = content.find_translation(series)
            if tr is None:
                return f"[{num}] 这一篇还没翻过，没有译文可以调。"
            n = len(tr.get("segments_list") or [])
            return f"这一篇的译文一共 {n} 段，没有第 {seg} 段。"
        if emit:
            await emit("quote", {"series": series, "seg": got.get("idx")})
        notes = content.notes_block(series, got.get("idx"))
        return (f"《{got.get('title')}》第 {(got.get('idx') or 0) + 1}/{got.get('total')} 段\n\n"
                f"原文：\n{got.get('source') or ''}\n\n译文：\n{got.get('target') or ''}"
                + (f"\n\n{notes}" if notes else ""))

    async def catalog() -> str:
        cat = content.catalog()
        recent = content.recent_docs(12)
        nums = mats.add(recent)
        lines = [cat] if cat else []
        if recent:
            lines.append("最近读过的（可以直接 `取`）：")
            lines.extend(f"  [{n}] {it.title} —— {it.source}，{it.complete}"
                         for n, it in zip(nums, recent))
        return "\n".join(lines) or "库里还是空的。"

    async def remember(text: str) -> str:
        """用户明确要求记住时，当场写进长期 Memory。"""
        pid = content.save_profile(text, confirmed=True)
        if emit:
            await emit("memory_written", {"profile": 1, "confirmed": True})
        return f"已经记住了：{text}"

    async def find(what: str) -> tuple[str, Result]:
        res = await run_find(
            what, budget=budget.child(config.SUBAGENT_BUDGET_SHARE["find"], label="find"),
            emit=emit, session_id=session_id,
        )
        sub_steps.extend(res.steps)
        if not res.items:
            return (res.text or "记忆里没找到相关的东西。", res)
        nums = mats.add(res.items)
        return (f"{res.text}\n\n{mats.render(res.items)}", res)

    async def search(what: str) -> tuple[str, Result]:
        res = await run_research(
            what, budget=budget.child(config.SUBAGENT_BUDGET_SHARE["research"],
                                      label="research"),
            emit=emit,
        )
        sub_steps.extend(res.steps)
        if not res.items:
            body = res.text or "这次没查到能用的材料。"
            if res.problems:
                body += "\n" + "\n".join(f"- {p}" for p in res.problems)
            return (body + "\n别用同样的说法再查一遍——换个角度，或者直接告诉用户没查到。", res)
        mats.add(res.items)
        tail = ("\n遇到的问题：\n" + "\n".join(f"- {p}" for p in res.problems)) if res.problems else ""
        return (f"{res.text}\n\n{mats.render(res.items)}{tail}", res)

    async def process(num, want: str = "") -> tuple[str, Result]:
        it = mats.get(num)
        if it is None:
            return (f"没有编号 {num} 这条材料。", Result())
        res = await run_process(
            it.id, want or question,
            budget=budget.child(config.SUBAGENT_BUDGET_SHARE["process"], label="process"),
            emit=emit,
        )
        sub_steps.extend(res.steps)
        if res.extra.get("translation"):
            artifact = dict(res.extra["translation"])
            artifact["num"] = mats.by_id.get(it.id)
            translations.append(artifact)
        if res.items:
            mats.add(res.items)
        return (res.text or "处理完了。", res)

    tools = [
        Tool(name="take", description="按编号把材料的内容取回来。几毫秒，不花钱。",
             parameters={"type": "object", "properties": {
                 "nums": {"type": "array", "items": {"type": "integer"},
                          "description": "材料编号，可以给几个"}},
                 "required": ["nums"]}, run=take),
        Tool(name="quote",
             description="把某一篇译文的某一段调出来（原文加译文）。用户就着某一段问的时候用。",
             parameters={"type": "object", "properties": {
                 "num": {"type": "integer", "description": "材料编号"},
                 "seg": {"type": "integer", "description": "第几段，从 1 开始"}},
                 "required": ["num", "seg"]}, run=quote),
        Tool(name="catalog", description="看看库里都有什么：记过哪些主题、最近读过什么。",
             parameters={"type": "object", "properties": {}}, run=catalog),
        Tool(name="remember",
             description="用户明确要求‘记住’或‘以后都这样’时，保存一条长期偏好或个人事实。",
             parameters={"type": "object", "properties": {
                 "text": {"type": "string", "description": "一句能独立看懂、长期成立的信息"}},
                 "required": ["text"]}, run=remember),
        Tool(name="recall", description="在记忆里找。不确定有没有、是哪一条的时候用。",
             parameters={"type": "object", "properties": {
                 "what": {"type": "string", "description": "要找什么，写成一句完整的话"}},
                 "required": ["what"]}, run=find, spawns="find"),
        Tool(name="research", description="去互联网上查。要新东西、外部事实、近况时用。",
             parameters={"type": "object", "properties": {
                 "what": {"type": "string",
                          "description": "要弄清楚什么。**写成一句不依赖上下文的完整的话**，"
                                         "所有指代都换成具体名字"}},
                 "required": ["what"]}, run=search, spawns="research"),
        Tool(name="process",
             description="把某一条材料整篇翻译或摘要。译文直接交给用户，你只拿回执。",
             parameters={"type": "object", "properties": {
                 "num": {"type": "integer", "description": "材料编号"},
                 "want": {"type": "string", "description": "用户具体要什么，比如「逐字翻译成中文」"}},
                 "required": ["num"]}, run=process, spawns="process"),
    ]

    system = SYSTEM.format(date=today, profile=content.profile_block() or "")
    out = await run_agent(
        name="main", system=system, task=question, tools=tools,
        budget=budget, emit=emit, history=history, on_text=on_text,
        stream=True,
    )
    out.steps = out.steps  # 主 agent 自己的步骤；子步骤已经嵌在各步的 children 里
    out.text = strip_bad_citations(out.text, mats)
    out.items = mats.used(out.text)
    if translations:
        # 多篇并发时完成顺序不稳定，交付顺序仍按主材料编号排。
        translations.sort(key=lambda x: x.get("num") or 10_000)
        out.extra["translations"] = translations
        # 旧客户端只认识单篇字段，保留第一篇作为向后兼容。
        out.extra["translation"] = translations[0]
    return out, mats
