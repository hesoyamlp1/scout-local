"""所有可调参数。

**这里只放三类东西**：外部服务的地址和 key、物理约束（并发、超时、预算上限）、
以及"用哪档模型干哪件活"。

**这里不放判断。**旧版本有 131 个参数，其中一大批是"正文短于多少字算空页"
"补读只在前几条里挑""哪些词算用户要整篇"这类东西——那些是判断，现在全部交给模型，
参数也就跟着消失了。往这里加参数之前先问一句：它是物理约束，还是我在替模型做决定？
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """读 .env。不引入 python-dotenv，省一个依赖。已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 模型

# 一家供应商、两档模型。两档都带推理（DeepSeek V4 的两个都是推理模型），
# 差别在推理深度和价格。**别再往这里加"不推理的那档"**：deepseek-chat
# 那个别名已经弃用，用户 2026-08-16 明确要求不再用。
MODEL_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL_PRO = os.environ.get("SCOUT_MODEL_PRO", "deepseek-v4-pro")
MODEL_FLASH = os.environ.get("SCOUT_MODEL_FLASH", "deepseek-v4-flash")

# 谁用哪一档。判据是"判断错了代价多大"：主 agent 判错整轮就废了，用 pro；
# 抓取一轮要派好几个、判断又很局部（这页是登录墙还是短文），用 flash。
# **这是配置不是代码**，随时可以调，调完重启即可。
AGENT_MODEL = {
    "main": "pro",        # 主 agent：跟用户说话、决定派谁
    "research": "pro",    # 搜索：挑哪几个网址打开，判断力值钱
    "process": "pro",     # 处理整篇：翻译质量
    "fetch": "flash",     # 抓取：高频、判断局部
    "find": "flash",      # 找一找：高频、判断简单
    "remember": "flash",  # 记结论：后台跑，不占用户等待
    "profile": "flash",   # 记关于我：定时跑
    "extract": "flash",   # 正文抽要点：全项目调用最多的一处
}

# 推理深度，一个 agent 一档。**不需要判断的活要关掉推理。**
#
# 实测（2026-08-16）：抽要点这一步用默认档，18380 个输出 token 里有 17090 是推理——
# 93% 的钱和时间花在"想"一件根本不用想的事上（按目标从正文里摘要点），
# 一轮下来它一个人占了 127 秒。翻译同理，那是转换不是推断。
#
# `none` 关掉，其余留空表示用模型默认。要判断的地方（主 agent、搜索、抓取、
# 找一找、记结论）一律保留推理——它们的判断质量正是这一版的立身之本。
AGENT_REASONING = {
    "extract": "none",
}
# 注意 process 不在上面：它的 loop 要判断"这一篇全不全、要不要去补"，那需要推理。
# 它内部**逐段翻译**那一步是纯转换，在 translate.py 里单独关掉。

LLM_TIMEOUT = _float("SCOUT_LLM_TIMEOUT", 180.0)
# 推理模型的输出里有一大块是推理过程。**不要给 max_tokens 设小值**：
# 实测 max_tokens=20 时 20 个 token 全被推理吃光，正文返回空。
# 这里给的是上限，正常情况下用不到。
LLM_MAX_TOKENS = _int("SCOUT_LLM_MAX_TOKENS", 8000)

# ---------------------------------------------------------------- 账本

# 一轮问答的总预算。subagent 从这里面划走，**不存在第二套上限**——
# 旧版本有 BRAIN_MAX_ROUNDS 和 RESEARCH_MAX_ROUNDS 两套，必须手工隔离，
# 不隔离就两层相乘跑穿超时。现在只有这一本账。
TURN_TOKEN_BUDGET = _int("SCOUT_TURN_TOKEN_BUDGET", 600_000)
TURN_WALL_SECONDS = _float("SCOUT_TURN_WALL_SECONDS", 600.0)

# 派一个 subagent 时，从父任务的剩余额度里划走多大一块（比例）。
# 划走的是上限不是预付：subagent 没花完，剩下的自动回到父任务手里。
SUBAGENT_BUDGET_SHARE = {
    "research": 0.55,
    "process": 0.55,
    "find": 0.10,
    "fetch": 0.25,
    "remember": 0.10,
    "profile": 0.20,
}

