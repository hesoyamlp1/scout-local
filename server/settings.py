"""界面上能改的设置。

**密钥只能写不能读。** `GET /api/config` 永远不返回 key 的内容，只说"配了没有"——
不这么做的话，谁拿到 token 谁就能把 key 读走。

存在 `data/settings.json`（权限 600）。改完当场生效：模型通道会重建，
搜索源会重新算可用性，不用重启。

**这里只列该由人调的东西**：地址、key、模型分档、预算、并发、阅读粒度。
`config.py` 里那些物理常量（超时、字节上限）不进来——它们没有调的理由。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("scout.settings")

PATH = Path(config.DATA_DIR) / "settings.json"
AGENTS = ["main", "research", "process", "fetch", "find", "remember", "profile", "extract"]
AGENT_CN = {
    "main": "主 agent（跟你说话、决定派谁）",
    "research": "搜索（挑哪几个网址打开）",
    "process": "处理整篇（翻译、摘要）",
    "fetch": "抓取（判断抓到的对不对）",
    "find": "找一找（判断记忆里的东西相不相关）",
    "remember": "记结论（后台跑）",
    "profile": "记关于我（定时跑）",
    "extract": "抽要点（调用最频繁）",
}


@dataclass
class Field_:
    key: str
    label: str
    group: str
    type: str = "text"          # text / int / float / bool / secret / choice
    help: str = ""
    choices: list = field(default_factory=list)
    rebuild: str = ""           # llm / search


FIELDS: list[Field_] = [
    # ── Codex Runtime ──
    Field_("CODEX_MODEL", "默认模型", "Codex", "choice",
           help="Terra 适合日常；Sol 更强更慢；Luna 适合明确的重复任务",
           choices=list(config.CODEX_MODELS)),
    Field_("CODEX_REASONING", "推理强度", "Codex", "choice",
           help="使用能完成任务的最低档；提高会增加耗时与套餐用量",
           choices=list(config.CODEX_REASONING_LEVELS)),

    # ── 模型 ──
    Field_("MODEL_BASE_URL", "接口地址", "模型", rebuild="llm"),
    Field_("MODEL_API_KEY", "API key", "模型", "secret", rebuild="llm"),
    Field_("MODEL_PRO", "强模型（管判断）", "模型", rebuild="llm",
           help="判断错了整轮就废的地方用它"),
    Field_("MODEL_FLASH", "快模型（管高频）", "模型", rebuild="llm",
           help="抓取、找一找、抽要点这些一轮要跑好几次的"),
    Field_("LLM_TIMEOUT", "单次调用超时（秒）", "模型", "float"),

    # ── 账本 ──
    Field_("TURN_TOKEN_BUDGET", "一轮的 token 上限", "账本", "int",
           help="所有 subagent 从这一本账里划，不存在第二套上限"),
    Field_("TURN_WALL_SECONDS", "一轮的墙钟上限（秒）", "账本", "float"),
    Field_("MAX_CONCURRENT_SUBAGENTS", "同时在跑的 subagent 上限", "账本", "int"),
    Field_("FETCH_CONCURRENCY", "同时抓几个网页", "账本", "int"),
    Field_("FETCH_PER_DOMAIN_CONCURRENCY", "同一个站同时抓几个", "账本", "int",
           help="礼貌，别把人家站点打疼了"),

    # ── 阅读 ──
    Field_("TRANSLATE_SEGMENT_CHARS", "翻译切段的字数", "阅读", "int",
           help="这个数就是对照阅读的粒度：一段太长就没法对着读"),
    Field_("TRANSLATE_CONCURRENCY", "同时翻几段", "阅读", "int"),
    Field_("EXTRACT_MAX_CHARS", "要点最长多少字", "阅读", "int",
           help="进模型上下文的量。原文永远不进，进的是这个"),
    Field_("HISTORY_FULL_TURNS", "保留几轮完整历史", "阅读", "int",
           help="再往前只留结论和编号，正文按编号随时取得回来"),

    # ── 抓取 ──
    Field_("BROWSER_ENABLED", "允许用浏览器抓", "抓取", "bool",
           help="慢十几倍，但能对付 JS 渲染的页面和一部分反爬"),
    Field_("FETCH_HARD_RETRY_CAP", "同一页最多试几次", "抓取", "int",
           help="硬顶，防跑飞用。要不要再试是抓取自己判断的"),
    Field_("PDF_MAX_PAGES", "PDF 最多读几页", "抓取", "int"),

    # ── 搜索 ──
    Field_("SEARCH_PROVIDERS", "用哪些搜索源", "搜索", "providers", rebuild="search"),
    Field_("SEARXNG_BASE_URL", "自建 SearXNG 的地址", "搜索", rebuild="search",
           help="无 key 无配额，聚合几十个引擎。留空就是不用"),
    Field_("SERPER_API_KEY", "Serper 的 key", "搜索", "secret", rebuild="search"),
    Field_("TAVILY_API_KEY", "Tavily 的 key", "搜索", "secret", rebuild="search"),
    Field_("SEARCH_RESULTS_PER_QUERY", "每条检索词取几条结果", "搜索", "int"),
]

BY_KEY = {f.key: f for f in FIELDS}
SECRETS = {f.key for f in FIELDS if f.type == "secret"}


def _saved() -> dict:
    if not PATH.exists():
        return {}
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PATH)
    PATH.chmod(0o600)          # 里面有 key


def current() -> dict:
    """给界面看的当前设置。**key 只说配了没有，不给内容。**"""
    saved = _saved()
    groups: dict[str, list] = {}
    for f in FIELDS:
        value: Any
        if f.type == "secret":
            value = bool(getattr(config, f.key, ""))
        else:
            value = getattr(config, f.key, None)
        groups.setdefault(f.group, []).append({
            "key": f.key, "label": f.label, "type": f.type, "help": f.help,
            "value": value, "changed": f.key in saved, "choices": f.choices,
        })
    return {
        "groups": groups,
        "agents": [
            {"key": a, "label": AGENT_CN.get(a, a),
             "model": config.AGENT_MODEL.get(a, "flash"),
             "reasoning": config.AGENT_REASONING.get(a, "")}
            for a in AGENTS
        ],
        "all_providers": list(config._ALL_PROVIDERS),
        "tiers": ["pro", "flash"],
        "reasoning_choices": ["", "none"],
    }


def _coerce(f: Field_, value: Any) -> Any:
    if f.type == "int":
        return int(value)
    if f.type == "float":
        return float(value)
    if f.type == "bool":
        return bool(value)
    if f.type == "providers":
        return [p for p in (value or []) if p in config._ALL_PROVIDERS]
    if f.type == "choice":
        selected = str(value)
        if selected not in f.choices:
            raise ValueError(f"{f.key} 不是合法选项")
        return selected
    return str(value)


def update(patch: dict) -> dict:
    """改设置。返回改了哪几项。

    `agent_model` 和 `agent_reasoning` 是两张表，单独走——它们是"谁用哪档"，
    界面上按 agent 一行一行改。
    """
    saved = _saved()
    changed: list[str] = []
    rebuild: set[str] = set()

    for key, value in (patch.get("fields") or {}).items():
        f = BY_KEY.get(key)
        if f is None:
            continue
        if f.type == "secret" and not str(value).strip():
            continue          # 空着表示不改，不是要清空
        try:
            v = _coerce(f, value)
        except (TypeError, ValueError):
            log.warning("设置项 %s 的值不合法：%r", key, value)
            continue
        setattr(config, key, v)
        saved[key] = v
        changed.append(key)
        if f.rebuild:
            rebuild.add(f.rebuild)

    am = patch.get("agent_model") or {}
    for agent, tier in am.items():
        if agent in AGENTS and tier in ("pro", "flash"):
            config.AGENT_MODEL[agent] = tier
    if am:
        saved["AGENT_MODEL"] = dict(config.AGENT_MODEL)
        changed.append("AGENT_MODEL")

    ar = patch.get("agent_reasoning") or {}
    for agent, eff in ar.items():
        if agent not in AGENTS:
            continue
        if eff:
            config.AGENT_REASONING[agent] = eff
        else:
            config.AGENT_REASONING.pop(agent, None)
    if ar:
        saved["AGENT_REASONING"] = dict(config.AGENT_REASONING)
        changed.append("AGENT_REASONING")

    _write(saved)
    if "llm" in rebuild or "AGENT_MODEL" in changed:
        from . import llm

        llm.reconfigure()
    if "search" in rebuild:
        from .search import _REGISTRY

        for p in _REGISTRY.values():
            if hasattr(p, "reset"):
                p.reset()
    log.info("设置改了 %d 项：%s", len(changed), "、".join(changed))
    return {"changed": changed}


def apply_saved() -> int:
    """启动时把存档应用到 config。两张表单独处理。"""
    saved = _saved()
    n = 0
    for key, value in saved.items():
        if key == "AGENT_MODEL" and isinstance(value, dict):
            config.AGENT_MODEL.update({k: v for k, v in value.items()
                                       if k in AGENTS and v in ("pro", "flash")})
            n += 1
        elif key == "AGENT_REASONING" and isinstance(value, dict):
            config.AGENT_REASONING.clear()
            config.AGENT_REASONING.update({k: v for k, v in value.items() if k in AGENTS})
            n += 1
        elif key in BY_KEY:
            f = BY_KEY[key]
            try:
                setattr(config, key, _coerce(f, value))
                n += 1
            except (TypeError, ValueError):
                pass
    return n
