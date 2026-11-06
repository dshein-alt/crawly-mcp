from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from patchright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from crawly_mcp.browser import SearchContextHandle
from crawly_mcp.constants import (
    MAX_SEARCH_RESULTS,
    PROVIDER_HOMEPAGE,
)
from crawly_mcp.errors import (
    ChallengeBlockedError,
    URLSafetyError,
    next_action_for,
)
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
    """A provider page.

    `install_frames` gives it frames to walk through, which is how a provider
    mid-check is expressed: it serves one page and lands on another a second
    later, so a static fake cannot show the case that matters — a first reading
    that is a wall and a later reading that is the answer.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self._url = "about:blank"
        self._frames: list[dict[str, str]] = []
        self._index = 0
        self.reads = 0

    def install_frames(self, frames: list[dict[str, str]]) -> None:
        self._frames = frames
        self._index = 0

    @property
    def _frame(self) -> dict[str, str] | None:
        if not self._frames:
            return None
        return self._frames[min(self._index, len(self._frames) - 1)]

    @property
    def url(self) -> str:
        frame = self._frame
        if frame is not None:
            return frame.get("url", self._url)
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value

    async def title(self) -> str:
        frame = self._frame
        if frame is not None:
            return frame.get("title", "")
        return self.spec.get("title", "")

    async def content(self) -> str:
        self.reads += 1
        frame = self._frame
        if frame is not None:
            html = frame.get("html", "")
            if self._index < len(self._frames) - 1:
                self._index += 1
            return html
        raised = self.spec.get("content_raises")
        if raised is not None:
            raise PlaywrightError(raised)
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
        page.spec = self.specs[url]
        frames = page.spec.get("frames")
        if frames is not None:
            # Each frame names where the browser stands at that moment, so the
            # page walks from a wall to what the check redirects to.
            page.install_frames(frames)
            return
        # A provider can leave the browser on a different address than the one
        # it was asked for (Google's /sorry, DuckDuckGo's /static-pages/418).
        page.url = page.spec.get("final_url", url)


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
async def test_google_opaque_result_redirects_are_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowserManager(
        {
            "https://www.google.com/search?q=python&hl=en": {
                "title": "python - Google Search",
                "html": (FIXTURES / "google_goto_only.html").read_text(
                    encoding="utf-8"
                ),
            }
        }
    )
    service = WebSearchService(browser)
    targets = {
        "CAESVAHEIkXXXUGWzJcP2zeQyIH": "https://www.python.org/",
        "CAESXgHEIkXXCkMWKPqsa4ZieKsm8n": "https://docs.python.org/3/tutorial/",
    }

    def resolve_redirect(request: httpx.Request) -> httpx.Response:
        target = targets[request.url.params["url"]]
        return httpx.Response(302, headers={"Location": target})

    service._http = httpx.AsyncClient(transport=httpx.MockTransport(resolve_redirect))
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google")
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    result = await service.search(provider="google", context="python")

    await service.aclose()
    assert result.status == "ok"
    assert result.provider == "google"
    assert result.urls == list(targets.values())
    assert result.attempted[0].results == 2


@pytest.mark.asyncio
async def test_google_defer_page_is_retried_with_the_token_it_hands_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttle page names a fresh token as its own next hop: follow it once.

    Google answers part of a burst of click-through requests with a 200 and no
    Location, whose only declared target is a refresh back to the same endpoint
    carrying a new token. Dropping the link there loses a result the page had.
    """
    browser = FakeBrowserManager(
        {
            "https://www.google.com/search?q=python&hl=en": {
                "title": "python - Google Search",
                "html": (FIXTURES / "google_goto_defer.html").read_text(
                    encoding="utf-8"
                ),
            }
        }
    )
    service = WebSearchService(browser)
    monkeypatch.setattr("crawly_mcp.service.OPAQUE_RESOLVE_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google")
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    seen: list[str] = []

    def resolve_redirect(request: httpx.Request) -> httpx.Response:
        ref = request.url.params["url"]
        seen.append(ref)
        if ref == "DEFER":
            return httpx.Response(
                200,
                text='<meta content="0;url=/goto?url=FRESH" http-equiv="refresh">',
            )
        if ref == "FRESH":
            return httpx.Response(302, headers={"Location": "https://www.python.org/"})
        return httpx.Response(200, text="<html>no destination declared</html>")

    service._http = httpx.AsyncClient(transport=httpx.MockTransport(resolve_redirect))

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    result = await service.search(provider="google", context="python")
    await service.aclose()

    assert result.status == "ok"
    assert result.urls == ["https://www.python.org/"]
    assert "FRESH" in seen


