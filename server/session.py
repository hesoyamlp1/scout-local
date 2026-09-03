"""会话和轮次。

**事件只走内存，不落库。** 一轮流式输出上千条 `answer_delta`，为了断线重连
把它们全存下来不值当。轮次本身是结构化存的（问答、材料、轨迹、译文、账），
刷新页面直接从库里重建；只有"这一轮正在跑"的那几秒才需要实时事件，
那段时间的事件放在内存里，够用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from . import config
from .content import connect

log = logging.getLogger("scout.session")


def _now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


@dataclass
class Turn:
    """一轮问答。**这是持久化的单位。**"""

    id: str
    idx: int
    question: str = ""
    answer: str = ""
    items: list = field(default_factory=list)      # 用到的材料（to_client 之后的 dict）
    trace: list = field(default_factory=list)      # 轨迹（Step.to_client）
    translation: dict = field(default_factory=dict)
    translations: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    created_at: float = field(default_factory=_now)
    done: bool = False

    def to_client(self) -> dict:
        return {
            "id": self.id, "idx": self.idx, "question": self.question,
            "answer": self.answer, "items": self.items, "trace": self.trace,
            "translation": self.translation, "translations": self.translations,
            "metrics": self.metrics,
            "created_at": self.created_at, "done": self.done,
        }


class Session:
    def __init__(self, sid: str, created_at: float | None = None) -> None:
        self.id = sid
        self.created_at = created_at or _now()
        self.touched_at = self.created_at
        self.turns: list[Turn] = []
        self.title = ""
        # 实时事件：内存里保留这一轮的，供断线重连补齐。
        self.events: list[dict] = []
        self.seq = 0
        self._subs: set[asyncio.Queue] = set()
        self.running = False

    # ---------------------------------------------------------- 事件

    async def emit(self, kind: str, data: dict) -> None:
        self.seq += 1
        ev = {"seq": self.seq, "type": kind, "data": data, "ts": _now()}
        self.events.append(ev)
        # 只留最近这些：正在跑的一轮撑死几百条，旧的没人要了
        if len(self.events) > 4000:
            del self.events[:2000]
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("会话 %s 有个订阅者跟不上，丢事件", self.id)

    def subscribe(self, since: int = 0) -> tuple[asyncio.Queue, list[dict]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        backlog = [e for e in self.events if e["seq"] > since]
        return q, backlog

    def publish(self, event: dict) -> None:
        """发布已经由持久任务账本分配过 seq 的事件。"""
        self.seq = max(self.seq, int(event.get("seq") or 0))
        self.events.append(event)
        if len(self.events) > 4000:
            del self.events[:2000]
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("会话 %s 有个订阅者跟不上，丢事件", self.id)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    # ---------------------------------------------------------- 轮次

    def start_turn(self, question: str) -> Turn:
        turn = Turn(id=new_id("t"), idx=len(self.turns), question=question)
        self.turns.append(turn)
        self.touched_at = _now()
        if not self.title:
            self.title = question[:60]
        self.events.clear()   # 新一轮开始，上一轮的实时事件不用留了
        return turn

    def finish_turn(self, turn: Turn) -> None:
        turn.done = True
        self.touched_at = _now()
        save_turn(self, turn)

    def history_messages(self) -> list[dict]:
        """给主 agent 的历史。

        **最近几轮完整放，再往前只留「结论 + 用过的编号」。**
        压掉的正文一直在库里，要用的时候按 ID 直接取——
        所以压缩是无损的，这正是"直接取内容库"那条路真正的价值。
        """
        done = [t for t in self.turns if t.done]
        out: list[dict] = []
        full_from = max(0, len(done) - config.HISTORY_FULL_TURNS)
        for i, t in enumerate(done[-config.HISTORY_COMPACT_TURNS:], start=max(0, len(done) - config.HISTORY_COMPACT_TURNS)):
            out.append({"role": "user", "content": t.question})
            if i >= full_from:
                out.append({"role": "assistant",
                            "content": t.answer[: config.HISTORY_ANSWER_MAX_CHARS]})
            else:
                ids = "、".join(f"[{it.get('num')}]" for it in t.items if it.get("num"))
                tail = f"\n（这一轮用过的材料：{ids}，需要时可以直接取）" if ids else ""
                out.append({"role": "assistant",
                            "content": t.answer[:400] + tail})
        return out

    def conversation_text(self) -> str:
        return "\n\n".join(
            f"用户：{t.question}\n助手：{t.answer[:1500]}" for t in self.turns if t.done
        )


# ---------------------------------------------------------------- 持久化

_live: dict[str, Session] = {}


def get(sid: str, *, create: bool = False) -> Session | None:
    s = _live.get(sid)
    if s is not None:
        return s
    conn = connect()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        if not create:
            return None
        s = Session(sid)
        conn.execute(
            "INSERT OR REPLACE INTO sessions(id, title, created_at, touched_at)"
            " VALUES(?,?,?,?)", (sid, "", s.created_at, s.touched_at))
        conn.commit()
        _live[sid] = s
        return s
    s = Session(sid, row["created_at"])
    s.touched_at = row["touched_at"]
    s.title = row["title"] or ""
    for r in conn.execute(
        "SELECT * FROM turns WHERE session_id=? ORDER BY idx", (sid,)
    ).fetchall():
        try:
            p = json.loads(r["payload"])
        except json.JSONDecodeError:
            continue
        legacy_translation = p.get("translation", {})
        translations = p.get("translations") or ([legacy_translation] if legacy_translation else [])
        s.turns.append(Turn(
            id=r["id"], idx=r["idx"], question=p.get("question", ""),
            answer=p.get("answer", ""), items=p.get("items", []),
            trace=p.get("trace", []), translation=p.get("translation", {}),
            translations=translations,
            metrics=p.get("metrics", {}), created_at=r["created_at"], done=True,
        ))
    _live[sid] = s
    return s


def create() -> Session:
    return get(new_id("s"), create=True)  # type: ignore[return-value]


def reader_chat(scope_key: str, title: str = "") -> Session:
    """取作品专属阅读会话；首次打开时创建，之后一直复用。"""
    conn = connect()
    row = conn.execute(
        "SELECT session_id FROM reader_chats WHERE scope_key=?", (scope_key,)
    ).fetchone()
    if row:
        session = get(row["session_id"], create=True)
        assert session is not None
        return session
    session = create()
    now = _now()
    session.title = title or "陪你读"
    conn.execute(
        "INSERT INTO reader_chats(scope_key,session_id,title,created_at,updated_at)"
        " VALUES(?,?,?,?,?)", (scope_key, session.id, session.title, now, now),
    )
    conn.commit()
    return session


def is_reader_chat(sid: str) -> bool:
    return connect().execute(
        "SELECT 1 FROM reader_chats WHERE session_id=?", (sid,)
    ).fetchone() is not None


def save_turn(s: Session, turn: Turn) -> None:
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO turns(session_id, id, idx, payload, created_at)"
        " VALUES(?,?,?,?,?)",
        (s.id, turn.id, turn.idx,
         json.dumps(turn.to_client(), ensure_ascii=False), turn.created_at),
    )
    conn.execute(
        "INSERT INTO sessions(id, title, created_at, touched_at) VALUES(?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET touched_at=excluded.touched_at,"
        " title=COALESCE(NULLIF(sessions.title,''), excluded.title)",
        (s.id, s.title, s.created_at, s.touched_at),
    )
    conn.commit()


def listing(limit: int = 60) -> list[dict]:
    rows = connect().execute(
        "SELECT s.id, s.title, s.created_at, s.touched_at,"
        " (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id) n,"
        " (SELECT COUNT(*) FROM agent_jobs j WHERE j.session_id=s.id"
        "   AND j.status IN ('queued','claimed','running')) active"
        " FROM sessions s WHERE NOT EXISTS"
        " (SELECT 1 FROM reader_chats r WHERE r.session_id=s.id)"
        " ORDER BY s.touched_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"id": r["id"], "title": r["title"] or "（没有标题）",
         "created_at": r["created_at"], "updated_at": r["touched_at"],
         "turns": r["n"], "running": bool(r["active"])}
        for r in rows if r["n"] or r["active"]
    ]


def delete(sid: str) -> bool:
    from . import content

    conn = connect()
    exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
    conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM reader_chats WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    _live.pop(sid, None)
    from . import jobs
    jobs.delete_session(sid)
    # 手动删会话 = 这次对话我不要了，聊过的内容也一起清掉。
    content.forget_session(sid)
    return bool(exists)


def idle_sessions(idle_seconds: float) -> list[dict]:
    """闲下来了、而且还有没处理过的轮次的会话。给"记关于我"那个定时任务用。"""
    cutoff = _now() - idle_seconds
    rows = connect().execute(
        "SELECT s.id, s.profiled_to,"
        " (SELECT MAX(idx) FROM turns t WHERE t.session_id=s.id) last_idx"
        " FROM sessions s WHERE s.touched_at < ? AND NOT EXISTS"
        " (SELECT 1 FROM reader_chats r WHERE r.session_id=s.id)", (cutoff,)
    ).fetchall()
    return [
        {"id": r["id"], "profiled_to": r["profiled_to"], "last_idx": r["last_idx"]}
        for r in rows
        if r["last_idx"] is not None and r["last_idx"] > r["profiled_to"]
    ]


def mark_profiled(sid: str, upto: int) -> None:
    conn = connect()
    conn.execute("UPDATE sessions SET profiled_to=? WHERE id=?", (upto, sid))
    conn.commit()


def sweep(ttl: int) -> int:
    conn = connect()
    cutoff = _now() - ttl
    dead = [r[0] for r in conn.execute(
        "SELECT id FROM sessions s WHERE touched_at<? AND NOT EXISTS"
        " (SELECT 1 FROM reader_chats r WHERE r.session_id=s.id)", (cutoff,)).fetchall()]
    for sid in dead:
        conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        _live.pop(sid, None)
    conn.commit()
    # **回收不等于失忆**：聊过的内容留在 dialogs 里，还能被找一找翻出来。
    # 手动删会话才连内容一起清。
    return len(dead)
