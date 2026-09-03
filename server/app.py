"""HTTP 接口。

**后端是纯 API，不掺任何界面逻辑。** 这个文件里不该出现任何"页面长什么样"
的东西——它只发数据。

认证：浏览器使用专门登录页和签名 HttpOnly Cookie；`SCOUT_AUTH_TOKEN` 继续留给
命令行/API 客户端。API、SSE、正文页面和静态资源走同一道认证门。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (auth, config, content, jobs, llm, net, pipeline,
               session as sessions, settings, sources, worker_tools)
from .search import active_providers, close_all as close_search

log = logging.getLogger("scout.app")

WEB = Path(config.ROOT) / "web"
_tasks: list[asyncio.Task] = []
_login_failures: dict[str, list[float]] = {}
_job_signal = asyncio.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configured = (config.AUTH_USERNAME, config.AUTH_PASSWORD_HASH, config.AUTH_SECRET)
    if any(configured) and not all(configured):
        raise RuntimeError("登录配置不完整：用户名、密码哈希和签名密钥必须一起设置")
    content.connect()          # 建表
    n = settings.apply_saved()
    if n:
        llm.reconfigure()
        log.info("应用了 %d 项存档设置", n)
    _tasks.append(asyncio.create_task(pipeline.profile_sweep()))
    _tasks.append(asyncio.create_task(pipeline.session_sweep()))
    _tasks.append(asyncio.create_task(sources.sweep()))
    log.info("scout 起来了：pro=%s flash=%s，搜索源 %s",
             config.MODEL_PRO, config.MODEL_FLASH,
             [p.name for p in active_providers()])
    yield
    for t in _tasks:
        t.cancel()
    await llm.close_all()
    await net.aclose()
    await close_search()
    content.close_all()


app = FastAPI(title="scout", lifespan=lifespan)


def _login_enabled() -> bool:
    return bool(config.AUTH_USERNAME and config.AUTH_PASSWORD_HASH and config.AUTH_SECRET)


def _authorized(request: Request, token: str = "") -> bool:
    bearer = token or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if config.AUTH_TOKEN and hmac.compare_digest(bearer, config.AUTH_TOKEN):
        return True
    if not _login_enabled():
        return not config.AUTH_TOKEN
    cookie = request.cookies.get(config.AUTH_COOKIE_NAME, "")
    return auth.verify_session(cookie, config.AUTH_SECRET, config.AUTH_USERNAME)


def _check(request: Request, token: str = "") -> None:
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="认证不通过")


def _worker_check(request: Request) -> None:
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not config.WORKER_TOKEN or not hmac.compare_digest(got, config.WORKER_TOKEN):
        raise HTTPException(status_code=401, detail="Worker 认证不通过")


def _tool_check(request: Request) -> None:
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not config.MCP_TOKEN or not hmac.compare_digest(got, config.MCP_TOKEN):
        raise HTTPException(status_code=401, detail="MCP 工具认证不通过")


@app.middleware("http")
async def login_gate(request: Request, call_next):
    """登录开启后，静态页面与 API 一起保护；登录页和健康检查保持公开。"""
    if not _login_enabled() or request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    worker_path = path.startswith("/api/worker/") or path == "/api/worker/claim"
    tool_path = path.startswith("/api/worker-tools/")
    if worker_path and config.WORKER_TOKEN and hmac.compare_digest(bearer, config.WORKER_TOKEN):
        return await call_next(request)
    if tool_path and config.MCP_TOKEN and hmac.compare_digest(bearer, config.MCP_TOKEN):
        return await call_next(request)
    public = path == "/login" or path in {"/api/login", "/api/auth", "/api/health"}
    if public:
        if path == "/login" and _authorized(request):
            return RedirectResponse("/", status_code=303)
        return await call_next(request)
    if _authorized(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


# ---------------------------------------------------------------- 登录


class LoginIn(BaseModel):
    username: str
    password: str


def _client_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    if host in {"127.0.0.1", "::1"}:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        if forwarded:
            host = forwarded
    return host


@app.get("/login")
async def login_page():
    return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/api/auth")
async def auth_status(request: Request):
    return {"authenticated": _authorized(request), "username": config.AUTH_USERNAME}


@app.post("/api/login")
async def login(body: LoginIn, request: Request):
    if not _login_enabled():
        raise HTTPException(status_code=404, detail="登录功能未配置")
    key = _client_key(request)
    now = time.monotonic()
    recent = [stamp for stamp in _login_failures.get(key, []) if now - stamp < 300]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    user_ok = hmac.compare_digest(body.username, config.AUTH_USERNAME)
    password_ok = auth.verify_password(body.password, config.AUTH_PASSWORD_HASH)
    if not (user_ok and password_ok):
        recent.append(now)
        _login_failures[key] = recent
        await asyncio.sleep(0.45)
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    _login_failures.pop(key, None)
    session = auth.issue_session(
        config.AUTH_USERNAME, config.AUTH_SECRET, max_age=config.AUTH_SESSION_SECONDS
    )
    response = JSONResponse({"ok": True, "username": config.AUTH_USERNAME})
    response.set_cookie(
        config.AUTH_COOKIE_NAME,
        session,
        max_age=config.AUTH_SESSION_SECONDS,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/logout")
async def logout(request: Request):
    _check(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(config.AUTH_COOKIE_NAME, path="/", secure=config.AUTH_COOKIE_SECURE, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- 问答


class AskIn(BaseModel):
    question: str
    session: str | None = None


@app.post("/api/ask")
async def ask(body: AskIn, request: Request):
    _check(request)
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="问题不能为空")
    s = sessions.get(body.session, create=True) if body.session else sessions.create()
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if s.running or jobs.session_running(s.id):
        raise HTTPException(status_code=409, detail="这个会话还有一轮在跑")
    if config.CODEX_WORKER_ENABLED:
        turn = s.start_turn(q)
        s.running = True
        job = jobs.enqueue(session_id=s.id, turn_id=turn.id, idx=turn.idx, question=q)
        event = jobs.append_event(job["id"], "turn_start", {
            "turn": turn.id, "idx": turn.idx, "question": q, "job": job["id"],
        }, after_seq=s.seq)
        s.publish(event)
        event = jobs.append_event(job["id"], "job_queued", {
            "job": job["id"], "worker": jobs.worker_status(config.WORKER_MAX_AGE),
        })
        s.publish(event)
        _job_signal.set()
        return {"session": s.id, "turn": turn.id, "job": job["id"], "seq": event["seq"],
                "ok": True, "mode": "codex"}
    asyncio.create_task(pipeline.ask(s, q))
    return {"session": s.id, "ok": True, "mode": "legacy"}


@app.get("/api/stream/{sid}")
async def stream(sid: str, request: Request, since: int = 0, token: str = Query("")):
    _check(request, token)
    s = sessions.get(sid, create=True)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    queue, memory_backlog = s.subscribe(since)
    persisted = jobs.events_since(sid, since)
    merged = {int(ev.get("seq") or 0): ev for ev in (*memory_backlog, *persisted)}
    backlog = [merged[key] for key in sorted(merged)]

    async def gen():
        try:
            yield "retry: 1000\n\n"
            last = since
            for ev in backlog:
                last = max(last, int(ev.get("seq") or 0))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            deadline = time.monotonic() + config.SSE_CONNECTION_SECONDS
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    break
                try:
                    remaining = max(0.05, deadline - time.monotonic())
                    ev = await asyncio.wait_for(
                        queue.get(), timeout=min(config.SSE_PING_SECONDS, remaining)
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if int(ev.get("seq") or 0) <= last:
                    continue
                last = int(ev.get("seq") or last)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            s.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------- 会话


@app.get("/api/sessions")
async def list_sessions(request: Request):
    _check(request)
    return {"sessions": sessions.listing()}


@app.get("/api/sessions/{sid}")
async def get_session(sid: str, request: Request):
    _check(request)
    s = sessions.get(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"id": s.id, "title": s.title, "seq": jobs.resume_seq(s.id),
            "running": s.running or jobs.session_running(s.id),
            "turns": [t.to_client() for t in s.turns if t.done]}


@app.delete("/api/sessions/{sid}")
async def del_session(sid: str, request: Request):
    _check(request)
    return {"deleted": sessions.delete(sid)}


# ---------------------------------------------------------------- 书架和阅读


@app.get("/api/shelf")
async def shelf(request: Request, q: str = "", limit: int = 200):
    _check(request)
    return {"items": content.shelf(limit=limit, q=q)}


@app.get("/api/read/{series}")
async def read(series: str, request: Request):
    _check(request)
    doc = content.read_work(series) or content.read_doc(series)
    if doc is None:
        raise HTTPException(status_code=404, detail="这一篇库里没有")
    return doc


def _reader_context(series: str, chapter_index: int = 0, quote_text: str = "") -> tuple[str, str, dict]:
    """把当前章的权威全文装进一次阅读问答；语义仍由 Codex 负责。"""
    reading = content.read_work(series) or content.read_doc(series)
    if reading is None:
        raise HTTPException(status_code=404, detail="这一篇库里没有")
    work = reading if reading.get("kind") == "series" else None
    chapter = None
    if work:
        chapters = work.get("chapters") or []
        if chapter_index < 0 or chapter_index >= len(chapters):
            raise HTTPException(status_code=400, detail="章节不存在")
        chapter = chapters[chapter_index]
        doc = chapter.get("document")
        if not doc:
            raise HTTPException(status_code=409, detail="这一章还没有抓取完成")
        scope_key = f"work:{work['series']}"
        title = f"陪你读 · {work['title']}"
        chapter_map = [{
            "position": item.get("position"), "label": item.get("label") or "",
            "title": item.get("title") or "", "doc_series_id": item.get("doc_series_id") or "",
            "fetched": bool(item.get("document")), "translated": bool(item.get("translated")),
        } for item in chapters]
    else:
        doc = reading
        scope_key = f"doc:{doc['series']}"
        title = f"陪你读 · {doc['title']}"
        chapter_map = []
    source_text = "\n\n".join(page.get("text") or "" for page in doc.get("pages") or [])
    translation = doc.get("translation") or {}
    context = {
        "kind": "reader",
        "scope_key": scope_key,
        "work_id": work.get("series") if work else "",
        "work_title": work.get("title") if work else doc.get("title") or "",
        "work_description": work.get("description") if work else "",
        "chapter_index": chapter_index if work else 0,
        "chapter_id": chapter.get("id") if chapter else "",
        "chapter_label": chapter.get("label") if chapter else "",
        "chapter_title": (chapter.get("title") or chapter.get("label")) if chapter else doc.get("title") or "",
        "doc_series_id": doc.get("series") or "",
        "url": doc.get("url") or "",
        "chapter_map": chapter_map,
        "source_text": source_text,
        "translation_text": translation.get("text") or "",
        "notes": doc.get("notes") or [],
        "quote": (quote_text or "").strip()[:3000],
    }
    return scope_key, title, context


def _reader_chat_payload(s: sessions.Session) -> dict:
    return {
        "session": s.id, "title": s.title, "seq": jobs.resume_seq(s.id),
        "running": s.running or jobs.session_running(s.id),
        "turns": [turn.to_client() for turn in s.turns if turn.done],
    }


@app.get("/api/reader-chat/{series}")
async def get_reader_chat(series: str, request: Request, chapter: int = 0):
    _check(request)
    scope_key, title, _ = _reader_context(series, chapter)
    return _reader_chat_payload(sessions.reader_chat(scope_key, title))


class ReaderAskIn(BaseModel):
    series: str
    chapter: int = 0
    question: str
    quote: str = ""


def _start_reader_job(s: sessions.Session, question: str, context: dict,
                      action: str = "chat") -> dict:
    turn = s.start_turn(question)
    s.running = True
    job = jobs.enqueue(session_id=s.id, turn_id=turn.id, idx=turn.idx,
                       question=question, context=context)
    event = jobs.append_event(job["id"], "turn_start", {
        "turn": turn.id, "idx": turn.idx, "question": question,
        "job": job["id"], "reader": True, "reader_action": action,
    }, after_seq=s.seq)
    s.publish(event)
    event = jobs.append_event(job["id"], "job_queued", {
        "job": job["id"], "reader": True, "reader_action": action,
        "worker": jobs.worker_status(config.WORKER_MAX_AGE),
    })
    s.publish(event)
    _job_signal.set()
    return {"session": s.id, "turn": turn.id, "job": job["id"],
            "seq": event["seq"], "ok": True, "reader_action": action}


@app.post("/api/reader-chat/ask")
async def ask_reader(body: ReaderAskIn, request: Request):
    _check(request)
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    scope_key, title, context = _reader_context(body.series, body.chapter, body.quote)
    s = sessions.reader_chat(scope_key, title)
    if s.running or jobs.session_running(s.id):
        raise HTTPException(status_code=409, detail="陪你读还在回答上一条")
    if not config.CODEX_WORKER_ENABLED:
        raise HTTPException(status_code=503, detail="陪你读需要 Codex Worker")
    return _start_reader_job(s, question, context)


class ReaderTranslateIn(BaseModel):
    series: str
    chapter: int = 0


@app.post("/api/reader-chat/translate")
async def translate_reader_chapter(body: ReaderTranslateIn, request: Request):
    _check(request)
    scope_key, title, context = _reader_context(body.series, body.chapter)
    if context.get("translation_text"):
        return {"ok": True, "already": True}
    if not config.CODEX_WORKER_ENABLED:
        raise HTTPException(status_code=503, detail="页内翻译需要 Codex Worker")
    s = sessions.reader_chat(scope_key, title)
    if s.running or jobs.session_running(s.id):
        raise HTTPException(status_code=409, detail="陪你读还在处理上一条")
    context["kind"] = "reader_translate"
    question = f"翻译本章 · {context.get('chapter_label') or context.get('chapter_title') or '当前内容'}"
    return _start_reader_job(s, question, context, "translate")


@app.get("/api/translations")
async def translations(request: Request):
    _check(request)
    return {"items": content.list_translations()}


class NoteIn(BaseModel):
    series: str
    text: str
    seg_idx: int = -1
    quote: str = ""


@app.post("/api/notes")
async def add_note(body: NoteIn, request: Request):
    _check(request)
    nid = content.add_note(series_id=body.series, text=body.text,
                           seg_idx=body.seg_idx, quote=body.quote)
    if not nid:
        raise HTTPException(status_code=400, detail="批注不能为空")
    return {"id": nid, "notes": content.notes_for(body.series)}


@app.delete("/api/notes/{nid}")
async def del_note(nid: str, request: Request):
    _check(request)
    return {"deleted": content.delete_note(nid)}


# ---------------------------------------------------------------- Memory


@app.get("/api/memory")
async def memory(request: Request):
    _check(request)
    items = content.profile_all()
    return {"items": [
        {"id": row["id"], "text": row["text"],
         "confirmed": bool(row.get("confirmed")),
         "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}
        for row in items
    ]}


@app.delete("/api/memory/{pid}")
async def del_memory(pid: str, request: Request):
    _check(request)
    return {"deleted": content.delete_profile(pid)}


# ---------------------------------------------------------------- Codex Worker


class WorkerHello(BaseModel):
    worker_id: str
    version: str = ""
    wait_seconds: float = 0


class WorkerPulse(WorkerHello):
    state: str = "running"


class WorkerEventIn(WorkerPulse):
    type: str
    data: dict = {}


class WorkerDone(WorkerHello):
    result: dict


class WorkerFail(WorkerHello):
    error: str


def _publish_job_event(job_id: str, kind: str, data: dict | None = None) -> dict:
    event = jobs.append_event(job_id, kind, data or {})
    job = jobs.get(job_id)
    if job:
        s = sessions.get(job["session_id"], create=True)
        if s is not None:
            s.publish(event)
    return event


def _artifact_payload(series_ids: list[str]) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    translations: list[dict] = []
    seen: set[str] = set()
    for raw in series_ids:
        series = str(raw or "").strip()
        if not series or series in seen:
            continue
        seen.add(series)
        work = content.get_work(series)
        if work:
            items.append({
                "num": len(items) + 1, "id": work["id"], "kind": "series",
                "title": work["title"], "url": work.get("source_url") or "",
                "summary": work.get("description") or f"{work['chapter_count']} 个语义章节",
                "when": "刚整理好", "source": content.host_of(work.get("source_url") or ""),
                "complete": (f"完整（{work['chapter_count']} 章）" if work["complete"]
                             else f"已抓 {work['fetched_count']}/{work['chapter_count']} 章"),
                "chars": sum(chapter.get("chars") or 0 for chapter in work["chapters"]),
                "series_id": work["id"], "chapters": work["chapter_count"],
            })
            content.mark_used([
                page["id"] for chapter in work["chapters"] if chapter.get("doc_series_id")
                for page in content.series_pages(chapter["doc_series_id"])
            ])
            continue
        pages = content.series_pages(series)
        pages = [page for page in pages if page.get("chars")]
        if not pages:
            continue
        head = pages[0]
        st = content.series_status(series)
        items.append({
            "num": len(items) + 1,
            "id": head["id"],
            "kind": "doc",
            "title": head.get("title") or head.get("url"),
            "url": head.get("url"),
            "summary": content.preview(head.get("text") or "", "", 240),
            "when": "刚读到",
            "source": content.host_of(head.get("url") or ""),
            "complete": content.complete_label(st),
            "chars": st.get("chars") or 0,
            "series_id": series,
            "pages": st.get("pages") or 0,
        })
        tr = content.find_translation(series)
        if tr:
            translations.append({
                "id": tr["id"], "series": series,
                "title": tr.get("title") or head.get("title"),
                "url": tr.get("url") or head.get("url"),
                "text": tr.get("text") or "",
                "segments": len(tr.get("segments_list") or []),
                "failed": tr.get("failed") or 0,
            })
        content.mark_used([page["id"] for page in pages])
    return items, translations


def _apply_completed_job(job: dict, result: dict) -> None:
    s = sessions.get(job["session_id"], create=True)
    if s is None:
        raise RuntimeError("会话不存在")
    turn = jobs.restore_turn(s, job)
    answer = str(result.get("answer") or "").strip() or "任务完成了。"
    series_ids = [str(value) for value in (result.get("series_ids") or [])]
    turn.items, turn.translations = _artifact_payload(series_ids)
    turn.translation = turn.translations[0] if turn.translations else {}
    turn.answer = answer
    turn.trace = result.get("trace") if isinstance(result.get("trace"), list) else []
    turn.metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    turn.metrics.setdefault("stopped", "done")
    turn.metrics.setdefault("provider", "codex")
    s.running = False
    s.finish_turn(turn)
    # 阅读伴侣是作品内的独立上下文，不进入主对话检索和“关于我”记忆。
    if not sessions.is_reader_chat(s.id):
        content.save_dialog(
            session_id=s.id, turn_id=turn.id, idx=turn.idx,
            question=turn.question, answer=turn.answer,
            used_ids=[item["id"] for item in turn.items if item.get("id")],
        )


@app.post("/api/worker/claim")
async def worker_claim(body: WorkerHello, request: Request):
    _worker_check(request)
    job = jobs.claim(body.worker_id, version=body.version)
    wait_seconds = max(0.0, min(float(body.wait_seconds or 0), 25.0))
    if not job and wait_seconds:
        # clear 后再 claim 一次封住“任务恰好在 clear 前入队”的竞态。
        _job_signal.clear()
        job = jobs.claim(body.worker_id, version=body.version)
        if not job:
            try:
                await asyncio.wait_for(_job_signal.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            job = jobs.claim(body.worker_id, version=body.version)
    if not job:
        return {"job": None}
    s = sessions.get(job["session_id"], create=True)
    history = s.history_messages() if s else []
    event = _publish_job_event(job["id"], "worker_claimed", {
        "job": job["id"], "worker": body.worker_id,
    })
    return {"job": {**job, "history": history, "runtime": {
        "model": config.CODEX_MODEL, "reasoning": config.CODEX_REASONING,
    }}, "event": event}


@app.post("/api/worker/{job_id}/heartbeat")
async def worker_heartbeat(job_id: str, body: WorkerPulse, request: Request):
    _worker_check(request)
    return {"ok": jobs.heartbeat(job_id, body.worker_id, version=body.version,
                                 state=body.state)}


@app.post("/api/worker/{job_id}/event")
async def worker_event(job_id: str, body: WorkerEventIn, request: Request):
    _worker_check(request)
    if not jobs.heartbeat(job_id, body.worker_id, version=body.version, state=body.state):
        raise HTTPException(status_code=409, detail="任务租约不属于这个 Worker")
    allowed = {"codex_start", "codex_search", "codex_tool", "codex_progress", "codex_message",
               "translate_start", "translate_segment", "translate_done"}
    kind = body.type if body.type in allowed else "codex_progress"
    return {"event": _publish_job_event(job_id, kind, body.data)}


@app.post("/api/worker/{job_id}/complete")
async def worker_complete(job_id: str, body: WorkerDone, request: Request):
    _worker_check(request)
    job = jobs.get(job_id)
    if not job or job.get("worker_id") != body.worker_id:
        raise HTTPException(status_code=409, detail="任务租约不属于这个 Worker")
    if job.get("status") == "done":
        return {"ok": True, "already": True}
    if not jobs.finish(job_id, body.worker_id, body.result):
        raise HTTPException(status_code=409, detail="任务状态已变化")
    job = jobs.get(job_id) or job
    _apply_completed_job(job, body.result)
    s = sessions.get(job["session_id"], create=True)
    turn = next((item for item in (s.turns if s else []) if item.id == job["turn_id"]), None)
    _publish_job_event(job_id, "answer_start", {})
    _publish_job_event(job_id, "answer_delta", {"text": turn.answer if turn else "任务完成了。"})
    _publish_job_event(job_id, "answer_final", {
        "text": turn.answer if turn else "任务完成了。",
        "items": turn.items if turn else [],
        "translation": turn.translation if turn else {},
        "translations": turn.translations if turn else [],
    })
    _publish_job_event(job_id, "turn_done", {
        "turn": job["turn_id"], "metrics": turn.metrics if turn else {},
    })
    return {"ok": True}


@app.post("/api/worker/{job_id}/fail")
async def worker_fail(job_id: str, body: WorkerFail, request: Request):
    _worker_check(request)
    job = jobs.get(job_id)
    if not job or job.get("worker_id") != body.worker_id:
        raise HTTPException(status_code=409, detail="任务租约不属于这个 Worker")
    if not jobs.fail(job_id, body.worker_id, body.error):
        raise HTTPException(status_code=409, detail="任务状态已变化")
    s = sessions.get(job["session_id"], create=True)
    if s is not None:
        turn = jobs.restore_turn(s, job)
        turn.answer = f"这次没有完成：{body.error[:300]}"
        turn.metrics = {"stopped": "error", "provider": "codex", "retry_job": job_id}
        s.running = False
        s.finish_turn(turn)
    _publish_job_event(job_id, "error", {"error": body.error[:300], "job": job_id})
    _publish_job_event(job_id, "turn_done", {
        "turn": job["turn_id"], "metrics": {"stopped": "error", "provider": "codex"},
        "retry_job": job_id,
    })
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request):
    _check(request)
    job = jobs.get(job_id)
    if not job or not jobs.retry(job_id):
        raise HTTPException(status_code=409, detail="这个任务不能重试")
    s = sessions.get(job["session_id"], create=True)
    if s is not None:
        turn = jobs.restore_turn(s, job)
        turn.done = False
        turn.answer = ""
        s.running = True
    _publish_job_event(job_id, "job_queued", {
        "job": job_id, "retry": True, "question": job.get("question") or "",
        "turn": job.get("turn_id") or "",
    })
    _job_signal.set()
    return {"ok": True, "job": job_id}


class ToolFetchIn(BaseModel):
    url: str
    refresh: bool = False
    extractor: str = "inspect"


class ToolCatalogIn(BaseModel):
    query: str = ""
    limit: int = 20


class ToolReadIn(BaseModel):
    series_id: str
    start: int = 0
    count: int = 8


class ToolSaveTranslationIn(BaseModel):
    series_id: str
    targets: list[dict]
    purpose: str = "完整中文翻译"


class ToolSeriesMapIn(BaseModel):
    query: str = ""
    work_id: str = ""
    limit: int = 20


class ToolSaveSeriesIn(BaseModel):
    title: str
    source_url: str = ""
    description: str = ""
    chapters: list[dict]
    complete: bool = False
    work_id: str = ""


class SourceIn(BaseModel):
    url: str
    name: str = ""
    topic: str = ""
    entry_urls: list[str] = []
    interval_seconds: int = 86400


class SourceEnabledIn(BaseModel):
    enabled: bool


@app.post("/api/worker-tools/catalog")
async def tool_catalog(body: ToolCatalogIn, request: Request):
    _tool_check(request)
    return worker_tools.catalog(body.query, body.limit)


@app.post("/api/worker-tools/fetch")
async def tool_fetch(body: ToolFetchIn, request: Request):
    _tool_check(request)
    try:
        return await worker_tools.fetch_url(
            body.url, refresh=body.refresh, extractor=body.extractor
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/worker-tools/read")
async def tool_read(body: ToolReadIn, request: Request):
    _tool_check(request)
    try:
        return worker_tools.read_series(body.series_id, start=body.start, count=body.count)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/worker-tools/save-translation")
async def tool_save_translation(body: ToolSaveTranslationIn, request: Request):
    _tool_check(request)
    try:
        return worker_tools.save_translation(body.series_id, body.targets,
                                             purpose=body.purpose)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/worker-tools/series-map")
async def tool_series_map(body: ToolSeriesMapIn, request: Request):
    _tool_check(request)
    try:
        return worker_tools.series_map(body.query, body.work_id, body.limit)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/worker-tools/save-series")
async def tool_save_series(body: ToolSaveSeriesIn, request: Request):
    _tool_check(request)
    try:
        return worker_tools.save_series(
            title=body.title, source_url=body.source_url,
            description=body.description, chapters=body.chapters,
            complete=body.complete, work_id=body.work_id,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/worker-tools/sources")
async def tool_sources(body: ToolCatalogIn, request: Request):
    _tool_check(request)
    return {"sources": sources.listing(body.limit),
            "candidates": sources.candidates(limit=body.limit)}


@app.post("/api/worker-tools/follow-source")
async def tool_follow_source(body: SourceIn, request: Request):
    _tool_check(request)
    try:
        return sources.follow(body.url, name=body.name, topic=body.topic,
                              entry_urls=body.entry_urls or None,
                              interval_seconds=body.interval_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/worker-tools/refresh-source")
async def tool_refresh_source(body: dict, request: Request):
    _tool_check(request)
    try:
        return await sources.refresh(str(body.get("source_id") or ""))
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/worker/status")
async def worker_status(request: Request):
    _check(request)
    return {
        "worker": jobs.worker_status(config.WORKER_MAX_AGE), "jobs": jobs.stats(),
        "runtime": {"model": config.CODEX_MODEL, "reasoning": config.CODEX_REASONING},
    }


# ---------------------------------------------------------------- 来源地图


@app.get("/api/sources")
async def list_sources(request: Request):
    _check(request)
    return {"sources": sources.listing(), "candidates": sources.candidates(limit=200)}


@app.post("/api/sources")
async def add_source(body: SourceIn, request: Request):
    _check(request)
    try:
        source = sources.follow(body.url, name=body.name, topic=body.topic,
                                entry_urls=body.entry_urls or None,
                                interval_seconds=body.interval_seconds)
        return {"source": source}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sources/{source_id}/refresh")
async def refresh_source(source_id: str, request: Request):
    _check(request)
    try:
        return {"source": await sources.refresh(source_id)}
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sources/{source_id}/enabled")
async def enable_source(source_id: str, body: SourceEnabledIn, request: Request):
    _check(request)
    return {"updated": sources.set_enabled(source_id, body.enabled)}


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: str, request: Request):
    _check(request)
    return {"deleted": sources.delete(source_id)}


# ---------------------------------------------------------------- 杂项


@app.get("/api/stats")
async def stats(request: Request):
    _check(request)
    return {
        "content": content.stats(),
        "models": {"pro": config.MODEL_PRO, "flash": config.MODEL_FLASH,
                   "by_agent": config.AGENT_MODEL},
        "search": [p.name for p in active_providers()],
        "codex": {"enabled": config.CODEX_WORKER_ENABLED,
                  "worker": jobs.worker_status(config.WORKER_MAX_AGE),
                  "jobs": jobs.stats()},
    }


class ConfigIn(BaseModel):
    fields: dict = {}
    agent_model: dict = {}
    agent_reasoning: dict = {}


@app.get("/api/config")
async def get_config(request: Request):
    _check(request)
    return settings.current()


@app.post("/api/config")
async def set_config(body: ConfigIn, request: Request):
    _check(request)
    out = settings.update({"fields": body.fields, "agent_model": body.agent_model,
                           "agent_reasoning": body.agent_reasoning})
    return {**out, "config": settings.current()}


@app.get("/api/health")
async def health():
    worker = jobs.worker_status(config.WORKER_MAX_AGE)
    return {"ok": True, "mode": "codex" if config.CODEX_WORKER_ENABLED else "legacy",
            "search": [p.name for p in active_providers()],
            "worker": {"online": bool(worker.get("online")),
                       "state": worker.get("state", "never_seen")}}


# ---------------------------------------------------------------- 静态


@app.get("/")
async def index():
    version = str(int(max((WEB / name).stat().st_mtime for name in ("app.js", "style.css"))))
    html = (WEB / "index.html").read_text(encoding="utf-8").replace("__ASSET_VERSION__", version)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