@pytest.mark.asyncio
async def test_google_resolves_past_the_cap_and_reports_unresolved_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Links dropped to throttling must not shrink the answer below the cap.

    Eight result links, three resolvable after the first five fail: asking only
    for as many links as the caller may receive loses the resolvable links.
    """
    browser = FakeBrowserManager(
        {
            "https://www.google.com/search?q=python&hl=en": {
                "title": "python - Google Search",
                "html": (FIXTURES / "google_goto_many.html").read_text(
                    encoding="utf-8"
                ),
            }
        }
    )
    service = WebSearchService(browser)
    monkeypatch.setattr("crawly_mcp.service.OPAQUE_RESOLVE_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google")
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    def resolve_redirect(request: httpx.Request) -> httpx.Response:
        ref = request.url.params["url"]
        if ref in {"T1", "T2", "T3", "T4", "T5"}:
            return httpx.Response(
                200,
                text='<meta http-equiv="refresh" content="0;url=/goto?url=SPENT">',
            )
        if ref == "SPENT":
            return httpx.Response(200, text="<html>still throttled</html>")
        return httpx.Response(302, headers={"Location": f"https://example.org/{ref}"})

    service._http = httpx.AsyncClient(transport=httpx.MockTransport(resolve_redirect))

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)

    result = await service.search(provider="google", context="python")
    await service.aclose()

    assert result.status == "ok"
    assert result.urls == [f"https://example.org/T{n}" for n in (6, 7, 8)]
    # The caller is told the answer is short because of throttling, not because
    # the page had little on it.
    assert "3 of 8" in result.attempted[0].detail
    assert "3 of 8" in result.detail
    assert "5 link(s) yielded no usable destination" in result.detail


@pytest.fixture
def google_opaque_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., WebSearchService]:
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google")
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")
    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", AsyncMock())
    monkeypatch.setattr("crawly_mcp.service.OPAQUE_RESOLVE_CLEANUP_SECONDS", 0.01)

    def create(handler: Callable[..., Any]) -> WebSearchService:
        browser = FakeBrowserManager(
            {
                "https://www.google.com/search?q=python&hl=en": {
                    "title": "python - Google Search",
                    "html": (FIXTURES / "google_goto_many.html").read_text(
                        encoding="utf-8"
                    ),
                }
            }
        )
        service = WebSearchService(browser)
        service._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return service

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_resolved_counts_precede_cap_and_deduplication(
    google_opaque_service: Callable[..., WebSearchService], duplicate: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = "T1" if duplicate else request.url.params["url"]
        return httpx.Response(302, headers={"Location": f"https://example.org/{token}"})

    service = google_opaque_service(handler)
    try:
        result = await service.search(provider="google", context="python")
    finally:
        await service.aclose()
    assert len(result.urls) == (1 if duplicate else MAX_SEARCH_RESULTS)
    assert "8 of 8" in result.detail
    assert "throttled" not in result.detail
    assert "no usable destination" not in result.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [True, False])
async def test_resolution_deadline_retains_results_and_cleans_up(
    google_opaque_service: Callable[..., WebSearchService],
    monkeypatch: pytest.MonkeyPatch,
    partial: bool,
) -> None:
    monkeypatch.setattr(
        "crawly_mcp.service.SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS", 0.2
    )
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            token = request.url.params["url"]
            if not partial or token not in {"T1", "T2"}:
                await asyncio.Event().wait()
            return httpx.Response(
                302, headers={"Location": f"https://example.org/{token}"}
            )
        finally:
            active -= 1

    service = google_opaque_service(handler)
    try:
        result = await service.search(provider="google", context="python")
    finally:
        await service.aclose()
    assert result.status == ("ok" if partial else "timeout")
    assert result.urls == (
        ["https://example.org/T1", "https://example.org/T2"] if partial else []
    )
    assert "budget expired" in (
        result.detail if partial else result.attempted[0].detail
    )
    assert peak == 2
    assert active == 0


@pytest.mark.asyncio
async def test_external_cancellation_is_not_converted_to_partial_success(
    google_opaque_service: Callable[..., WebSearchService],
) -> None:
    entered = asyncio.Event()
    active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
        return httpx.Response(200)

    service = google_opaque_service(handler)
    task = asyncio.create_task(service.search(provider="google", context="python"))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_status", [200, 302])
async def test_defer_honors_declared_delay_and_retries_only_once(
    google_opaque_service: Callable[..., WebSearchService],
    monkeypatch: pytest.MonkeyPatch,
    retry_status: int,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["url"])
        if len(seen) == 2 and retry_status == 302:
            return httpx.Response(302, headers={"Location": "https://example.org/"})
        return httpx.Response(
            200, text='<meta content="7;url=/goto?url=FRESH" http-equiv="refresh">'
        )

    service = google_opaque_service(handler)
    sleep = AsyncMock()
    monkeypatch.setattr("crawly_mcp.service.asyncio.sleep", sleep)
    base = "https://www.google.com/search?q=python"
    try:
        result = await service._resolve_result_redirect(
            "google", "https://www.google.com/goto?url=OLD", base, URLSafetyGuard()
        )
    finally:
        await service.aclose()
    assert result == ("https://example.org/" if retry_status == 302 else None)
    assert seen == ["OLD", "FRESH"]
    sleep.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_opaque_retry_never_requests_foreign_origin(
    google_opaque_service: Callable[..., WebSearchService],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(
            200,
            text='<meta content="0;url=https://evil.example/goto?url=FRESH" http-equiv="refresh">',
        )

    service = google_opaque_service(handler)
    try:
        result = await service._resolve_result_redirect(
            "google",
            "https://www.google.com/goto?url=OLD",
            "https://www.google.com/search",
            URLSafetyGuard(),
        )
    finally:
        await service.aclose()
    assert result is None
    assert seen == ["www.google.com"]


@pytest.mark.asyncio
async def test_opaque_ad_links_never_reach_the_resolver(
    google_opaque_service: Callable[..., WebSearchService],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params["url"]
        seen.append(token)
        return httpx.Response(302, headers={"Location": f"https://example.org/{token}"})

    service = google_opaque_service(handler)
    spec = service._browser_manager.specs[
        "https://www.google.com/search?q=python&hl=en"
    ]
    spec["html"] += (
        '<div data-text-ad="1"><div class="yuRUbf">'
        '<a class="zReHs" href="/goto?url=AD">Sponsored</a></div></div>'
    )
    try:
        result = await service.search(provider="google", context="python")
    finally:
        await service.aclose()
    assert "AD" not in seen
    assert "8 of 8" in result.detail
    assert len(result.urls) == MAX_SEARCH_RESULTS


@pytest.mark.asyncio
async def test_trace_capture_does_not_discard_partial_results(
    google_opaque_service: Callable[..., WebSearchService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRAWLY_TRACE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "crawly_mcp.service.SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS", 0.3
    )
    monkeypatch.setattr("crawly_mcp.service.TRACE_CAPTURE_TIMEOUT_SECONDS", 0.05)

    async def hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(SearchTrace, "finalize", hang)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["url"] != "T1":
            await asyncio.Event().wait()
        return httpx.Response(302, headers={"Location": "https://example.org/"})

    service = google_opaque_service(handler)
    try:
        result = await service.search(provider="google", context="python")
    finally:
        await service.aclose()
    assert result.status == "ok"
    assert result.urls == ["https://example.org/"]


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


@pytest.mark.asyncio
async def test_a_tab_that_dies_after_navigation_cascades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab that dies mid-read is one provider failing, not the search dying.

    Reading the title and content happens after navigation, and a "Target
    closed" there used to escape `search()` entirely: the remaining providers
    went untried and the caller got a protocol error where the next provider
    had an answer.
    """
    html = (FIXTURES / "google_results.html").read_text(encoding="utf-8")
    browser = FakeBrowserManager(
        {
            "https://duckduckgo.com/?q=python&ia=web": {
                "title": "python at DuckDuckGo",
                "content_raises": "Target page, context or browser has been closed",
            },
            "https://www.google.com/search?q=python&hl=en": {
                "title": "python - Google Search",
                "html": html,
            },
        }
    )
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.delenv("CRAWLY_SEARCH_PROVIDER_ORDER", raising=False)

    resp = await service.search(context="python")

    assert resp.provider == "google"
    assert resp.status == "ok"
    assert resp.attempted[0].provider == "duckduckgo"
    assert resp.attempted[0].status == "navigation_failed"
    assert resp.urls


