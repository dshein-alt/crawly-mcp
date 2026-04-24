from __future__ import annotations

import httpx
import pytest

from crawly_mcp.models import PageSearchResult
from types import SimpleNamespace

from crawly_mcp.errors import NavigationFailedError
from crawly_mcp.page_search import (
    AlgoliaHit,
    AlgoliaTier,
    FormHit,
    FormTier,
    OpenSearchHit,
    OpenSearchTier,
    PageSearchService,
    ReadthedocsHit,
    ReadthedocsTier,
    TextHit,
    TextTier,
)
from crawly_mcp.parsing import SearchFormHit


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


_OSD = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>Docs</ShortName>
  <Url type="text/html" template="https://example.com/search?q={searchTerms}&amp;lang=en"/>
</OpenSearchDescription>
"""


class _UnusedFetcher:
    async def __call__(self, url: str) -> str:
        raise AssertionError("should not be invoked")


def test_opensearch_tier_detect_returns_href_when_link_present() -> None:
    html = """<link rel="search" type="application/opensearchdescription+xml" href="/osd.xml">"""
    tier = OpenSearchTier(
        http_client_factory=lambda: httpx.AsyncClient(),
        page_fetcher=_UnusedFetcher(),
    )
    hit = tier.detect(html, "https://example.com/")
    assert isinstance(hit, OpenSearchHit)
    assert hit.descriptor_url == "https://example.com/osd.xml"


@pytest.mark.asyncio
async def test_opensearch_tier_execute_substitutes_query_and_fetches_results(
    _noop_ssrf_guard: None,
) -> None:
    descriptor_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text=_OSD)
    )
    captured: list[str] = []

    async def fake_fetch(url: str) -> str:
        captured.append(url)
        return (
            "<html><title>Results</title>"
            "<body><p>Hello world match here</p></body></html>"
        )

    tier = OpenSearchTier(
        http_client_factory=lambda: httpx.AsyncClient(transport=descriptor_transport),
        page_fetcher=fake_fetch,
    )
    hit = OpenSearchHit(descriptor_url="https://example.com/osd.xml")

    results = await tier.execute(hit, "match")

    assert len(captured) == 1
    assert (
        "q=match" in captured[0]
        or "q=match&" in captured[0]
        or captured[0].endswith("q=match&lang=en")
    )
    assert any("match" in r.snippet.lower() for r in results)


def test_readthedocs_tier_detect_parses_slug_and_version() -> None:
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient())
    hit = tier.detect(
        "", "https://myproject.readthedocs.io/en/stable/guide/intro.html"
    )
    assert isinstance(hit, ReadthedocsHit)
    assert hit.project == "myproject"
    assert hit.version == "stable"


def test_readthedocs_tier_detect_skips_root_path() -> None:
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient())
    assert tier.detect("", "https://myproject.readthedocs.io/") is None


def test_readthedocs_tier_detect_skips_non_readthedocs_host() -> None:
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient())
    assert tier.detect("", "https://example.com/en/stable/page.html") is None


@pytest.mark.asyncio
async def test_readthedocs_tier_execute_maps_blocks_to_results(
    _noop_ssrf_guard: None,
) -> None:
    payload = {
        "results": [
            {
                "path": "guide/intro.html",
                "project": "myproject",
                "domain": "https://myproject.readthedocs.io",
                "blocks": [
                    {
                        "type": "section",
                        "id": "intro",
                        "title": "Introduction",
                        "content": "This introduces the <span>project</span> and its goals",
                    }
                ],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=intro" in str(request.url)
        assert "project=myproject" in str(request.url)
        assert "version=stable" in str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    tier = ReadthedocsTier(
        http_client_factory=lambda: httpx.AsyncClient(transport=transport)
    )
    hit = ReadthedocsHit(project="myproject", version="stable")

    results = await tier.execute(hit, "intro")

    assert len(results) == 1
    r = results[0]
    assert r.url is not None and "myproject.readthedocs.io" in r.url
    assert r.title == "Introduction"
    assert "<span>" not in r.snippet


def test_form_tier_detect_returns_hit_when_form_present() -> None:
    html = """<form role="search" action="/s" method="get"><input name="q"></form>"""
    tier = FormTier(page_fetcher=_UnusedFetcher())
    hit = tier.detect(html, "https://example.com/")
    assert isinstance(hit, FormHit)
    assert hit.form.action == "https://example.com/s"
    assert hit.form.input_name == "q"


def test_form_tier_detect_none_when_no_form() -> None:
    tier = FormTier(page_fetcher=_UnusedFetcher())
    assert tier.detect("<html><body>nothing</body></html>", "https://example.com/") is None


@pytest.mark.asyncio
async def test_form_tier_execute_constructs_url_and_fetches() -> None:
    captured: list[str] = []

    async def fake_fetch(url: str) -> str:
        captured.append(url)
        return "<html><title>Results</title><body>term found here</body></html>"

    tier = FormTier(page_fetcher=fake_fetch)
    hit = FormHit(
        form=SearchFormHit(action="https://example.com/search", input_name="q")
    )

    results = await tier.execute(hit, "term")

    assert captured == ["https://example.com/search?q=term"]
    assert len(results) == 1
    assert "term" in results[0].snippet.lower()


@pytest.mark.asyncio
async def test_form_tier_execute_preserves_existing_action_query_params() -> None:
    captured: list[str] = []

    async def fake_fetch(url: str) -> str:
        captured.append(url)
        return "<html><body>ok</body></html>"

    tier = FormTier(page_fetcher=fake_fetch)
    hit = FormHit(
        form=SearchFormHit(
            action="https://example.com/s?lang=en", input_name="q"
        )
    )
    await tier.execute(hit, "hello world")

    assert "lang=en" in captured[0]
    assert "q=hello+world" in captured[0] or "q=hello%20world" in captured[0]


class _FakeBrowser:
    def __init__(
        self, *, html: str, navigate_raises: BaseException | None = None
    ) -> None:
        self._html = html
        self._navigate_raises = navigate_raises
        self.close_calls = 0

    async def new_context(self):
        outer = self

        class _Ctx:
            async def new_page(self_inner):
                del self_inner

                async def content() -> str:
                    return outer._html

                async def close() -> None:
                    return None

                async def goto(url, **kwargs):
                    del url, kwargs
                    if outer._navigate_raises is not None:
                        raise outer._navigate_raises

                async def route(pattern, handler):
                    del pattern, handler

                return SimpleNamespace(
                    content=content,
                    close=close,
                    goto=goto,
                    route=route,
                    url="",
                )

            async def close(self_inner):
                del self_inner
                outer.close_calls += 1

            async def route(self_inner, pattern, handler):
                del self_inner, pattern, handler

        return _Ctx()

    async def goto(self, page, url, timeout_ms=None):
        del timeout_ms
        await page.goto(url)


@pytest.mark.asyncio
async def test_source_fetch_returns_html() -> None:
    browser = _FakeBrowser(html="<html><body>hello</body></html>")
    service = PageSearchService(
        browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient()
    )
    html = await service._fetch_source_html("https://example.com/")
    assert "hello" in html
    assert browser.close_calls == 1


@pytest.mark.asyncio
async def test_source_fetch_raises_navigation_failed_on_error() -> None:
    from patchright.async_api import Error as PlaywrightError

    browser = _FakeBrowser(html="", navigate_raises=PlaywrightError("nope"))
    service = PageSearchService(
        browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient()
    )
    with pytest.raises(NavigationFailedError):
        await service._fetch_source_html("https://example.com/")
