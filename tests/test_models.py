import asyncio

from pydantic import ValidationError

from crawly_mcp.constants import ALLOWED_PROVIDERS
from crawly_mcp.mcp_server import create_server
from crawly_mcp.models import (
    FetchRequest,
    PageSearchRequest,
    PageSearchResponse,
    PageSearchResult,
    SearchRequest,
)
from crawly_mcp.version import get_package_version


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


def test_fetch_request_schema_advertises_content_format_enum() -> None:
    schema = FetchRequest.model_json_schema()
    format_schema = schema["properties"]["content_format"]

    assert format_schema["default"] == "html"
    assert set(format_schema["enum"]) == {"html", "text"}


def test_fetch_tool_schema_advertises_content_format_enum() -> None:
    async def _run() -> dict:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "fetch":
                return tool.inputSchema
        raise AssertionError("fetch tool missing from list_tools()")

    schema = asyncio.run(_run())
    format_schema = schema["properties"]["content_format"]

    assert format_schema["default"] == "html"
    assert set(format_schema["enum"]) == {"html", "text"}


def test_create_server_reports_package_version() -> None:
    server = create_server()
    init_options = server._mcp_server.create_initialization_options()

    assert init_options.server_name == "crawly"
    assert init_options.server_version == get_package_version()


def test_create_server_instructions_guide_silent_page_search_workflow() -> None:
    server = create_server()
    init_options = server._mcp_server.create_initialization_options()
    instructions = init_options.instructions or ""

    assert "Use tools silently" in instructions
    assert "`page_search(url, query)`" in instructions
    assert "`fetch(..., content_format=\"text\")`" in instructions
    assert "Final answers should be concise prose" in instructions
    assert "Do not claim a timeout" in instructions


def test_page_search_request_requires_non_empty_query() -> None:
    try:
        PageSearchRequest(url="https://example.com", query="")
    except ValidationError as exc:
        assert "query" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_page_search_request_requires_non_empty_url() -> None:
    try:
        PageSearchRequest(url="", query="hello")
    except ValidationError as exc:
        assert "url" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_page_search_response_defaults() -> None:
    response = PageSearchResponse(
        mode="text",
        attempted=["text"],
        source_url="https://example.com",
        results_url=None,
        results=[],
        truncated=False,
    )
    assert response.mode == "text"
    assert response.results == []
    assert response.truncated is False


def test_page_search_result_allows_missing_url_and_title() -> None:
    result = PageSearchResult(snippet="hello world", url=None, title=None)
    assert result.snippet == "hello world"
    assert result.url is None
    assert result.title is None


def test_page_search_request_schema_advertises_query_and_url() -> None:
    schema = PageSearchRequest.model_json_schema()
    assert {"url", "query"}.issubset(schema["properties"].keys())
    assert schema["additionalProperties"] is False


def test_page_search_tool_schema_advertises_required_fields() -> None:
    async def _run() -> dict:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "page_search":
                return tool.inputSchema
        raise AssertionError("page_search tool missing from list_tools()")

    schema = asyncio.run(_run())
    assert set(schema["required"]) == {"url", "query"}
    assert schema["properties"]["url"]["type"] == "string"
    assert schema["properties"]["query"]["type"] == "string"


def test_page_search_tool_description_advertises_real_result_urls() -> None:
    async def _run() -> str:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "page_search":
                return tool.description
        raise AssertionError("page_search tool missing from list_tools()")

    description = asyncio.run(_run())

    assert "`mode`" in description
    assert "`results_url`" in description
    assert "real result URL to fetch next" in description
    assert "text snippets" in description


def test_fetch_tool_description_recommends_text_for_prose_answers() -> None:
    async def _run() -> str:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "fetch":
                return tool.description
        raise AssertionError("fetch tool missing from list_tools()")

    description = asyncio.run(_run())

    assert '`content_format="text"`' in description
    assert "final prose answers" in description
