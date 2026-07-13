"""Playwright 无头 Chromium 浏览器池。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

logger = logging.getLogger(__name__)

# 常见 Chrome User-Agent 池（随机轮换，降低指纹一致性）
CHROME_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
]

# 环境变量优先级：PLAYWRIGHT_CHROMIUM_EXECUTABLE > CHROMIUM_EXECUTABLE_PATH
# PLAYWRIGHT_BROWSERS_PATH 由 Playwright 自身读取，指向 ms-playwright 目录结构
_EXECUTABLE_ENV_KEYS = ("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "CHROMIUM_EXECUTABLE_PATH")

_DOCS_CHROME_REL = (
    Path("docs")
    / "chrome-mac-arm64"
    / "Google Chrome for Testing.app"
    / "Contents"
    / "MacOS"
    / "Google Chrome for Testing"
)


def _project_root() -> Path:
    """项目根目录（src/core/browser.py 的上两级）。"""
    return Path(__file__).resolve().parent.parent.parent


def default_chromium_executable() -> Optional[str]:
    """未设置环境变量时，使用 docs/ 下 Chrome for Testing（若存在）。"""
    candidate = _project_root() / _DOCS_CHROME_REL
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def resolve_chromium_executable(explicit: Optional[str] = None) -> Optional[str]:
    """解析 Chromium 可执行文件路径（构造参数 > 环境变量 > docs 默认），返回绝对路径。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = _project_root() / path
        return str(path.resolve())
    for key in _EXECUTABLE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = _project_root() / path
            return str(path.resolve())
    return default_chromium_executable()


def random_user_agent() -> str:
    """从 UA 池中随机选取一个 Chrome User-Agent。"""
    return random.choice(CHROME_USER_AGENTS)


class BrowserPool:
    """轻量浏览器池：复用 Browser，每次任务新建 Context 以隔离 Cookie。"""

    def __init__(
        self,
        headless: bool = True,
        pool_size: int = 2,
        timeout_ms: int = 30000,
        executable_path: Optional[str] = None,
    ) -> None:
        self.headless = headless
        self.pool_size = max(1, pool_size)
        self.timeout_ms = timeout_ms
        self.executable_path = executable_path
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        chromium_executable = resolve_chromium_executable(self.executable_path)
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
            logger.info("使用本地 Chromium: %s", chromium_executable)
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._semaphore = asyncio.Semaphore(self.pool_size)
        logger.info("BrowserPool 已启动 (headless=%s, pool_size=%d)", self.headless, self.pool_size)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("BrowserPool 已关闭")

    @asynccontextmanager
    async def new_page(
        self,
        user_agent: Optional[str] = None,
        *,
        cookies: Optional[list[dict]] = None,
        extra_http_headers: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[Page]:
        """获取一个隔离的 Page，使用完毕后自动关闭 Context。"""
        if self._browser is None or self._semaphore is None:
            raise RuntimeError("BrowserPool 未启动，请先调用 start()")

        from src.core.site_credentials import get_current_browser_auth

        auth = get_current_browser_auth()
        effective_cookies = cookies if cookies is not None else auth.get("cookies") or []
        effective_headers = (
            extra_http_headers
            if extra_http_headers is not None
            else auth.get("extra_http_headers") or {}
        )

        await self._semaphore.acquire()
        context: Optional[BrowserContext] = None
        try:
            context_kwargs: dict = {
                "user_agent": user_agent or random_user_agent(),
                "locale": "zh-CN",
            }
            if effective_headers:
                context_kwargs["extra_http_headers"] = effective_headers
            context = await self._browser.new_context(**context_kwargs)
            if effective_cookies:
                await context.add_cookies(effective_cookies)
            context.set_default_timeout(self.timeout_ms)
            page = await context.new_page()
            yield page
        finally:
            if context:
                await context.close()
            self._semaphore.release()

    async def fetch_html(self, url: str, wait_until: str = "domcontentloaded") -> str:
        """便捷方法：打开 URL 并返回 HTML。"""
        async with self.new_page() as page:
            await page.goto(url, wait_until=wait_until)
            return await page.content()
