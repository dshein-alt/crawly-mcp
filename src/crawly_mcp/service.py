from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from patchright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import ValidationError

from crawly_mcp.browser import BrowserManager
from crawly_mcp.challenge import resolve_fetch_content
from crawly_mcp.constants import (
    CHALLENGE_SETTLE_TIMEOUT_SECONDS,
    CRAWLY_SEARCH_JITTER_MS_ENV_VAR,
    CRAWLY_TRACE_DIR_ENV_VAR,
    DEFAULT_SEARCH_JITTER_MS,
    FETCH_PAGE_TIMEOUT_SECONDS,
    FETCH_TOTAL_TIMEOUT_SECONDS,
    MAX_HTML_BYTES,
    PROVIDER_HOMEPAGE,
    SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS,
    SEARCH_PAGE_TIMEOUT_SECONDS,
    SEARCH_TOTAL_TIMEOUT_SECONDS,
    TRACE_CAPTURE_TIMEOUT_SECONDS,
    WARMUP_PAGE_TIMEOUT_SECONDS,
)
from crawly_mcp.errors import (
    BrowserUnavailableError,
    InvalidInputError,
    NavigationFailedError,
    ProviderBlockedError,
    TimeoutExceededError,
    URLSafetyError,
    WebSearchError,
)
from crawly_mcp.models import (
    FetchError,
    FetchRequest,
    FetchResponse,
    SearchRequest,
    SearchResponse,
)
from crawly_mcp.parsing import (
    build_search_url,
    extract_search_results,
    search_block_marker,
)
from crawly_mcp.security import URLSafetyGuard


@dataclass(slots=True)
class FetchOutcome:
    url: str
    html: str | None = None
    truncated: bool = False
    error: FetchError | None = None


