from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from crawly_mcp.browser import SearchContextHandle
from crawly_mcp.constants import PROVIDER_HOMEPAGE
from crawly_mcp.errors import ChallengeBlockedError
from crawly_mcp.security import URLSafetyGuard  # used for URL validation only
from crawly_mcp.service import WebSearchService, truncate_html

FIXTURES = Path(__file__).parent / "fixtures"


class FakePage:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.url = "about:blank"

    async def title(self) -> str:
        return self.spec.get("title", "")

    async def content(self) -> str:
        return self.spec.get("html", "")

    async def close(self) -> None:
        return None


class FakeContext:
    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        self.specs = specs
        self.pages: list[FakePage] = []

    async def route(self, pattern: str, handler: Callable[..., object]) -> None:
        del pattern, handler

    async def new_page(self) -> FakePage:
        page = FakePage({})
        self.pages.append(page)
        return page

    async def close(self) -> None:
        return


class FakeBrowserManager:
    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        self.specs = specs
        self.context = FakeContext(specs)

    async def new_context(self) -> FakeContext:
        return self.context

    async def search_context(self, provider: str) -> SearchContextHandle:
        del provider

        class _PassthroughGuard:
            def pop_blocked_error(self, page: object) -> None:
                return None

        return SearchContextHandle(
            context=self.context, guard=_PassthroughGuard(), first_use=False,
        )

    async def goto(self, page: FakePage, url: str, *, timeout_ms: int) -> None:
        del timeout_ms
        if url not in self.specs:
            # warmup or unknown URL — silently ignore
            return
        page.url = url
        page.spec = self.specs[url]


@pytest.mark.asyncio
async def test_search_returns_extracted_urls_from_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (FIXTURES / "duckduckgo_results.html").read_text(encoding="utf-8")
    browser = FakeBrowserManager(
        {
            "https://duckduckgo.com/?q=python&ia=web": {
                "title": "python at DuckDuckGo",
                "html": html,
            }
        }
    )
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr("crawly_mcp.service.URLSafetyGuard.validate_user_url", allow_all)

    result = await service.search(context="python")

    assert result.urls == [
        "https://example.com/alpha",
        "https://example.org/beta",
        "https://example.net/gamma",
    ]


@pytest.mark.asyncio
async def test_fetch_returns_partial_success_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = ["https://example.com/ok", "https://example.com/challenge"]
    browser = FakeBrowserManager({url: {"title": "title", "html": "<html></html>"} for url in urls})
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr("crawly_mcp.service.URLSafetyGuard.validate_user_url", allow_all)

    async def fake_resolve_fetch_content(page: FakePage, *, settle_timeout_seconds: float) -> str:
        del settle_timeout_seconds
        if page.url.endswith("/challenge"):
            raise ChallengeBlockedError("page stayed on a browser challenge screen")
        return "x" * 12

    monkeypatch.setattr("crawly_mcp.service.resolve_fetch_content", fake_resolve_fetch_content)
    monkeypatch.setattr("crawly_mcp.service.MAX_HTML_BYTES", 10)

    result = await service.fetch(urls=urls)

    assert result.pages == {"https://example.com/ok": "x" * 10}
    assert result.errors["https://example.com/challenge"].type == "challenge_blocked"
    assert result.truncated == ["https://example.com/ok"]


def test_truncate_html_marks_oversized_payloads() -> None:
    html, truncated = truncate_html("hello world", limit_bytes=5)

    assert html == "hello"
    assert truncated is True


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://duckduckgo.com/html/?q=test"
        self.closed = False

    async def title(self) -> str: return "results"
    async def content(self) -> str:
        return '<html><a class="result__a" href="https://example.com/1">r1</a></html>'
    async def close(self) -> None: self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.page: _FakePage | None = None

    async def new_page(self) -> _FakePage:
        self.page = _FakePage()
        return self.page


class _FakeGuard:
    def pop_blocked_error(self, page: object) -> None: return None


class _FakeBrowserManager:
    def __init__(self) -> None:
        self._first_call_done = False
        self.goto_calls: list[str] = []

    async def search_context(self, provider: str) -> SearchContextHandle:
        first = not self._first_call_done
        self._first_call_done = True
        return SearchContextHandle(
            context=_FakeContext(), guard=_FakeGuard(), first_use=first,
        )

    async def goto(self, page: object, url: str, *, timeout_ms: int) -> None:
        self.goto_calls.append(url)


@pytest.mark.asyncio
async def test_search_warms_up_on_first_use_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # URLSafetyGuard hits DNS on validate_user_url; short-circuit that:
    async def fake_validate(self, url: str) -> None: return None
    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", fake_validate)
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")  # deterministic test

    manager = _FakeBrowserManager()
    service = WebSearchService(browser_manager=manager)

    await service.search(provider="duckduckgo", context="one")
    await service.search(provider="duckduckgo", context="two")

    homepage = PROVIDER_HOMEPAGE["duckduckgo"]
    # First call: warmup + search. Second: search only.
    assert manager.goto_calls.count(homepage) == 1
    assert sum(1 for u in manager.goto_calls if "?q=" in u and u != homepage) == 2


@pytest.mark.asyncio
async def test_search_continues_when_warmup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warm-up failures are best-effort and must not fail the search."""
    async def fake_validate(self, url: str) -> None: return None
    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", fake_validate)
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")

    manager = _FakeBrowserManager()
    # Make the homepage goto raise; real search goto succeeds.
    original_goto = manager.goto

    async def failing_goto(page: object, url: str, *, timeout_ms: int) -> None:
        if url == PROVIDER_HOMEPAGE["duckduckgo"]:
            raise PlaywrightTimeoutError("warmup timed out")
        await original_goto(page, url, timeout_ms=timeout_ms)

    manager.goto = failing_goto  # type: ignore[assignment]

    service = WebSearchService(browser_manager=manager)
    response = await service.search(provider="duckduckgo", context="test")
    assert response.urls  # search still succeeded despite warmup failure
