"""一轮问答从头到尾。

主 agent 出话之后还有两件事，都跑在**用户的等待路径之外**：
把这一轮存进对话历史，以及让「记结论」看一眼有什么值得记的。
答案早就发出去了，这两件事慢一点没人等，写不进去也不该让这一轮变成失败。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from . import config, content, session as sessions
from .agent.main import run_main
from .agent.subagents.memory import run_remember
from .cost import Budget

log = logging.getLogger("scout.pipeline")


def today() -> str:
    return datetime.now().strftime("%Y 年 %m 月 %d 日")


async def ask(s: sessions.Session, question: str) -> sessions.Turn:
    """跑完一轮。事件通过 `s.emit` 实时发出去，返回收好尾的那一轮。"""
    turn = s.start_turn(question)
    s.running = True
    started = time.monotonic()
    budget = Budget.root(config.TURN_TOKEN_BUDGET, config.TURN_WALL_SECONDS)
    ttfb: list[int] = []

    await s.emit("turn_start", {"turn": turn.id, "idx": turn.idx, "question": question})

    async def on_text(chunk: str) -> None:
        if not ttfb:
            ttfb.append(int((time.monotonic() - started) * 1000))
            await s.emit("first_token", {"ms": ttfb[0]})
            await s.emit("answer_start", {})
        turn.answer += chunk
        await s.emit("answer_delta", {"text": chunk})

    try:
        out, mats = await run_main(
            question, budget=budget, emit=s.emit,
            history=s.history_messages(), on_text=on_text,
            session_id=s.id, today=today(),
        )
    except Exception as exc:  # noqa: BLE001 —— 一轮挂了不该把服务带走
        log.exception("这一轮出错了")
        s.running = False
        turn.answer = turn.answer or f"这一轮出错了：{exc}"
        await s.emit("error", {"error": str(exc)[:300]})
        s.finish_turn(turn)
        return turn

    # 主 agent 说的话就是答案。流式已经发过的部分在 turn.answer 里，
    # 没走流式（比如撞了预算上限）的话这里补上。
    if out.text and not out.extra.get("streamed"):
        turn.answer = out.text
        await s.emit("answer_start", {})
        await s.emit("answer_delta", {"text": out.text})
    elif out.text:
        turn.answer = out.text     # 以定稿为准（去掉了无效角标）
    if not turn.answer.strip():
        turn.answer = "这一轮没有产出内容。"

    turn.items = [it.to_client(mats.by_id.get(it.id)) for it in out.items]
    turn.trace = [st.to_client() for st in out.steps]
    turn.translation = out.extra.get("translation") or {}
    turn.translations = out.extra.get("translations") or \
        ([turn.translation] if turn.translation else [])
    elapsed = int((time.monotonic() - started) * 1000)
    turn.metrics = {
        "ms": elapsed,
        "ttfb_ms": ttfb[0] if ttfb else None,
        "stopped": out.stopped,
        "steps": len(out.steps),
        **budget.ledger.summary(),
    }

    await s.emit("answer_final", {
        "text": turn.answer,
        "items": turn.items,
        "translation": turn.translation,
        "translations": turn.translations,
    })
    await s.emit("turn_done", {"turn": turn.id, "metrics": turn.metrics})
    s.running = False
    s.finish_turn(turn)

    # ---- 以下跑在等待路径之外 ----
    asyncio.create_task(_after(s, turn, mats))
    return turn


async def _after(s: sessions.Session, turn: sessions.Turn, mats) -> None:
    """答案发完之后的收尾。每一件都自己兜住异常。"""
    used_ids = [it.get("id") for it in turn.items if it.get("id")]
    try:
        content.save_dialog(
            session_id=s.id, turn_id=turn.id, idx=turn.idx,
            question=turn.question, answer=turn.answer, used_ids=used_ids,
        )
        content.mark_used(used_ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("存对话历史失败：%s", exc)

    try:
        budget = Budget.root(int(config.TURN_TOKEN_BUDGET * 0.1), 120.0)
        res = await run_remember(
            question=turn.question, answer=turn.answer, used_ids=used_ids,
            session_id=s.id, turn_id=turn.id, budget=budget, emit=s.emit,
        )
        if res.extra.get("new") or res.extra.get("updated"):
            log.info("这一轮记了 %s 条新结论、更新 %s 条",
                     res.extra.get("new"), res.extra.get("updated"))
    except Exception as exc:  # noqa: BLE001
        log.warning("记结论失败：%s", exc)


# ---------------------------------------------------------------- 定时任务


async def profile_sweep() -> None:
    """每小时跑一次：给闲下来的会话抽一次「关于我」。

    **看的是整段对话，不是几轮。** 每三轮抽一次的话，模型只看得见那三轮，
    会把"这会儿在问 Rust"抽成"用户是 Rust 开发者"。
    """
    from .agent.subagents.memory import run_profile

    while True:
        try:
            await asyncio.sleep(config.PROFILE_SWEEP_INTERVAL)
            for row in sessions.idle_sessions(config.PROFILE_IDLE_SECONDS):
                s = sessions.get(row["id"])
                if s is None or not s.turns:
                    continue
                budget = Budget.root(int(config.TURN_TOKEN_BUDGET * 0.2), 180.0)
                try:
                    res = await run_profile(
                        conversation=s.conversation_text(), budget=budget)
                    sessions.mark_profiled(s.id, row["last_idx"])
                    log.info("会话 %s 的「关于我」：加 %s 改 %s 删 %s", s.id,
                             res.extra.get("added"), res.extra.get("updated"),
                             res.extra.get("deleted"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("抽「关于我」失败（%s）：%s", s.id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("定时任务出错：%s", exc)


async def session_sweep() -> None:
    while True:
        try:
            await asyncio.sleep(config.SWEEP_INTERVAL)
            n = sessions.sweep(config.SESSION_TTL)
            if n:
                log.info("回收了 %d 个太久没动的会话（聊过的内容留在库里）", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("回收会话出错：%s", exc)
