from pydantic import ValidationError

from web_search_mcp.models import FetchRequest, SearchRequest


def test_search_request_defaults_provider_and_trims_context() -> None:
    request = SearchRequest(provider=None, context="  test query  ")

    assert request.provider == "duckduckgo"
    assert request.context == "test query"


def test_search_request_rejects_unknown_provider() -> None:
    try:
        SearchRequest(provider="bing", context="python")
    except ValidationError as exc:
        assert "provider must be one of" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_fetch_request_rejects_more_than_five_urls() -> None:
    urls = [f"https://example.com/{index}" for index in range(6)]

    try:
        FetchRequest(urls=urls)
    except ValidationError as exc:
        assert "at most 5 URLs" in str(exc)
    else:
        raise AssertionError("expected ValidationError")
