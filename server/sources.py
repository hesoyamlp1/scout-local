"""用户维护的来源地图，以及低频、单页、增量的礼貌巡游。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.parse

from bs4 import BeautifulSoup

from . import net
from .content import connect, get_doc_by_url, normalize_url
from .worker_tools import _public_url

_DROP_PATH = ("/tag", "/tags", "/category", "/author", "/login", "/search",
              "/privacy", "/terms", "/contact", "/ranking")


def _now() -> float:
    return time.time()


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:15]


def _decode(value: str, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def follow(url: str, *, name: str = "", topic: str = "",
           entry_urls: list[str] | None = None, interval_seconds: int = 86400) -> dict:
    canonical = normalize_url(_public_url(url))
    entries = []
    for entry in entry_urls or [canonical]:
        safe = normalize_url(_public_url(entry))
        if safe not in entries:
            entries.append(safe)
    now = _now()
    sid = _id("src", canonical)
    connect().execute(
        "INSERT INTO sources(id,url,name,topic,entry_urls,interval_seconds,enabled,status,"
        " next_check,created_at,updated_at) VALUES(?,?,?,?,?,?,1,'new',0,?,?)"
        " ON CONFLICT(id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''),sources.name),"
        " topic=COALESCE(NULLIF(excluded.topic,''),sources.topic),entry_urls=excluded.entry_urls,"
        " interval_seconds=excluded.interval_seconds,enabled=1,updated_at=excluded.updated_at",
        (sid, canonical, name.strip(), topic.strip(), json.dumps(entries, ensure_ascii=False),
         max(3600, min(int(interval_seconds), 30 * 86400)), now, now),
    )
    connect().commit()
    return get(sid) or {}


def get(source_id: str) -> dict | None:
    row = connect().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["entry_urls"] = _decode(out.get("entry_urls"), [out["url"]])
    out["enabled"] = bool(out.get("enabled"))
    out["candidate_count"] = connect().execute(
        "SELECT COUNT(*) FROM source_candidates WHERE source_id=? AND status='new'", (source_id,)
    ).fetchone()[0]
    return out


def listing(limit: int = 100) -> list[dict]:
    rows = connect().execute(
        "SELECT id FROM sources ORDER BY enabled DESC,updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [item for row in rows if (item := get(row["id"]))]


def candidates(source_id: str = "", limit: int = 100) -> list[dict]:
    if source_id:
        rows = connect().execute(
            "SELECT * FROM source_candidates WHERE source_id=? ORDER BY discovered_at DESC LIMIT ?",
            (source_id, limit),
        ).fetchall()
    else:
        rows = connect().execute(
            "SELECT * FROM source_candidates ORDER BY discovered_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def set_enabled(source_id: str, enabled: bool) -> bool:
    changed = connect().execute(
        "UPDATE sources SET enabled=?,updated_at=? WHERE id=?",
        (1 if enabled else 0, _now(), source_id),
    ).rowcount
    connect().commit()
    return bool(changed)


def delete(source_id: str) -> bool:
    conn = connect()
    conn.execute("DELETE FROM source_candidates WHERE source_id=?", (source_id,))
    changed = conn.execute("DELETE FROM sources WHERE id=?", (source_id,)).rowcount
    conn.commit()
    return bool(changed)


def _article_links(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    base_host = urllib.parse.urlsplit(base_url).hostname
    numeric: list[tuple[str, str]] = []
    semantic: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin(base_url, anchor.get("href") or "")
        parts = urllib.parse.urlsplit(href)
        if parts.scheme not in ("http", "https") or parts.hostname != base_host:
            continue
        path = parts.path.rstrip("/") or "/"
        lower = path.lower()
        if path == "/" or any(lower.startswith(prefix) for prefix in _DROP_PATH):
            continue
        # 文章 URL 通常有数字 ID 或两层以上的语义路径；浅层导航不收。
        has_numeric_id = any(piece.isdigit() for piece in path.split("/"))
        if not has_numeric_id and path.count("/") < 2:
            continue
        url = normalize_url(urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, "")))
        if url in seen:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if len(title) < 2:
            continue
        seen.add(url)
        (numeric if has_numeric_id else semantic).append((url, title[:180]))
        if len(numeric) + len(semantic) >= 300:
            break
    # 很多内容站文章使用根级数字 ID，而页面同时挂着大量分类/奖项归档。
    # 有足够数字 ID 时，它们是更强的文章实例信号，优先丢掉归档噪声。
    return (numeric if len(numeric) >= 3 else numeric + semantic)[:100]


async def refresh(source_id: str) -> dict:
    source = get(source_id)
    if not source:
        raise KeyError("来源不存在")
    now = _now()
    discovered = 0
    error = ""
    try:
        for entry in source["entry_urls"][:5]:
            raw = await net.http_get(_public_url(entry))
            if not raw.html:
                raise RuntimeError(raw.error or "入口页没有 HTML")
            for url, title in _article_links(raw.html, raw.url):
                if get_doc_by_url(url):
                    continue
                cid = _id("cand", source_id + "\n" + url)
                before = connect().execute(
                    "SELECT 1 FROM source_candidates WHERE id=?", (cid,)
                ).fetchone()
                connect().execute(
                    "INSERT INTO source_candidates(id,source_id,url,title,status,discovered_at,updated_at)"
                    " VALUES(?,?,?,?,'new',?,?) ON CONFLICT(id) DO UPDATE SET"
                    " title=excluded.title,updated_at=excluded.updated_at",
                    (cid, source_id, url, title, now, now),
                )
                if before is None:
                    discovered += 1
        status = "ok"
    except Exception as exc:  # noqa: BLE001
        status, error = "error", f"{type(exc).__name__}: {exc}"[:500]
    interval = int(source["interval_seconds"])
    # 失败至少休息 6 小时；成功按来源自己的检查周期。
    next_check = now + (max(21600, interval) if error else interval)
    connect().execute(
        "UPDATE sources SET status=?,last_checked=?,next_check=?,error=?,updated_at=? WHERE id=?",
        (status, now, next_check, error, now, source_id),
    )
    connect().commit()
    out = get(source_id) or {}
    out.update({"discovered": discovered, "error": error})
    return out


async def sweep(interval: int = 300) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            row = connect().execute(
                "SELECT id FROM sources WHERE enabled=1 AND next_check<=?"
                " ORDER BY next_check LIMIT 1", (_now(),)
            ).fetchone()
            if row:
                await refresh(row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)
