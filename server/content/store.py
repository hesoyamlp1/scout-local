"""内容库的存储层：SQLite 连接、表结构、全文检索的查询构造。

**内容库 ID 是整个系统的枢纽。** 抓取干完的活就是"正文进库，交回一个 ID"，
此后所有事围绕 ID 转：主 agent 手里只有 ID 和摘要，要翻译就把 ID 交给处理整篇，
它自己按 ID 去库里取原文。**上下文里流转的永远只有 ID 和摘要，原文一个字都不进。**

两条从旧版本带过来的教训，都不能丢：

**连接每线程一条。** SQLite 的连接不是并发安全的（`check_same_thread=False`
只是关掉检查，不提供保证）。全进程共用一条会让内部状态错乱——旧版本实测
32 题回放里 41 轮撞 1 次，症状是某一轮直接没答案，报 "cannot commit"。
加锁救不了，因为共用它的模块不止一个；每线程一条才是标准做法，
WAL 本来就是为多连接并发设计的。

**全文检索的分词器必须是 trigram，而且查询串要自己再切一刀。**
默认的 unicode61 不切中文，一整句会被当成一个词；而 trigram 索引下，
把整句丢进去等于要求"这一整串一字不差地出现"，纯中文提问命中率趋近于零。
所以含汉字的段落按三字滑窗切开（正好是 trigram 的粒度），见 `fts_query`。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path

from .. import config

log = logging.getLogger("scout.content")

SCHEMA = """
-- 文档：读回来的网页正文。一页一条记录，同一篇的各页共用 series_id。
CREATE TABLE IF NOT EXISTS docs (
    id          TEXT PRIMARY KEY,       -- 网址的哈希，稳定不变
    series_id   TEXT NOT NULL,          -- 同一篇的各页共用（第一页的 id）
    page_no     INTEGER NOT NULL DEFAULT 1,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,          -- 正文全文，落盘不截
    chars       INTEGER NOT NULL DEFAULT 0,
    lang        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'article',   -- article / list
    -- 这一页后面还有没有下一页。'1' 有，'0' 明确没有，'' 不知道（老记录）。
    -- 「这一篇全不全」就是从各页的这个字段推出来的，见 series_status。
    has_next    TEXT NOT NULL DEFAULT '',
    via         TEXT NOT NULL DEFAULT '',          -- http / browser / pdf
    fetched_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    times_used  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_docs_series ON docs(series_id, page_no);
CREATE INDEX IF NOT EXISTS idx_docs_updated ON docs(updated_at DESC);

-- 结论：从对话里抽出来的、离开当次对话也成立的一句话。
CREATE TABLE IF NOT EXISTS facts (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    source_ids  TEXT NOT NULL DEFAULT '',   -- 从哪几篇文档得出的，逗号分隔
    session_id  TEXT NOT NULL DEFAULT '',
    turn_id     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    recalled    INTEGER NOT NULL DEFAULT 0,
    last_recall REAL NOT NULL DEFAULT 0
);

-- 关于用户本人。定时任务写，一段对话抽一次。
CREATE TABLE IF NOT EXISTS profile (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    confirmed   INTEGER NOT NULL DEFAULT 0,   -- 用户明说"记住…"的标 1
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- 对话历史：每一轮的问答。完整历史永远在这里，上下文里放的只是摘要。
CREATE TABLE IF NOT EXISTS dialogs (
    id          TEXT PRIMARY KEY,       -- session_id:turn_id
    session_id  TEXT NOT NULL,
    turn_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL DEFAULT 0,
    question    TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL DEFAULT '',
    used_ids    TEXT NOT NULL DEFAULT '',   -- 这一轮用过哪几条内容
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dialogs_session ON dialogs(session_id, idx);

-- 译文：翻好的一篇。它是资产，同一篇不重翻。
CREATE TABLE IF NOT EXISTS translations (
    id          TEXT PRIMARY KEY,
    series_id   TEXT NOT NULL,          -- 翻的是哪一篇
    url         TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    purpose     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,          -- 整篇译文
    segments    TEXT NOT NULL DEFAULT '[]',  -- JSON：原文段↔译文段的配对
    covered     INTEGER NOT NULL DEFAULT 0,  -- 这份译文覆盖了多少字原文
    failed      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    times_used  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tr_series ON translations(series_id);

-- Codex 理解出的语义作品/专栏。这里不从 URL 或标题猜任何东西；
-- title、章节范围、顺序和完成状态全部由 Agent 通过 typed tool 明确写入。
CREATE TABLE IF NOT EXISTS works (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    source_url  TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    complete    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS work_chapters (
    id          TEXT PRIMARY KEY,
    work_id     TEXT NOT NULL,
    position    INTEGER NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    doc_series_id TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE(work_id, url),
    UNIQUE(work_id, position)
);
CREATE INDEX IF NOT EXISTS idx_work_chapters_work
    ON work_chapters(work_id, position);
CREATE INDEX IF NOT EXISTS idx_work_chapters_doc
    ON work_chapters(doc_series_id);

-- 批注：用户在某一段上写的东西。段落是稳定的锚点（译文本来就按段存）。
CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    series_id   TEXT NOT NULL,
    seg_idx     INTEGER NOT NULL DEFAULT -1,   -- -1 表示整篇的批注
    text        TEXT NOT NULL,
    quote       TEXT NOT NULL DEFAULT '',      -- 批的是哪句话
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_series ON notes(series_id, seg_idx);

-- 会话和轮次。
-- **事件不落库。** 一轮流式输出上千条 delta，存下来只为了断线重连不值当；
-- 轮次本身是结构化存的（问答、材料、轨迹、译文），刷新页面直接从这里重建。
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    touched_at  REAL NOT NULL,
    -- 关于我那个定时任务处理到第几轮了。天然幂等：处理过的下次自动跳过，
    -- 用户回来又聊了五轮，下次只处理新增那五轮。
    profiled_to INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS idx_sessions_touched ON sessions(touched_at DESC);

-- 阅读器里的“陪你聊”是独立会话：按作品（或单篇）长期复用，不混进主对话列表。
CREATE TABLE IF NOT EXISTS reader_chats (
    scope_key   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reader_chats_session ON reader_chats(session_id);

CREATE TABLE IF NOT EXISTS turns (
    session_id  TEXT NOT NULL,
    id          TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    payload     TEXT NOT NULL,          -- JSON：问答、材料、轨迹、译文、指标
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, idx);

-- Codex Worker 任务。任务和事件落库，因此 Scout 或 Worker 任一端重启都能续上。
CREATE TABLE IF NOT EXISTS agent_jobs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    turn_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    question    TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',
    worker_id   TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    completed_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_queue
    ON agent_jobs(status, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_session
    ON agent_jobs(session_id, idx);

CREATE TABLE IF NOT EXISTS agent_job_events (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    job_id      TEXT NOT NULL,
    type        TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_job_events_job
    ON agent_job_events(job_id, seq);

CREATE TABLE IF NOT EXISTS agent_workers (
    id          TEXT PRIMARY KEY,
    version     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT '',
    job_id      TEXT NOT NULL DEFAULT '',
    last_seen   REAL NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);

-- 用户认可的长期来源与低频发现到的候选文章。
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    topic       TEXT NOT NULL DEFAULT '',
    entry_urls  TEXT NOT NULL DEFAULT '[]',
    interval_seconds INTEGER NOT NULL DEFAULT 86400,
    enabled     INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'new',
    last_checked REAL NOT NULL DEFAULT 0,
    next_check  REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_due ON sources(enabled, next_check);

CREATE TABLE IF NOT EXISTS source_candidates (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',
    discovered_at REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE(source_id, url)
);
CREATE INDEX IF NOT EXISTS idx_source_candidates_source
    ON source_candidates(source_id, status, discovered_at DESC);

-- 站点档案：这个域名抓不抓得动、要用什么方法。
-- 用户 2026-08-16 提的：记一笔，下次直接走对的路，不用再试错一遍。
CREATE TABLE IF NOT EXISTS hosts (
    host        TEXT PRIMARY KEY,
    method      TEXT NOT NULL DEFAULT '',   -- 成功过的方法：http / browser
    note        TEXT NOT NULL DEFAULT '',   -- 登录墙 / 挑战页 / 要浏览器
    ok          INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);
"""

# FTS 表用 external content（content=）省一份正文的存储，**同步一律交给触发器**。
#
# 手工同步是踩过的坑：对一个从未进过索引的 rowid 执行 `VALUES('delete', ...)`，
# 会把索引写坏，之后每一次读写都报 `database disk image is malformed`——
# 而报错的地方是抓取，看起来像网络问题，其实是这里。
# 触发器的 delete 用的是 `old.*`（真正在索引里的那份值），这才是 FTS5 要的。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, text, content='docs', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, text)
        VALUES('delete', old.rowid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, text)
        VALUES('delete', old.rowid, old.title, old.text);
    INSERT INTO docs_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text, subject, content='facts', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text, subject) VALUES (new.rowid, new.text, new.subject);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, subject)
        VALUES('delete', old.rowid, old.text, old.subject);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, subject)
        VALUES('delete', old.rowid, old.text, old.subject);
    INSERT INTO facts_fts(rowid, text, subject) VALUES (new.rowid, new.text, new.subject);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS dialogs_fts USING fts5(
    question, answer, content='dialogs', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS dialogs_ai AFTER INSERT ON dialogs BEGIN
    INSERT INTO dialogs_fts(rowid, question, answer)
        VALUES (new.rowid, new.question, new.answer);
END;
CREATE TRIGGER IF NOT EXISTS dialogs_ad AFTER DELETE ON dialogs BEGIN
    INSERT INTO dialogs_fts(dialogs_fts, rowid, question, answer)
        VALUES('delete', old.rowid, old.question, old.answer);
END;
CREATE TRIGGER IF NOT EXISTS dialogs_au AFTER UPDATE ON dialogs BEGIN
    INSERT INTO dialogs_fts(dialogs_fts, rowid, question, answer)
        VALUES('delete', old.rowid, old.question, old.answer);
    INSERT INTO dialogs_fts(rowid, question, answer)
        VALUES (new.rowid, new.question, new.answer);
END;
"""

_local = threading.local()
_all_conns: list[sqlite3.Connection] = []
_conns_lock = threading.RLock()
fts_ok = True


def connect() -> sqlite3.Connection:
    """本线程的连接。没有就建一条。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={config.STORAGE_BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS 不会给旧库补列；迁移保持小而显式。
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_jobs)")}
    if "context" not in job_columns:
        conn.execute("ALTER TABLE agent_jobs ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
    global fts_ok
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        # 这个 SQLite 没编 FTS5。检索退回 LIKE，功能降级但不崩。
        fts_ok = False
        log.warning("建 FTS5 表失败，全文检索退回 LIKE：%s", exc)
    conn.commit()
    _local.conn = conn
    with _conns_lock:
        _all_conns.append(conn)
    return conn


def close_all() -> None:
    with _conns_lock:
        conns, _all_conns[:] = list(_all_conns), []
    for c in conns:
        try:
            c.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------- ID

def normalize_url(url: str) -> str:
    """去重用的规范化：丢掉 fragment 和常见跟踪参数，末尾斜杠统一。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "ref", "fbclid", "gclid"}
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in drop]
    )
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urllib.parse.urlunsplit((parts.scheme, host, path, query, ""))


def doc_id_for(url: str) -> str:
    """网址 → 文档 ID。**同一个网址永远得到同一个 ID**，这是幂等入库的基础。"""
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def new_id(prefix: str, *parts: str) -> str:
    """给非网页的东西编一个稳定 ID。同样的输入给同样的 ID。"""
    raw = "\x00".join(parts) or str(time.time())
    return prefix + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def host_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url or "").netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- 全文检索

# 按空白和标点切词。连字符和下划线留在词里（async-std、io_uring 是一个词）。
_SPLIT = re.compile(r"[^\w一-鿿ぁ-ゟ゠-ヿ-]+")
_CJK = re.compile(r"[㐀-鿿豈-﫿ぁ-ゟ゠-ヿ]")
MIN_TERM = 3
MAX_TERMS = 24


def fts_terms(q: str) -> list[str]:
    """把一句话拆成 FTS5 用的词。

    两刀：先按空白和标点切开，再把含汉字/假名的那一段按三字滑窗切开。

    第一刀不能省：trigram 下双引号括起来的一串是"原样连续出现"的意思，
    整句丢进去等于要求六个词一字不差地挨着出现，那当然永远零结果。

    第二刀也不能省：中文句子里没有空白，第一刀对它等于没切，于是又回到
    "要求原样出现"。三字滑窗正好是 trigram 的粒度。日文假名同理。
    """
    terms: list[str] = []
    for tok in _SPLIT.split(q or ""):
        if not tok:
            continue
        if _CJK.search(tok):
            for i in range(max(1, len(tok) - MIN_TERM + 1)):
                piece = tok[i : i + MIN_TERM]
                if len(piece) >= 2:
                    terms.append(piece)
        elif len(tok) >= MIN_TERM:
            terms.append(tok)
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:MAX_TERMS]


def fts_query(q: str, mode: str = "or") -> str | None:
    """包成 FTS5 认的查询串。拆不出词就返回 None，调用方退回 LIKE。

    - `or`：召回和模型给的检索词。一次给好几个词，中几个就该拿出来，排序交给 bm25。
    - `and`：界面上的搜索框。人手打两个词是想缩小范围。
    """
    terms = fts_terms(q)
    if not terms:
        return None
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join('"' + t.replace('"', '""') + '"' for t in terms)


def preview(text: str, q: str = "", width: int = 200) -> str:
    """摘一段带关键词的正文。找不到关键词就取开头。"""
    body = re.sub(r"\s+", " ", (text or "")).strip()
    if not body:
        return ""
    if q:
        for term in fts_terms(q):
            i = body.lower().find(term.lower())
            if i >= 0:
                start = max(0, i - width // 3)
                out = body[start : start + width]
                return ("…" if start else "") + out + ("…" if start + width < len(body) else "")
    return body[:width] + ("…" if len(body) > width else "")


def rebuild_fts(conn: sqlite3.Connection, table: str) -> None:
    """**索引同步归触发器管，这里什么都不用做。**

    留着这个函数是因为调用点还在，而且"改完数据要不要手动同步索引"这个问题
    以后还会有人问——答案是不用，写坏索引的正是手工同步（见 FTS_SCHEMA 上面那段）。
    """
    return