@pytest.mark.asyncio
async def test_a_wall_attempt_keeps_the_address_the_wall_was_seen_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "google blocked" with no address says nothing about where the wall was.

    The browser is closed by the time the caller reads the report, so the
    landed URL has to survive the exception-to-attempt conversion.
    """
    html = (FIXTURES / "google_sorry.html").read_text(encoding="utf-8")
    browser = FakeBrowserManager(
        {
            "https://www.google.com/search?q=x&hl=en": {
                "title": "Unusual traffic from your computer network",
                "html": html,
                "final_url": "https://www.google.com/sorry/index",
            }
        }
    )
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    resp = await service.search(provider="google", context="x")

    assert resp.attempted[0].status == "blocked"
    assert resp.attempted[0].page_kind == "captcha"
    assert resp.attempted[0].final_url == "https://www.google.com/sorry/index"


def test_providers_reports_providers_left_out_of_the_priority_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-provider order must not make the other providers disappear.

    "1 of 1 providers are usable" has to be readable as "DuckDuckGo is
    reachable, just out of the order" and not as "this build has no
    DuckDuckGo" — which is the difference between editing a setting and
    redeploying.
    """
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google")
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.delenv("CRAWLY_SEARCH_PROVIDER_EXCLUDE", raising=False)
    service = WebSearchService(FakeBrowserManager({}))

    report = service.providers()

    assert {entry.provider for entry in report.providers} == {
        "duckduckgo",
        "google",
        "yandex",
        "searxng",
    }
    assert report.cascade_order == ["google"]
    duckduckgo = next(e for e in report.providers if e.provider == "duckduckgo")
    assert duckduckgo.available is False
    assert "priority order" in duckduckgo.detail
    assert report.detail.startswith("1 of 4 providers are usable")