# 每个 agent 自己的循环圈数硬顶。这是防跑飞的，不是调节行为的旋钮——
# 正常情况下 agent 自己就收尾了，撞上这个数说明它绕不出来。
AGENT_MAX_STEPS = {
    "main": _int("SCOUT_MAIN_MAX_STEPS", 12),
    "research": _int("SCOUT_RESEARCH_MAX_STEPS", 10),
    "process": _int("SCOUT_PROCESS_MAX_STEPS", 8),
    "fetch": _int("SCOUT_FETCH_MAX_STEPS", 5),
    "find": _int("SCOUT_FIND_MAX_STEPS", 4),
    "remember": 3,
    "profile": 3,
}

# ---------------------------------------------------------------- 并发

# 同时在跑的 subagent 总数。并发是这一版的核心：读四篇网页就是四个抓取
# 同时跑，墙钟时间等于最慢那个，不是四个之和。
MAX_CONCURRENT_SUBAGENTS = _int("SCOUT_MAX_CONCURRENT_SUBAGENTS", 8)

# ---------------------------------------------------------------- 搜索
#
# 这一段的字段名跟旧版本保持一致，因为 data/settings.json 里存着用户配好的
# key（TAVILY_API_KEY / SERPER_API_KEY），改名会读不到。

# **library 和 dialog 不再是搜索源。**旧版本把知识库和对话历史包装成搜索源，
# 跟联网结果一起融合排序，于是问"什么是幂等"能召回一篇讲书法的东西并挤进前三。
# 召回归「找一找」，跟联网搜索分开呈现，不共用排序。
_ALL_PROVIDERS = ("yahoo_japan", "searxng", "serper", "tavily", "mojeek",
                  "wikipedia", "hackernews", "stackexchange")
_DEFAULT_PROVIDERS = ("yahoo_japan",)
SEARCH_PROVIDERS = [
    p.strip()
    for p in os.environ.get("SCOUT_SEARCH_PROVIDERS", ",".join(_DEFAULT_PROVIDERS)).split(",")
    if p.strip() in _ALL_PROVIDERS
]
SEARCH_MODE = os.environ.get("SCOUT_SEARCH_MODE", "fanout")
SEARCH_WEIGHTS = {
    # 通用搜索源权重给满；专门源略低，避免窄领域结果挤掉正文网页。
    "yahoo_japan": 1.0, "searxng": 1.0, "serper": 1.0,
    "tavily": 0.9, "mojeek": 0.9,
    "wikipedia": 0.8, "hackernews": 0.8, "stackexchange": 0.8,
}

# 自建的 SearXNG。**无 key、无配额**，是为了摆脱付费 API 的配额加进来的。
# 没起的时候留空即可，它会自动跳过。起法见 docs/searxng.md。
SEARXNG_BASE_URL = os.environ.get("SCOUT_SEARXNG_BASE_URL", "http://127.0.0.1:8888")
# 只用某几个上游引擎时填，逗号分隔（比如 "google,duckduckgo,bing"）。留空用它的默认。
SEARXNG_ENGINES = os.environ.get("SCOUT_SEARXNG_ENGINES", "")
# 日本本地搜索不允许悄悄从 Scout 主机直出。线上显式填 socks5://127.0.0.1:1080，
# 本地开发留空时 provider 自动跳过。
YAHOO_JAPAN_PROXY_URL = os.environ.get("SCOUT_YAHOO_JAPAN_PROXY_URL", "")
YAHOO_JAPAN_BASE_URL = os.environ.get(
    "SCOUT_YAHOO_JAPAN_BASE_URL", "https://search.yahoo.co.jp"
)
SEARCH_RRF_K = _int("SCOUT_SEARCH_RRF_K", 10)
SEARCH_TIMEOUT = _float("SCOUT_SEARCH_TIMEOUT", 20.0)
SEARCH_RESULTS_PER_QUERY = _int("SCOUT_SEARCH_RESULTS_PER_QUERY", 8)
SEARCH_CONCURRENCY = _int("SCOUT_SEARCH_CONCURRENCY", 6)
SEARCH_CACHE_TTL = _int("SCOUT_SEARCH_CACHE_TTL", 900)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com")
TAVILY_SEARCH_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "basic")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_BASE_URL = os.environ.get("SERPER_BASE_URL", "https://google.serper.dev")
SERPER_MAX_RESULTS = _int("SCOUT_SERPER_MAX_RESULTS", 10)
SERPER_AUTO_LOCALE = _bool("SCOUT_SERPER_AUTO_LOCALE", True)
SERPER_GL = os.environ.get("SCOUT_SERPER_GL", "")
SERPER_HL = os.environ.get("SCOUT_SERPER_HL", "")
SERPER_TBS = os.environ.get("SCOUT_SERPER_TBS", "")
MOJEEK_MIN_INTERVAL = _float("SCOUT_MOJEEK_MIN_INTERVAL", 2.0)
MOJEEK_BACKOFF = _float("SCOUT_MOJEEK_BACKOFF", 5.0)
MOJEEK_MAX_RETRY = _int("SCOUT_MOJEEK_MAX_RETRY", 2)
MOJEEK_SKIP_IF_BLOCKED_OVER = _int("SCOUT_MOJEEK_SKIP_IF_BLOCKED_OVER", 3)
STACKEXCHANGE_ANSWERS_PER_QUESTION = _int("SCOUT_SE_ANSWERS", 2)

