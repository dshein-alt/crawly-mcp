from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import suppress
from pathlib import Path

import playwright.async_api as playwright_api
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
)

from crawly_mcp.constants import (
    ALLOWED_BROWSER_SOURCES,
    BROWSER_SOURCE_SYSTEM,
    MAX_CONCURRENT_NAVIGATIONS,
    PLAYWRIGHT_BROWSER_SOURCE_ENV_VAR,
    STANDARD_HEADERS,
    STANDARD_USER_AGENT,
    SYSTEM_CHROMIUM_ENV_VAR,
)
from crawly_mcp.errors import BrowserUnavailableError


class BrowserManager:
    def __init__(self, *, max_concurrent_navigations: int = MAX_CONCURRENT_NAVIGATIONS) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._navigation_semaphore = asyncio.Semaphore(max_concurrent_navigations)

    async def start(self) -> None:
        await self._ensure_browser()

    async def new_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        return await browser.new_context(
            user_agent=STANDARD_USER_AGENT,
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1366, "height": 768},
            java_script_enabled=True,
            extra_http_headers=STANDARD_HEADERS,
        )

    async def goto(self, page: Page, url: str, *, timeout_ms: int) -> None:
        async with self._navigation_semaphore:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            try:
                if self._playwright is None:
                    self._playwright = await playwright_api.async_playwright().start()
                launch_options = {"headless": True, "args": ["--disable-dev-shm-usage"]}
                if resolve_browser_source() == BROWSER_SOURCE_SYSTEM:
                    launch_options["executable_path"] = resolve_chromium_executable()
                self._browser = await self._playwright.chromium.launch(**launch_options)
                self._browser.on("disconnected", self._handle_disconnect)
            except PlaywrightError as exc:
                await self._shutdown_playwright()
                browser_source = resolve_browser_source()
                if browser_source == BROWSER_SOURCE_SYSTEM:
                    hint = (
                        "failed to start system Chromium; set "
                        f"`{SYSTEM_CHROMIUM_ENV_VAR}` to the Chromium binary path if needed"
                    )
                else:
                    hint = "failed to start bundled Playwright Chromium"
                raise BrowserUnavailableError(
                    hint
                ) from exc
            except Exception:
                await self._shutdown_playwright()
                raise

            return self._browser

    def _handle_disconnect(self) -> None:
        self._browser = None

    async def _shutdown_playwright(self) -> None:
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None


def resolve_chromium_executable() -> str:
    configured = os.environ.get(SYSTEM_CHROMIUM_ENV_VAR)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise BrowserUnavailableError(
            f"{SYSTEM_CHROMIUM_ENV_VAR} points to a missing file: {configured}"
        )

    discovered = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if discovered:
        return discovered

    raise BrowserUnavailableError(
        "system Chromium was not found in PATH; install Chromium or set "
        f"{SYSTEM_CHROMIUM_ENV_VAR}"
    )


def resolve_browser_source() -> str:
    source = os.environ.get(PLAYWRIGHT_BROWSER_SOURCE_ENV_VAR, BROWSER_SOURCE_SYSTEM).strip().lower()
    if not source:
        return BROWSER_SOURCE_SYSTEM
    if source in ALLOWED_BROWSER_SOURCES:
        return source
    allowed = ", ".join(ALLOWED_BROWSER_SOURCES)
    raise BrowserUnavailableError(
        f"{PLAYWRIGHT_BROWSER_SOURCE_ENV_VAR} must be one of: {allowed}"
    )
