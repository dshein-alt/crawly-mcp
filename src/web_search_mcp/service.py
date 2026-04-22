from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import ValidationError

from web_search_mcp.browser import BrowserManager
from web_search_mcp.challenge import resolve_fetch_content
from web_search_mcp.constants import (
    CHALLENGE_SETTLE_TIMEOUT_SECONDS,
    FETCH_PAGE_TIMEOUT_SECONDS,
    FETCH_TOTAL_TIMEOUT_SECONDS,
    MAX_HTML_BYTES,
    SEARCH_PAGE_TIMEOUT_SECONDS,
    SEARCH_TOTAL_TIMEOUT_SECONDS,
)
from web_search_mcp.errors import (
    BrowserUnavailableError,
    InvalidInputError,
    NavigationFailedError,
    ProviderBlockedError,
    TimeoutExceededError,
    URLSafetyError,
    WebSearchError,
)
from web_search_mcp.models import (
    FetchError,
    FetchRequest,
    FetchResponse,
    SearchRequest,
    SearchResponse,
)
from web_search_mcp.parsing import (
    build_search_url,
    extract_search_results,
    is_search_blocked,
)
from web_search_mcp.security import URLSafetyGuard


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
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

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
                        blocked = guard.pop_blocked_error()
                        if blocked is not None:
                            raise blocked from exc
                        raise NavigationFailedError(f"search navigation failed: {exc}") from exc

                    title = await page.title()
                    html = await page.content()
                    if is_search_blocked(request.provider, page.url, title, html):
                        raise ProviderBlockedError("search provider returned a consent, CAPTCHA, or challenge page")

                    return SearchResponse(urls=extract_search_results(request.provider, html, page.url))
                finally:
                    await browser_context.close()
        except BrowserUnavailableError:
            raise
        except URLSafetyError:
            raise
        except WebSearchError:
            raise
        except TimeoutError as exc:
            raise TimeoutExceededError("search exceeded the overall timeout") from exc

    async def fetch(self, *, urls: list[str]) -> FetchResponse:
        try:
            request = FetchRequest(urls=urls)
        except ValidationError as exc:
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

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
            raise
        except URLSafetyError:
            raise
        except TimeoutError as exc:
            raise TimeoutExceededError("fetch exceeded the overall timeout") from exc

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
                blocked = guard.pop_blocked_error()
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
