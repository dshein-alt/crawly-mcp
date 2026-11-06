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

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from patchright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import ValidationError

from crawly_mcp.browser import BrowserManager
from crawly_mcp.challenge import resolve_fetch_content
from crawly_mcp.constants import (
    ALLOWED_PROVIDERS,
    CHALLENGE_SETTLE_TIMEOUT_SECONDS,
    CRAWLY_FETCH_MAX_SIZE_ENV_VAR,
    CRAWLY_SEARCH_JITTER_MS_ENV_VAR,
    CRAWLY_SEARCH_MAX_PROVIDERS_ENV_VAR,
    CRAWLY_SEARCH_PROVIDER_EXCLUDE_ENV_VAR,
    CRAWLY_SEARCH_PROVIDER_ORDER_ENV_VAR,
    CRAWLY_SEARXNG_URL_ENV_VAR,
    CRAWLY_TRACE_DIR_ENV_VAR,
    DEFAULT_MAX_FETCH_SIZE,
    DEFAULT_SEARCH_JITTER_MS,
    DEFAULT_SEARCH_PROVIDER_ORDER,
    FETCH_PAGE_TIMEOUT_SECONDS,
    FETCH_TOTAL_TIMEOUT_SECONDS,
    MAX_SEARCH_PROVIDERS,
    PROVIDER_FAILURE_THRESHOLD,
    PROVIDER_HOMEPAGE,
    SEARCH_CASCADE_TIMEOUT_SECONDS,
    SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS,
    SEARCH_PAGE_TIMEOUT_SECONDS,
    SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
    SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS,
    TRACE_CAPTURE_TIMEOUT_SECONDS,
    WARMUP_PAGE_TIMEOUT_SECONDS,
    FetchContentFormat,
    NextAction,
    SearchProvider,
    ToolStatus,
)
from crawly_mcp.errors import (
    NEXT_ACTION_BY_TYPE,
    STATUS_BY_TYPE,
    InvalidInputError,
    NavigationFailedError,
    ProviderBlockedError,
    ProviderUnavailableError,
    TimeoutExceededError,
    URLSafetyError,
    WebSearchError,
)
from crawly_mcp.models import (
    FetchError,
    FetchRequest,
    FetchResponse,
    ProviderAttempt,
    ProvidersResponse,
    ProviderStatus,
    SearchRequest,
    SearchResponse,
)
from crawly_mcp.parsing import (
    SearchPageEvidence,
    build_search_url,
    classify_search_page,
    extract_search_results,
)
from crawly_mcp.searxng import searxng_search
from crawly_mcp.security import URLSafetyGuard


@dataclass(slots=True)
class FetchOutcome:
    url: str
    html: str | None = None
    truncated: bool = False
    error: FetchError | None = None