# ---------------------------------------------------------------- 抓取
#
# 这里只剩物理约束。**重试几次、什么时候上浏览器、抓回来的算不算内容**，
# 这三件事以前是参数（FETCH_MAX_RETRY / BROWSER_ENABLED 的触发条件 /
# FETCH_MIN_CONTENT_CHARS），现在归抓取 subagent 判断。

FETCH_TIMEOUT = _float("SCOUT_FETCH_TIMEOUT", 25.0)
FETCH_CONCURRENCY = _int("SCOUT_FETCH_CONCURRENCY", 8)
FETCH_PER_DOMAIN_CONCURRENCY = _int("SCOUT_FETCH_PER_DOMAIN_CONCURRENCY", 2)
FETCH_MAX_BYTES = _int("SCOUT_FETCH_MAX_BYTES", 6_000_000)
# 抓取 subagent 最多重试几次。硬顶，防跑飞用，不是它的判断依据——
# 它自己决定要不要再试，这个数只保证它不会试到天亮。
FETCH_HARD_RETRY_CAP = _int("SCOUT_FETCH_HARD_RETRY_CAP", 4)
FETCH_PROXY_URL = os.environ.get("SCOUT_FETCH_PROXY_URL", "")
PDF_MAX_PAGES = _int("SCOUT_PDF_MAX_PAGES", 60)

BROWSER_ENABLED = _bool("SCOUT_BROWSER_ENABLED", True)
BROWSER_CHANNEL = os.environ.get("SCOUT_BROWSER_CHANNEL", "chromium")
BROWSER_TIMEOUT = _float("SCOUT_BROWSER_TIMEOUT", 35.0)
BROWSER_SETTLE = _float("SCOUT_BROWSER_SETTLE", 1.5)
BROWSER_IDLE_TIMEOUT = _float("SCOUT_BROWSER_IDLE_TIMEOUT", 300.0)

USER_AGENT = os.environ.get(
    "SCOUT_USER_AGENT",
    "Scout/2.0 (+https://github.com/hesoyamlp1/scout-local; personal-reading-assistant)",
)

# 分页链接一页最多认几个。认多了会顺着不相干的链条翻下去。
PAGINATION_MAX_LINKS = _int("SCOUT_PAGINATION_MAX_LINKS", 3)
# 一次"读到末页"最多翻多少页的硬顶。
PAGINATION_HARD_CAP = _int("SCOUT_PAGINATION_HARD_CAP", 40)

# ---------------------------------------------------------------- 上下文

# 送给模型的单条要点上限。原文永远不进上下文，进的是这个。
EXTRACT_MAX_CHARS = _int("SCOUT_EXTRACT_MAX_CHARS", 1800)
# 送去抽要点的正文上限（一次喂给模型多少原文）。超出的分段喂。
EXTRACT_INPUT_MAX_CHARS = _int("SCOUT_EXTRACT_INPUT_MAX_CHARS", 60_000)
# 主 agent 上下文里保留几轮完整历史，再往前压成「结论 + ID」。
HISTORY_FULL_TURNS = _int("SCOUT_HISTORY_FULL_TURNS", 3)
HISTORY_COMPACT_TURNS = _int("SCOUT_HISTORY_COMPACT_TURNS", 20)
HISTORY_ANSWER_MAX_CHARS = _int("SCOUT_HISTORY_ANSWER_MAX_CHARS", 1200)