@pytest.mark.asyncio
async def test_fetch_wall_advises_a_move_the_caller_can_actually_make(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch has no provider to switch to, so "try another provider" is noise."""
    urls = ["https://example.com/walled"]
    browser = FakeBrowserManager(
        {url: {"title": "t", "html": "<html></html>"} for url in urls}
    )
    service = WebSearchService(browser)

    async def allow_all(self, url: str) -> None:
        del self, url

    async def stay_on_challenge(
        page: FakePage, *, settle_timeout_seconds: float
    ) -> str:
        del settle_timeout_seconds, page
        raise ChallengeBlockedError("page stayed on a browser challenge screen")

    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", allow_all)
    monkeypatch.setattr("crawly_mcp.service.resolve_fetch_content", stay_on_challenge)

    result = await service.fetch(urls=urls)

    assert result.errors[urls[0]].next_action == "retry_later"
    assert result.next_action == "retry_later"


def test_the_same_wall_advises_differently_per_tool() -> None:
    assert next_action_for("challenge_blocked", tool="search") == "try_other_provider"
    assert next_action_for("challenge_blocked", tool="fetch") == "retry_later"
    assert next_action_for("timeout", tool="fetch") == "retry_later"
    assert next_action_for("invalid_url", tool="fetch") == "report_to_user"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["context", "page"])
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (PlaywrightError("Target closed"), "navigation_failed"),
        (PlaywrightTimeoutError("Context timed out"), "timeout"),
    ],
)
async def test_browser_acquisition_failure_cascades(
    monkeypatch: pytest.MonkeyPatch, stage: str, error: Exception, status: str
) -> None:
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "duckduckgo,google")
    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", AsyncMock())
    manager = FakeBrowserManager(
        {
            "https://www.google.com/search?q=python&hl=en": {
                "html": GOOGLE_RESULT_HTML,
                "title": "python - Google Search",
            }
        }
    )
    working = await manager.search_context("google")
    failed_context = FakeContext({})
    failed_context.new_page = AsyncMock(side_effect=error)
    failed_context.close = AsyncMock()
    first = (
        error
        if stage == "context"
        else SearchContextHandle(
            context=failed_context,
            guard=working.guard,
            first_use=False,
            should_close_context=True,
        )
    )
    manager.search_context = AsyncMock(side_effect=[first, working])

    response = await WebSearchService(manager).search(context="python")

    assert response.status == "ok"
    assert response.provider == "google"
    assert response.attempted[0].status == status
    assert manager.search_context.await_count == 2
    if stage == "page":
        failed_context.close.assert_awaited_once()


YANDEX_SEARCH_URL = "https://yandex.ru/search/?text=python"


def _yandex_frames() -> list[dict[str, str]]:
    """The captured check page, then the results page it redirects to."""
    return [
        {
            "url": "https://yandex.ru/showcaptchafast?d=EA09&retpath=x",
            "title": "Верификация",
            "html": (FIXTURES / "yandex_captcha_auto.html").read_text(encoding="utf-8"),
        },
        {
            "url": YANDEX_SEARCH_URL,
            "title": "python - Yandex Search",
            "html": (FIXTURES / "yandex_results.html").read_text(encoding="utf-8"),
        },
    ]


def _yandex_smartcaptcha_frames() -> list[dict[str, str]]:
    """The check page, then the checkbox wall it falls through to."""
    return [
        {
            "url": "https://yandex.ru/showcaptchafast?d=EA09&retpath=x",
            "title": "Верификация",
            "html": (FIXTURES / "yandex_captcha_auto.html").read_text(encoding="utf-8"),
        },
        {
            "url": "https://yandex.ru/showcaptcha?cc=1",
            "title": "Вы не робот?",
            "html": (FIXTURES / "yandex_captcha_smart.html").read_text(
                encoding="utf-8"
            ),
        },
    ]


def _allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow_all(self: object, url: str) -> None:
        del self, url

    monkeypatch.setattr(
        "crawly_mcp.service.URLSafetyGuard.validate_user_url", allow_all
    )


@pytest.mark.asyncio
async def test_yandexs_automatic_check_that_reaches_results_is_not_a_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wall that was about to answer is reported as an answer.

    Yandex's /showcaptchafast submits a fingerprint and redirects to the
    results. Reading the page once reported `blocked` with zero URLs; the
    settled reading is the results page, and the caller gets its URLs.
    """
    _allow_all(monkeypatch)
    browser = FakeBrowserManager({YANDEX_SEARCH_URL: {"frames": _yandex_frames()}})
    service = WebSearchService(browser)
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    resp = await service.search(provider="yandex", context="python")

    assert resp.status == "ok"
    assert resp.provider == "yandex"
    assert resp.urls
    assert resp.attempted[0].page_kind == "results"


@pytest.mark.asyncio
async def test_yandexs_smartcaptcha_stays_blocked_and_spends_no_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall that asks a human is refused at once, and stays refused.

    The check falls through to the checkbox, which promises nothing; the block
    is preserved, it names the wall the provider ended on, and no window is
    burned waiting on a page that will not move.
    """
    _allow_all(monkeypatch)
    browser = FakeBrowserManager(
        {YANDEX_SEARCH_URL: {"frames": _yandex_smartcaptcha_frames()}}
    )
    service = WebSearchService(browser)
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    resp = await service.search(provider="yandex", context="python")

    assert resp.status == "blocked"
    assert resp.next_action != "refine_query"  # a refusal, not an empty web
    assert resp.urls == []
    attempt = resp.attempted[0]
    assert attempt.page_kind == "captcha"
    assert attempt.final_url == "https://yandex.ru/showcaptcha?cc=1"
    assert browser.context.pages[0].reads == 2  # wall, then the checkbox


@pytest.mark.asyncio
async def test_yandexs_check_that_never_finishes_is_a_block_not_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settle that runs out ends as the wall it is, inside its budget."""
    _allow_all(monkeypatch)
    monkeypatch.setattr("crawly_mcp.service.SEARCH_WALL_SETTLE_SECONDS", 0.3)
    stuck = _yandex_frames()[:1] * 4
    browser = FakeBrowserManager({YANDEX_SEARCH_URL: {"frames": stuck}})
    service = WebSearchService(browser)
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")

    resp = await service.search(provider="yandex", context="python")

    assert resp.status == "blocked"
    assert resp.attempted[0].page_kind == "captcha"


@pytest.mark.asyncio
async def test_a_walled_yandex_cascades_to_the_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The block is one provider's answer, not the end of the search."""
    _allow_all(monkeypatch)
    html = (FIXTURES / "duckduckgo_results.html").read_text(encoding="utf-8")
    browser = FakeBrowserManager(
        {
            YANDEX_SEARCH_URL: {"frames": _yandex_smartcaptcha_frames()},
            "https://duckduckgo.com/?q=python&ia=web": {
                "title": "python at DuckDuckGo",
                "html": html,
            },
        }
    )
    service = WebSearchService(browser)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "yandex,duckduckgo")
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "2")

    resp = await service.search(context="python")

    assert resp.status == "ok"
    assert resp.provider == "duckduckgo"
    assert resp.attempted[0].provider == "yandex"
    assert resp.attempted[0].status == "blocked"