class SearchTrace:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.network_events: list[dict[str, Any]] = []
        self._pending_tasks: set[asyncio.Task[None]] = set()

        self.provider = ""
        self.query = ""
        self.search_url = ""
        self.first_use = False
        self.warmup_attempted = False
        self.jitter_ms = 0
        self.final_url = ""
        self.final_title = ""
        self.block_marker: str | None = None
        self.results: list[str] = []
        self.error_type: str | None = None
        self.error_message: str | None = None
        self.context_options: dict[str, Any] = {}

    @classmethod
    def create(cls, provider: str, query: str) -> SearchTrace | None:
        root = os.environ.get(CRAWLY_TRACE_DIR_ENV_VAR, "").strip()
        if not root:
            return None
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        stem = _slugify(f"{provider}_{timestamp}_{query}")[:96]
        output_dir = Path(root).expanduser() / stem
        output_dir.mkdir(parents=True, exist_ok=True)
        trace = cls(output_dir)
        trace.provider = provider
        trace.query = query
        return trace

    def attach(self, page: Any) -> None:
        if not hasattr(page, "on"):
            return
        page.on("request", lambda request: self._schedule(self._capture_request(request)))
        page.on("response", lambda response: self._schedule(self._capture_response(response)))
        page.on("requestfailed", lambda request: self._schedule(self._capture_request_failed(request)))
        page.on("popup", self._capture_popup)

    def _schedule(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _capture_popup(self, popup: Any) -> None:
        self.network_events.append(
            {
                "type": "popup",
                "url": getattr(popup, "url", ""),
            }
        )

    async def _capture_request(self, request: Any) -> None:
        headers = await _maybe_all_headers(request)
        self.network_events.append(
            {
                "type": "request",
                "url": getattr(request, "url", ""),
                "method": getattr(request, "method", ""),
                "headers": headers,
                "resource_type": _call_or_get(request, "resource_type"),
            }
        )

    async def _capture_response(self, response: Any) -> None:
        headers = await _maybe_all_headers(response)
        request = getattr(response, "request", None)
        self.network_events.append(
            {
                "type": "response",
                "url": getattr(response, "url", ""),
                "status": getattr(response, "status", None),
                "headers": headers,
                "request_url": getattr(request, "url", None),
                "request_method": getattr(request, "method", None),
            }
        )

    async def _capture_request_failed(self, request: Any) -> None:
        failure = _call_or_get(request, "failure")
        if isinstance(failure, dict):
            error_text = failure.get("errorText")
        else:
            error_text = getattr(failure, "error_text", None) or str(failure) if failure else None
        self.network_events.append(
            {
                "type": "requestfailed",
                "url": getattr(request, "url", ""),
                "method": getattr(request, "method", ""),
                "error_text": error_text,
            }
        )

    async def finalize(self, page: Any, *, html: str | None) -> None:
        if self._pending_tasks:
            with suppress(Exception):
                async with asyncio.timeout(TRACE_CAPTURE_TIMEOUT_SECONDS):
                    await asyncio.gather(*self._pending_tasks, return_exceptions=True)

        final_html = html
        if final_html is None and hasattr(page, "content"):
            with suppress(Exception):
                final_html = await page.content()

        if hasattr(page, "title"):
            with suppress(Exception):
                self.final_title = await page.title()
        self.final_url = getattr(page, "url", self.final_url)

        if hasattr(page, "evaluate"):
            with suppress(Exception):
                fingerprint = await page.evaluate(FINGERPRINT_SNAPSHOT_JS)
                (self.output_dir / "fingerprint.json").write_text(
                    json.dumps(fingerprint, indent=2, sort_keys=True),
                    encoding="utf-8",
                )

        if hasattr(page, "screenshot"):
            with suppress(Exception):
                await page.screenshot(path=str(self.output_dir / "screenshot.png"), full_page=True)

        if final_html is not None:
            (self.output_dir / "page.html").write_text(final_html, encoding="utf-8")

        meta = {
            "tool": "search",
            "provider": self.provider,
            "query": self.query,
            "search_url": self.search_url,
            "first_use": self.first_use,
            "warmup_attempted": self.warmup_attempted,
            "jitter_ms": self.jitter_ms,
            "context_options": self.context_options,
            "final_url": self.final_url,
            "final_title": self.final_title,
            "block_marker": self.block_marker,
            "results": self.results,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (self.output_dir / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        with (self.output_dir / "network.jsonl").open("w", encoding="utf-8") as handle:
            for event in self.network_events:
                handle.write(json.dumps(event, sort_keys=True) + "\n")


FINGERPRINT_SNAPSHOT_JS = """async () => {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    const permissions = navigator.permissions
        ? await navigator.permissions.query({name:'notifications'})
        : null;
    return {
        ua: navigator.userAgent,
        webdriver: navigator.webdriver,
        languages: navigator.languages,
        plugins: navigator.plugins.length,
        vendor: navigator.vendor,
        platform: navigator.platform,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        locale: navigator.language,
        screen: {w: screen.width, h: screen.height, dpr: devicePixelRatio},
        chromeType: typeof window.chrome,
        notificationPermission: typeof Notification !== 'undefined' ? Notification.permission : null,
        permissionsState: permissions ? permissions.state : null,
        webglRenderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
        webglVendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
    };
}"""


class WebSearchService:
    def __init__(self, browser_manager: BrowserManager) -> None:
        self._browser_manager = browser_manager

    async def search(self, *, provider: str | None = None, context: str) -> SearchResponse:
        try:
            request = SearchRequest(provider=provider, context=context)
        except ValidationError as exc:
            logger.warning("search rejected invalid input: {}", exc.errors()[0]["msg"])
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        logger.info("search entry provider={} context={!r}", request.provider, request.context)
        started = time.monotonic()
        trace = SearchTrace.create(request.provider, request.context)

        guard_upfront = URLSafetyGuard()
        search_url = build_search_url(request.provider, request.context)
        await guard_upfront.validate_user_url(search_url)

        # Two timeouts: acquisition outside, per-request work inside.
        try:
            async with asyncio.timeout(SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS):
                handle = await self._browser_manager.search_context(request.provider)
                page = await handle.context.new_page()
        except TimeoutError as exc:
            raise TimeoutExceededError("search context acquisition timed out") from exc

        trace_html: str | None = None
        try:
            return await self._run_search_with_timeout(
                handle=handle, page=page, request=request,
                search_url=search_url, started=started, trace=trace,
            )
        except (BrowserUnavailableError, URLSafetyError, WebSearchError) as exc:
            self._handle_search_error(
                provider=request.provider,
                search_url=search_url,
                trace=trace,
                error=exc,
            )
            raise
        except TimeoutError as exc:
            _record_trace_failure(
                trace,
                error_type="timeout",
                message="search exceeded the overall timeout",
            )
            logger.warning("search timed out provider={}", request.provider)
            raise TimeoutExceededError("search exceeded the overall timeout") from exc
        finally:
            if trace is not None:
                with suppress(Exception):
                    await trace.finalize(page, html=trace_html)
            with suppress(Exception):
                await page.close()
            # Persistent contexts stay cached for cookie continuity; ephemeral
            # ones (CRAWLY_USE_PERSISTENT_PROFILES=false) must be closed here.
            if handle.should_close_context:
                with suppress(Exception):
                    await handle.context.close()

    def _handle_search_error(
        self,
        *,
        provider: str,
        search_url: str,
        trace: SearchTrace | None,
        error: BrowserUnavailableError | URLSafetyError | WebSearchError,
    ) -> None:
        if isinstance(error, BrowserUnavailableError):
            _record_trace_failure(
                trace,
                error_type="browser_unavailable",
                message="browser unavailable",
            )
            logger.error("search failed provider={} reason=browser_unavailable", provider)
            return
        if isinstance(error, URLSafetyError):
            _record_trace_failure(
                trace,
                error_type="unsafe_url",
                message=f"unsafe url: {search_url}",
            )
            logger.warning("search rejected unsafe url={!r}", search_url)
            return
        _record_trace_failure(trace, error_type=error.error_type, message=error.message)
        logger.warning(
            "search failed provider={} type={} message={}",
            provider,
            error.error_type,
            error.message,
        )

    async def _run_search_with_timeout(
        self,
        *,
        handle: Any,
        page: Any,
        request: Any,
        search_url: str,
        started: float,
        trace: SearchTrace | None,
    ) -> SearchResponse:
        async with asyncio.timeout(SEARCH_TOTAL_TIMEOUT_SECONDS):
            if trace is not None:
                trace.search_url = search_url
                trace.first_use = handle.first_use
                trace.context_options = _context_options_for_trace(self._browser_manager)
                trace.attach(page)
            if handle.first_use:
                if trace is not None:
                    trace.warmup_attempted = True
                await self._maybe_warmup(page, request.provider)
            jitter_ms = await self._sleep_jitter()
            if trace is not None:
                trace.jitter_ms = jitter_ms
            try:
                await self._browser_manager.goto(
                    page, search_url, timeout_ms=SEARCH_PAGE_TIMEOUT_SECONDS * 1000,
                )
            except PlaywrightTimeoutError as exc:
                raise TimeoutExceededError("search timed out before the results page loaded") from exc
            except PlaywrightError as exc:
                blocked = handle.guard.pop_blocked_error(page)
                if blocked is not None:
                    raise blocked from exc
                raise NavigationFailedError(f"search navigation failed: {exc}") from exc

            title = await page.title()
            html = await page.content()
            marker = search_block_marker(request.provider, page.url, title, html)
            if trace is not None:
                trace.final_url = page.url
                trace.final_title = title
                trace.block_marker = marker
            self._raise_if_provider_blocked(request.provider, page.url, title, html, marker=marker)

            results = extract_search_results(request.provider, html, page.url)
            if trace is not None:
                trace.results = results
            duration = time.monotonic() - started
            logger.info(
                "search done provider={} results={} final_url={!r} duration={:.2f}s",
                request.provider, len(results), page.url, duration,
            )
            return SearchResponse(urls=results)

    async def _maybe_warmup(self, page: Any, provider: str) -> None:
        try:
            await self._browser_manager.goto(
                page, PROVIDER_HOMEPAGE[provider],
                timeout_ms=WARMUP_PAGE_TIMEOUT_SECONDS * 1000,
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            logger.warning("warmup failed provider={} reason={}", provider, exc)

    async def _sleep_jitter(self) -> int:
        raw = os.environ.get(CRAWLY_SEARCH_JITTER_MS_ENV_VAR)
        if raw:
            try:
                lo, hi = (int(x) for x in raw.split(","))
            except ValueError:
                lo, hi = DEFAULT_SEARCH_JITTER_MS
        else:
            lo, hi = DEFAULT_SEARCH_JITTER_MS
        jitter_ms = int(random.uniform(lo, hi))  # noqa: S311
        await asyncio.sleep(jitter_ms / 1000.0)
        return jitter_ms

    def _raise_if_provider_blocked(
        self, provider: str, final_url: str, title: str, html: str, *, marker: str | None = None
    ) -> None:
        if marker is None:
            marker = search_block_marker(provider, final_url, title, html)
        if marker is None:
            return
        logger.warning(
            "search blocked provider={} marker={!r} final_url={!r} title={!r}",
            provider,
            marker,
            final_url,
            title,
        )
        raise ProviderBlockedError(
            f"search provider returned a consent, CAPTCHA, or challenge page (marker={marker!r})"
        )

    async def fetch(self, *, urls: list[str]) -> FetchResponse:
        try:
            request = FetchRequest(urls=urls)
        except ValidationError as exc:
            logger.warning("fetch rejected invalid input: {}", exc.errors()[0]["msg"])
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        logger.info("fetch entry urls_count={}", len(request.urls))
        started = time.monotonic()

        response = FetchResponse()
        upfront_guard = URLSafetyGuard()
        for url in request.urls:
            await upfront_guard.validate_user_url(url)

        try:
            async with asyncio.timeout(FETCH_TOTAL_TIMEOUT_SECONDS):
                browser_context = await self._browser_manager.new_context()
                guard = URLSafetyGuard()
                await guard.attach(browser_context)
                try:
                    tasks = [
                        asyncio.create_task(self._fetch_one(browser_context, guard, url))
                        for url in request.urls
                    ]
                    for outcome in await asyncio.gather(*tasks):
                        if outcome.error is not None:
                            response.errors[outcome.url] = outcome.error
                            continue
                        if outcome.html is None:
                            response.errors[outcome.url] = FetchError(
                                type="internal_error",
                                message="fetch completed without HTML content",
                            )
                            continue
                        response.pages[outcome.url] = outcome.html
                        if outcome.truncated:
                            response.truncated.append(outcome.url)
                finally:
                    await browser_context.close()
        except BrowserUnavailableError:
            logger.error("fetch failed reason=browser_unavailable")
            raise
        except URLSafetyError:
            logger.warning("fetch rejected by SSRF guard urls={}", request.urls)
            raise
        except TimeoutError as exc:
            logger.warning("fetch timed out urls_count={}", len(request.urls))
            raise TimeoutExceededError("fetch exceeded the overall timeout") from exc

        duration = time.monotonic() - started
        logger.info(
            "fetch done pages={} errors={} truncated={} duration={:.2f}s",
            len(response.pages),
            len(response.errors),
            len(response.truncated),
            duration,
        )
        return response

    async def _fetch_one(
        self,
        browser_context: Any,
        guard: URLSafetyGuard,
        url: str,
    ) -> FetchOutcome:
        page = await browser_context.new_page()
        try:
            try:
                await self._browser_manager.goto(
                    page,
                    url,
                    timeout_ms=FETCH_PAGE_TIMEOUT_SECONDS * 1000,
                )
            except PlaywrightTimeoutError:
                return FetchOutcome(
                    url=url,
                    error=FetchError(type="timeout", message="page load timed out"),
                )
            except PlaywrightError as exc:
                blocked = guard.pop_blocked_error(page)
                if blocked is not None:
                    return FetchOutcome(
                        url=url,
                        error=FetchError(type=blocked.error_type, message=blocked.message),
                    )
                return FetchOutcome(
                    url=url,
                    error=FetchError(type="navigation_failed", message=f"navigation failed: {exc}"),
                )

            try:
                html = await resolve_fetch_content(
                    page,
                    settle_timeout_seconds=CHALLENGE_SETTLE_TIMEOUT_SECONDS,
                )
            except WebSearchError as exc:
                return FetchOutcome(
                    url=url,
                    error=FetchError(type=exc.error_type, message=exc.message),
                )

            content, truncated = truncate_html(html, limit_bytes=MAX_HTML_BYTES)
            return FetchOutcome(url=url, html=content, truncated=truncated)
        finally:
            await page.close()


def truncate_html(html: str, *, limit_bytes: int) -> tuple[str, bool]:
    encoded = html.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return html, False
    truncated = encoded[:limit_bytes]
    return truncated.decode("utf-8", errors="ignore"), True


async def _maybe_all_headers(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "all_headers"):
        with suppress(Exception):
            return await obj.all_headers()
    headers = getattr(obj, "headers", None)
    if callable(headers):
        with suppress(Exception):
            value = headers()
            if isinstance(value, dict):
                return value
    if isinstance(headers, dict):
        return headers
    return {}


def _call_or_get(obj: Any, name: str) -> Any:
    value = getattr(obj, name, None)
    if callable(value):
        with suppress(Exception):
            return value()
    return value


def _context_options_for_trace(browser_manager: Any) -> dict[str, Any]:
    options = {}
    if hasattr(browser_manager, "_context_options"):
        with suppress(Exception):
            options = browser_manager._context_options()
    return options if isinstance(options, dict) else {}


def _record_trace_failure(
    trace: SearchTrace | None,
    *,
    error_type: str,
    message: str,
) -> None:
    if trace is None:
        return
    trace.error_type = error_type
    trace.error_message = message


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug or "trace"
