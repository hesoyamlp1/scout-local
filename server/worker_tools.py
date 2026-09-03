"""Codex Worker 可调用的 Scout 业务工具。

开放式判断交给 Codex；这里保证 URL 安全、礼貌抓取、分页、幂等存储和译文结构。
"""

from __future__ import annotations

from . import config, content, net
from .translate import split_segments


def _public_url(url: str) -> str:
    return net.public_url(url)


def _doc_result(doc_id: str, *, cached: bool = False) -> dict:
    doc = content.get_doc(doc_id) or {}
    st = content.series_status(doc_id)
    tr = content.find_translation(st["series"])
    return {
        "id": doc_id,
        "series_id": st["series"],
        "url": doc.get("url") or "",
        "title": doc.get("title") or doc.get("url") or "",
        "kind": doc.get("kind") or "article",
        "chars": st.get("chars") or 0,
        "pages": st.get("pages") or 0,
        "complete": bool(st.get("complete")),
        "status": content.complete_label(st),
        "cached": cached,
        "translated": bool(tr),
    }


def _page_body(raw: net.Raw, *, continuing: bool, extractor: str = "article") -> tuple[str, str]:
    """分页文章的短尾页不能被页面下方更长的推荐列表覆盖。"""
    if extractor == "dense" and raw.dense_text.strip():
        return raw.dense_text.strip(), "article"
    if extractor == "article" and raw.text.strip():
        return raw.text.strip(), "article"
    if extractor == "list" and raw.list_text.strip():
        return raw.list_text.strip(), "list"
    if continuing and raw.text.strip():
        return raw.text.strip(), "article"
    return raw.best_text()


def _extraction_review(raw: net.Raw, selected: str = "") -> dict:
    candidates = [{"extractor": "article", **net.content_shape(raw.text)}]
    if raw.dense_text:
        candidates.append({
            "extractor": "dense", "selector": raw.dense_selector,
            "links": raw.dense_links, **net.content_shape(raw.dense_text),
        })
    if raw.list_text:
        candidates.append({
            "extractor": "list", "items": raw.list_items,
            **net.content_shape(raw.list_text),
        })
    return {"selected": selected, "candidates": candidates}


async def fetch_url(url: str, *, refresh: bool = False, extractor: str = "inspect") -> dict:
    """抓一篇公开网页并沿明确的下一页读到末页。"""
    if extractor not in {"inspect", "article", "dense", "list"}:
        raise ValueError("extractor 只允许 inspect/article/dense/list")
    first_url = _public_url(url)
    if extractor == "inspect":
        raw = await net.http_get(first_url)
        empty = not (raw.text.strip() or raw.dense_text.strip() or raw.list_text.strip())
        if (empty or raw.challenge) and config.BROWSER_ENABLED:
            rendered = await net.browser_get(first_url)
            if rendered.text.strip() or rendered.dense_text.strip() or rendered.list_text.strip():
                raw = rendered
        if not (raw.text.strip() or raw.dense_text.strip() or raw.list_text.strip()):
            raise RuntimeError(raw.error or "页面没有可供 AI 验收的内容候选")
        return {
            "url": raw.url,
            "needs_selection": True,
            "has_next": bool(raw.next_links),
            "extraction": _extraction_review(raw),
            "links": raw.page_links,
        }
    old = content.get_doc_by_url(first_url)
    if old and old.get("chars") and not refresh:
        state = content.series_status(old["id"])
        if state.get("complete"):
            return _doc_result(old["id"], cached=True)

    current = first_url
    series_id = ""
    seen: set[str] = set()
    first_id = ""
    review: dict = {}
    for page_index in range(config.PAGINATION_HARD_CAP):
        current = _public_url(current)
        if current in seen:
            break
        seen.add(current)
        raw = await net.http_get(current)
        body, kind = _page_body(raw, continuing=bool(series_id), extractor=extractor)
        selected = (
            "dense" if body.strip() == raw.dense_text.strip() and raw.dense_text.strip()
            else "list" if body.strip() == raw.list_text.strip() and raw.list_text.strip()
            else "article"
        )
        if page_index == 0:
            review = _extraction_review(raw, selected)
        if (not body.strip() or raw.challenge) and config.BROWSER_ENABLED:
            rendered = await net.browser_get(current)
            rendered_body, rendered_kind = _page_body(
                rendered, continuing=bool(series_id), extractor=extractor
            )
            if rendered_body.strip():
                raw, body, kind = rendered, rendered_body, rendered_kind
                selected = (
                    "dense" if body.strip() == raw.dense_text.strip() and raw.dense_text.strip()
                    else "list" if body.strip() == raw.list_text.strip() and raw.list_text.strip()
                    else "article"
                )
                if page_index == 0:
                    review = _extraction_review(raw, selected)
        if not body.strip():
            raise RuntimeError(raw.error or "页面没有可保存的正文")
        next_url = raw.next_links[0][0] if raw.next_links else ""
        did = content.save_doc(
            url=current,
            text=body,
            title=raw.title or current,
            kind=kind,
            via=raw.via,
            has_next=bool(next_url),
            series_id=series_id,
            page_no=page_index + 1,
        )
        if not did:
            raise RuntimeError("正文没有保存成功")
        if not first_id:
            first_id = did
            series_id = content.series_status(did)["series"]
        if not next_url:
            break
        next_url = _public_url(next_url)
        content.link_page(current, next_url)
        current = next_url
    if not first_id:
        raise RuntimeError("没有抓到正文")
    return {**_doc_result(first_id), "extraction": review}


