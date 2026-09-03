"""跨机 Codex Worker 的持久任务、租约与事件账本。"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .content import connect

ACTIVE = ("queued", "claimed", "running")
TERMINAL = ("done", "error", "cancelled")


def _now() -> float:
    return time.time()


def _row(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("result", "detail", "context"):
        if key in out:
            try:
                out[key] = json.loads(out[key] or "{}")
            except json.JSONDecodeError:
                out[key] = {}
    return out


def enqueue(*, session_id: str, turn_id: str, idx: int, question: str,
            context: dict | None = None) -> dict:
    conn = connect()
    now = _now()
    jid = "j" + uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO agent_jobs(id,session_id,turn_id,idx,question,context,status,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,'queued',?,?)",
        (jid, session_id, turn_id, idx, question,
         json.dumps(context or {}, ensure_ascii=False), now, now),
    )
    conn.commit()
    return get(jid) or {}


def get(job_id: str) -> dict | None:
    return _row(connect().execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,)).fetchone())


def for_turn(session_id: str, turn_id: str) -> dict | None:
    return _row(connect().execute(
        "SELECT * FROM agent_jobs WHERE session_id=? AND turn_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id, turn_id),
    ).fetchone())


def pending_for_session(session_id: str) -> list[dict]:
    marks = ",".join("?" for _ in ACTIVE)
    rows = connect().execute(
        f"SELECT * FROM agent_jobs WHERE session_id=? AND status IN ({marks}) ORDER BY idx",
        (session_id, *ACTIVE),
    ).fetchall()
    return [_row(row) or {} for row in rows]


def session_running(session_id: str) -> bool:
    marks = ",".join("?" for _ in ACTIVE)
    row = connect().execute(
        f"SELECT 1 FROM agent_jobs WHERE session_id=? AND status IN ({marks}) LIMIT 1",
        (session_id, *ACTIVE),
    ).fetchone()
    return row is not None


def append_event(job_id: str, kind: str, data: dict | None = None, *, after_seq: int = 0) -> dict:
    conn = connect()
    job = conn.execute("SELECT session_id FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise KeyError(job_id)
    sid = job["session_id"]
    previous = conn.execute(
        "SELECT COALESCE(MAX(seq),0) FROM agent_job_events WHERE session_id=?", (sid,)
    ).fetchone()[0]
    seq = max(int(previous), int(after_seq)) + 1
    now = _now()
    payload = data or {}
    conn.execute(
        "INSERT INTO agent_job_events(session_id,seq,job_id,type,data,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (sid, seq, job_id, kind, json.dumps(payload, ensure_ascii=False), now),
    )
    conn.execute("UPDATE agent_jobs SET updated_at=? WHERE id=?", (now, job_id))
    conn.commit()
    return {"seq": seq, "type": kind, "data": payload, "ts": now, "job": job_id}


def events_since(session_id: str, since: int = 0, limit: int = 4000) -> list[dict]:
    rows = connect().execute(
        "SELECT seq,job_id,type,data,created_at FROM agent_job_events"
        " WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
        (session_id, max(0, since), limit),
    ).fetchall()
    out = []
    for row in rows:
        try:
            data = json.loads(row["data"] or "{}")
        except json.JSONDecodeError:
            data = {}
        out.append({"seq": row["seq"], "type": row["type"], "data": data,
                    "ts": row["created_at"], "job": row["job_id"]})
    return out


def latest_seq(session_id: str) -> int:
    row = connect().execute(
        "SELECT COALESCE(MAX(seq),0) FROM agent_job_events WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return int(row[0] or 0)


def resume_seq(session_id: str) -> int:
    """完成会话从末尾续；运行中会话只重放当前 Job，不重演以前的生命周期。"""
    row = connect().execute(
        "SELECT id FROM agent_jobs WHERE session_id=?"
        " AND status IN ('queued','claimed','running') ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return latest_seq(session_id)
    first = connect().execute(
        "SELECT MIN(seq) FROM agent_job_events WHERE session_id=? AND job_id=?",
        (session_id, row["id"]),
    ).fetchone()[0]
    return max(0, int(first or 1) - 1)


def touch_worker(worker_id: str, *, version: str = "", state: str = "idle",
                 job_id: str = "", detail: dict | None = None) -> None:
    now = _now()
    connect().execute(
        "INSERT INTO agent_workers(id,version,state,job_id,last_seen,detail) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET version=excluded.version,state=excluded.state,"
        " job_id=excluded.job_id,last_seen=excluded.last_seen,detail=excluded.detail",
        (worker_id, version, state, job_id, now,
         json.dumps(detail or {}, ensure_ascii=False)),
    )
    connect().commit()


def claim(worker_id: str, *, version: str = "", lease_seconds: int = 420) -> dict | None:
    conn = connect()
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM agent_jobs WHERE status='queued'"
        " OR (status IN ('claimed','running') AND lease_until<?)"
        " ORDER BY created_at LIMIT 1",
        (now,),
    ).fetchone()
    if row is None:
        conn.commit()
        touch_worker(worker_id, version=version, state="idle")
        return None
    conn.execute(
        "UPDATE agent_jobs SET status='claimed',worker_id=?,lease_until=?,attempts=attempts+1,"
        " updated_at=? WHERE id=?",
        (worker_id, now + lease_seconds, now, row["id"]),
    )
    conn.commit()
    touch_worker(worker_id, version=version, state="claimed", job_id=row["id"])
    return get(row["id"])


def heartbeat(job_id: str, worker_id: str, *, version: str = "",
              lease_seconds: int = 420, state: str = "running") -> bool:
    now = _now()
    conn = connect()
    changed = conn.execute(
        "UPDATE agent_jobs SET status='running',lease_until=?,updated_at=?"
        " WHERE id=? AND worker_id=? AND status IN ('claimed','running')",
        (now + lease_seconds, now, job_id, worker_id),
    ).rowcount
    conn.commit()
    touch_worker(worker_id, version=version, state=state, job_id=job_id)
    return bool(changed)


def finish(job_id: str, worker_id: str, result: dict) -> bool:
    now = _now()
    conn = connect()
    changed = conn.execute(
        "UPDATE agent_jobs SET status='done',result=?,error='',lease_until=0,"
        " updated_at=?,completed_at=? WHERE id=? AND worker_id=?",
        (json.dumps(result, ensure_ascii=False), now, now, job_id, worker_id),
    ).rowcount
    conn.commit()
    touch_worker(worker_id, state="idle")
    return bool(changed)


def fail(job_id: str, worker_id: str, error: str) -> bool:
    now = _now()
    conn = connect()
    changed = conn.execute(
        "UPDATE agent_jobs SET status='error',error=?,lease_until=0,updated_at=?,completed_at=?"
        " WHERE id=? AND worker_id=?",
        ((error or "Worker 失败")[:2000], now, now, job_id, worker_id),
    ).rowcount
    conn.commit()
    touch_worker(worker_id, state="idle")
    return bool(changed)


def retry(job_id: str) -> bool:
    now = _now()
    changed = connect().execute(
        "UPDATE agent_jobs SET status='queued',worker_id='',lease_until=0,error='',"
        " completed_at=0,updated_at=? WHERE id=? AND status='error'",
        (now, job_id),
    ).rowcount
    connect().commit()
    return bool(changed)


def worker_status(max_age: int = 90) -> dict:
    rows = connect().execute(
        "SELECT * FROM agent_workers ORDER BY last_seen DESC LIMIT 20"
    ).fetchall()
    live = [_row(row) or {} for row in rows
            if _now() - float(row["last_seen"] or 0) <= max_age]
    if not live:
        return {"online": False, "state": "never_seen"}
    out = dict(live[0])
    busy = [worker for worker in live if worker.get("state") in ("claimed", "running")]
    out["online"] = True
    out["state"] = "running" if busy else "idle"
    out["slots"] = len(live)
    out["busy"] = len(busy)
    out["workers"] = [{
        "id": worker.get("id") or "", "state": worker.get("state") or "",
        "job_id": worker.get("job_id") or "", "last_seen": worker.get("last_seen") or 0,
    } for worker in live]
    return out


def stats() -> dict:
    conn = connect()
    return {status: conn.execute(
        "SELECT COUNT(*) FROM agent_jobs WHERE status=?", (status,)
    ).fetchone()[0] for status in (*ACTIVE, *TERMINAL)}


def delete_session(session_id: str) -> None:
    conn = connect()
    conn.execute("DELETE FROM agent_job_events WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM agent_jobs WHERE session_id=?", (session_id,))
    conn.commit()


def restore_turn(session, job: dict):
    from .session import Turn

    for turn in session.turns:
        if turn.id == job["turn_id"]:
            return turn
    turn = Turn(id=job["turn_id"], idx=int(job["idx"]), question=job["question"])
    session.turns.append(turn)
    session.turns.sort(key=lambda item: item.idx)
    return turn
