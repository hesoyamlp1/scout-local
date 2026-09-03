"""处理整篇 subagent：翻译、摘要、逐段处理。

**判断权在它手上**：这一篇够不够完整、缺页要不要去补、专有名词保不保留原文、
这一段是对话还是叙述该用什么口气。

旧版本这些事分散在三处、约 400 行，全都长在 agent loop 里：`wants_whole_text`
拿 15 个关键词猜用户要不要整篇、`autofill_incomplete_archive` 替它补读、
`ensure_whole_text_ready` 在翻译前设关卡。用户说"帮我把这篇弄成中文"不带
"翻译"两个字，整条路就不触发。现在没有关键词，主 agent 派它来，它自己看这一篇全不全。

**产物走旁路。** 译文进库、进前端部件，**不进任何 agent 的上下文**——
一篇上万字的译文进了上下文，往后每一轮都要重复付一次钱。
主 agent 只拿到一张回执（翻了多少段、多少字、有没有失败）。
"""

from __future__ import annotations

import logging

from ... import config, content
from ...cost import Budget
from ...translate import segments_to_text, translate_all
from ..runner import Emit, Result, Tool, run_agent
from .fetch import run_fetch

log = logging.getLogger("scout.agent.process")

SYSTEM = """你负责把库里的一篇东西整篇处理掉——翻译、摘要、或者按用户要的方式加工。

先搞清楚这一篇在库里是什么状态，再动手。工具：

- `inspect`：这一篇现在库里有几页、多少字、是不是读到结尾了。**第一步先调它。**
- `fill`：顺着分页链接一直读到末页。**库里不完整就先补**——
  分页文章很容易只存了第一页，直接翻就是给用户半篇。
  补一次可能读好几页，读完再 `看这一篇` 确认。
- `translate`：把整篇逐段翻成中文。译文会直接交给用户，**不经过你**，
  你只会拿到一张回执（翻了多少段、多少字）。
- `summarize`：把整篇缩成一段。要摘要而不是全文时用它。

判断要点：

- 库里标着"不完整""还缺几页"——**先补齐再翻**。补不动（页面读不下来）就翻现有的，
  但要在结论里说清楚这是半篇。
- 标着"不确定是不是全篇"的老记录——补一次试试，补不出新页面就按现有的处理。
- 已经完整的，直接动手，别多此一举地再补一遍。
- 这一篇以前翻过、而且覆盖了现在的全部原文——直接用库里那份，**别重翻**，
  除非用户明说要重新翻一遍。

结论怎么写：一两句话说清做了什么（翻了多少字、是不是全篇、有没有哪几段没成）。
**不要复述译文内容**，用户那边已经直接看到译文了。"""