def catalog(query: str = "", limit: int = 20) -> dict:
    if query.strip():
        items = [item.to_client(i + 1) for i, item in enumerate(
            content.search_docs(query.strip(), max(1, min(limit, 30)))
        )]
    else:
        items = content.shelf(max(1, min(limit, 50)))
    return {"items": items, "count": len(items)}


def series_map(query: str = "", work_id: str = "", limit: int = 20) -> dict:
    if work_id:
        work = content.get_work(work_id)
        if not work:
            raise KeyError("系列不存在")
        return {"series": work}
    return {"series": content.list_works(max(1, min(limit, 50)), q=query)}


def save_series(*, title: str, source_url: str, chapters: list[dict],
                description: str = "", complete: bool = False,
                work_id: str = "") -> dict:
    safe_source = _public_url(source_url) if source_url else ""
    safe_chapters = []
    for row in chapters or []:
        safe_chapters.append({**row, "url": _public_url(str(row.get("url") or ""))})
    return content.save_work(
        title=title, source_url=safe_source, description=description,
        chapters=safe_chapters, complete=complete, work_id=work_id,
    )


def read_series(series_id: str, *, start: int = 0, count: int = 8) -> dict:
    body, pages = content.series_text(series_id)
    if not body:
        raise KeyError("内容不存在")
    segments = split_segments(body)
    begin = max(0, int(start))
    end = min(len(segments), begin + max(1, min(int(count), 12)))
    head = pages[0]
    return {
        "series_id": series_id,
        "title": head.get("title") or head.get("url"),
        "url": head.get("url"),
        "total": len(segments),
        "start": begin,
        "segments": [{"idx": i, "source": segments[i],
                      "lines": len(_nonempty_lines(segments[i]))}
                     for i in range(begin, end)],
    }


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _translation_lines_match(source: str, target: str) -> bool:
    return len(_nonempty_lines(source)) == len(_nonempty_lines(target))


def save_translation(series_id: str, targets: list[dict], *, purpose: str = "完整中文翻译") -> dict:
    body, pages = content.series_text(series_id)
    if not body or not pages:
        raise KeyError("内容不存在")
    source = split_segments(body)
    previous = content.find_translation(series_id)
    translated = {}
    for seg in ((previous or {}).get("segments_list") or []):
        idx = int(seg.get("idx", -1))
        target = (seg.get("target") or "").strip()
        if 0 <= idx < len(source) and target and _translation_lines_match(source[idx], target):
            translated[idx] = target
    for row in targets or []:
        idx = int(row.get("idx", -1))
        target = (row.get("target") or "").strip()
        if not 0 <= idx < len(source) or not target:
            raise ValueError(f"无效译文段：{idx}")
        source_lines = len(_nonempty_lines(source[idx]))
        target_lines = len(_nonempty_lines(target))
        if source_lines != target_lines:
            raise ValueError(
                f"译文段 {idx} 换行未对齐：原文 {source_lines} 行，译文 {target_lines} 行；"
                "请逐行翻译，每个原文非空行对应一个译文非空行"
            )
        translated[idx] = target
    segments = [
        {"idx": idx, "total": len(source), "source": text,
         "target": translated.get(idx, ""), "ok": bool(translated.get(idx))}
        for idx, text in enumerate(source)
    ]
    text = "\n\n".join(seg["target"] for seg in segments if seg["target"])
    failed = sum(1 for seg in segments if not seg["ok"])
    head = pages[0]
    tid = content.save_translation(
        series_id=series_id,
        url=head.get("url") or "",
        title=head.get("title") or head.get("url") or "",
        purpose=purpose,
        text=text,
        segments=segments,
        covered=sum(len(seg["source"]) for seg in segments if seg["ok"]),
        failed=failed,
    )
    content.mark_used([page["id"] for page in pages])
    return {
        "id": tid,
        "series_id": series_id,
        "title": head.get("title") or head.get("url"),
        "url": head.get("url"),
        "segments": len(segments),
        "completed": len(segments) - failed,
        "failed": failed,
        "done": failed == 0,
    }
