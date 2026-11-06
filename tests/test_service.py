from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from crawly_mcp.browser import SearchContextHandle
from crawly_mcp.constants import PROVIDER_HOMEPAGE
from crawly_mcp.errors import ChallengeBlockedError, URLSafetyError
from crawly_mcp.security import URLSafetyGuard  # used for URL validation only
from crawly_mcp.service import (
    ProviderHealth,
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

    @property
    def is_ready(self) -> bool:
        return True

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
        # Shaped like a real results page: the result anchor lives inside a
        # result container, which is what makes it a result and not page chrome.
        return (
            "<html><body><article>"
            '<a class="result__a" href="https://example.com/1">r1</a>'
            "</article></body></html>"
        )

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


class _RecordingBrowserManager(FakeBrowserManager):
    """FakeBrowserManager plus the navigation log the cascade tests need."""

    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        super().__init__(specs)
        self.visited: list[str] = []

    async def goto(self, page: FakePage, url: str, *, timeout_ms: int) -> None:
        self.visited.append(url)
        await super().goto(page, url, timeout_ms=timeout_ms)


GOOGLE_RESULT_HTML = (
    "<html><body><div class='yuRUbf'>"
    "<a href='https://example.com/alpha'>Alpha</a>"
    "</div></body></html>"
)


@pytest.mark.asyncio
async def test_search_cascades_past_a_page_that_never_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core usability fix: a provider that never reached a results view
    sends the search on to the next provider instead of returning `urls: []`
    and letting the caller conclude the topic is empty."""
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    shell = (FIXTURES / "duckduckgo_shell.html").read_text(encoding="utf-8")
    browser = _RecordingBrowserManager(
        {
            "https://duckduckgo.com/?q=python&ia=web": {
                "title": "python at DuckDuckGo",
                "html": shell,
            },
            "https://www.google.com/search?q=python&hl=en": {
                "title": "python - Google Search",
                "html": GOOGLE_RESULT_HTML,
            },
        }
    )
    service = WebSearchService(browser)

    result = await service.search(provider="duckduckgo", context="python")

    assert result.urls == ["https://example.com/alpha"]
    assert result.status == "ok"
    assert result.provider == "google"
    assert result.attempted[0].provider == "duckduckgo"
    assert result.attempted[0].status == "unavailable"
    assert result.attempted[0].page_kind == "shell"
    assert result.attempted[1].provider == "google"


@pytest.mark.asyncio
async def test_search_stops_cascading_on_a_genuine_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that says "nothing found" is an answer, not a failure."""
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    zero = (FIXTURES / "duckduckgo_zero_results.html").read_text(encoding="utf-8")
    browser = _RecordingBrowserManager(
        {
            "https://duckduckgo.com/?q=zzqxvplm&ia=web": {
                "title": "zzqxvplm at DuckDuckGo",
                "html": zero,
            },
            "https://www.google.com/search?q=zzqxvplm&hl=en": {
                "title": "zzqxvplm - Google Search",
                "html": GOOGLE_RESULT_HTML,
            },
        }
    )
    service = WebSearchService(browser)

    result = await service.search(provider="duckduckgo", context="zzqxvplm")

    assert result.urls == []
    assert result.status == "no_results"
    assert result.next_action == "refine_query"
    assert not any("google.com" in url for url in browser.visited)


@pytest.mark.asyncio
async def test_search_reports_failure_when_no_provider_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "duckduckgo")

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    shell = (FIXTURES / "duckduckgo_shell.html").read_text(encoding="utf-8")
    browser = _RecordingBrowserManager(
        {
            "https://duckduckgo.com/?q=python&ia=web": {
                "title": "python at DuckDuckGo",
                "html": shell,
            }
        }
    )
    service = WebSearchService(browser)

    result = await service.search(provider="duckduckgo", context="python")

    assert result.urls == []
    assert result.status == "unavailable"
    assert result.next_action == "report_to_user"
    assert result.suggested_providers == []
    assert "never reached a results view" in result.detail


@pytest.mark.asyncio
async def test_search_skips_a_provider_that_keeps_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that has failed repeatedly is not offered as a fill slot."""
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    shell = (FIXTURES / "duckduckgo_shell.html").read_text(encoding="utf-8")
    browser = _RecordingBrowserManager(
        {
            "https://duckduckgo.com/?q=python&ia=web": {
                "title": "python at DuckDuckGo",
                "html": shell,
            },
            "https://www.google.com/search?q=python&hl=en": {
                "title": "python - Google Search",
                "html": GOOGLE_RESULT_HTML,
            },
        }
    )
    service = WebSearchService(browser)
    service._health["duckduckgo"] = ProviderHealth(
        failures=2, consecutive_failures=2, last_status="unavailable"
    )

    result = await service.search(provider="google", context="python")

    assert result.provider == "google"
    assert [a.provider for a in result.attempted] == ["google"]
    assert not any("duckduckgo.com" in url for url in browser.visited)
    report = {p.provider: p for p in service.providers().providers}
    assert report["duckduckgo"].skipped is True


@pytest.mark.asyncio
async def test_fetch_keeps_good_pages_when_one_url_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused URL is one entry in `errors`, not a lost batch.

    The guard used to run over every URL before any page loaded, so one
    loopback address destroyed the content of the URLs that would have worked.
    """
    ok = "https://example.com/ok"

    async def selective_validate(self, url: str) -> None:
        del self
        if "127.0.0.1" in url:
            raise URLSafetyError(
                "blocked_target", "URL resolves to non-public address(es): 127.0.0.1"
            )

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", selective_validate)
    browser = FakeBrowserManager({ok: {"title": "ok", "html": "<html>ok</html>"}})
    service = WebSearchService(browser)

    result = await service.fetch(urls=[ok, "http://127.0.0.1:9/"])

    assert result.pages[ok] == "<html>ok</html>"
    assert result.errors["http://127.0.0.1:9/"].type == "blocked_target"
    assert result.errors["http://127.0.0.1:9/"].next_action == "report_to_user"
    assert result.status == "ok"
    assert result.succeeded == 1
    assert result.failed == 1


@pytest.mark.asyncio
async def test_fetch_reports_a_batch_where_every_url_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse_all(self, url: str) -> None:
        del self, url
        raise URLSafetyError("blocked_target", "URL resolves to non-public address")

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", refuse_all)
    browser = FakeBrowserManager({})
    service = WebSearchService(browser)

    result = await service.fetch(urls=["http://127.0.0.1:9/"])

    assert result.pages == {}
    assert result.status == "unsafe_url"
    assert result.next_action == "report_to_user"
    assert result.failed == 1
