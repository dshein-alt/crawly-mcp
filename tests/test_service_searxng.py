from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crawly_mcp.errors import InvalidInputError, ProviderBlockedError
from crawly_mcp.models import SearchResponse
from crawly_mcp.service import WebSearchService


def _make_service(monkeypatch, *, adapter=None) -> WebSearchService:
    """Construct a WebSearchService stub that bypasses __init__.

    The real __init__ would start a BrowserManager; tests use __new__ and
    replace the SearXNG adapter via monkeypatch on `crawly_mcp.service.searxng_search`.
    `_search_via_browser` is stubbed so we can assert it's never called from the
    SearXNG path (no automatic cross-provider fallback in the demoted feature).
    """
    svc = WebSearchService.__new__(WebSearchService)
    svc._http = MagicMock()
    svc._search_via_browser = AsyncMock(
        return_value=SearchResponse(urls=["https://ddg.example/"])
    )
    monkeypatch.setattr(
        "crawly_mcp.service.searxng_search",
        adapter or AsyncMock(return_value=["https://r.example/"]),
    )
    return svc


@pytest.mark.asyncio
async def test_searxng_without_env_raises_invalid_input(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    adapter = AsyncMock()
    svc = _make_service(monkeypatch, adapter=adapter)

    with pytest.raises(InvalidInputError):
        await svc.search(provider="searxng", context="x")
    adapter.assert_not_awaited()
    svc._search_via_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_with_env_calls_adapter_once(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(return_value=["https://r.example/"])
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.urls == ["https://r.example/"]
    adapter.assert_awaited_once()
    assert adapter.await_args.args[0] == "https://pinned.example/"
    svc._search_via_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_normalizes_trailing_slash(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example")  # no slash
    adapter = AsyncMock(return_value=["https://r.example/"])
    svc = _make_service(monkeypatch, adapter=adapter)

    await svc.search(provider="searxng", context="x")
    assert adapter.await_args.args[0] == "https://pinned.example/"


@pytest.mark.asyncio
async def test_searxng_zero_results_returned_as_empty(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(return_value=[])
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="obscure")
    assert resp.urls == []
    svc._search_via_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_provider_blocked_propagates(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(side_effect=ProviderBlockedError("blocked"))
    svc = _make_service(monkeypatch, adapter=adapter)

    with pytest.raises(ProviderBlockedError):
        await svc.search(provider="searxng", context="x")
    svc._search_via_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_rejects_non_http_scheme(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "ftp://wrong.example/")
    svc = _make_service(monkeypatch)

    with pytest.raises(InvalidInputError):
        await svc.search(provider="searxng", context="x")


@pytest.mark.asyncio
async def test_searxng_accepts_plain_http(monkeypatch) -> None:
    """http:// is allowed so users can point at a localhost / private SearXNG."""
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "http://searxng.local:8080/")
    adapter = AsyncMock(return_value=["https://r.example/"])
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.urls == ["https://r.example/"]
    assert adapter.await_args.args[0] == "http://searxng.local:8080/"


@pytest.mark.asyncio
async def test_non_searxng_provider_skips_searxng_path(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    svc = _make_service(monkeypatch)
    monkeypatch.setattr(
        "crawly_mcp.service.searxng_search",
        AsyncMock(side_effect=AssertionError("must not be called")),
    )
    resp = await svc.search(provider="duckduckgo", context="x")
    assert resp.urls == ["https://ddg.example/"]
    svc._search_via_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_closes_http_client(monkeypatch) -> None:
    svc = _make_service(monkeypatch)
    svc._http.aclose = AsyncMock()
    await svc.aclose()
    svc._http.aclose.assert_awaited_once()
