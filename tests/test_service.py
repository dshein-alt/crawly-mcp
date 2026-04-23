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
from crawly_mcp.service import (
    SearchTrace,
    WebSearchService,
    extract_readable_text,
    render_fetch_content,
    resolve_fetch_max_size,
    truncate_content,
)

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
            context=self.context,
            guard=_PassthroughGuard(),
            first_use=False,
        )

    async def goto(self, page: FakePage, url: str, *, timeout_ms: int) -> None:
        del timeout_ms
        if url not in self.specs:
            # warmup or unknown URL — silently ignore
            return
        page.url = url
        page.spec = self.specs[url]


@pytest.mark.asyncio
async def test_search_returns_extracted_urls_from_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        "crawly_mcp.service.URLSafetyGuard.validate_user_url", allow_all
    )

    result = await service.search(context="python")

    assert result.urls == [
        "https://example.com/alpha",
        "https://example.org/beta",
        "https://example.net/gamma",
    ]


@pytest.mark.asyncio
async def test_fetch_returns_partial_success_and_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = ["https://example.com/ok", "https://example.com/challenge"]
    browser = FakeBrowserManager(
        {url: {"title": "title", "html": "<html></html>"} for url in urls}
    )
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(
        "crawly_mcp.service.URLSafetyGuard.validate_user_url", allow_all
    )

    async def fake_resolve_fetch_content(
        page: FakePage, *, settle_timeout_seconds: float
    ) -> str:
        del settle_timeout_seconds
        if page.url.endswith("/challenge"):
            raise ChallengeBlockedError("page stayed on a browser challenge screen")
        return "x" * 12

    monkeypatch.setattr(
        "crawly_mcp.service.resolve_fetch_content", fake_resolve_fetch_content
    )
    monkeypatch.setenv("CRAWLY_FETCH_MAX_SIZE", "10")

    result = await service.fetch(urls=urls)

    assert result.pages == {"https://example.com/ok": "x" * 10}
    assert result.errors["https://example.com/challenge"].type == "challenge_blocked"
    assert result.truncated == ["https://example.com/ok"]
    assert result.content_format == "html"


def test_truncate_content_marks_oversized_payloads() -> None:
    content, truncated = truncate_content("hello world", limit_bytes=5)

    assert content == "hello"
    assert truncated is True


def test_resolve_fetch_max_size_uses_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CRAWLY_FETCH_MAX_SIZE", raising=False)

    assert resolve_fetch_max_size() == 1024 * 1024


def test_resolve_fetch_max_size_accepts_positive_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRAWLY_FETCH_MAX_SIZE", "65536")

    assert resolve_fetch_max_size() == 65536


def test_resolve_fetch_max_size_rejects_invalid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRAWLY_FETCH_MAX_SIZE", "not-a-number")

    assert resolve_fetch_max_size() == 1024 * 1024


def test_extract_readable_text_drops_boilerplate_and_scripts() -> None:
    html = """
    <html>
      <head>
        <title>Example title</title>
        <meta name="description" content="Short summary">
        <script>console.log("ignore me")</script>
      </head>
      <body>
        <header>Site navigation</header>
        <main>
          <h1>Article heading</h1>
          <p>Hello <strong>world</strong>.</p>
          <p>Hello world.</p>
        </main>
        <footer>Footer links</footer>
      </body>
    </html>
    """

    text = extract_readable_text(html)

    assert "Title: Example title" in text
    assert "Description: Short summary" in text
    assert "Article heading" in text
    assert "Hello" in text
    assert "Site navigation" not in text
    assert "ignore me" not in text


def test_render_fetch_content_returns_text_when_requested() -> None:
    rendered = render_fetch_content(
        "<html><body><main><p>Readable body</p></main></body></html>",
        content_format="text",
    )

    assert "Readable body" in rendered


@pytest.mark.asyncio
async def test_fetch_returns_text_content_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/ok"
    browser = FakeBrowserManager({url: {"title": "title", "html": "<html></html>"}})
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(
        "crawly_mcp.service.URLSafetyGuard.validate_user_url", allow_all
    )
    monkeypatch.setenv("CRAWLY_FETCH_MAX_SIZE", "1024")

    async def fake_resolve_fetch_content(
        page: FakePage, *, settle_timeout_seconds: float
    ) -> str:
        del page, settle_timeout_seconds
        return (
            "<html><head><title>Readable page</title></head>"
            "<body><main><p>Hello compact world.</p></main></body></html>"
        )

    monkeypatch.setattr(
        "crawly_mcp.service.resolve_fetch_content", fake_resolve_fetch_content
    )

    result = await service.fetch(urls=[url], content_format="text")

    assert result.content_format == "text"
    assert "Hello compact world." in result.pages[url]
    assert "<html>" not in result.pages[url]


def test_search_trace_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRAWLY_TRACE_DIR", raising=False)

    assert SearchTrace.create("google", "omnicoder") is None


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://duckduckgo.com/html/?q=test"
        self.closed = False

    async def title(self) -> str:
        return "results"

    async def content(self) -> str:
        return '<html><a class="result__a" href="https://example.com/1">r1</a></html>'

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.page: _FakePage | None = None

    async def new_page(self) -> _FakePage:
        self.page = _FakePage()
        return self.page


class _FakeGuard:
    def pop_blocked_error(self, page: object) -> None:
        return None


class _FakeBrowserManager:
    def __init__(self) -> None:
        self._first_call_done = False
        self.goto_calls: list[str] = []

    async def search_context(self, provider: str) -> SearchContextHandle:
        first = not self._first_call_done
        self._first_call_done = True
        return SearchContextHandle(
            context=_FakeContext(),
            guard=_FakeGuard(),
            first_use=first,
        )

    async def goto(self, page: object, url: str, *, timeout_ms: int) -> None:
        self.goto_calls.append(url)


@pytest.mark.asyncio
async def test_search_warms_up_on_first_use_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # URLSafetyGuard hits DNS on validate_user_url; short-circuit that:
    async def fake_validate(self, url: str) -> None:
        return None

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
async def test_search_closes_context_when_handle_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the handle's should_close_context=True (ephemeral mode), the
    service must close the BrowserContext after the request."""

    async def fake_validate(self, url: str) -> None:
        return None

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", fake_validate)
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")

    closed = {"flag": False}

    class _ClosableContext(_FakeContext):
        async def close(self) -> None:
            closed["flag"] = True

    class _EphemeralBrowserManager(_FakeBrowserManager):
        async def search_context(self, provider: str) -> SearchContextHandle:
            return SearchContextHandle(
                context=_ClosableContext(),
                guard=_FakeGuard(),
                first_use=True,
                should_close_context=True,
            )

    manager = _EphemeralBrowserManager()
    service = WebSearchService(browser_manager=manager)
    await service.search(provider="duckduckgo", context="anything")
    assert closed["flag"] is True


@pytest.mark.asyncio
async def test_search_continues_when_warmup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm-up failures are best-effort and must not fail the search."""

    async def fake_validate(self, url: str) -> None:
        return None

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
