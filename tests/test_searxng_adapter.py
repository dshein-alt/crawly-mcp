from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crawly_mcp.errors import ProviderBlockedError
from crawly_mcp.searxng import searxng_search

FIXTURES = Path(__file__).parent / "fixtures" / "searxng"


def _good_payload() -> bytes:
    return (FIXTURES / "search_response_good.json").read_bytes()


def _empty_payload() -> bytes:
    return (FIXTURES / "search_response_empty.json").read_bytes()


def _blocked_html() -> bytes:
    return (FIXTURES / "search_response_blocked.html").read_bytes()


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_good_response_returns_deduped_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search")
        assert request.url.params["format"] == "json"
        assert request.url.params["q"] == "python async playwright"
        assert request.url.params["safesearch"] == "0"
        assert request.url.params["language"] == "en"
        return httpx.Response(
            200, content=_good_payload(), headers={"content-type": "application/json"}
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/",
            "python async playwright",
            client=client,
            timeout=5.0,
        )
        urls = outcome.urls
    assert urls[0] == "https://playwright.dev/python/docs/intro"
    assert len(urls) == 5
    assert len(set(urls)) == 5


@pytest.mark.asyncio
async def test_caps_results_at_max() -> None:
    payload = {
        "results": [{"url": f"https://example-{i}.test/"} for i in range(12)],
    }
    raw = json.dumps(payload).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=raw, headers={"content-type": "application/json"}
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
        urls = outcome.urls
    assert len(urls) == 5
    assert urls == [f"https://example-{i}.test/" for i in range(5)]


@pytest.mark.asyncio
async def test_dedupes_duplicate_urls() -> None:
    payload = {
        "results": [
            {"url": "https://a.test/"},
            {"url": "https://b.test/"},
            {"url": "https://a.test/"},
            {"url": "https://c.test/"},
        ],
    }
    raw = json.dumps(payload).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=raw, headers={"content-type": "application/json"}
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
        urls = outcome.urls
    assert urls == ["https://a.test/", "https://b.test/", "https://c.test/"]


@pytest.mark.asyncio
async def test_empty_results_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_empty_payload(), headers={"content-type": "application/json"}
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
        urls = outcome.urls
    assert urls == []


@pytest.mark.asyncio
async def test_non_json_content_type_raises_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_blocked_html(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with _make_client(handler) as client:
        with pytest.raises(ProviderBlockedError):
            await searxng_search(
                "https://example.searxng/", "q", client=client, timeout=5.0
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_blocking_status_codes_raise_blocked(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"nope")

    async with _make_client(handler) as client:
        with pytest.raises(ProviderBlockedError):
            await searxng_search(
                "https://example.searxng/", "q", client=client, timeout=5.0
            )


@pytest.mark.asyncio
async def test_server_error_raises_http_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    async with _make_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await searxng_search(
                "https://example.searxng/", "q", client=client, timeout=5.0
            )


@pytest.mark.asyncio
async def test_drops_non_http_scheme_urls() -> None:
    payload = {
        "results": [
            {"url": "javascript:alert(1)"},
            {"url": "https://ok.example/"},
            {"url": "data:text/plain,evil"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
        urls = outcome.urls
    assert urls == ["https://ok.example/"]


@pytest.mark.asyncio
async def test_malformed_json_raises_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not-valid-json{{{",
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        with pytest.raises(ProviderBlockedError):
            await searxng_search(
                "https://example.searxng/", "q", client=client, timeout=5.0
            )


@pytest.mark.asyncio
async def test_upstream_error_field_is_reported_not_swallowed() -> None:
    """HTTP 200 with an `error` field is a failed search, not an empty one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"error": "too many requests"}).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.urls == []
    assert outcome.error == "too many requests"
    assert outcome.answered_empty is False


@pytest.mark.asyncio
async def test_unresponsive_engines_are_carried_out_of_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {"results": [], "unresponsive_engines": ["google:bot"]}
            ).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.urls == []
    assert outcome.unresponsive_engines == ("google:bot",)
    assert outcome.answered_empty is False


@pytest.mark.asyncio
async def test_missing_results_field_is_not_read_as_an_empty_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"suggestions": []}).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.urls == []
    assert outcome.error == "response carried no 'results' field"


@pytest.mark.asyncio
async def test_clean_empty_answer_is_a_trustworthy_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {"results": [], "unresponsive_engines": [], "answers": []}
            ).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.answered_empty is True


@pytest.mark.asyncio
@pytest.mark.parametrize("results", ["abc", {"a": "b"}, 42])
async def test_non_list_results_is_a_provider_failure_not_an_empty_search(
    results: object,
) -> None:
    """`{"results": "abc"}` iterates as characters, not as a search."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"results": results}).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.urls == []
    assert outcome.error
    assert outcome.answered_empty is False


@pytest.mark.asyncio
async def test_non_string_url_fails_the_shape_instead_of_raising() -> None:
    """`{"url": 42}` used to raise AttributeError past the cascade's handling."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"results": [{"url": 42}]}).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.urls == []
    assert "url is int" in outcome.error


@pytest.mark.asyncio
async def test_non_dict_entry_fails_the_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"results": ["just a string"]}).encode(),
            headers={"content-type": "application/json"},
        )

    async with _make_client(handler) as client:
        outcome = await searxng_search(
            "https://example.searxng/", "q", client=client, timeout=5.0
        )
    assert outcome.error
    assert "entry is str" in outcome.error
