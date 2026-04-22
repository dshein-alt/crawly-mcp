from pydantic import ValidationError

from crawly_mcp.constants import ALLOWED_PROVIDERS
from crawly_mcp.models import FetchRequest, SearchRequest


def test_search_request_schema_advertises_provider_enum() -> None:
    schema = SearchRequest.model_json_schema()
    provider_schema = schema["properties"]["provider"]
    allowed = set(ALLOWED_PROVIDERS)

    # The enum must appear somewhere in the provider schema so MCP clients
    # can read the valid values straight from tools/list.
    found_enum: set[str] = set()
    for branch in provider_schema.get("anyOf", [provider_schema]):
        if "enum" in branch:
            found_enum.update(branch["enum"])

    assert found_enum == allowed, (
        f"provider schema missing enum {allowed}; got {provider_schema!r}"
    )


def test_search_request_defaults_provider_and_trims_context() -> None:
    request = SearchRequest(provider=None, context="  test query  ")

    assert request.provider == "duckduckgo"
    assert request.context == "test query"


def test_search_request_rejects_unknown_provider() -> None:
    try:
        SearchRequest(provider="bing", context="python")
    except ValidationError as exc:
        message = str(exc)
        for allowed in ALLOWED_PROVIDERS:
            assert allowed in message, f"{allowed} missing from error: {message}"
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