@dataclass(slots=True)
class ProviderHealth:
    """What this process has seen from one provider so far.

    Deliberately not persisted: a provider that is blocked today is not
    blocked next week, and caching a verdict across restarts would turn a
    transient wall into a permanent skip.
    """

    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_status: ToolStatus | None = None
    last_detail: str = ""
    page_kind: str | None = None


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
        self.page_kind: str = ""
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
        page.on(
            "request", lambda request: self._schedule(self._capture_request(request))
        )
        page.on(
            "response",
            lambda response: self._schedule(self._capture_response(response)),
        )
        page.on(
            "requestfailed",
            lambda request: self._schedule(self._capture_request_failed(request)),
        )
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
            error_text = (
                getattr(failure, "error_text", None) or str(failure)
                if failure
                else None
            )
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
                await page.screenshot(
                    path=str(self.output_dir / "screenshot.png"), full_page=True
                )

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
            "page_kind": self.page_kind,
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
        # _http is built lazily on first use and re-created after aclose().
        # The FastMCP streamable-http transport fires `lifespan` per MCP
        # session, so aclose() can run multiple times in a single process;
        # eagerly constructing in __init__ would leave a closed client behind.
        self._http: httpx.AsyncClient | None = None
        self._health: dict[str, ProviderHealth] = {}

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
                max_redirects=3,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is None:
            return
        client = self._http
        self._http = None
        with suppress(Exception):
            await client.aclose()

    async def search(
        self, *, provider: str | None = None, context: str
    ) -> SearchResponse:
        try:
            request = SearchRequest(provider=provider, context=context)
        except ValidationError as exc:
            logger.warning("search rejected invalid input: {}", exc.errors()[0]["msg"])
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        logger.info(
            "search entry provider={} context={!r}", request.provider, request.context
        )
        available = self.available_providers()
        order = self._cascade_order(request.provider)
        skipped = self._health_skipped(available, order)
        logger.info(
            "search cascade order={} available={} skipped={}",
            ",".join(order) or "<none>",
            ",".join(available) or "<none>",
            ",".join(skipped) or "<none>",
        )

        started = time.monotonic()
        deadline = started + SEARCH_CASCADE_TIMEOUT_SECONDS
        attempted: list[ProviderAttempt] = []
        urls: list[str] = []
        winner: SearchProvider | None = None

        for candidate in order:
            budget = min(
                SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
                deadline - time.monotonic(),
            )
            if budget <= 0:
                attempted.append(
                    ProviderAttempt(
                        provider=candidate,
                        status="timeout",
                        detail="cascade budget was spent before this provider was tried",
                    )
                )
                continue
            try:
                async with asyncio.timeout(budget):
                    urls, attempt = await self._attempt_provider(
                        candidate, request.context
                    )
            except TimeoutError:
                logger.warning(
                    "search provider={} gave no answer within {:.0f}s",
                    candidate,
                    budget,
                )
                urls, attempt = (
                    [],
                    ProviderAttempt(
                        provider=candidate,
                        status="timeout",
                        detail=f"provider gave no answer within {budget:.0f}s",
                    ),
                )
            except WebSearchError as exc:
                logger.warning(
                    "search provider={} answered type={} status={} message={}",
                    candidate,
                    exc.error_type,
                    exc.status,
                    exc.message,
                )
                urls, attempt = (
                    [],
                    ProviderAttempt(
                        provider=candidate,
                        status=exc.status,
                        page_kind=exc.page_kind,
                        detail=exc.message,
                    ),
                )
            attempted.append(attempt)
            self._record_health(attempt)

            if attempt.status == "ok" and urls:
                winner = candidate
                break
            if attempt.status == "no_results":
                # A provider that positively reported an empty result set is
                # an answer, not a failure: cascading on top of it would turn
                # "the web has nothing on this" into three more requests.
                break

        duration = time.monotonic() - started
        if winner is not None:
            logger.info(
                "search done provider={} results={} duration={:.2f}s",
                winner,
                len(urls),
                duration,
            )
            return SearchResponse(
                status="ok",
                next_action="none",
                detail=f"{len(urls)} result URL(s) from {winner}",
                urls=urls,
                query=request.context,
                provider=winner,
                attempted=attempted,
                available_providers=available,
                suggested_providers=[],
            )

        logger.info(
            "search exhausted providers={} duration={:.2f}s", len(attempted), duration
        )
        return self._exhausted_response(
            query=request.context,
            attempted=attempted,
            available=available,
            skipped=skipped,
        )

    def _exhausted_response(
        self,
        *,
        query: str,
        attempted: list[ProviderAttempt],
        available: list[SearchProvider],
        skipped: list[SearchProvider],
    ) -> SearchResponse:
        """Report every provider that was tried, and what each one did.

        The distinction that matters lives here: a provider that said "nothing
        found" is reported as `no_results` with `refine_query`, while providers
        that refused or broke are reported as their own status with the
        providers still worth calling named in `suggested_providers`.
        """
        tried = {attempt.provider for attempt in attempted}
        remaining = [p for p in available if p not in tried]
        zero = next((a for a in attempted if a.status == "no_results"), None)
        failed = [a for a in attempted if a.status not in ("ok", "no_results")]

        if zero is not None:
            detail = f"{zero.provider} searched the query and found nothing"
            if failed:
                detail += (
                    f"; {len(failed)} other provider(s) could not answer "
                    f"({', '.join(a.provider for a in failed)})"
                )
            return SearchResponse(
                status="no_results",
                next_action="refine_query",
                detail=detail,
                query=query,
                attempted=attempted,
                available_providers=available,
                suggested_providers=[],
            )

        detail = (
            "; ".join(f"{a.provider}: {a.status} ({a.detail})" for a in attempted)
            or "no provider was available to try"
        )
        if skipped:
            detail += f"; skipped as repeatedly failing: {', '.join(skipped)}"
        if len(available) > len(attempted):
            # The cascade stopped on its budget, not on exhaustion. Say so, so
            # the caller knows that asking for another provider by name can
            # still work rather than concluding nothing else exists.
            detail += (
                f"; the cascade tries at most {self.max_providers()} provider(s) "
                "per call, so calling again with a named provider may work"
            )
        return SearchResponse(
            status=_dominant_status(attempted),
            next_action="try_other_provider" if remaining else "report_to_user",
            detail=detail,
            query=query,
            attempted=attempted,
            available_providers=available,
            suggested_providers=remaining,
        )

    async def _attempt_provider(
        self, provider: SearchProvider, context: str
    ) -> tuple[list[str], ProviderAttempt]:
        if provider == "searxng":
            return await self._attempt_searxng(provider, context)
        return await self._attempt_browser(provider, context)

    async def _attempt_searxng(
        self, provider: SearchProvider, context: str
    ) -> tuple[list[str], ProviderAttempt]:
        instance_url = self._searxng_instance_url()
        if instance_url is None:
            raise InvalidInputError(self._searxng_missing_message())

        logger.info("searxng entry instance={}", instance_url)
        try:
            urls = await searxng_search(
                instance_url,
                context,
                client=self._get_http(),
                timeout=SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            logger.warning("searxng timeout instance={} message={}", instance_url, exc)
            raise TimeoutExceededError(
                f"searxng instance {instance_url!r} timed out"
            ) from exc
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning(
                "searxng instance failed url={} type={} message={}",
                instance_url,
                type(exc).__name__,
                exc,
            )
            raise ProviderUnavailableError(
                f"searxng instance {instance_url!r} request failed: {exc}"
            ) from exc

        logger.info("searxng instance={} results={}", instance_url, len(urls))
        if not urls:
            # A JSON API that answers with an empty list is a trustworthy
            # "nothing found", not a provider failure.
            return [], ProviderAttempt(
                provider=provider,
                status="no_results",
                final_url=instance_url,
                detail="searxng instance answered with no results",
            )
        return urls, ProviderAttempt(
            provider=provider,
            status="ok",
            results=len(urls),
            final_url=instance_url,
            detail=f"{len(urls)} result URL(s)",
        )

    async def _attempt_browser(
        self, provider: SearchProvider, context: str
    ) -> tuple[list[str], ProviderAttempt]:
        trace = SearchTrace.create(provider, context)
        guard_upfront = URLSafetyGuard()
        search_url = build_search_url(provider, context)
        await guard_upfront.validate_user_url(search_url)

        try:
            async with asyncio.timeout(SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS):
                handle = await self._browser_manager.search_context(provider)
                page = await handle.context.new_page()
        except TimeoutError as exc:
            raise TimeoutExceededError("search context acquisition timed out") from exc

        trace_html: str | None = None
        try:
            urls, evidence, final_url = await self._run_search(
                handle=handle,
                page=page,
                provider=provider,
                search_url=search_url,
                trace=trace,
                query=context,
            )
            trace_html = None
            status: ToolStatus = "ok" if urls else "no_results"
            detail = evidence.detail or (
                f"{len(urls)} result URL(s)"
                if urls
                else "results page had nothing to show"
            )
            return urls, ProviderAttempt(
                provider=provider,
                status=status,
                page_kind=evidence.kind,
                final_url=final_url,
                results=len(urls),
                detail=detail,
            )
        except WebSearchError as exc:
            _record_trace_failure(trace, error_type=exc.error_type, message=exc.message)
            raise
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

    async def _run_search(
        self,
        *,
        handle: Any,
        page: Any,
        provider: str,
        search_url: str,
        trace: SearchTrace | None,
        query: str = "",
    ) -> tuple[list[str], SearchPageEvidence, str]:
        """Navigate one provider and say what the page actually was.

        A wall raises a block, a page that never reached a results view raises
        an unavailable, and only a page that states it found nothing is
        returned as an empty answer.
        """
        await self._prepare_provider_page(handle, page, provider, search_url, trace)
        await self._navigate_provider(handle, page, search_url)

        title = await page.title()
        html = await page.content()
        evidence = classify_search_page(provider, page.url, title, html, query=query)
        self._record_probe_trace(trace, page, title, evidence)
        logger.info(
            "search probe provider={} page_kind={} anchors={} opaque={} "
            "text_chars={} marker={!r} final_url={!r}",
            provider,
            evidence.kind,
            evidence.result_anchors,
            evidence.opaque_anchors,
            evidence.text_chars,
            evidence.marker,
            page.url,
        )
        return self._verdict_from_evidence(provider, html, page.url, evidence, trace)

    async def _prepare_provider_page(
        self,
        handle: Any,
        page: Any,
        provider: str,
        search_url: str,
        trace: SearchTrace | None,
    ) -> None:
        if trace is not None:
            trace.search_url = search_url
            trace.first_use = handle.first_use
            trace.context_options = _context_options_for_trace(self._browser_manager)
            trace.attach(page)
        if handle.first_use:
            if trace is not None:
                trace.warmup_attempted = True
            await self._maybe_warmup(page, provider)
        jitter_ms = await self._sleep_jitter()
        if trace is not None:
            trace.jitter_ms = jitter_ms

    async def _navigate_provider(self, handle: Any, page: Any, search_url: str) -> None:
        try:
            await self._browser_manager.goto(
                page,
                search_url,
                timeout_ms=SEARCH_PAGE_TIMEOUT_SECONDS * 1000,
            )
        except PlaywrightTimeoutError as exc:
            raise TimeoutExceededError(
                "search timed out before the results page loaded"
            ) from exc
        except PlaywrightError as exc:
            blocked = handle.guard.pop_blocked_error(page)
            if blocked is not None:
                raise blocked from exc
            raise NavigationFailedError(f"search navigation failed: {exc}") from exc

    @staticmethod
    def _record_probe_trace(
        trace: SearchTrace | None, page: Any, title: str, evidence: SearchPageEvidence
    ) -> None:
        if trace is None:
            return
        trace.final_url = page.url
        trace.final_title = title
        trace.block_marker = evidence.marker
        trace.page_kind = evidence.kind

    def _verdict_from_evidence(
        self,
        provider: str,
        html: str,
        final_url: str,
        evidence: SearchPageEvidence,
        trace: SearchTrace | None,
    ) -> tuple[list[str], SearchPageEvidence, str]:
        if evidence.kind in ("captcha", "consent"):
            raise ProviderBlockedError(evidence.detail, page_kind=evidence.kind)
        if evidence.kind in ("shell", "unknown"):
            raise ProviderUnavailableError(evidence.detail, page_kind=evidence.kind)
        if evidence.kind == "zero_results":
            return [], evidence, final_url

        urls = extract_search_results(provider, html, final_url)
        if not urls:
            raise ProviderUnavailableError(
                evidence.detail or "results page exposed no extractable result URLs",
                page_kind=evidence.kind,
            )
        if trace is not None:
            trace.results = urls
        return urls, evidence, final_url

    async def _maybe_warmup(self, page: Any, provider: str) -> None:
        try:
            await self._browser_manager.goto(
                page,
                PROVIDER_HOMEPAGE[provider],
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

    # --- provider availability, order, and health ---

    def _searxng_instance_url(self) -> str | None:
        if not self._searxng_configured():
            return None
        pinned = (os.environ.get(CRAWLY_SEARXNG_URL_ENV_VAR) or "").strip()
        return pinned if pinned.endswith("/") else pinned + "/"

    def _searxng_configured(self) -> bool:
        """Whether SearXNG is usable here, refusing a misconfigured URL loudly.

        A non-http scheme is an operator mistake that no other provider can
        paper over, so it raises rather than degrading into a cascade.
        """
        pinned = (os.environ.get(CRAWLY_SEARXNG_URL_ENV_VAR) or "").strip()
        if not pinned:
            return False
        if not pinned.startswith(("https://", "http://")):
            raise InvalidInputError(
                f"{CRAWLY_SEARXNG_URL_ENV_VAR} must be http(s)://; got {pinned!r}"
            )
        return True

    @staticmethod
    def _searxng_missing_message() -> str:
        return (
            f"provider='searxng' requires {CRAWLY_SEARXNG_URL_ENV_VAR} to be set "
            f"to the URL of a SearXNG instance with JSON output enabled."
        )

    def provider_order(self) -> list[SearchProvider]:
        """The configured priority order, with excluded providers removed.

        Operators own this: `CRAWLY_SEARCH_PROVIDER_ORDER` sets the priority,
        `CRAWLY_SEARCH_PROVIDER_EXCLUDE` removes providers from the service
        entirely. Exclusion wins over ordering, so a provider named in both is
        gone rather than merely last.
        """
        excluded = self.excluded_providers()
        raw = (os.environ.get(CRAWLY_SEARCH_PROVIDER_ORDER_ENV_VAR) or "").strip()
        configured = self._parse_provider_list(
            raw, CRAWLY_SEARCH_PROVIDER_ORDER_ENV_VAR
        ) or list(DEFAULT_SEARCH_PROVIDER_ORDER)
        return [provider for provider in configured if provider not in excluded]

    def excluded_providers(self) -> frozenset[SearchProvider]:
        raw = (os.environ.get(CRAWLY_SEARCH_PROVIDER_EXCLUDE_ENV_VAR) or "").strip()
        return frozenset(
            self._parse_provider_list(raw, CRAWLY_SEARCH_PROVIDER_EXCLUDE_ENV_VAR)
        )

    def max_providers(self) -> int:
        """How many providers one search call may try; 1 disables cascading."""
        raw = (os.environ.get(CRAWLY_SEARCH_MAX_PROVIDERS_ENV_VAR) or "").strip()
        if not raw:
            return MAX_SEARCH_PROVIDERS
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "invalid {}={!r}; using default {}",
                CRAWLY_SEARCH_MAX_PROVIDERS_ENV_VAR,
                raw,
                MAX_SEARCH_PROVIDERS,
            )
            return MAX_SEARCH_PROVIDERS
        if value < 1:
            logger.warning(
                "non-positive {}={!r}; using default {}",
                CRAWLY_SEARCH_MAX_PROVIDERS_ENV_VAR,
                raw,
                MAX_SEARCH_PROVIDERS,
            )
            return MAX_SEARCH_PROVIDERS
        return value

    @staticmethod
    def _parse_provider_list(raw: str, env_var: str) -> list[SearchProvider]:
        parsed: list[SearchProvider] = []
        for part in raw.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in ALLOWED_PROVIDERS:
                logger.warning("ignoring unknown provider {!r} in {}", name, env_var)
                continue
            provider: SearchProvider = name
            if provider not in parsed:
                parsed.append(provider)
        return parsed

    def available_providers(self) -> list[SearchProvider]:
        searxng_ready = self._searxng_configured()
        available: list[SearchProvider] = []
        for provider in self.provider_order():
            if provider == "searxng" and not searxng_ready:
                continue
            if provider not in available:
                available.append(provider)
        return available

    def _cascade_order(self, requested: SearchProvider | None) -> list[SearchProvider]:
        available = self.available_providers()
        limit = self.max_providers()
        if requested is not None and requested not in available:
            if requested in self.excluded_providers():
                raise InvalidInputError(
                    f"provider {requested!r} is excluded by "
                    f"{CRAWLY_SEARCH_PROVIDER_EXCLUDE_ENV_VAR} in this deployment; "
                    f"available providers: {', '.join(available) or 'none'}"
                )
            if requested == "searxng" and requested in self.provider_order():
                raise InvalidInputError(self._searxng_missing_message())
            raise InvalidInputError(
                f"provider {requested!r} is not available in this deployment; "
                f"available providers: {', '.join(available) or 'none'}"
            )

        order: list[SearchProvider] = [requested] if requested else []
        for provider in available:
            if provider in order:
                continue
            if len(order) >= limit:
                break
            if order and self._is_degraded(provider):
                continue
            order.append(provider)
        return order[:limit]

    def _health_skipped(
        self,
        available: list[SearchProvider],
        order: list[SearchProvider],
    ) -> list[SearchProvider]:
        return [p for p in available if p not in order and self._is_degraded(p)]

    def _is_degraded(self, provider: SearchProvider) -> bool:
        health = self._health.get(provider)
        return bool(
            health and health.consecutive_failures >= PROVIDER_FAILURE_THRESHOLD
        )

    def _record_health(self, attempt: ProviderAttempt) -> None:
        health = self._health.setdefault(attempt.provider, ProviderHealth())
        health.last_status = attempt.status
        health.last_detail = attempt.detail
        health.page_kind = attempt.page_kind
        if attempt.status in ("ok", "no_results"):
            # Reporting an honest empty result set is a working provider.
            health.successes += 1
            health.consecutive_failures = 0
            return
        health.failures += 1
        health.consecutive_failures += 1

    def providers(self) -> ProvidersResponse:
        """Report what can be searched with, before anyone pays for a call.

        Every provider the service knows about is listed, including excluded
        and unconfigured ones, each with the reason it is not usable: an
        operator changing priority or excluding a provider needs to see the
        setting take effect, not infer it from a missing row.
        """
        available = self.available_providers()
        excluded = self.excluded_providers()
        configured = self.provider_order()
        known = list(configured) + [p for p in sorted(excluded) if p not in configured]
        searxng_configured = self._searxng_configured()
        statuses: list[ProviderStatus] = []
        for provider in known:
            health = self._health.get(provider)
            is_available = provider in available
            is_excluded = provider in excluded
            statuses.append(
                ProviderStatus(
                    provider=provider,
                    available=is_available,
                    configured=(provider != "searxng" or searxng_configured),
                    status=health.last_status if health else None,
                    detail=self._provider_detail(
                        provider,
                        health=health,
                        is_available=is_available,
                        is_excluded=is_excluded,
                        searxng_configured=searxng_configured,
                    ),
                    successes=health.successes if health else 0,
                    failures=health.failures if health else 0,
                    skipped=self._is_degraded(provider),
                    excluded=is_excluded,
                )
            )
        unusable = [s.provider for s in statuses if not s.available and not s.excluded]
        degraded = [s.provider for s in statuses if s.skipped]
        detail = f"{len(available)} of {len(statuses)} providers are usable"
        if unusable:
            detail += f"; not usable: {', '.join(unusable)}"
        if excluded:
            detail += f"; excluded by configuration: {', '.join(sorted(excluded))}"
        if degraded:
            detail += f"; failing repeatedly: {', '.join(degraded)}"
        return ProvidersResponse(
            browser_ready=self._browser_manager.is_ready,
            searxng_configured=searxng_configured,
            cascade_order=available,
            max_providers=self.max_providers(),
            providers=statuses,
            detail=detail,
        )

    def _provider_detail(
        self,
        provider: SearchProvider,
        *,
        health: ProviderHealth | None,
        is_available: bool,
        is_excluded: bool,
        searxng_configured: bool,
    ) -> str:
        if health:
            return health.last_detail
        if is_excluded:
            return (
                f"excluded by {CRAWLY_SEARCH_PROVIDER_EXCLUDE_ENV_VAR}; "
                "never tried, even when requested by name"
            )
        if is_available:
            return "not called yet in this process"
        if provider == "searxng" and not searxng_configured:
            return self._searxng_missing_message()
        return (
            f"not in {CRAWLY_SEARCH_PROVIDER_ORDER_ENV_VAR}; "
            "add it to the priority order to use it"
        )

    async def fetch(
        self,
        *,
        urls: list[str],
        content_format: FetchContentFormat = "html",
    ) -> FetchResponse:
        try:
            request = FetchRequest(urls=urls, content_format=content_format)
        except ValidationError as exc:
            logger.warning("fetch rejected invalid input: {}", exc.errors()[0]["msg"])
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        logger.info("fetch entry urls_count={}", len(request.urls))
        started = time.monotonic()

        response = FetchResponse(
            status="ok",
            next_action="none",
            content_format=request.content_format,
        )
        # A browser that will not start is a real infrastructure failure and
        # stays an error; everything below the browser is per-URL.
        browser_context = await self._browser_manager.new_context()
        guard = URLSafetyGuard()
        await guard.attach(browser_context)
        outcomes: list[FetchOutcome] = []
        try:
            tasks = {
                asyncio.create_task(
                    self._fetch_one(
                        browser_context,
                        guard,
                        url,
                        content_format=request.content_format,
                    )
                ): url
                for url in request.urls
            }
            _, pending = await asyncio.wait(tasks, timeout=FETCH_TOTAL_TIMEOUT_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task, url in tasks.items():
                if task in pending:
                    outcomes.append(
                        FetchOutcome(
                            url=url,
                            error=FetchError(
                                type="timeout",
                                message=(
                                    f"fetch budget of "
                                    f"{FETCH_TOTAL_TIMEOUT_SECONDS}s was spent "
                                    "before this URL finished"
                                ),
                                next_action="retry_later",
                            ),
                        )
                    )
                    continue
                outcomes.append(task.result())
        finally:
            await browser_context.close()

        for outcome in outcomes:
            if outcome.error is not None:
                response.errors[outcome.url] = outcome.error
                continue
            if outcome.html is None:
                response.errors[outcome.url] = FetchError(
                    type="internal_error",
                    message="fetch completed without HTML content",
                    next_action="retry_later",
                )
                continue
            response.pages[outcome.url] = outcome.html
            if outcome.truncated:
                response.truncated.append(outcome.url)

        response.succeeded = len(response.pages)
        response.failed = len(response.errors)
        response.status, response.next_action, response.detail = _fetch_verdict(
            requested=len(request.urls),
            succeeded=response.succeeded,
            errors=response.errors,
            truncated=len(response.truncated),
        )

        duration = time.monotonic() - started
        logger.info(
            "fetch done status={} pages={} errors={} truncated={} duration={:.2f}s",
            response.status,
            response.succeeded,
            response.failed,
            len(response.truncated),
            duration,
        )
        return response

    async def _fetch_one(
        self,
        browser_context: Any,
        guard: URLSafetyGuard,
        url: str,
        *,
        content_format: FetchContentFormat,
    ) -> FetchOutcome:
        """Fetch one URL, and never let it take the batch down with it.

        Safety refusals, DNS failures, timeouts, and challenge walls all land
        in this URL's own error entry, each naming the move that would fix it.
        """
        budget = FETCH_PAGE_TIMEOUT_SECONDS + CHALLENGE_SETTLE_TIMEOUT_SECONDS + 5
        try:
            async with asyncio.timeout(budget):
                return await self._fetch_one_inner(
                    browser_context, guard, url, content_format=content_format
                )
        except TimeoutError:
            logger.warning("fetch url={!r} exceeded its {}s budget", url, budget)
            return FetchOutcome(
                url=url,
                error=FetchError(
                    type="timeout",
                    message=f"page did not finish within {budget}s",
                    next_action="retry_later",
                ),
            )
        except Exception as exc:  # one URL must never fail the whole batch
            logger.warning(
                "fetch url={!r} failed type={} message={}", url, type(exc).__name__, exc
            )
            return FetchOutcome(
                url=url,
                error=FetchError(
                    type="navigation_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    next_action="retry_later",
                ),
            )

    async def _fetch_one_inner(
        self,
        browser_context: Any,
        guard: URLSafetyGuard,
        url: str,
        *,
        content_format: FetchContentFormat,
    ) -> FetchOutcome:
        try:
            await URLSafetyGuard().validate_user_url(url)
        except URLSafetyError as exc:
            return FetchOutcome(
                url=url,
                error=FetchError(
                    type=exc.error_type,
                    message=f"{url}: {exc.message}",
                    next_action=exc.next_action,
                ),
            )

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
                    error=FetchError(
                        type="timeout",
                        message="page load timed out",
                        next_action="retry_later",
                    ),
                )
            except PlaywrightError as exc:
                blocked = guard.pop_blocked_error(page)
                if blocked is not None:
                    return FetchOutcome(
                        url=url,
                        error=FetchError(
                            type=blocked.error_type,
                            message=f"{url}: {blocked.message}",
                            next_action=blocked.next_action,
                        ),
                    )
                return FetchOutcome(
                    url=url,
                    error=FetchError(
                        type="navigation_failed",
                        message=f"navigation failed: {exc}",
                        next_action="retry_later",
                    ),
                )

            try:
                html = await resolve_fetch_content(
                    page,
                    settle_timeout_seconds=CHALLENGE_SETTLE_TIMEOUT_SECONDS,
                )
            except WebSearchError as exc:
                return FetchOutcome(
                    url=url,
                    error=FetchError(
                        type=exc.error_type,
                        message=exc.message,
                        next_action=exc.next_action,
                    ),
                )

            content = render_fetch_content(
                html,
                content_format=content_format,
            )
            content, truncated = truncate_content(
                content, limit_bytes=resolve_fetch_max_size()
            )
            return FetchOutcome(url=url, html=content, truncated=truncated)
        finally:
            await page.close()


def _fetch_verdict(
    *,
    requested: int,
    succeeded: int,
    errors: dict[str, FetchError],
    truncated: int,
) -> tuple[ToolStatus, NextAction, str]:
    """Say what a fetch batch achieved, in one status and one instruction.

    Partial success is the normal shape of this tool, so a batch with some
    failures and some pages is `ok`: the caller has the data it can get and a
    per-URL list of what it did not get. Only a batch with nothing in it is a
    failure, and then the dominant per-URL reason decides the status.
    """
    if succeeded and not errors:
        detail = f"{succeeded} of {requested} URL(s) returned"
        if truncated:
            detail += f"; {truncated} truncated to the size cap"
        return "ok", "none", detail
    if succeeded and errors:
        failed = ", ".join(errors)
        return (
            "ok",
            "none",
            f"{succeeded} of {requested} URL(s) returned; "
            f"{len(errors)} failed ({failed}) and each names its own reason",
        )

    types = {error.type for error in errors.values()}
    dominant = next(
        (
            t
            for t in (
                "blocked_target",
                "invalid_url",
                "challenge_blocked",
                "timeout",
                "navigation_failed",
            )
            if t in types
        ),
        next(iter(types), "internal_error"),
    )
    status = STATUS_BY_TYPE.get(dominant, "unavailable")
    next_action = NEXT_ACTION_BY_TYPE.get(dominant, "report_to_user")
    return (
        status,
        next_action,
        f"none of the {requested} URL(s) returned content; dominant reason: {dominant}",
    )


def _dominant_status(attempted: list[ProviderAttempt]) -> ToolStatus:
    """Report the most informative status a cascade saw.

    A provider that refused is more actionable than a provider that timed out,
    which is more actionable than one whose layout moved.
    """
    seen = {attempt.status for attempt in attempted}
    for status in (
        "blocked",
        "unavailable",
        "navigation_failed",
        "timeout",
        "unsafe_url",
        "invalid_input",
    ):
        if status in seen:
            return status
    return "unavailable"


def resolve_fetch_max_size() -> int:
    raw = os.environ.get(CRAWLY_FETCH_MAX_SIZE_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MAX_FETCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid {}={!r}; using default {}",
            CRAWLY_FETCH_MAX_SIZE_ENV_VAR,
            raw,
            DEFAULT_MAX_FETCH_SIZE,
        )
        return DEFAULT_MAX_FETCH_SIZE
    if value <= 0:
        logger.warning(
            "non-positive {}={!r}; using default {}",
            CRAWLY_FETCH_MAX_SIZE_ENV_VAR,
            raw,
            DEFAULT_MAX_FETCH_SIZE,
        )
        return DEFAULT_MAX_FETCH_SIZE
    return value


def render_fetch_content(html: str, *, content_format: FetchContentFormat) -> str:
    if content_format == "text":
        return extract_readable_text(html)
    return html


def extract_readable_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(
        ["script", "style", "noscript", "svg", "canvas", "iframe", "template"]
    ):
        tag.decompose()

    root = soup.find("main") or soup.find("article") or soup.body or soup
    for tag in root.find_all(["nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    parts: list[str] = []

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if title:
        parts.append(f"Title: {title}")

    description = ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if meta is not None:
        description = (meta.get("content") or "").strip()
    if description:
        parts.append(f"Description: {description}")

    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in root.get_text(separator="\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)

    if lines:
        parts.append("Text:\n" + "\n".join(lines))

    return "\n\n".join(parts).strip()


def truncate_content(content: str, *, limit_bytes: int) -> tuple[str, bool]:
    encoded = content.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return content, False
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