# 主 agent 边生成边发时，先攒够这么多字再往外送。
# **攒着的这一段还没出门，期间要是冒出工具调用，就把它扔了当无事发生。**
# 字一旦发出去就收不回来——界面那边是往上累加的，不清屏。
STREAM_HOLDBACK = _int("SCOUT_STREAM_HOLDBACK", 24)

# 翻译切段的目标长度。**这个数决定的是对照阅读的粒度**，不只是喂给模型的量：
# 原文段和译文段是一一配对存下来的，界面上"对照"和"在这一段写批注"都锚在段上。
# 实测切成整篇一段（1200 字）时，对照视图是一大坨日文后面跟一大坨中文，
# 根本没法对着读。四百字上下是一屏能同时看见两种语言的粒度。
TRANSLATE_SEGMENT_CHARS = _int("SCOUT_TRANSLATE_SEGMENT_CHARS", 400)
# 单段的硬上限，超了才强切（自然段本来就长的时候）。
TRANSLATE_CHUNK_CHARS = _int("SCOUT_TRANSLATE_CHUNK_CHARS", 900)
TRANSLATE_CONCURRENCY = _int("SCOUT_TRANSLATE_CONCURRENCY", 4)
TRANSLATE_SEGMENT_RETRY = _int("SCOUT_TRANSLATE_SEGMENT_RETRY", 2)

# ---------------------------------------------------------------- 内容库

DATA_DIR = os.environ.get("SCOUT_DATA_DIR", str(ROOT / "data"))
DB_PATH = os.environ.get("SCOUT_DB_PATH", str(Path(DATA_DIR) / "scout.db"))
LOG_DIR = os.environ.get("SCOUT_LOG_DIR", str(ROOT / "logs"))

# 单篇正文的落库上限。**这不是给模型看的量**（模型只看要点），
# 是磁盘上的一道闸，给得很宽。
DOC_MAX_CHARS = _int("SCOUT_DOC_MAX_CHARS", 400_000)
# 结论库上限，超了淘汰最久没被召回的。
FACTS_MAX = _int("SCOUT_FACTS_MAX", 3000)
FACTS_STALE_DAYS = _int("SCOUT_FACTS_STALE_DAYS", 60)
PROFILE_MAX = _int("SCOUT_PROFILE_MAX", 40)

# 记关于我：定时任务每小时跑一次，只处理闲置超过这么久的会话。
PROFILE_IDLE_SECONDS = _float("SCOUT_PROFILE_IDLE_SECONDS", 900.0)
PROFILE_SWEEP_INTERVAL = _float("SCOUT_PROFILE_SWEEP_INTERVAL", 3600.0)

SESSION_TTL = _int("SCOUT_SESSION_TTL", 30 * 86400)
SWEEP_INTERVAL = _int("SCOUT_SWEEP_INTERVAL", 3600)
STORAGE_FLUSH_BATCH = _int("SCOUT_STORAGE_FLUSH_BATCH", 64)
STORAGE_FLUSH_INTERVAL = _float("SCOUT_STORAGE_FLUSH_INTERVAL", 1.0)
STORAGE_BUSY_TIMEOUT_MS = _int("SCOUT_STORAGE_BUSY_TIMEOUT_MS", 5000)

# ---------------------------------------------------------------- 服务

