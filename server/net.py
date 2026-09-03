"""网络原子工具：把网页取回来，把 HTML 变成文本。

**这一层只报告事实，一个决定都不做。** 抓回来是 403 还是 200、正文抽出多少字、
页面里有没有反爬挑战的特征、有没有下一页——全部如实交出去，
**要不要重试、要不要换浏览器、这算不算抓到了，由抓取 subagent 判断。**

旧版本这些判断写在这里：`FETCH_MAX_RETRY` 决定重试几次，
`FETCH_MIN_CONTENT_CHARS` 一个数字决定"这页算不算空"，于是登录墙、
cookie 同意页、JS 渲染页、真短文四种情况全归成一句"正文太短"。
那是判断被写死成了配置项。现在参数只剩物理约束（超时、并发、字节上限）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import io
import logging
import re
import socket
import urllib.parse
from dataclasses import dataclass, field

import httpx
import trafilatura
from bs4 import BeautifulSoup

from . import browser, config

log = logging.getLogger("scout.net")

_global_sem: asyncio.Semaphore | None = None
_domain_sems: dict[str, asyncio.Semaphore] = {}
_client: httpx.AsyncClient | None = None

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# 反爬挑战页的特征。这些页面状态码往往是 200，正文里只有一句"正在验证你是不是人类"。
# **这是事实检测不是判断**：认出特征就如实说"页面里有这些字样"，
# 至于要不要因此换浏览器，是 subagent 的事。
_CHALLENGE = re.compile(
    r"(cf-browser-verification|cf_chl_opt|__cf_chl|checking your browser|"
    r"just a moment\.\.\.|enable javascript and cookies to continue|"
    r"ddos-guard|请开启 ?javascript|verifying you are human|"
    r"attention required! \| cloudflare|incapsula incident id)",
    re.I,
)
# 登录墙 / 付费墙的特征，同样只是如实报告。
_WALL = re.compile(
    r"(sign in to continue|log ?in to (?:read|continue|view)|subscribe to (?:read|continue)|"
    r"members only|会員限定|ログインしてください|请登录|登录后查看|付费阅读|"
    r"this content is for subscribers)",
    re.I,
)


def _sem() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
    return _global_sem


def _domain_sem(host: str) -> asyncio.Semaphore:
    sem = _domain_sems.get(host)
    if sem is None:
        sem = asyncio.Semaphore(config.FETCH_PER_DOMAIN_CONCURRENCY)
        _domain_sems[host] = sem
    return sem


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            proxy=config.FETCH_PROXY_URL or None,
            timeout=httpx.Timeout(config.FETCH_TIMEOUT, connect=10.0),
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "application/pdf;q=0.8,*/*;q=0.7",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,ja;q=0.7",
            },
            # 重定向必须由 http_get 逐跳验证，不能交给 httpx 自动跟随。
            follow_redirects=False,
        )
    return _client


def public_url(url: str) -> str:
    """只接受当前解析到公网地址的普通 HTTP(S) URL。

    调用方必须对初始地址和每一次重定向都调用本函数。浏览器路径还会对页面
    发出的每一个子请求重复检查，避免公开入口把抓取器带进本机、内网或云元数据地址。
    """
    raw = (url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("只允许公开的 http/https URL")
    if parts.username or parts.password:
        raise ValueError("URL 不能包含用户名或密码")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL 端口不合法") from exc
    if port not in (None, 80, 443):
        raise ValueError("只允许 80/443 端口")
    host = parts.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("不允许本机或内网地址")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError("域名解析失败") from exc
    if not infos:
        raise ValueError("域名没有可用地址")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast,
                ip.is_reserved, ip.is_unspecified)):
            raise ValueError("不允许访问内网或保留地址")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                                    parts.query, ""))


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
    await browser.shutdown()


@dataclass
class Raw:
    """取回来的东西，以及关于它的事实。**没有 ok 字段**——算不算成功是判断，不在这一层。"""

    url: str
    status: int | None = None
    via: str = "http"                 # http / browser / pdf
    error: str = ""                   # 网络层面真的没拿到东西时才有
    ctype: str = ""
    bytes_len: int = 0
    html: str = ""
    # 抽取结果
    text: str = ""                    # 常规正文抽取
    title: str = ""
    list_text: str = ""               # 列表页抽取（榜单、目录）
    list_items: int = 0
    dense_text: str = ""              # DOM 里的低链接密度连续正文候选
    dense_selector: str = ""
    dense_links: int = 0
    page_links: list = field(default_factory=list)
    # 事实标记
    challenge: bool = False
    wall: bool = False
    next_links: list = field(default_factory=list)

    def best_text(self) -> tuple[str, str]:
        """正文和它的来源。正文短而列表抽出东西时给列表——但两个字段都还在，
        subagent 看得见原始情况，可以自己判断这一页到底是文章还是榜单。"""
        if len(self.text.strip()) >= 200 or not self.list_text:
            return self.text, "article"
        return self.list_text, "list"

    def report(self, *, head_chars: int = 400) -> str:
        """交给模型看的事实清单。**给正文开头一段**——模型看到"请登录后查看"
        就知道这是登录墙，看到正常的第一段就知道抓对了。光给字数它判断不了。"""
        body, kind = self.best_text()
        bits = [f"网址：{self.url}"]
        if self.error:
            bits.append(f"没拿到东西：{self.error}")
            return "\n".join(bits)
        bits.append(f"HTTP {self.status}（走的是{ {'http': '普通请求', 'browser': '浏览器', 'pdf': 'PDF'}.get(self.via, self.via)}）")
        bits.append(f"正文抽出 {len(self.text.strip())} 字" +
                    (f"；列表路径能抽出 {self.list_items} 条、{len(self.list_text)} 字"
                     if self.list_text else ""))
        if self.challenge:
            bits.append("⚠ 页面里有反爬挑战的字样（Cloudflare / 人机验证那一类）")
        if self.wall:
            bits.append("⚠ 页面里有登录墙或付费墙的字样")
        if self.next_links:
            bits.append("这一页有「下一页」链接：" +
                        "；".join(u for u, _ in self.next_links[:3]))
        if body.strip():
            head = re.sub(r"\s+", " ", body.strip())[:head_chars]
            bits.append(f"开头是这样（{'榜单条目' if kind == 'list' else '正文'}）：\n{head}…")
        else:
            bits.append("一个字都没抽出来。")
        return "\n".join(bits)


# ---------------------------------------------------------------- 解码


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def decode_html(body: bytes, ctype: str = "") -> str:
    """把字节按正确的编码解成文本。

    四步找编码，**每一步都真解一遍验证**（声明成 gb2312 实际是 gbk 的站点是存在的）：
    HTTP 头 → HTML meta → 自动检测 → utf-8 兜底。

    这一步做错的代价是静默的：旧版本一律按 utf-8 加 `errors="ignore"` 解，
    日文站（Shift_JIS）解出来是乱码，解不出的字节被默默丢掉——
    实测青空文库《人間失格》44286 字只剩 21614 字，而且不可读。
    """
    def _try(enc: str | None) -> str | None:
        if not enc:
            return None
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return None

    m = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_\-]+)", ctype or "", re.I)
    if m and (out := _try(m.group(1))) is not None:
        return out
    m2 = _META_CHARSET.search(body[:4096])
    if m2 and (out := _try(m2.group(1).decode("ascii", "ignore"))) is not None:
        return out
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body[:200_000]).best()
        if best is not None and (out := _try(best.encoding)) is not None:
            return out
    except Exception as exc:  # noqa: BLE001
        log.debug("编码自动检测没成：%s", exc)
    return body.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- 抽取


def extract_article(html: str, url: str) -> tuple[str, str]:
    """常规正文抽取。返回（正文，标题）。"""
    try:
        text = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=True,
            favor_precision=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("正文抽取出错 %s：%s", url, exc)
        text = None
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta is not None and meta.title:
            title = meta.title
    except Exception:  # noqa: BLE001
        pass
    return (text or "").strip(), title


def _node_text(node) -> str:
    """保留旧站常用 <br> 的行结构，同时去掉空行和连续重复行。"""
    try:
        root = BeautifulSoup(str(node), "lxml")
    except Exception:  # noqa: BLE001
        return ""
    for tag in root(list(_LIST_DROP)):
        tag.decompose()
    lines: list[str] = []
    for raw in root.get_text("\n", strip=True).splitlines():
        line = _norm(raw)
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def extract_dense_article(html: str) -> tuple[str, str, int]:
    """找一个连续、长、低链接密度的 DOM 正文候选。

    旧式 table 布局常把正文和期数导航包在同一张大表里；通用抽取会把整张表
    Markdown 化。这里不判断“它是不是正文”，只产出事实更干净的候选给上层比较。
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return "", "", 0
    candidates: list[tuple[float, int, str, str, int]] = []
    for order, node in enumerate(soup.select("article, main, [role=main], section, div, td")):
        text = _node_text(node)
        chars = len(text)
        if chars < 400 or chars > config.FETCH_MAX_BYTES:
            continue
        links = node.find_all("a", href=True)
        link_chars = sum(len(_norm(link.get_text(" ", strip=True))) for link in links)
        link_ratio = link_chars / max(1, chars)
        # 链接个数比链接文字占比更能识别“正文 + 十几期期数导航”的旧 table 壳。
        score = chars * max(0.05, (1 - min(link_ratio, 0.95)) ** 4) - len(links) * 80
        selector = node.name
        if node.get("id"):
            selector += f"#{node.get('id')}"
        elif node.get("class"):
            selector += "." + ".".join(str(value) for value in node.get("class")[:3])
        candidates.append((score, -order, selector, text, len(links)))
    if not candidates:
        return "", "", 0
    _score, _order, selector, text, links = max(candidates)
    return text, selector, links


