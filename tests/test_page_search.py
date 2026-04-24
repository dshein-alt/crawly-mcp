from __future__ import annotations

import httpx
import pytest

from crawly_mcp.models import PageSearchResult
from crawly_mcp.page_search import AlgoliaHit, AlgoliaTier, TextHit, TextTier


def test_text_tier_detect_always_returns_hit() -> None:
    tier = TextTier()
    hit = tier.detect(
        "<html><title>T</title><body>hello</body></html>",
        "https://example.com/",
    )
    assert isinstance(hit, TextHit)
    assert hit.title == "T"


@pytest.mark.asyncio
async def test_text_tier_execute_returns_snippets() -> None:
    html = (
        "<html><title>Docs</title><body>"
        "<p>fetch returns structured content</p>"
        "</body></html>"
    )
    tier = TextTier()
    hit = tier.detect(html, "https://example.com/")
    results = await tier.execute(hit, "structured")
    assert len(results) == 1
    assert isinstance(results[0], PageSearchResult)
    assert "structured" in results[0].snippet.lower()
    assert results[0].url is None
    assert results[0].title == "Docs"


@pytest.mark.asyncio
async def test_text_tier_execute_empty_on_no_match() -> None:
    html = "<html><title>T</title><body>nothing relevant here</body></html>"
    tier = TextTier()
    hit = tier.detect(html, "https://example.com/")
    results = await tier.execute(hit, "structured")
    assert results == []


def _mock_algolia_transport(
    hits: list[dict], *, expect_app_id: str = "APP"
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == f"{expect_app_id.lower()}-dsn.algolia.net"
        assert request.headers["X-Algolia-Application-Id"] == expect_app_id
        return httpx.Response(200, json={"hits": hits})

    return httpx.MockTransport(handler)


def test_algolia_tier_detect_returns_config_when_present() -> None:
    html = """
    <script>
      docsearch({ appId: "APP", apiKey: "KEY", indexName: "docs" });
    </script>
    """
    tier = AlgoliaTier(http_client_factory=lambda: httpx.AsyncClient())
    hit = tier.detect(html, "https://example.com/")
    assert isinstance(hit, AlgoliaHit)
    assert hit.app_id == "APP"
    assert hit.api_key == "KEY"
    assert hit.index_name == "docs"


def test_algolia_tier_detect_none_when_absent() -> None:
    tier = AlgoliaTier(http_client_factory=lambda: httpx.AsyncClient())
    assert (
        tier.detect("<html><body>no docsearch</body></html>", "https://example.com/")
        is None
    )


@pytest.fixture
def _noop_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(self, url: str) -> None:
        del self, url

    monkeypatch.setattr(
        "crawly_mcp.page_search.URLSafetyGuard.validate_user_url", _noop
    )


@pytest.mark.asyncio
async def test_algolia_tier_execute_maps_hits_to_results(
    _noop_ssrf_guard: None,
) -> None:
    hits = [
        {
            "url": "https://docs.example.com/page#anchor",
            "hierarchy": {"lvl0": "Guide", "lvl1": "Auth", "lvl2": "Login"},
            "_snippetResult": {
                "content": {"value": "Login flow <em>works</em> like this"}
            },
        }
    ]
    transport = _mock_algolia_transport(hits)
    tier = AlgoliaTier(
        http_client_factory=lambda: httpx.AsyncClient(transport=transport)
    )
    hit = AlgoliaHit(app_id="APP", api_key="KEY", index_name="docs")

    results = await tier.execute(hit, "login")

    assert len(results) == 1
    r = results[0]
    assert r.url == "https://docs.example.com/page#anchor"
    assert "Guide" in (r.title or "")
    assert "<em>" not in r.snippet