async def run_process(
    doc_id: str,
    want: str,
    *,
    budget: Budget,
    emit: Emit | None = None,
    retranslate: bool = False,
) -> Result:
    """把一篇整个处理掉。译文放在 `extra["translation"]` 里，走旁路交付。"""
    doc = content.get_doc(doc_id)
    if doc is None:
        out = Result(text=f"库里没有 id 是 {doc_id} 的东西。")
        out.problems.append(f"找不到 {doc_id}")
        return out
    series = doc.get("series_id") or doc_id
    state: dict = {"translation": None, "summary": ""}

    async def look() -> str:
        st = content.series_status(series)
        pages = content.series_pages(series)
        got = [p for p in pages if (p["chars"] or 0) > 0]
        holes = [p for p in pages if (p["chars"] or 0) == 0]
        had = content.find_translation(series)
        bits = [
            f"这一篇：{doc.get('title') or doc.get('url')}",
            f"状态：{content.complete_label(st)}",
            f"库里已有 {len(got)} 页，共 {st.get('chars')} 字",
        ]
        if holes:
            bits.append(f"还有 {len(holes)} 页登记了但没抓：" +
                        "、".join(p["url"] for p in holes[:3]))
        if had:
            covered = had.get("covered") or 0
            bits.append(
                f"以前翻过一次（{had.get('created_at') and ''}{had['chars'] if 'chars' in had else len(had.get('text') or '')} 字译文，"
                f"当时覆盖了 {covered} 字原文）。"
                + ("原文后来变长了，那份是残篇，要重翻。"
                   if covered and st.get("chars", 0) > covered * 1.1
                   else "覆盖得上现在的原文，可以直接用。")
            )
        return "\n".join(bits)

    async def fill() -> str:
        """顺着分页读到末页。**停止条件是真读到末页**，不是翻够几次。"""
        read = 0
        for _ in range(config.PAGINATION_HARD_CAP):
            nxt = content.next_page_url(series)
            if not nxt:
                break
            sub = await run_fetch(
                nxt, want or "把这一页的正文读下来",
                budget=budget.child(config.SUBAGENT_BUDGET_SHARE["fetch"],
                                    label="fetch:page"),
                emit=emit, series_hint=series,
            )
            if not sub.items:
                return (f"补到第 {read + 1} 页的时候读不下来了（{'；'.join(sub.problems) or '没拿到正文'}）。"
                        f"已经补了 {read} 页。要么用现有的，要么换个办法。")
            read += 1
            if budget.exhausted():
                break
        st = content.series_status(series)
        if emit:
            await emit("series_filled", {"series": series, "read": read,
                                         "pages": st.get("pages"),
                                         "chars": st.get("chars")})
        return (f"补读了 {read} 页。现在这一篇：{content.complete_label(st)}"
                if read else "没有找到还没抓的页面，这一篇已经是库里能有的全部了。")

    async def do_translate(purpose: str = "") -> str:
        st = content.series_status(series)
        had = content.find_translation(series)
        if had and not retranslate:
            covered = had.get("covered") or 0
            if not (covered and st.get("chars", 0) > covered * 1.1):
                content.touch_translation(had["id"])
                state["translation"] = {
                    "id": had["id"], "series": series, "title": had.get("title"),
                    "url": had.get("url"), "text": had.get("text") or "",
                    "segments": had.get("segments_list") or [], "reused": True,
                }
                if emit:
                    await emit("translation_reused", {
                        "id": had["id"], "series": series,
                        "chars": len(had.get("text") or ""),
                        "segments": len(had.get("segments_list") or []),
                        "title": had.get("title"), "url": had.get("url"),
                    })
                return (f"这一篇以前翻过，直接用了库里那份（{len(had.get('text') or '')} 字，"
                        f"{len(had.get('segments_list') or [])} 段）。没有重翻，省了一次。")

        body, pages = content.series_text(series)
        if not body.strip():
            return "这一篇库里没有正文，翻不了。先补齐。"

        note = ""
        if not st.get("complete"):
            note = (f"（说明：库里这一篇是 {len(pages)} 页 {len(body)} 字，"
                    f"{content.complete_label(st)}。下面是现有部分的翻译。）")
        if emit:
            await emit("translate_start", {
                "series": series, "chars": len(body), "pages": len(pages),
                "title": doc.get("title"), "complete": bool(st.get("complete")),
            })

        async def on_seg(seg) -> None:
            if emit:
                await emit("translate_segment", {
                    "series": series, "idx": seg.idx, "total": seg.total,
                    "source": seg.source, "target": seg.target, "ok": seg.ok,
                })

        segs = await translate_all(body, purpose or want, budget=budget,
                                   on_segment=on_seg)
        text = segments_to_text(segs, note=note)
        seg_dicts = [{"idx": s.idx, "total": s.total, "source": s.source,
                      "target": s.target, "ok": s.ok} for s in segs]
        failed = sum(1 for s in segs if not s.ok)
        tid = content.save_translation(
            series_id=series, text=text, segments=seg_dicts,
            url=doc.get("url") or "", title=doc.get("title") or "",
            purpose=purpose or want, covered=len(body), failed=failed,
        )
        state["translation"] = {
            "id": tid, "series": series, "title": doc.get("title"),
            "url": doc.get("url"), "text": text, "segments": seg_dicts,
            "reused": False,
        }
        if emit:
            await emit("translate_done", {
                "id": tid, "series": series, "segments": len(segs),
                "failed": failed, "chars": len(text),
                "title": doc.get("title"), "url": doc.get("url"),
            })
        tail = f"，其中 {failed} 段没翻成（位置已在译文里标出）" if failed else ""
        return (f"翻完了：原文 {len(body)} 字，切成 {len(segs)} 段{tail}。"
                f"译文已经直接交给用户了，**不在你的上下文里**，你不用也没法复述它。"
                + ("这一篇原文本身不完整，译文开头已经写明。" if note else ""))

    async def do_summary(purpose: str = "") -> str:
        from ...extract import extract

        body, pages = content.series_text(series)
        if not body.strip():
            return "这一篇库里没有正文。先补齐。"
        got = await extract(body, purpose or want or "把这一篇讲了什么说清楚",
                            title=doc.get("title") or "", budget=budget, limit=2000)
        state["summary"] = got
        return f"摘完了（{len(pages)} 页 {len(body)} 字）：\n\n{got}"

    tools = [
        Tool(name="inspect", description="这一篇在库里有几页、多少字、完不完整、以前翻没翻过。",
             parameters={"type": "object", "properties": {}}, run=look),
        Tool(name="fill", description="顺着分页链接一直读到末页。库里不完整时先用它。",
             parameters={"type": "object", "properties": {}}, run=fill, spawns="fetch"),
        Tool(name="translate", description="把整篇逐段翻成中文。译文直接交给用户，你只拿回执。",
             parameters={"type": "object", "properties": {
                 "purpose": {"type": "string", "description": "具体要求，比如「保留人名原文」"}}},
             run=do_translate),
        Tool(name="summarize", description="把整篇缩成一段。要摘要而不是全文时用。",
             parameters={"type": "object", "properties": {
                 "purpose": {"type": "string", "description": "摘什么"}}},
             run=do_summary),
    ]

    out = await run_agent(
        name="process", system=SYSTEM,
        task=f"要处理的是库里这一篇：{doc.get('title') or doc.get('url')}（id {doc_id}）\n"
             f"用户要的是：{want}",
        tools=tools, budget=budget, emit=emit,
    )
    if state["translation"]:
        out.extra["translation"] = state["translation"]
    if state["summary"]:
        out.extra["summary"] = state["summary"]
    st = content.series_status(series)
    out.items = [content.Item(
        id=doc_id, kind="doc", title=doc.get("title") or doc.get("url"),
        url=doc.get("url") or "", summary=state["summary"] or "（整篇已处理，见译文）",
        when="刚处理", source=content.host_of(doc.get("url") or ""),
        complete=content.complete_label(st), chars=st.get("chars") or 0,
        extra={"series_id": series},
    )]
    return out
