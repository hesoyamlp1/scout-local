"""内容库：存进来、要出去。

对 agent 层只暴露**动词**，不暴露内部状态。agent 说"给我这一篇的全文"，
这里负责它是完整的（该拼页拼页）；agent 说"帮我找 X"，这里负责交出的每一条
都带着**出处、时间、完整性**——让 agent 自己判断有没有用。

**一条红线：这一层不做意图判断。** 它不猜"用户是不是想要整篇"，
不按关键词决定走哪条路。它只负责把活干对：幂等入库、分页归组、去重、
如实报告一篇全不全。判断全部在 agent 层。

`Item` 是所有内容的统一形状。找一找交回来的是它，主 agent 上下文里看到的也是它。
它的 `line()` 就是模型眼里一条素材长什么样——出处和完整性写在脸上，
所以模型能自己丢掉不相干的东西，不需要程序替它打分筛选。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import config
from .store import (  # noqa: F401  对外转出
    connect,
    close_all,
    doc_id_for,
    fts_ok,
    fts_query,
    fts_terms,
    host_of,
    new_id,
    normalize_url,
    preview,
    rebuild_fts,
)

log = logging.getLogger("scout.content")


def _now() -> float:
    return time.time()


def _day(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _ago(ts: float) -> str:
    """人话的相对时间。模型看日期判断新旧，看"多久以前"更直接。"""
    if not ts:
        return ""
    d = max(0, _now() - ts)
    if d < 3600:
        return "刚刚"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d // 86400)} 天前"
    if d < 86400 * 365:
        return f"{int(d // (86400 * 30))} 个月前"
    return f"{int(d // (86400 * 365))} 年前"


# ---------------------------------------------------------------- 统一形状


@dataclass
class Item:
    """内容库里的一条东西，也是模型眼里的一条素材。"""

    id: str
    kind: str                      # doc / fact / dialog / translation
    title: str = ""
    url: str = ""
    summary: str = ""              # 给模型看的内容（要点 / 结论正文 / 问答摘要）
    when: str = ""                 # 什么时候的
    source: str = ""               # 出处：域名 / "第 4 轮对话"
    complete: str = ""             # 完整性，见 series_status
    chars: int = 0
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    def line(self, num: int | None = None) -> str:
        """渲染成上下文里的一行。**出处和完整性必须写在脸上。**

        旧版本这里只有标题和要点，于是三个月前记的一条书法笔记和刚搜到的官方文档
        长得一模一样，模型分不出来，只能被程序打分筛掉——而打分筛不准。
        写清楚它就自己丢了。
        """
        head = f"[{num}] " if num is not None else ""
        kind_label = {
            "doc": "网页", "fact": "结论", "dialog": "以前聊过", "translation": "译文",
            "note": "我的批注",
        }.get(self.kind, self.kind)
        bits = [b for b in (self.source, self.when, self.complete) if b]
        meta = " · ".join(bits)
        body = (self.summary or "").strip()
        return f"{head}{kind_label}｜{self.title}\n    {meta}\n    {body}" if meta else \
               f"{head}{kind_label}｜{self.title}\n    {body}"

    def to_client(self, num: int | None = None) -> dict:
        return {
            "num": num, "id": self.id, "kind": self.kind, "title": self.title,
            "url": self.url, "summary": self.summary, "when": self.when,
            "source": self.source, "complete": self.complete, "chars": self.chars,
            **self.extra,
        }


# ---------------------------------------------------------------- 文档：写


def save_doc(
    *,
    url: str,
    text: str,
    title: str = "",
    lang: str = "",
    kind: str = "article",
    via: str = "",
    has_next: bool | None = None,
    series_id: str = "",
    page_no: int = 0,
) -> str:
    """把一页正文存进库，返回它的 ID。**幂等**：同一个网址存多少次都是一条记录。

    正文更长的那一份留下来——同一页可能第一次只拿到搜索源顺带给的一小段，
    第二次才真的抓了全文。

    **落盘不截**（只有一道很宽的 DOC_MAX_CHARS 防爆盘）。库是资产，
    翻译和逐段处理要的就是全文；存的时候截掉，后面谁也变不出来。
    """
    url = (url or "").strip()
    body = (text or "").strip()
    if not url or not body:
        return ""
    body = body[: config.DOC_MAX_CHARS]
    did = doc_id_for(url)
    conn = connect()
    now = _now()
    row = conn.execute(
        "SELECT text, series_id, page_no FROM docs WHERE id=?", (did,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO docs(id, series_id, page_no, url, title, text, chars, lang,"
            " kind, has_next, via, fetched_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, series_id or did, page_no or 1, url, title or url, body, len(body),
             lang, kind, "" if has_next is None else ("1" if has_next else "0"),
             via, now, now),
        )
    else:
        # **没有明确传 series_id 就保留库里那个。**
        #
        # 这里踩过一次：原来写的是 `series_id or did`，也就是"没传就等于它自己"。
        # 于是分页归组会被悄悄冲掉——第 1 页读完时 `link_page` 已经把第 2 页登记成
        # "属于这一篇、第 2 页"，等第 2 页自己被抓、走到这里时，series_id 又被改回
        # 它自己，一篇两页的文章就变成两篇孤立的页。
        # 实测：kikikaikai 那篇 40074（747 字）和 40074/2（1172 字）各自成篇，
        # 于是"这一篇完整吗"永远答"完整"，翻译出来只有半篇。
        # **真抓回来的就是最新的，直接覆盖。**
        #
        # 这里原来是"留更长的那一份"，理由是"第一次可能只拿到搜索源顺带给的一小段"。
        # 那个理由现在不成立了：入库只有一条路——抓取 subagent 真抓之后存，
        # 搜索结果不再直接入库。留着这条规则反而会拒绝真实的更新
        # （页面改短了就永远停在旧版本），自检里抓到过。
        keep = body
        conn.execute(
            "UPDATE docs SET text=?, chars=?, title=COALESCE(NULLIF(?,''), title),"
            " series_id=?, page_no=?, kind=?, via=COALESCE(NULLIF(?,''), via),"
            " has_next=CASE WHEN ?='' THEN has_next ELSE ? END, updated_at=?"
            " WHERE id=?",
            (keep, len(keep), title,
             series_id or row["series_id"] or did,
             page_no or row["page_no"] or 1,
             kind, via,
             "" if has_next is None else "x",
             "" if has_next is None else ("1" if has_next else "0"), now, did),
        )
    conn.commit()   # 索引由触发器同步，这里不碰
    return did


def link_page(prev_url: str, next_url: str) -> None:
    """把 next_url 认成 prev_url 这一篇的下一页：继承 series_id，页码加一。

    **分页归组只在这里做一次**，四处入库共用同一个口径。
    """
    conn = connect()
    prev = conn.execute("SELECT series_id, page_no FROM docs WHERE id=?",
                        (doc_id_for(prev_url),)).fetchone()
    series = prev["series_id"] if prev else doc_id_for(prev_url)
    page_no = (prev["page_no"] if prev else 1) + 1
    nid = doc_id_for(next_url)
    row = conn.execute("SELECT id FROM docs WHERE id=?", (nid,)).fetchone()
    if row is None:
        # 下一页还没抓，先占个位：记住它属于哪一篇、是第几页。
        conn.execute(
            "INSERT INTO docs(id, series_id, page_no, url, title, text, chars,"
            " fetched_at, updated_at) VALUES(?,?,?,?,?,'',0,?,?)",
            (nid, series, page_no, next_url, next_url, _now(), _now()),
        )
    else:
        conn.execute("UPDATE docs SET series_id=?, page_no=? WHERE id=?",
                     (series, page_no, nid))
    conn.commit()


def mark_used(ids: list[str]) -> None:
    if not ids:
        return
    conn = connect()
    conn.executemany("UPDATE docs SET times_used=times_used+1 WHERE id=?",
                     [(i,) for i in ids])
    conn.commit()


# ---------------------------------------------------------------- 文档：读


def get_doc(doc_id: str) -> dict | None:
    row = connect().execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def get_doc_by_url(url: str) -> dict | None:
    return get_doc(doc_id_for(url))


def series_pages(series_id: str) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM docs WHERE series_id=? ORDER BY page_no", (series_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def series_status(doc_id_or_series: str) -> dict:
    """这一篇在库里全不全。**如实报告，不猜。**

    三种结果，处理整篇 subagent 就是看着它决定要不要去补：
      complete=True            读到末页了（最后一页明确说了没有下一页）
      complete=False, 有缺口   某一页说了还有下一页，而那一页正文是空的
      complete=False, unknown  老记录，当初读到多少存了多少，全不全无从判断
    """
    doc = get_doc(doc_id_or_series)
    series = (doc or {}).get("series_id") or doc_id_or_series
    pages = [p for p in series_pages(series) if (p["chars"] or 0) > 0]
    holes = [p for p in series_pages(series) if (p["chars"] or 0) == 0]
    if not pages:
        return {"known": False, "complete": False, "unknown_tail": True,
                "pages": 0, "chars": 0, "series": series, "missing": len(holes)}
    last = pages[-1]
    chars = sum(p["chars"] or 0 for p in pages)
    if holes:
        return {"known": True, "complete": False, "unknown_tail": False,
                "pages": len(pages), "chars": chars, "series": series,
                "missing": len(holes)}
    if last["has_next"] == "0":
        return {"known": True, "complete": True, "unknown_tail": False,
                "pages": len(pages), "chars": chars, "series": series, "missing": 0}
    if last["has_next"] == "1":
        return {"known": True, "complete": False, "unknown_tail": False,
                "pages": len(pages), "chars": chars, "series": series, "missing": 1}
    return {"known": True, "complete": False, "unknown_tail": True,
            "pages": len(pages), "chars": chars, "series": series, "missing": 0}


def complete_label(st: dict) -> str:
    """完整性写成一句人话，直接进模型的上下文。"""
    if not st.get("known"):
        return "库里还没有正文"
    n = st.get("pages") or 0
    if st.get("complete"):
        return f"完整（{n} 页 {st.get('chars')} 字）" if n > 1 else f"完整（{st.get('chars')} 字）"
    if st.get("missing"):
        return f"不完整：有 {n} 页，还缺 {st['missing']} 页没抓"
    if st.get("unknown_tail"):
        return f"{n} 页 {st.get('chars')} 字，不确定是不是全篇"
    return f"不完整：有 {n} 页，后面还有"


def series_text(series_id: str) -> tuple[str, list[dict]]:
    """把一篇的各页按页序拼成整篇。**处理整篇就是从这里取原文的。**"""
    pages = [p for p in series_pages(series_id) if (p["chars"] or 0) > 0]
    return "\n\n".join(p["text"] for p in pages), pages


def next_page_url(series_id: str) -> str:
    """这一篇里下一个还没抓的页面。没有就返回空。"""
    for p in series_pages(series_id):
        if (p["chars"] or 0) == 0:
            return p["url"]
    return ""


# ---------------------------------------------------------------- 检索


def _rows_to_items(rows, kind: str, q: str = "") -> list[Item]:
    out: list[Item] = []
    for r in rows:
        d = dict(r)
        if kind == "doc":
            st = series_status(d["id"])
            out.append(Item(
                id=d["id"], kind="doc", title=d["title"] or d["url"],
                url=d["url"], summary=preview(d["text"], q, config.EXTRACT_MAX_CHARS // 3),
                when=_ago(d["fetched_at"]), source=host_of(d["url"]),
                complete=complete_label(st), chars=d["chars"] or 0,
                score=float(d.get("rank") or 0),
                extra={"series_id": d["series_id"], "pages": st.get("pages")},
            ))
        elif kind == "fact":
            out.append(Item(
                id=d["id"], kind="fact", title=d["subject"] or "结论",
                url=f"scout://fact/{d['id']}", summary=d["text"],
                when=f"记于 {_day(d['created_at'])}", source="记忆",
                chars=len(d["text"] or ""), score=float(d.get("rank") or 0),
            ))
        elif kind == "dialog":
            out.append(Item(
                id=d["id"], kind="dialog", title=(d["question"] or "")[:40],
                url=f"scout://dialog/{d['id']}",
                summary=preview(d["answer"], q, 300),
                when=_ago(d["created_at"]), source=f"第 {d['idx'] + 1} 轮",
                chars=len(d["answer"] or ""), score=float(d.get("rank") or 0),
                extra={"session_id": d["session_id"]},
            ))
    return out


def _search_table(table: str, cols: str, q: str, limit: int, kind: str) -> list[Item]:
    conn = connect()
    expr = fts_query(q, "or") if fts_ok else None
    rows = []
    if expr:
        try:
            rows = conn.execute(
                f"SELECT t.*, bm25({table}_fts) AS rank FROM {table}_fts"
                f" JOIN {table} t ON t.rowid = {table}_fts.rowid"
                f" WHERE {table}_fts MATCH ? ORDER BY rank LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("%s 全文检索失败，退回 LIKE：%s", table, exc)
            rows = []
    if not rows:
        like = f"%{(q or '').strip()}%"
        where = " OR ".join(f"{c} LIKE ?" for c in cols.split(","))
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {where} LIMIT ?",
            (*([like] * len(cols.split(","))), limit),
        ).fetchall()
    return _rows_to_items(rows, kind, q)


def search_docs(q: str, limit: int = 6) -> list[Item]:
    # 正文为空的是分页占位记录，不该出现在检索结果里
    items = _search_table("docs", "title,text", q, limit * 2, "doc")
    return [i for i in items if i.chars > 0][:limit]


def search_facts(q: str, limit: int = 6) -> list[Item]:
    return _search_table("facts", "text,subject", q, limit, "fact")


def search_dialogs(q: str, limit: int = 4) -> list[Item]:
    return _search_table("dialogs", "question,answer", q, limit, "dialog")


def recent_dialogs(limit: int = 5, *, exclude_session: str = "") -> list[Item]:
    """最近聊过的几轮。**"上次我们聊到哪了"这类问法字面检索天然够不着**——
    问句和它要找的那轮对话可能一个字都不重叠，所以按时间取。"""
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM dialogs WHERE session_id != ? ORDER BY created_at DESC LIMIT ?",
        (exclude_session, limit),
    ).fetchall()
    return _rows_to_items(rows, "dialog")


def recent_docs(limit: int = 20) -> list[Item]:
    rows = connect().execute(
        "SELECT * FROM docs WHERE chars > 0 ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return _rows_to_items(rows, "doc")


def catalog(max_subjects: int = 24, max_titles: int = 16) -> str:
    """记忆的目录：记过哪些主题、读过哪些网页。

    **给模型看目录比让它反复换检索词有用**：字面检索是死板的，
    目录里认出"这个主题说的就是我要找的东西"，直接点名要过来。
    """
    conn = connect()
    subjects = [r[0] for r in conn.execute(
        "SELECT subject, COUNT(*) n FROM facts WHERE subject != ''"
        " GROUP BY subject ORDER BY MAX(updated_at) DESC LIMIT ?", (max_subjects,)
    ).fetchall()]
    titles = [r[0] for r in conn.execute(
        "SELECT title FROM docs WHERE chars > 0 ORDER BY updated_at DESC LIMIT ?",
        (max_titles,)
    ).fetchall()]
    parts = []
    if subjects:
        parts.append("记过这些主题：" + "、".join(subjects))
    if titles:
        parts.append("读过这些（最近的）：" + "；".join(t[:32] for t in titles))
    return "\n".join(parts)


# ---------------------------------------------------------------- Codex 语义作品 / 章节


def save_work(*, title: str, source_url: str = "", description: str = "",
              chapters: list[dict], complete: bool = False, work_id: str = "") -> dict:
    """忠实保存 Codex 给出的系列地图；不从标签、URL 或标题推断语义。"""
    title = (title or "").strip()
    if not title or not chapters:
        raise ValueError("系列标题和章节地图不能为空")
    wid = (work_id or "").strip() or new_id("w", source_url or title)
    normalized: list[dict] = []
    seen_positions: set[int] = set()
    seen_urls: set[str] = set()
    for raw in chapters:
        position = int(raw.get("position", len(normalized) + 1))
        url = normalize_url(str(raw.get("url") or "").strip())
        if position < 1 or not url or position in seen_positions or url in seen_urls:
            raise ValueError("章节 position/url 必须非空且在系列内唯一")
        seen_positions.add(position)
        seen_urls.add(url)
        doc = get_doc_by_url(url)
        doc_series = (doc or {}).get("series_id") if doc and (doc.get("chars") or 0) else ""
        normalized.append({
            "position": position,
            "label": str(raw.get("label") or "").strip(),
            "title": str(raw.get("title") or raw.get("label") or "").strip(),
            "url": url,
            "doc_series_id": doc_series or "",
        })
    normalized.sort(key=lambda row: row["position"])
    missing = [row["url"] for row in normalized if not row["doc_series_id"]]
    now = _now()
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO works(id,title,source_url,description,complete,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
        " title=excluded.title,source_url=excluded.source_url,description=excluded.description,"
        " complete=excluded.complete,updated_at=excluded.updated_at",
        (wid, title, source_url, description, 1 if complete and not missing else 0, now, now),
    )
    conn.execute("DELETE FROM work_chapters WHERE work_id=?", (wid,))
    for row in normalized:
        cid = new_id("c", wid, row["url"])
        conn.execute(
            "INSERT INTO work_chapters(id,work_id,position,label,title,url,doc_series_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, wid, row["position"], row["label"], row["title"], row["url"],
             row["doc_series_id"], now, now),
        )
    conn.commit()
    out = get_work(wid) or {}
    out["missing_urls"] = missing
    out["requested_complete"] = bool(complete)
    return out


def get_work(work_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    chapters = []
    for chapter in conn.execute(
        "SELECT * FROM work_chapters WHERE work_id=? ORDER BY position", (work_id,)
    ).fetchall():
        item = dict(chapter)
        doc_series = item.get("doc_series_id") or ""
        pages = series_pages(doc_series) if doc_series else []
        item["fetched"] = bool(pages and any((page.get("chars") or 0) > 0 for page in pages))
        item["chars"] = sum(page.get("chars") or 0 for page in pages)
        item["translated"] = bool(find_translation(doc_series)) if doc_series else False
        chapters.append(item)
    out["complete"] = bool(out.get("complete"))
    out["chapters"] = chapters
    out["chapter_count"] = len(chapters)
    out["fetched_count"] = sum(1 for chapter in chapters if chapter["fetched"])
    out["translated_count"] = sum(1 for chapter in chapters if chapter["translated"])
    return out


def list_works(limit: int = 100, q: str = "") -> list[dict]:
    rows = connect().execute(
        "SELECT id FROM works ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    works = [work for row in rows if (work := get_work(row["id"]))]
    if q:
        needle = q.lower()
        works = [work for work in works if needle in work["title"].lower()
                 or any(needle in (chapter.get("title") or "").lower()
                        for chapter in work["chapters"])]
    return works


def shelf(limit: int = 200, *, q: str = "") -> list[dict]:
    """书架：读过的东西**按篇聚合**，不是按页也不是按会话。

    这是新界面的主视图。用户回来找东西时找的是"那篇苏格兰怪谈"，
    不是"上周三那次对话"——所以列表按篇组织，带上读没读完、翻没翻过、有几条批注。
    """
    conn = connect()
    rows = conn.execute(
        "SELECT d.series_id AS series,"
        "       MIN(d.page_no) AS first_page,"
        "       COUNT(*) AS pages,"
        "       SUM(d.chars) AS chars,"
        "       MAX(d.times_used) AS times_used,"
        "       MAX(d.updated_at) AS updated_at,"
        "       MIN(d.fetched_at) AS fetched_at"
        "  FROM docs d WHERE d.chars > 0"
        " GROUP BY d.series_id ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    works = list_works(limit, q=q)
    linked_series = {
        chapter.get("doc_series_id") for work in list_works(limit)
        for chapter in work["chapters"] if chapter.get("doc_series_id")
    }
    out = [{
        "series": work["id"], "id": work["id"], "title": work["title"],
        "url": work.get("source_url") or "", "host": host_of(work.get("source_url") or ""),
        "kind": "series", "chapters": work["chapter_count"],
        "fetched_chapters": work["fetched_count"],
        "translated_chapters": work["translated_count"],
        "chars": sum(chapter.get("chars") or 0 for chapter in work["chapters"]),
        "complete": bool(work["complete"]),
        "status": (f"完整（{work['chapter_count']} 章）" if work["complete"]
                   else f"已抓 {work['fetched_count']}/{work['chapter_count']} 章"),
        "when": _ago(work.get("updated_at") or 0), "updated_at": work.get("updated_at") or 0,
        "translation": ({"chapters": work["translated_count"]}
                        if work["translated_count"] else None),
        "translation_complete": work["chapter_count"] > 0
                                and work["translated_count"] == work["chapter_count"],
        "notes": 0,
    } for work in works]
    for r in rows:
        if r["series"] in linked_series:
            continue
        head = conn.execute(
            "SELECT * FROM docs WHERE series_id=? AND chars>0 ORDER BY page_no LIMIT 1",
            (r["series"],),
        ).fetchone()
        if head is None:
            continue
        if q and q.lower() not in (head["title"] or "").lower() \
                and q.lower() not in (head["url"] or "").lower():
            continue
        st = series_status(head["id"])
        tr = conn.execute(
            "SELECT id, covered, LENGTH(text) AS chars FROM translations"
            " WHERE series_id=? ORDER BY updated_at DESC LIMIT 1", (r["series"],)
        ).fetchone()
        n_notes = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE series_id=?", (r["series"],)
        ).fetchone()[0]
        # 抓取缓存和用户书架是两层：只在搜索过程中被打开、但最终没有进入回答，
        # 也没被翻译或批注的页面不该污染用户看见的书架。
        if not r["times_used"] and tr is None and not n_notes:
            continue
        out.append({
            "series": r["series"],
            "id": head["id"],
            "title": head["title"] or head["url"],
            "url": head["url"],
            "host": host_of(head["url"]),
            "kind": head["kind"],
            "pages": r["pages"],
            "chars": r["chars"] or 0,
            "complete": bool(st.get("complete")),
            "status": complete_label(st),
            "when": _ago(r["updated_at"]),
            "updated_at": r["updated_at"],
            "translation": ({"id": tr["id"], "chars": tr["chars"]} if tr else None),
            "notes": n_notes,
        })
    out.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return out[:limit]


def read_doc(series_id: str) -> dict | None:
    """阅读界面要的一整篇：原文各页、译文段落配对、批注。"""
    pages = [p for p in series_pages(series_id) if (p["chars"] or 0) > 0]
    if not pages:
        return None
    head = pages[0]
    st = series_status(head["id"])
    tr = find_translation(series_id)
    from ..translate import split_segments

    source_body = "\n\n".join(p["text"] for p in pages if p.get("text"))
    source_segments = split_segments(source_body)
    return {
        "series": series_id,
        "title": head["title"] or head["url"],
        "url": head["url"],
        "host": host_of(head["url"]),
        "status": complete_label(st),
        "complete": bool(st.get("complete")),
        "pages": [{"page_no": p["page_no"], "url": p["url"], "chars": p["chars"],
                   "text": p["text"]} for p in pages],
        "source_segments": [
            {"idx": idx, "total": len(source_segments), "source": text, "target": ""}
            for idx, text in enumerate(source_segments)
        ],
        "chars": sum(p["chars"] or 0 for p in pages),
        "when": _ago(head["fetched_at"]),
        "translation": ({
            "id": tr["id"],
            "text": tr.get("text") or "",
            "segments": tr.get("segments_list") or [],
            "failed": tr.get("failed") or 0,
            "when": _ago(tr.get("updated_at") or 0),
        } if tr else None),
        "notes": notes_for(series_id),
    }


def read_work(work_id: str) -> dict | None:
    work = get_work(work_id)
    if not work:
        return None
    chapters = []
    for chapter in work["chapters"]:
        item = {key: chapter.get(key) for key in (
            "id", "position", "label", "title", "url", "doc_series_id",
            "fetched", "chars", "translated",
        )}
        item["document"] = read_doc(chapter.get("doc_series_id") or "")
        chapters.append(item)
    return {
        "series": work_id, "kind": "series", "title": work["title"],
        "url": work.get("source_url") or "", "description": work.get("description") or "",
        "complete": work["complete"], "status": (
            f"完整（{work['chapter_count']} 章）" if work["complete"]
            else f"已抓 {work['fetched_count']}/{work['chapter_count']} 章"
        ),
        "chapter_count": work["chapter_count"], "chapters": chapters,
    }


def stats() -> dict:
    conn = connect()
    def one(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0] or 0
        except sqlite3.Error:
            return 0
    return {
        "docs": one("SELECT COUNT(*) FROM docs WHERE chars>0"),
        "doc_chars": one("SELECT SUM(chars) FROM docs"),
        "series": one("SELECT COUNT(DISTINCT series_id) FROM docs WHERE chars>0"),
        "facts": one("SELECT COUNT(*) FROM facts"),
        "dialogs": one("SELECT COUNT(*) FROM dialogs"),
        "translations": one("SELECT COUNT(*) FROM translations"),
        "notes": one("SELECT COUNT(*) FROM notes"),
        "profile": one("SELECT COUNT(*) FROM profile"),
        "sources": one("SELECT COUNT(*) FROM sources WHERE enabled=1"),
        "source_candidates": one("SELECT COUNT(*) FROM source_candidates WHERE status='new'"),
    }


# ---------------------------------------------------------------- 结论


def save_fact(text: str, *, subject: str = "", source_ids: list[str] | None = None,
              session_id: str = "", turn_id: str = "", fact_id: str = "") -> str:
    """写一条结论。给了 fact_id 就是更新那一条（记结论 subagent 判断该更新时用）。"""
    text = (text or "").strip()
    if not text:
        return ""
    conn = connect()
    now = _now()
    fid = fact_id or new_id("f", text)
    src = ",".join(source_ids or [])
    row = conn.execute("SELECT id FROM facts WHERE id=?", (fid,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO facts(id, text, subject, source_ids, session_id, turn_id,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (fid, text, subject, src, session_id, turn_id, now, now),
        )
    else:
        conn.execute(
            "UPDATE facts SET text=?, subject=COALESCE(NULLIF(?,''), subject),"
            " source_ids=COALESCE(NULLIF(?,''), source_ids), updated_at=? WHERE id=?",
            (text, subject, src, now, fid),
        )
    conn.commit()
    rebuild_fts(conn, "facts")
    _prune_facts(conn)
    return fid


def delete_fact(fact_id: str) -> bool:
    conn = connect()
    n = conn.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount
    conn.commit()
    if n:
        rebuild_fts(conn, "facts")
    return bool(n)


def mark_recalled(ids: list[str]) -> None:
    if not ids:
        return
    conn = connect()
    conn.executemany(
        "UPDATE facts SET recalled=recalled+1, last_recall=? WHERE id=?",
        [(_now(), i) for i in ids],
    )
    conn.commit()


def _prune_facts(conn: sqlite3.Connection) -> None:
    """超上限就淘汰：没被召回过、又很久没更新的先走。"""
    n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    if n <= config.FACTS_MAX:
        return
    cutoff = _now() - config.FACTS_STALE_DAYS * 86400
    conn.execute(
        "DELETE FROM facts WHERE id IN ("
        " SELECT id FROM facts WHERE recalled=0 AND updated_at<?"
        " ORDER BY updated_at LIMIT ?)",
        (cutoff, n - config.FACTS_MAX),
    )
    conn.commit()
    rebuild_fts(conn, "facts")


# ---------------------------------------------------------------- 关于我


def profile_all() -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM profile ORDER BY confirmed DESC, updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def profile_block() -> str:
    """关于用户的那几条，拼成一段放进主 agent 的系统提示词。"""
    rows = profile_all()[: config.PROFILE_MAX]
    if not rows:
        return ""
    return "关于用户（以前记下的）：\n" + "\n".join(f"- {r['text']}" for r in rows)


def save_profile(text: str, *, confirmed: bool = False, pid: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return ""
    conn = connect()
    now = _now()
    key = pid or new_id("p", text)
    row = conn.execute("SELECT id FROM profile WHERE id=?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO profile(id, text, confirmed, created_at, updated_at)"
            " VALUES(?,?,?,?,?)", (key, text, int(confirmed), now, now))
    else:
        conn.execute("UPDATE profile SET text=?, confirmed=?, updated_at=? WHERE id=?",
                     (text, int(confirmed), now, key))
    conn.commit()
    return key


def delete_profile(pid: str) -> bool:
    conn = connect()
    n = conn.execute("DELETE FROM profile WHERE id=?", (pid,)).rowcount
    conn.commit()
    return bool(n)


# ---------------------------------------------------------------- 对话历史


def save_dialog(*, session_id: str, turn_id: str, idx: int, question: str,
                answer: str, used_ids: list[str] | None = None) -> str:
    if not (question or answer).strip():
        return ""
    conn = connect()
    did = f"{session_id}:{turn_id}"
    conn.execute(
        "INSERT OR REPLACE INTO dialogs(id, session_id, turn_id, idx, question,"
        " answer, used_ids, created_at) VALUES(?,?,?,?,?,?,?,"
        " COALESCE((SELECT created_at FROM dialogs WHERE id=?), ?))",
        (did, session_id, turn_id, idx, question, answer,
         ",".join(used_ids or []), did, _now()),
    )
    conn.commit()
    rebuild_fts(conn, "dialogs")
    return did


def forget_session(session_id: str) -> int:
    conn = connect()
    n = conn.execute("DELETE FROM dialogs WHERE session_id=?", (session_id,)).rowcount
    conn.commit()
    if n:
        rebuild_fts(conn, "dialogs")
    return n


def session_dialogs(session_id: str) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM dialogs WHERE session_id=? ORDER BY idx", (session_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 译文


def save_translation(*, series_id: str, text: str, segments: list[dict],
                     url: str = "", title: str = "", purpose: str = "",
                     covered: int = 0, failed: int = 0) -> str:
    conn = connect()
    now = _now()
    tid = new_id("t", series_id)
    row = conn.execute("SELECT id FROM translations WHERE id=?", (tid,)).fetchone()
    blob = json.dumps(segments, ensure_ascii=False)
    if row is None:
        conn.execute(
            "INSERT INTO translations(id, series_id, url, title, purpose, text,"
            " segments, covered, failed, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tid, series_id, url, title, purpose, text, blob, covered, failed, now, now),
        )
    else:
        conn.execute(
            "UPDATE translations SET text=?, segments=?, covered=?, failed=?,"
            " purpose=?, updated_at=? WHERE id=?",
            (text, blob, covered, failed, purpose, now, tid),
        )
    conn.commit()
    return tid


def find_translation(series_id: str) -> dict | None:
    row = connect().execute(
        "SELECT * FROM translations WHERE series_id=? ORDER BY updated_at DESC LIMIT 1",
        (series_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["segments_list"] = json.loads(d.get("segments") or "[]")
    except json.JSONDecodeError:
        d["segments_list"] = []
    return d


def translation_segment(series_id: str, seg_idx: int) -> dict | None:
    """取译文的某一段（原文段 + 译文段）。

    **这是"选择性进上下文"的机制。** 一篇上万字的译文默认不进任何 agent 的上下文，
    但用户就着某一段问问题时，那一段是必须进去的——按段取，只取那一段。
    """
    tr = find_translation(series_id)
    if not tr:
        return None
    segs = tr.get("segments_list") or []
    if not (0 <= seg_idx < len(segs)):
        return None
    seg = dict(segs[seg_idx])
    seg.update({"series": series_id, "title": tr.get("title") or "",
                "url": tr.get("url") or "", "total": len(segs)})
    return seg


def list_translations(limit: int = 50) -> list[dict]:
    rows = connect().execute(
        "SELECT id, series_id, url, title, covered, failed, created_at, updated_at,"
        " LENGTH(text) AS chars FROM translations ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def touch_translation(tid: str) -> None:
    conn = connect()
    conn.execute("UPDATE translations SET times_used=times_used+1 WHERE id=?", (tid,))
    conn.commit()


# ---------------------------------------------------------------- 批注


def add_note(*, series_id: str, text: str, seg_idx: int = -1, quote: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return ""
    conn = connect()
    nid = new_id("n", series_id, str(seg_idx), text, str(_now()))
    conn.execute(
        "INSERT OR REPLACE INTO notes(id, series_id, seg_idx, text, quote, created_at)"
        " VALUES(?,?,?,?,?,?)", (nid, series_id, seg_idx, text, quote, _now()))
    conn.commit()
    return nid


def notes_for(series_id: str) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM notes WHERE series_id=? ORDER BY seg_idx, created_at",
        (series_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def notes_block(series_id: str, seg_idx: int | None = None) -> str:
    """这一篇（或这一段）上的批注，拼成给模型看的一段。

    **批注是用户自己写的东西，比任何抽取物都值钱。** agent 取一篇材料、
    调出某一段的时候，用户在上面写过什么必须一起给它——不然
    "我上次在这篇上批了什么"根本问不出来。
    """
    rows = notes_for(series_id)
    if seg_idx is not None:
        rows = [r for r in rows if r["seg_idx"] == seg_idx]
    if not rows:
        return ""
    lines = []
    for r in rows:
        where = f"第 {r['seg_idx'] + 1} 段" if r["seg_idx"] >= 0 else "整篇"
        lines.append(f"  · {where}（{_ago(r['created_at'])}）：{r['text']}")
    return "用户在这上面写过的批注：\n" + "\n".join(lines)


def search_notes(q: str, limit: int = 6) -> list[Item]:
    """在批注里找。批注量小，用 LIKE 就够，不必单独建索引。"""
    terms = [t for t in fts_terms(q) if len(t) >= 2][:6]
    if not terms:
        return []
    conn = connect()
    where = " OR ".join(["n.text LIKE ?"] * len(terms))
    rows = conn.execute(
        f"SELECT n.*, d.title AS doc_title, d.url AS doc_url FROM notes n"
        f" LEFT JOIN docs d ON d.id = n.series_id"
        f" WHERE {where} ORDER BY n.created_at DESC LIMIT ?",
        (*[f"%{t}%" for t in terms], limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        where_txt = f"第 {d['seg_idx'] + 1} 段" if d["seg_idx"] >= 0 else "整篇"
        out.append(Item(
            id=d["series_id"], kind="note",
            title=d.get("doc_title") or "（这一篇）",
            url=d.get("doc_url") or "",
            summary=f"{d['text']}" + (f"\n（批的是：{d['quote'][:80]}）" if d["quote"] else ""),
            when=_ago(d["created_at"]), source=f"我的批注 · {where_txt}",
            extra={"series_id": d["series_id"], "seg_idx": d["seg_idx"]},
        ))
    return out


def delete_note(note_id: str) -> bool:
    conn = connect()
    n = conn.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount
    conn.commit()
    return bool(n)


# ---------------------------------------------------------------- 站点档案


def host_note(url: str) -> str:
    """这个站以前抓过吗、用什么方法成的。给抓取 subagent 当先验，省一轮试错。"""
    host = host_of(url)
    if not host:
        return ""
    row = connect().execute("SELECT * FROM hosts WHERE host=?", (host,)).fetchone()
    if row is None:
        return ""
    bits = []
    if row["method"]:
        bits.append(f"以前用「{row['method']}」抓成过")
    if row["note"]:
        bits.append(row["note"])
    if row["failed"] and not row["ok"]:
        bits.append(f"失败过 {row['failed']} 次")
    return "；".join(bits)


def remember_host(url: str, *, method: str = "", note: str = "", ok: bool = True) -> None:
    host = host_of(url)
    if not host:
        return
    conn = connect()
    conn.execute(
        "INSERT INTO hosts(host, method, note, ok, failed, updated_at)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(host) DO UPDATE SET"
        "   method=COALESCE(NULLIF(excluded.method,''), hosts.method),"
        "   note=COALESCE(NULLIF(excluded.note,''), hosts.note),"
        "   ok=hosts.ok+excluded.ok, failed=hosts.failed+excluded.failed,"
        "   updated_at=excluded.updated_at",
        (host, method, note, int(ok), int(not ok), _now()),
    )
    conn.commit()
