#!/usr/bin/env python3
"""给 Scout Codex 使用的最小 stdio MCP：代理到当前 Scout 服务。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("SCOUT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")

def load_token() -> str:
    direct = os.environ.get("SCOUT_MCP_TOKEN", "").strip()
    if direct:
        return direct
    path = os.environ.get("SCOUT_MCP_TOKEN_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("读不到 SCOUT_MCP_TOKEN_FILE 指向的凭据文件") from exc


TOKEN = load_token()

TOOLS = [
    {
        "name": "scout_catalog",
        "description": "查看 Scout 书架或按关键词查已经读过的内容；先查库以避免重复。",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}
        }},
    },
    {
        "name": "scout_fetch",
        "description": "两阶段抓取。第一次使用 extractor=inspect，只返回 article/dense/list 候选的字数、行数、链接/表格壳和首尾预览，不入库；你判断哪个才是用户要的内容后，必须第二次明确传 article、dense 或 list，并用 refresh=true 才保存。",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}, "refresh": {"type": "boolean"},
            "extractor": {"type": "string", "enum": ["inspect", "article", "dense", "list"]}
        }, "required": ["url"]},
    },
    {
        "name": "scout_read",
        "description": "按段读取已保存文章的完整原文，用于翻译。每段的 lines 是非空行数；译文必须逐行对应并保留换行。每次最多 12 段，按 start 继续。",
        "inputSchema": {"type": "object", "properties": {
            "series_id": {"type": "string"}, "start": {"type": "integer", "minimum": 0},
            "count": {"type": "integer", "minimum": 1, "maximum": 12}
        }, "required": ["series_id"]},
    },
    {
        "name": "scout_save_translation",
        "description": "保存文章译文。targets 是 idx/target 数组；每个 target 的非空行数必须等于 scout_read 返回的 lines，每个原文行对应一个译文行并用换行分隔。可分批调用，直到 done=true。",
        "inputSchema": {"type": "object", "properties": {
            "series_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "object", "properties": {
                "idx": {"type": "integer", "minimum": 0}, "target": {"type": "string"}
            }, "required": ["idx", "target"]}},
            "purpose": {"type": "string"}
        }, "required": ["series_id", "targets"]},
    },
    {
        "name": "scout_series_map",
        "description": "查看 Codex 已建立的语义作品/专栏及章节地图。可按 query 列表，或传 work_id 读取完整章节状态。",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "work_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}
        }},
    },
    {
        "name": "scout_save_series",
        "description": "保存你理解出的 Series→Chapters 语义地图。章节 label/title/position/url 全部由你判断；程序只按 URL 关联已抓正文。complete=true 只有所有章节均已入库才会成立，否则返回 missing_urls。再次提交同一 work_id 可修订地图。",
        "inputSchema": {"type": "object", "properties": {
            "work_id": {"type": "string"}, "title": {"type": "string"},
            "source_url": {"type": "string"}, "description": {"type": "string"},
            "complete": {"type": "boolean"},
            "chapters": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": {
                    "position": {"type": "integer", "minimum": 1},
                    "label": {"type": "string"}, "title": {"type": "string"},
                    "url": {"type": "string"}
                }, "required": ["position", "label", "url"]
            }}
        }, "required": ["title", "source_url", "chapters"]},
    },
    {
        "name": "scout_sources",
        "description": "查看用户长期关注的来源地图和新发现的候选文章。",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}
        }},
    },
    {
        "name": "scout_follow_source",
        "description": "用户明确要求关注某网站时保存来源地图。不要在普通搜索任务中擅自关注。",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}, "name": {"type": "string"},
            "topic": {"type": "string"},
            "entry_urls": {"type": "array", "items": {"type": "string"}},
            "interval_seconds": {"type": "integer", "minimum": 3600, "maximum": 2592000}
        }, "required": ["url"]},
    },
    {
        "name": "scout_refresh_source",
        "description": "礼貌检查一个已关注来源的入口页，增量发现候选文章。",
        "inputSchema": {"type": "object", "properties": {
            "source_id": {"type": "string"}
        }, "required": ["source_id"]},
    },
]

ROUTES = {
    "scout_catalog": "/api/worker-tools/catalog",
    "scout_fetch": "/api/worker-tools/fetch",
    "scout_read": "/api/worker-tools/read",
    "scout_save_translation": "/api/worker-tools/save-translation",
    "scout_series_map": "/api/worker-tools/series-map",
    "scout_save_series": "/api/worker-tools/save-series",
    "scout_sources": "/api/worker-tools/sources",
    "scout_follow_source": "/api/worker-tools/follow-source",
    "scout_refresh_source": "/api/worker-tools/refresh-source",
}


def call(name: str, arguments: dict) -> dict:
    if name not in ROUTES:
        raise ValueError(f"未知工具：{name}")
    if not TOKEN:
        raise RuntimeError("SCOUT_MCP_TOKEN 没配置")
    req = urllib.request.Request(
        BASE + ROUTES[name],
        data=json.dumps(arguments or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}",
                 "User-Agent": "scout-codex-mcp/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"Scout HTTP {exc.code}: {detail}") from exc


def respond(rid, result=None, error=None) -> None:
    if rid is None:
        return
    message = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        message["error"] = {"code": -32000, "message": str(error)[:1200]}
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rid, method = msg.get("id"), msg.get("method")
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                respond(rid, {"protocolVersion": requested or "2025-03-26",
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "scout", "version": "1.0.0"},
                              "instructions": "先查库避免重复；只抓公开文章；完整翻译必须读完并保存所有段。"})
            elif method == "tools/list":
                respond(rid, {"tools": TOOLS})
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = call(params.get("name") or "", params.get("arguments") or {})
                respond(rid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                              "structuredContent": result, "isError": False})
            elif method == "ping":
                respond(rid, {})
            elif rid is not None and method not in ("notifications/initialized", "initialized"):
                respond(rid, error=f"不支持的方法：{method}")
        except Exception as exc:  # noqa: BLE001
            if method == "tools/call":
                respond(rid, {"content": [{"type": "text", "text": str(exc)[:1200]}],
                              "isError": True})
            else:
                respond(rid, error=exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