HOST = os.environ.get("SCOUT_HOST", "127.0.0.1")
PORT = _int("SCOUT_PORT", 8080)
# 旧的 Bearer token 保留给命令行/API 客户端；浏览器登录使用下面四项。
AUTH_TOKEN = os.environ.get("SCOUT_AUTH_TOKEN", "")
AUTH_USERNAME = os.environ.get("SCOUT_AUTH_USERNAME", "")
AUTH_PASSWORD_HASH = os.environ.get("SCOUT_AUTH_PASSWORD_HASH", "")
AUTH_SECRET = os.environ.get("SCOUT_AUTH_SECRET", "")
AUTH_COOKIE_NAME = os.environ.get("SCOUT_AUTH_COOKIE_NAME", "scout_session")
AUTH_SESSION_SECONDS = _int("SCOUT_AUTH_SESSION_SECONDS", 30 * 86400)
AUTH_COOKIE_SECURE = _bool("SCOUT_AUTH_COOKIE_SECURE", True)
CODEX_WORKER_ENABLED = _bool("SCOUT_CODEX_WORKER_ENABLED", False)
WORKER_TOKEN = os.environ.get("SCOUT_WORKER_TOKEN", "")
MCP_TOKEN = os.environ.get("SCOUT_MCP_TOKEN", "")
WORKER_MAX_AGE = _int("SCOUT_WORKER_MAX_AGE", 90)
CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
CODEX_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
CODEX_MODEL = os.environ.get("SCOUT_CODEX_MODEL", "gpt-5.6-terra")
if CODEX_MODEL not in CODEX_MODELS:
    CODEX_MODEL = "gpt-5.6-terra"
CODEX_REASONING = os.environ.get("SCOUT_CODEX_REASONING", "medium")
if CODEX_REASONING not in CODEX_REASONING_LEVELS:
    CODEX_REASONING = "medium"
SSE_CONNECTION_SECONDS = _float("SCOUT_SSE_CONNECTION_SECONDS", 8.0)
SSE_PING_SECONDS = _float("SCOUT_SSE_PING_SECONDS", 2.0)


# ---------------------------------------------------------------- 存档的设置
#
# `data/settings.json` 里存着用户在界面上配过的东西，**其中包括搜索引擎的 key**。
# 只认这个模块里已经定义过的名字，别的一律忽略——旧版本存了一堆现在不存在的参数
# （RESEARCH_MAX_ROUNDS 这类），它们的判断已经交给模型了，读进来只会误导。

_SETTABLE = {
    "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_PRO", "MODEL_FLASH",
    "TAVILY_API_KEY", "TAVILY_BASE_URL", "TAVILY_SEARCH_DEPTH",
    "SERPER_API_KEY", "SERPER_BASE_URL", "SERPER_MAX_RESULTS",
    "SERPER_AUTO_LOCALE", "SERPER_GL", "SERPER_HL", "SERPER_TBS",
    "SEARCH_PROVIDERS", "SEARCH_MODE", "SEARCH_WEIGHTS", "SEARCH_RESULTS_PER_QUERY",
    "MOJEEK_MIN_INTERVAL", "LLM_TIMEOUT", "BROWSER_ENABLED", "PDF_MAX_PAGES",
    "TURN_TOKEN_BUDGET", "TURN_WALL_SECONDS", "MAX_CONCURRENT_SUBAGENTS",
    "AGENT_MODEL", "EXTRACT_MAX_CHARS", "TRANSLATE_CHUNK_CHARS",
    "HISTORY_FULL_TURNS", "PROFILE_IDLE_SECONDS",
    "CODEX_MODEL", "CODEX_REASONING",
}

# 旧版本两档通道叫 CHEAP_* / WRITER_*，key 存在那个名字下面。
# 只搬 base_url 和 key，模型名不搬（gpt-5.6-sol 那条通道已经不用了）。
_ALIASES = {"CHEAP_BASE_URL": "MODEL_BASE_URL", "CHEAP_API_KEY": "MODEL_API_KEY"}


def apply_saved() -> int:
    """读 data/settings.json，把认识的项应用到本模块。返回应用了几项。"""
    import json

    path = Path(DATA_DIR) / "settings.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    for key, value in (data or {}).items():
        key = _ALIASES.get(key, key)
        if key not in _SETTABLE or value in (None, ""):
            continue
        cur = globals().get(key)
        if cur is not None and not isinstance(value, type(cur)) and not (
            isinstance(cur, (int, float)) and isinstance(value, (int, float))
        ):
            continue
        if key == "SEARCH_PROVIDERS":
            value = [p for p in value if p in _ALL_PROVIDERS]
            if not value:
                continue
        globals()[key] = value
        n += 1
    return n


_applied = apply_saved()
