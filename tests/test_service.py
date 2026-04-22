from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from web_search_mcp.errors import ChallengeBlockedError
from web_search_mcp.service import WebSearchService, truncate_html

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

    async def goto(self, page: FakePage, url: str, *, timeout_ms: int) -> None:
        del timeout_ms
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

    monkeypatch.setattr("web_search_mcp.service.URLSafetyGuard.validate_user_url", allow_all)

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

    monkeypatch.setattr("web_search_mcp.service.URLSafetyGuard.validate_user_url", allow_all)

    async def fake_resolve_fetch_content(page: FakePage, *, settle_timeout_seconds: float) -> str:
        del settle_timeout_seconds
        if page.url.endswith("/challenge"):
            raise ChallengeBlockedError("page stayed on a browser challenge screen")
        return "x" * 12

    monkeypatch.setattr("web_search_mcp.service.resolve_fetch_content", fake_resolve_fetch_content)
    monkeypatch.setattr("web_search_mcp.service.MAX_HTML_BYTES", 10)

    result = await service.fetch(urls=urls)

    assert result.pages == {"https://example.com/ok": "x" * 10}
    assert result.errors["https://example.com/challenge"].type == "challenge_blocked"
    assert result.truncated == ["https://example.com/ok"]


def test_truncate_html_marks_oversized_payloads() -> None:
    html, truncated = truncate_html("hello world", limit_bytes=5)

    assert html == "hello"
    assert truncated is True
