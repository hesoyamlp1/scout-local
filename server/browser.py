"""无头浏览器：只在普通 HTTP 抓不下来的时候才用。

设计里的原话：先试普通 HTTP，只在遇到 403 或者认出 Cloudflare 特征时才上无头浏览器，
绝大多数页面不需要付浏览器的成本。所以这个模块是降级路径，不是主路径。

**全进程共享一个浏览器实例，用锁保护，空闲一段时间自动关掉。**
不这么做的话，每抓一个页面起一个浏览器，几个并发就把内存吃光。

用的是系统里已经装好的 Google Chrome（channel="chrome"），
不另外下载 playwright 自带的那份浏览器，省一百多兆磁盘。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from . import config

log = logging.getLogger("scout.browser")

_playwright = None
_browser = None
_lock = asyncio.Lock()
_last_used = 0.0
_reaper: asyncio.Task | None = None
_unavailable_reason = ""


class BrowserUnavailable(RuntimeError):
    pass


async def _ensure_browser():
    global _playwright, _browser, _reaper, _unavailable_reason
    if _unavailable_reason:
        raise BrowserUnavailable(_unavailable_reason)
    if _browser is not None and _browser.is_connected():
        return _browser
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        _unavailable_reason = f"没装 playwright：{exc}"
        raise BrowserUnavailable(_unavailable_reason) from exc

    try:
        _playwright = await async_playwright().start()
        launch_args = {}
        if config.FETCH_PROXY_URL:
            launch_args["proxy"] = {"server": config.FETCH_PROXY_URL}
        _browser = await _playwright.chromium.launch(
            channel=config.BROWSER_CHANNEL,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            **launch_args,
        )
    except Exception as exc:  # noqa: BLE001
        _unavailable_reason = f"起不来浏览器：{type(exc).__name__}: {exc}"
        log.warning(_unavailable_reason)
        raise BrowserUnavailable(_unavailable_reason) from exc

    log.info("无头浏览器已启动（%s）", config.BROWSER_CHANNEL)
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap_when_idle())
    return _browser


async def _reap_when_idle() -> None:
    """空闲超过设定时间就把浏览器关掉，别让它一直占着内存。"""
    while True:
        await asyncio.sleep(5)
        async with _lock:
            if _browser is None:
                return
            if time.monotonic() - _last_used > config.BROWSER_IDLE_TIMEOUT:
                log.info("无头浏览器空闲 %.0f 秒，关掉", config.BROWSER_IDLE_TIMEOUT)
                await _shutdown_locked()
                return


async def _shutdown_locked() -> None:
    global _browser, _playwright
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _playwright is not None:
            await _playwright.stop()
    except Exception:  # noqa: BLE001
        pass
    _browser = None
    _playwright = None


async def shutdown() -> None:
    async with _lock:
        await _shutdown_locked()


def available() -> bool:
    return config.BROWSER_ENABLED and not _unavailable_reason


async def guard_public_request(route, request, url_validator: Callable[[str], str]) -> None:
    """Playwright 路由门：页面、重定向和子资源发包前都必须仍是公网 URL。"""
    try:
        url_validator(request.url)
    except (TypeError, ValueError):
        await route.abort("blockedbyclient")
        return
    await route.continue_()


async def fetch_html(
    url: str, *, url_validator: Callable[[str], str]
) -> tuple[int, str, str]:
    """用浏览器打开一个页面，返回（状态码，渲染后的 HTML）。

    整个函数在一把锁里，同一时刻只有一个页面在跑。浏览器本来就重，
    并发开页面既不快也容易被反爬识别。
    """
    global _last_used
    async with _lock:
        browser = await _ensure_browser()
        _last_used = time.monotonic()
        context = await browser.new_context(
            user_agent=config.USER_AGENT,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = await context.new_page()

            # 包括主文档重定向和页面子资源；任何一次转向本机、内网、保留地址
            # 或非常规端口都会在浏览器真正发包前被中止。
            await context.route(
                "**/*",
                lambda route, request: guard_public_request(
                    route, request, url_validator
                ),
            )
            resp = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(config.BROWSER_TIMEOUT * 1000),
            )
            status = resp.status if resp else 0
            # Cloudflare 那类挑战页会在几秒内自己跳走，给它一点时间
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=int(config.BROWSER_SETTLE * 1000)
                )
            except Exception:  # noqa: BLE001 —— 等不到就算了，拿当前的
                pass
            final_url = url_validator(page.url)
            html = await page.content()
            return status, html, final_url
        finally:
            _last_used = time.monotonic()
            await context.close()
