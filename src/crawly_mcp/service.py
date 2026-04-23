from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
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
    FETCH_PAGE_TIMEOUT_SECONDS,
    FETCH_TOTAL_TIMEOUT_SECONDS,
    MAX_HTML_BYTES,
    SEARCH_PAGE_TIMEOUT_SECONDS,
    SEARCH_TOTAL_TIMEOUT_SECONDS,
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


class WebSearchService:
    def __init__(self, browser_manager: BrowserManager) -> None:
        self._browser_manager = browser_manager

    async def search(self, *, provider: str | None = None, context: str) -> SearchResponse:
        try:
            request = SearchRequest(provider=provider, context=context)
        except ValidationError as exc:
            logger.warning("search rejected invalid input: {}", exc.errors()[0]["msg"])
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        logger.info(
            "search entry provider={} context={!r}", request.provider, request.context
        )
        started = time.monotonic()

        guard = URLSafetyGuard()
        search_url = build_search_url(request.provider, request.context)
        await guard.validate_user_url(search_url)

        try:
            async with asyncio.timeout(SEARCH_TOTAL_TIMEOUT_SECONDS):
                browser_context = await self._browser_manager.new_context()
                await guard.attach(browser_context)
                try:
                    page = await browser_context.new_page()
                    try:
                        await self._browser_manager.goto(
                            page,
                            search_url,
                            timeout_ms=SEARCH_PAGE_TIMEOUT_SECONDS * 1000,
                        )
                    except PlaywrightTimeoutError as exc:
                        raise TimeoutExceededError("search timed out before the results page loaded") from exc
                    except PlaywrightError as exc:
                        blocked = guard.pop_blocked_error(page)
                        if blocked is not None:
                            raise blocked from exc
                        raise NavigationFailedError(f"search navigation failed: {exc}") from exc

                    title = await page.title()
                    html = await page.content()
                    self._raise_if_provider_blocked(request.provider, page.url, title, html)

                    results = extract_search_results(request.provider, html, page.url)
                    duration = time.monotonic() - started
                    logger.info(
                        "search done provider={} results={} final_url={!r} duration={:.2f}s",
                        request.provider,
                        len(results),
                        page.url,
                        duration,
                    )
                    return SearchResponse(urls=results)
                finally:
                    await browser_context.close()
        except BrowserUnavailableError:
            logger.error("search failed provider={} reason=browser_unavailable", request.provider)
            raise
        except URLSafetyError:
            logger.warning("search rejected unsafe url={!r}", search_url)
            raise
        except WebSearchError as exc:
            logger.warning(
                "search failed provider={} type={} message={}",
                request.provider,
                exc.error_type,
                exc.message,
            )
            raise
        except TimeoutError as exc:
            logger.warning("search timed out provider={}", request.provider)
            raise TimeoutExceededError("search exceeded the overall timeout") from exc

    def _raise_if_provider_blocked(
        self, provider: str, final_url: str, title: str, html: str
    ) -> None:
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