@pytest.fixture
def yandex_auto_service(monkeypatch: pytest.MonkeyPatch) -> WebSearchService:
    _allow_all(monkeypatch)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "yandex")
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")
    auto = _yandex_frames()[0]
    return WebSearchService(
        FakeBrowserManager(
            {
                YANDEX_SEARCH_URL: {
                    "html": auto["html"],
                    "title": auto["title"],
                    "final_url": auto["url"],
                }
            }
        )
    )


@pytest.mark.asyncio
async def test_settle_retries_navigation_errors_from_the_first_poll(
    yandex_auto_service: WebSearchService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FakePage.content
    calls = 0

    async def moving_content(self: FakePage) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PlaywrightError("Execution context was destroyed during navigation")
        if calls > 2:
            self.url = YANDEX_SEARCH_URL
            return (FIXTURES / "yandex_results.html").read_text(encoding="utf-8")
        return await original(self)

    monkeypatch.setattr(FakePage, "content", moving_content)
    monkeypatch.setattr("crawly_mcp.service.SEARCH_WALL_SETTLE_SAMPLE_SECONDS", 0.001)
    result = await yandex_auto_service.search(provider="yandex", context="python")
    assert result.status == "ok"
    assert result.attempted[0].final_url == YANDEX_SEARCH_URL
    assert calls == 3


@pytest.mark.asyncio
async def test_stalled_settle_read_preserves_the_block_and_its_url(
    yandex_auto_service: WebSearchService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FakePage.content
    monkeypatch.setattr(
        "crawly_mcp.service.SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS", 0.2
    )
    monkeypatch.setattr("crawly_mcp.service.SEARCH_WALL_SETTLE_CLEANUP_SECONDS", 0.02)
    monkeypatch.setattr("crawly_mcp.service.SEARCH_WALL_SETTLE_SAMPLE_SECONDS", 0.001)
    cancelled = asyncio.Event()

    async def stalled_content(self: FakePage) -> str:
        if self.reads:
            self.url = YANDEX_SEARCH_URL
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(self)

    monkeypatch.setattr(FakePage, "content", stalled_content)
    result = await yandex_auto_service.search(provider="yandex", context="python")
    assert result.status == "blocked"
    assert result.attempted[0].final_url == _yandex_frames()[0]["url"]
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation_seconds", [0.3, 0.45])
async def test_settle_and_trace_share_the_provider_budget(
    yandex_auto_service: WebSearchService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    navigation_seconds: float,
) -> None:
    monkeypatch.setenv("CRAWLY_TRACE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "crawly_mcp.service.SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS", 0.5
    )
    monkeypatch.setattr("crawly_mcp.service.SEARCH_WALL_SETTLE_CLEANUP_SECONDS", 0.02)
    monkeypatch.setattr("crawly_mcp.service.SEARCH_WALL_SETTLE_SAMPLE_SECONDS", 0.005)
    monkeypatch.setattr("crawly_mcp.service.OPAQUE_RESOLVE_CLEANUP_SECONDS", 0.01)
    monkeypatch.setattr("crawly_mcp.service.TRACE_CAPTURE_TIMEOUT_SECONDS", 0.08)
    original = yandex_auto_service._browser_manager.goto
    trace_started = asyncio.Event()

    async def slow_navigation(page: FakePage, url: str, *, timeout_ms: int) -> None:
        await asyncio.sleep(navigation_seconds)
        await original(page, url, timeout_ms=timeout_ms)

    async def finalize(self: SearchTrace, page: FakePage, *, html: str | None) -> None:
        trace_started.set()
        await asyncio.sleep(0.07)

    monkeypatch.setattr(yandex_auto_service._browser_manager, "goto", slow_navigation)
    monkeypatch.setattr(SearchTrace, "finalize", finalize)
    result = await yandex_auto_service.search(provider="yandex", context="python")
    assert result.status == "blocked"
    assert result.attempted[0].page_kind == "captcha"
    assert trace_started.is_set()