def extract_page_links(html: str, base_url: str) -> list[dict]:
    """把页面链接事实交给 Codex；不判断哪些属于章节或系列。"""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return []
    base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for anchor in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base_url, (anchor.get("href") or "").strip())
        parts = urllib.parse.urlsplit(href)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                                       parts.query, ""))
        text = _norm(anchor.get_text(" ", strip=True) or anchor.get("title") or "")
        if not text:
            image = anchor.find("img")
            text = _norm((image or {}).get("alt") if image else "")
        if not text:
            continue
        identity = (text, url)
        if identity in seen:
            continue
        seen.add(identity)
        out.append({
            "text": text[:180], "url": url,
            "same_host": parts.hostname.lower() == base_host,
        })
        if len(out) >= 200:
            break
    return out


def content_shape(text: str) -> dict:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    table_lines = sum(1 for line in lines if line.startswith("|") and line.endswith("|"))
    return {
        "chars": len(text or ""),
        "lines": len(lines),
        "table_lines": table_lines,
        "head": " / ".join(lines[:2])[:220],
        "tail": " / ".join(lines[-4:])[:300],
    }


_LIST_DROP = ("script", "style", "noscript", "template", "svg",
              "nav", "header", "footer", "aside", "form", "select")
_TRUNCATED = re.compile(r"(\S+?)(?:…|\.\.\.)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _item_text(item) -> str:
    """一个列表条目的可见文本。被模板截断的标题用 <a title> 补回来。"""
    text = _norm(item.get_text(" ", strip=True))
    for a in item.find_all("a"):
        title = _norm(a.get("title") or "")
        if not title or title in text:
            continue
        m = _TRUNCATED.search(_norm(a.get_text(" ", strip=True)))
        if m and len(m.group(1)) >= 2 and title.startswith(m.group(1)):
            text = text.replace(m.group(0), title, 1)
    return text


def extract_list(html: str) -> tuple[str, int]:
    """列表页抽取：榜单、目录这类整页链接列表。返回（正文，条目数）。

    常规抽取库看的是文字密度，整页链接列表在它眼里就是导航栏、会被整块丢掉。
    **这里不判断"该不该走列表路径"**，只负责"能抽出多少条"，交给 subagent 看着办。
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # noqa: BLE001
        log.warning("列表页解析出错：%s", exc)
        return "", 0

    for tag in soup(list(_LIST_DROP)):
        tag.decompose()

    containers = soup.find_all(["ul", "ol", "table", "dl"])
    picked: list[tuple[int, str, list[str]]] = []
    taken: set[int] = set()

    # 从里往外看：嵌套的列表先收，外面那层布局壳因为里面已经收过就跳过。
    for order in range(len(containers) - 1, -1, -1):
        c = containers[order]
        if any(id(d) in taken for d in c.find_all(["ul", "ol", "table", "dl"])):
            continue
        if c.name in ("ul", "ol"):
            items = c.find_all("li", recursive=False)
        elif c.name == "dl":
            items = c.find_all("dd", recursive=False)
        else:
            items = c.find_all("tr")
        rows = [t for t in (_item_text(i) for i in items) if t]
        if len(rows) < 4 or sum(len(t) for t in rows) / max(1, len(rows)) < 8:
            continue
        taken.add(id(c))
        node = c.find_previous(["h1", "h2", "h3", "h4"])
        heading = _norm(node.get_text(" ", strip=True)) if node else ""
        picked.append((order, heading if len(heading) <= 60 else "", rows))

    picked.sort(key=lambda x: x[0])
    lines: list[str] = []
    seen: set[str] = set()
    total = 0
    last_heading = ""
    for _, heading, rows in picked:
        kept = [t for t in rows if not (t in seen or seen.add(t))][:200]
        if not kept:
            continue
        if heading and heading != last_heading:
            lines.append(f"## {heading}")
            last_heading = heading
        lines.extend(f"- {t}" for t in kept)
        lines.append("")
        total += len(kept)
    return "\n".join(lines).strip(), total


def extract_pdf(data: bytes) -> tuple[str, str]:
    try:
        import pypdf
    except ImportError:
        return "", ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages[: config.PDF_MAX_PAGES]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        text = "\n\n".join(c.strip() for c in chunks if c.strip())
        title = ""
        try:
            if reader.metadata and reader.metadata.title:
                title = str(reader.metadata.title)
        except Exception:  # noqa: BLE001
            pass
        return re.sub(r"\n{3,}", "\n\n", text).strip(), title
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF 解析失败：%s", exc)
        return "", ""


# ---------------------------------------------------------------- 分页

_NEXT_LABEL = re.compile(
    r"^\s*(?:次のページ|次ページ|次の頁|次へ|つぎへ|"
    r"下一页|下一頁|下页|下頁|后一页|後一頁|下一节|下一節|"
    r"next(?:\s*page)?|older\s+(?:posts?|entries))\s*[»>›→]?\s*$", re.I,
)
_CONTINUE_LABEL = re.compile(
    r"^\s*(?:続き(?:を読む|はこちら)?|つづき|继续|繼續|続きへ|"
    r"continue(?:\s+reading)?|more|\d{1,3})\s*[»>›→]?\s*$", re.I,
)
_PAGE_SEG = re.compile(r"^(?P<base>.*)/page/(?P<no>\d+)/?$")
_PAGE_TAIL = re.compile(r"^(?P<base>.+?)/(?P<no>\d+)/?$")
_PAGE_IN_QUERY = ("page", "paged", "p", "pg")
_MAX_PAGE_NO = 999


def _split_page(url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    no, rest = 0, []
    for k, v in query:
        if no == 0 and k.lower() in _PAGE_IN_QUERY and v.isdigit():
            no = int(v)
            continue
        rest.append((k, v))
    if no:
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(rest), "")
        ), no
    for pattern in (_PAGE_SEG, _PAGE_TAIL):
        m = pattern.match(parts.path)
        if m and 0 < int(m.group("no")) <= _MAX_PAGE_NO:
            return urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, m.group("base"), parts.query, "")
            ), int(m.group("no"))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, "")
    ), 1


def _same_site(a: str, b: str) -> bool:
    def host(u: str) -> str:
        h = (urllib.parse.urlsplit(u).netloc or "").lower()
        return h[4:] if h.startswith("www.") else h
    return bool(host(a)) and host(a) == host(b)


def find_next_links(html: str, url: str) -> list[tuple[str, str]]:
    """认出这一页的「下一页」。**只认往后不认往前**，两个方向都跟会在两页之间绕圈。

    三档，可信度从高到低：`rel="next"`；文字明确写着"次のページ"这类只有一个意思的；
    文字是"続き"或光秃秃一个页码——这一档**还要网址形状对得上**（基址相同、页码加一），
    因为"続きを読む"在文章页是下一页，在目录页是另一篇文章的入口。
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return []
    tiers: list[list[tuple[str, str]]] = [[], [], []]
    seen: set[str] = set()
    here = urllib.parse.urldefrag(url)[0].rstrip("/")
    for node in soup.find_all("link", href=True) + soup.find_all("a", href=True):
        href = (node.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        target = urllib.parse.urldefrag(urllib.parse.urljoin(url, href))[0]
        if not target.startswith("http") or not _same_site(url, target):
            continue
        if target.rstrip("/") == here or target in seen:
            continue
        rel = " ".join(node.get("rel") or []).lower()
        label = _norm(node.get_text(" ", strip=True)) if node.name == "a" else ""
        if "next" in rel.split():
            tier = 0
        elif label and _NEXT_LABEL.match(label):
            tier = 1
        elif label and _CONTINUE_LABEL.match(label):
            cur_base, cur_no = _split_page(url)
            cand_base, cand_no = _split_page(target)
            if not (cur_base.rstrip("/") == cand_base.rstrip("/") and cand_no == cur_no + 1):
                continue
            tier = 2
        else:
            continue
        seen.add(target)
        tiers[tier].append((target, label or "rel=next"))
    out: list[tuple[str, str]] = []
    for tier in tiers:
        for item in tier:
            if len(out) >= config.PAGINATION_MAX_LINKS:
                return out
            out.append(item)
    return out


# ---------------------------------------------------------------- 取


async def http_get(url: str) -> Raw:
    """发一次逻辑 HTTP 请求；每一次跳转都重新校验公网地址。"""
    try:
        current = public_url(url)
    except ValueError as exc:
        return Raw(url, error=f"地址被安全策略拦截：{exc}")
    resp = None
    for redirect_count in range(6):
        host = (urllib.parse.urlsplit(current).netloc or "").lower()
        async with _sem():
            async with _domain_sem(host):
                try:
                    client = await _get_client()
                    resp = await client.get(current)
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    return Raw(current, error="超时")
                except Exception as exc:  # noqa: BLE001
                    return Raw(current, error=f"连不上（{type(exc).__name__}）")
        if resp.status_code not in _REDIRECT_STATUSES:
            break
        location = (resp.headers.get("location") or "").strip()
        if not location:
            break
        if redirect_count >= 5:
            return Raw(current, status=resp.status_code, error="重定向次数超过 5 次")
        target = urllib.parse.urljoin(current, location)
        try:
            current = public_url(target)
        except ValueError as exc:
            return Raw(current, status=resp.status_code,
                       error=f"重定向被安全策略拦截：{exc}")

    assert resp is not None

    raw = Raw(current, status=resp.status_code,
              ctype=(resp.headers.get("content-type") or "").lower())
    body = resp.content[: config.FETCH_MAX_BYTES]
    raw.bytes_len = len(body)

    if "pdf" in raw.ctype or current.lower().endswith(".pdf"):
        raw.via = "pdf"
        raw.text, raw.title = extract_pdf(body)
        return raw
    if raw.ctype and not any(t in raw.ctype for t in ("html", "text", "xml", "json")):
        raw.error = f"不是网页（{raw.ctype}）"
        return raw

    raw.html = decode_html(body, raw.ctype)
    _fill(raw)
    return raw


async def browser_get(url: str) -> Raw:
    """用无头浏览器取。页面靠 JS 渲染、或者被普通请求挡住时，这条路有机会。"""
    if not config.BROWSER_ENABLED:
        return Raw(url, via="browser", error="浏览器没开（BROWSER_ENABLED=0）")
    try:
        safe = public_url(url)
        status, html, final_url = await asyncio.wait_for(
            browser.fetch_html(safe, url_validator=public_url),
            timeout=config.BROWSER_TIMEOUT + config.BROWSER_SETTLE + 10,
        )
    except browser.BrowserUnavailable as exc:
        return Raw(url, via="browser", error=f"浏览器用不了：{exc}")
    except Exception as exc:  # noqa: BLE001
        return Raw(url, via="browser", error=f"浏览器抓取失败：{type(exc).__name__}")
    raw = Raw(final_url, status=status or 200, via="browser", html=html)
    _fill(raw)
    return raw


def _fill(raw: Raw) -> None:
    """从 HTML 里把该报告的事实都取出来。"""
    raw.challenge = bool(_CHALLENGE.search(raw.html[:200_000]))
    raw.wall = bool(_WALL.search(raw.html[:200_000]))
    raw.text, raw.title = extract_article(raw.html, raw.url)
    raw.dense_text, raw.dense_selector, raw.dense_links = extract_dense_article(raw.html)
    raw.page_links = extract_page_links(raw.html, raw.url)
    if len(raw.text.strip()) < 400:
        raw.list_text, raw.list_items = extract_list(raw.html)
    raw.next_links = find_next_links(raw.html, raw.url)
