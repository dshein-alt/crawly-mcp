from __future__ import annotations

import asyncio
import os
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import patchright.async_api as playwright_api
from loguru import logger
from patchright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
)

from crawly_mcp.constants import (
    ALLOWED_BROWSER_SOURCES,
    BROWSER_SOURCE_SYSTEM,
    CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR,
    CRAWLY_PROFILE_DIR_ENV_VAR,
    CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR,
    CRAWLY_USE_XVFB_ENV_VAR,
    DEFAULT_PROFILE_DIR,
    DEFAULT_PROFILE_MAX_AGE_DAYS,
    DEFAULT_TIMEZONE_ID,
    MAX_CONCURRENT_NAVIGATIONS,
    PLAYWRIGHT_BROWSER_SOURCE_ENV_VAR,
    STANDARD_HEADERS,
    STANDARD_USER_AGENT,
    SYSTEM_CHROMIUM_ENV_VAR,
)
from crawly_mcp.errors import BrowserUnavailableError
from crawly_mcp.security import URLSafetyGuard


@dataclass(frozen=True, slots=True)
class SearchContextHandle:
    context: BrowserContext
    guard: URLSafetyGuard
    first_use: bool


class BrowserManager:
    def __init__(self, *, max_concurrent_navigations: int = MAX_CONCURRENT_NAVIGATIONS) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._navigation_semaphore = asyncio.Semaphore(max_concurrent_navigations)
        self._search_contexts: dict[str, BrowserContext] = {}
        self._search_guards: dict[str, URLSafetyGuard] = {}

    async def start(self) -> None:
        await self._cleanup_stale_profiles()
        await self._ensure_browser()

    async def new_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        return await browser.new_context(**self._context_options())

    def _context_options(self) -> dict[str, Any]:
        tz = os.environ.get("TZ") or DEFAULT_TIMEZONE_ID
        return {
            "user_agent": STANDARD_USER_AGENT,
            "locale": "en-US",
            "timezone_id": tz,
            "viewport": {"width": 1366, "height": 768},
            "java_script_enabled": True,
            "extra_http_headers": STANDARD_HEADERS,
        }

    async def goto(self, page: Page, url: str, *, timeout_ms: int) -> None:
        async with self._navigation_semaphore:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    async def close(self) -> None:
        async with self._lock:
            for provider, ctx in list(self._search_contexts.items()):
                with suppress(Exception):
                    await ctx.close()
                self._search_contexts.pop(provider, None)
                self._search_guards.pop(provider, None)
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def search_context(self, provider: str) -> SearchContextHandle:
        async with self._lock:
            cached = self._search_contexts.get(provider)
            if cached is not None and not cached.is_closed():
                return SearchContextHandle(
                    context=cached,
                    guard=self._search_guards[provider],
                    first_use=False,
                )
            if cached is not None:
                # Stale; drop it and recreate.
                self._search_contexts.pop(provider, None)
                self._search_guards.pop(provider, None)

            await self._ensure_playwright_started()
            ctx = await self._create_persistent_context(provider)
            guard = URLSafetyGuard()
            await guard.attach(ctx)
            self._search_contexts[provider] = ctx
            self._search_guards[provider] = guard
            return SearchContextHandle(context=ctx, guard=guard, first_use=True)

    async def _cleanup_stale_profiles(self) -> None:
        if os.environ.get(CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR, "").lower() not in ("1", "true", "yes"):
            return

        profile_parent = Path(
            os.environ.get(CRAWLY_PROFILE_DIR_ENV_VAR, DEFAULT_PROFILE_DIR)
        ).expanduser()
        if not profile_parent.is_dir():
            return

        max_age_days = int(
            os.environ.get(CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR, str(DEFAULT_PROFILE_MAX_AGE_DAYS))
        )
        threshold = time.time() - max_age_days * 24 * 3600

        deleted = 0
        reclaimed = 0
        for entry in profile_parent.iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime >= threshold:
                    continue
                size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
                shutil.rmtree(entry)
                deleted += 1
                reclaimed += size
            except OSError as exc:
                logger.warning("profile cleanup failed entry={} error={}", entry, exc)
        if deleted:
            logger.info("profile cleanup deleted={} reclaimed_bytes={}", deleted, reclaimed)

    async def _ensure_playwright_started(self) -> None:
        if self._playwright is None:
            self._playwright = await playwright_api.async_playwright().start()

    async def _create_persistent_context(self, provider: str) -> BrowserContext:
        profile_parent = Path(
            os.environ.get(CRAWLY_PROFILE_DIR_ENV_VAR, DEFAULT_PROFILE_DIR)
        ).expanduser()
        user_data_dir = profile_parent / provider
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return await self._playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            **self._launch_options(),
            **self._context_options(),
        )

    def _launch_options(self) -> dict[str, Any]:
        """Launch options shared by both the incognito Browser and each
        persistent context. Keep this dict free of keys that overlap with
        _context_options() (user_agent, locale, timezone_id, viewport,
        java_script_enabled, extra_http_headers); those two dicts get
        merged via **unpack at call sites."""
        args = ["--disable-dev-shm-usage"]
        xvfb = os.environ.get(CRAWLY_USE_XVFB_ENV_VAR, "").lower() in ("1", "true", "yes")
        if not xvfb:
            args.append("--headless=new")
        # headless=False in both branches is intentional: under Xvfb the
        # browser is headed inside the virtual display; under --headless=new
        # the arg drives headlessness and Playwright's `headless` kwarg
        # would force legacy headless if set to True.
        return {"headless": False, "args": args}

    async def _xvfb_preflight(self) -> None:
        if os.environ.get(CRAWLY_USE_XVFB_ENV_VAR, "").lower() not in ("1", "true", "yes"):
            return
        if not os.environ.get("DISPLAY"):
            raise BrowserUnavailableError(
                "CRAWLY_USE_XVFB=true but DISPLAY is not set; "
                "use scripts/run-with-xvfb.sh as the entrypoint"
            )

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            await self._xvfb_preflight()
            source = resolve_browser_source()
            logger.info("chromium starting source={}", source)
            try:
                if self._playwright is None:
                    self._playwright = await playwright_api.async_playwright().start()
                launch_options = self._launch_options()
                if source == BROWSER_SOURCE_SYSTEM:
                    launch_options["executable_path"] = resolve_chromium_executable()
                self._browser = await self._playwright.chromium.launch(**launch_options)
                self._browser.on("disconnected", self._handle_disconnect)
                logger.info("chromium ready source={}", source)
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
        logger.warning("chromium disconnected; will relaunch on next request")
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
