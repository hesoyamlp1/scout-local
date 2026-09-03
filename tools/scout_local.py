#!/usr/bin/env python3
"""Scout 单机版的安装后控制入口。

所有持久数据和 Scout 专用 Codex 登录都放在 SCOUT_HOME；源码目录可以更新或替换。
这个文件只用标准库，便于安装失败时仍能给出可读诊断。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765


def scout_home() -> Path:
    configured = os.environ.get("SCOUT_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return base / "Scout"
    return Path.home() / ".local/share/scout"


def paths() -> dict[str, Path]:
    home = scout_home()
    return {
        "home": home,
        "data": home / "data",
        "logs": home / "logs",
        "codex_home": home / "codex-home",
        "playwright": home / "playwright",
        "worker_token": home / "worker-token",
        "mcp_token": home / "mcp-token",
        "state": home / "processes.json",
    }


def port() -> int:
    try:
        value = int(os.environ.get("SCOUT_PORT", str(DEFAULT_PORT)))
    except ValueError:
        value = DEFAULT_PORT
    if not 1024 <= value <= 65535:
        raise RuntimeError("SCOUT_PORT 必须在 1024..65535 之间")
    return value


def base_url() -> str:
    return f"http://127.0.0.1:{port()}"


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _token(path: Path) -> str:
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    value = secrets.token_urlsafe(36)
    _private_write(path, value + "\n")
    return value


def _toml(value: str) -> str:
    return json.dumps(value.replace("\\", "/"), ensure_ascii=False)


def write_codex_config() -> Path:
    p = paths()
    mcp = ROOT / "tools/scout_mcp_proxy.py"
    python = Path(sys.executable)
    config = f'''model = "gpt-5.6-terra"
model_reasoning_effort = "medium"
web_search = "live"
approval_policy = "never"
default_permissions = "scout-agent"

[permissions.scout-agent]
description = "Scout: read product code, use only the local Scout MCP for content operations."

[permissions.scout-agent.filesystem]
":minimal" = "read"
glob_scan_max_depth = 4

[permissions.scout-agent.filesystem.":workspace_roots"]
"." = "read"
".env" = "deny"
"data" = "deny"
"logs" = "deny"
"**/*.env" = "deny"

[permissions.scout-agent.network]
enabled = false

[mcp_servers.scout]
command = {_toml(str(python))}
args = [{_toml(str(mcp))}]
startup_timeout_sec = 15
tool_timeout_sec = 90

[mcp_servers.scout.env]
SCOUT_BASE_URL = {_toml(base_url())}
SCOUT_MCP_TOKEN_FILE = {_toml(str(p["mcp_token"]))}

[mcp_servers.scout.tools.scout_catalog]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_fetch]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_read]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_save_translation]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_series_map]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_save_series]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_sources]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_follow_source]
approval_mode = "approve"
[mcp_servers.scout.tools.scout_refresh_source]
approval_mode = "approve"
'''
    target = p["codex_home"] / "config.toml"
    _private_write(target, config)
    return target


def init() -> None:
    p = paths()
    for key in ("home", "data", "logs", "codex_home", "playwright"):
        p[key].mkdir(parents=True, exist_ok=True)
    _token(p["worker_token"])
    _token(p["mcp_token"])
    target = write_codex_config()
    print(f"Scout 本地目录：{p['home']}")
    print(f"Scout Codex 配置：{target}")


def _codex_bin() -> Path:
    try:
        from codex_cli_bin import bundled_codex_path
    except ImportError as exc:
        raise RuntimeError("缺少 Scout 固定版本的 Codex Runtime，请重新运行 install.ps1") from exc
    binary = bundled_codex_path()
    if not binary.exists():
        raise RuntimeError(f"Scout Codex Runtime 不存在：{binary}")
    return binary


def codex_env() -> dict[str, str]:
    env = os.environ.copy()
    p = paths()
    env["CODEX_HOME"] = str(p["codex_home"])
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(p["playwright"])
    return env


def login_status(*, quiet: bool = False) -> bool:
    init()
    try:
        binary = _codex_bin()
    except RuntimeError as exc:
        if not quiet:
            print(str(exc))
        return False
    proc = subprocess.run(
        [str(binary), "login", "status"], env=codex_env(), cwd=ROOT,
        text=True, capture_output=True, check=False,
    )
    message = (proc.stdout or proc.stderr or "未登录").strip()
    if not quiet:
        print(message)
    return proc.returncode == 0


def login() -> int:
    if login_status(quiet=True):
        print("Scout 已经连接到你的 Codex。")
        return 0
    print("请在接下来打开的浏览器中登录你自己的 ChatGPT/Codex 账户。")
    return subprocess.call([str(_codex_bin()), "login"], env=codex_env(), cwd=ROOT)


def runtime_env() -> dict[str, str]:
    init()
    p = paths()
    env = codex_env()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "SCOUT_HOST": "127.0.0.1",
        "SCOUT_PORT": str(port()),
        "SCOUT_BASE_URL": base_url(),
        "SCOUT_DATA_DIR": str(p["data"]),
        "SCOUT_DB_PATH": str(p["data"] / "scout.db"),
        "SCOUT_LOG_DIR": str(p["logs"]),
        "SCOUT_CODEX_HOME": str(p["codex_home"]),
        "SCOUT_CODEX_WORKER_ENABLED": "true",
        "SCOUT_WORKER_TOKEN_FILE": str(p["worker_token"]),
        "SCOUT_MCP_TOKEN_FILE": str(p["mcp_token"]),
        "SCOUT_WORKER_CONCURRENCY": "2",
        "SCOUT_SEARCH_PROVIDERS": "",
        "SCOUT_AUTH_COOKIE_SECURE": "false",
    })
    env.pop("SCOUT_CODEX_BIN", None)
    env.pop("SCOUT_CODEX_BYPASS_HOOK_TRUST", None)
    return env


def _json_get(path: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(base_url() + path, timeout=timeout) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _load_state() -> dict:
    path = paths()["state"]
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    _private_write(paths()["state"], json.dumps(state, indent=2) + "\n")


def _spawn(name: str, args: list[str], log_name: str) -> subprocess.Popen:
    log_path = paths()["logs"] / log_name
    log = log_path.open("a", encoding="utf-8")
    kwargs: dict = {
        "cwd": str(ROOT), "env": runtime_env(), "stdin": subprocess.DEVNULL,
        "stdout": log, "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(args, **kwargs)
    finally:
        log.close()


def _wait_for(path: str, seconds: float) -> dict | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = _json_get(path)
        if value is not None:
            return value
        time.sleep(0.25)
    return None


def start(*, open_browser: bool = True) -> int:
    init()
    if not login_status(quiet=True):
        print("Scout 还没有连接 Codex。请先运行：.\\scout.ps1 login", file=sys.stderr)
        return 2
    state = _load_state()
    if _json_get("/api/health") is None:
        server = _spawn(
            "server",
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port())],
            "server.log",
        )
        state["server"] = server.pid
        state["root"] = str(ROOT)
        state["port"] = port()
        _save_state(state)
        if _wait_for("/api/health", 20) is None:
            print(f"Scout 服务启动失败，请查看 {paths()['logs'] / 'server.log'}", file=sys.stderr)
            return 1
    worker = _json_get("/api/worker/status") or {}
    if not worker.get("online"):
        process = _spawn("worker", [sys.executable, "tools/codex_worker.py"], "worker.log")
        state["worker"] = process.pid
        _save_state(state)
    print(f"Scout 已启动：{base_url()}")
    if open_browser:
        webbrowser.open(base_url())
    return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_pid(pid: int) -> None:
    if not _pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)


def stop() -> int:
    state = _load_state()
    for name in ("worker", "server"):
        try:
            pid = int(state.get(name) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid:
            _stop_pid(pid)
            print(f"已停止 {name}（pid {pid}）")
    try:
        paths()["state"].unlink()
    except FileNotFoundError:
        pass
    return 0


def status() -> int:
    health = _json_get("/api/health")
    worker = _json_get("/api/worker/status")
    if health is None:
        print("Scout 没有运行。")
        return 1
    worker_text = "在线" if worker and worker.get("online") else "正在启动或离线"
    print(f"Scout 服务正常：{base_url()}")
    print(f"Codex Worker：{worker_text}")
    return 0


def doctor() -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 10), sys.version.split()[0]))
    try:
        import fastapi  # noqa: F401
        import openai_codex  # noqa: F401
        checks.append(("依赖", True, "已安装"))
    except ImportError as exc:
        checks.append(("依赖", False, str(exc)))
    try:
        binary = _codex_bin()
        checks.append(("Codex Runtime", True, str(binary)))
    except RuntimeError as exc:
        checks.append(("Codex Runtime", False, str(exc)))
    try:
        home = paths()["home"]
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("本地数据", True, str(home)))
    except OSError as exc:
        checks.append(("本地数据", False, str(exc)))
    checks.append(("Codex 登录", login_status(quiet=True), "使用 Scout 独立登录"))
    health = _wait_for("/api/health", 2)
    checks.append(("Scout 服务", health is not None, base_url()))
    worker = _wait_for("/api/worker/status", 30) if health is not None else None
    checks.append(("Codex Worker", bool(worker and worker.get("online")), "本机任务执行器"))
    failed = False
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}：{detail}")
        failed = failed or not ok
    if failed:
        print(f"日志目录：{paths()['logs']}")
        return 1
    print(f"Scout 可以使用：{base_url()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scout 本地版")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("login")
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--no-open", action="store_true")
    sub.add_parser("stop")
    restart_parser = sub.add_parser("restart")
    restart_parser.add_argument("--no-open", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("open")
    args = parser.parse_args()
    if args.command == "init":
        init()
        return 0
    if args.command == "login":
        return login()
    if args.command == "start":
        return start(open_browser=not args.no_open)
    if args.command == "stop":
        return stop()
    if args.command == "restart":
        stop()
        return start(open_browser=not args.no_open)
    if args.command == "status":
        return status()
    if args.command == "doctor":
        return doctor()
    webbrowser.open(base_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
