"""一个 Worker 生命周期只启动一次 Codex App Server；每个 Job 建独立 ephemeral thread。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable


class WarmCodexRuntime:
    def __init__(self, root: Path, codex_home: str) -> None:
        self.root = root
        self.codex_home = codex_home
        self.codex = None

    def __enter__(self):
        from openai_codex import Codex, CodexConfig

        env = os.environ.copy()
        env["CODEX_HOME"] = self.codex_home
        env.pop("SCOUT_WORKER_TOKEN", None)
        env.pop("SCOUT_MCP_TOKEN", None)
        codex_bin = os.environ.get("SCOUT_CODEX_BIN", "").strip() or None
        launch_args = None
        if codex_bin:
            launch_args_list = [codex_bin, "--strict-config"]
            if os.environ.get("SCOUT_CODEX_BYPASS_HOOK_TRUST", "").lower() in {
                "1", "true", "yes", "on",
            }:
                launch_args_list.append("--dangerously-bypass-hook-trust")
            launch_args_list.extend(("app-server", "--listen", "stdio://"))
            launch_args = tuple(launch_args_list)
        config = CodexConfig(
            codex_bin=codex_bin,
            launch_args_override=launch_args,
            cwd=str(self.root), env=env,
            client_name="scout_worker", client_title="Scout Worker",
            client_version="1.0",
        )
        self.codex = Codex(config=config)
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.codex is not None:
            self.codex.close()
            self.codex = None

    @staticmethod
    def event_dict(event) -> dict:
        payload = event.payload
        if hasattr(payload, "model_dump"):
            data = payload.model_dump(by_alias=True, exclude_none=True, mode="json")
        elif hasattr(payload, "params"):
            data = dict(payload.params)
        else:
            data = {}
        return {"type": event.method, **data}

    def run(
        self, *, prompt: str, model: str, reasoning: str,
        output_schema_path: Path, timeout_seconds: int,
        on_event: Callable[[dict], None],
    ) -> dict:
        if self.codex is None:
            raise RuntimeError("Codex runtime 尚未启动")
        from openai_codex import ApprovalMode
        from openai_codex._run import _collect_turn_result
        from openai_codex.types import ReasoningEffort

        schema = json.loads(output_schema_path.read_text(encoding="utf-8"))
        thread = self.codex.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(self.root), ephemeral=True, model=model,
            service_name="scout",
        )
        turn = thread.turn(
            prompt,
            approval_mode=ApprovalMode.deny_all,
            cwd=str(self.root), effort=ReasoningEffort(reasoning), model=model,
            output_schema=schema,
        )
        timed_out = threading.Event()

        def interrupt() -> None:
            timed_out.set()
            try:
                turn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(timeout_seconds, interrupt)
        timer.daemon = True
        timer.start()

        def observed():
            for event in turn.stream():
                try:
                    on_event(self.event_dict(event))
                except Exception:
                    try:
                        turn.interrupt()
                    except Exception:
                        pass
                    raise
                yield event

        try:
            result = _collect_turn_result(observed(), turn_id=turn.id)
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise TimeoutError(f"Codex 超过 {timeout_seconds} 秒")
        raw = (result.final_response or "").strip()
        if not raw:
            raise RuntimeError("Codex 没有生成结构化结果")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("Codex 结构化结果不是对象")
        return parsed
