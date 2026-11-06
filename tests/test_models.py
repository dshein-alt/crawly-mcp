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
    TierAttempt,
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


def test_search_request_defaults_to_no_provider_preference() -> None:
    """An omitted provider means "use the operator's priority order".

    Defaulting to a named provider made every caller request duckduckgo, so an
    operator who excluded it broke the default call path with an input error.
    """
    for value in (None, "", "auto", " AUTO "):
        request = SearchRequest(provider=value, context="  test query  ")
        assert request.provider is None, value
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
    assert '`fetch(..., content_format="text")`' in instructions
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
        status="no_results",
        next_action="refine_query",
        detail="nothing matched",
        mode="text",
        attempted=[TierAttempt(tier="text", outcome="empty", detail="no match")],
        source_url="https://example.com",
        results_url=None,
        results=[],
        truncated=False,
    )
    assert response.mode == "text"
    assert response.results == []
    assert response.truncated is False
    assert response.status == "no_results"
    assert response.attempted[0].outcome == "empty"


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


def test_search_tool_schema_advertises_provider_enum_and_descriptions() -> None:
    async def _run() -> dict:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "search":
                return tool.inputSchema
        raise AssertionError("search tool missing from list_tools()")

    schema = asyncio.run(_run())

    provider_schema = schema["properties"]["provider"]
    found_enum: set[str] = set()
    for branch in provider_schema.get("anyOf", [provider_schema]):
        if "enum" in branch:
            found_enum.update(branch["enum"])
    assert found_enum == set(ALLOWED_PROVIDERS) | {"auto"}, (
        f"provider tool schema missing enum {set(ALLOWED_PROVIDERS)} or 'auto'; "
        f"got {provider_schema!r}"
    )
    assert provider_schema.get("description"), (
        "provider parameter must carry a description so MCP clients know "
        "the allowed values without guessing"
    )
    for allowed in ALLOWED_PROVIDERS:
        assert allowed in provider_schema["description"], (
            f"{allowed!r} missing from provider description: "
            f"{provider_schema['description']!r}"
        )
    assert provider_schema.get("default") == "auto"

    context_schema = schema["properties"]["context"]
    assert context_schema.get("description"), (
        "context parameter must carry a description"
    )


def test_fetch_tool_schema_carries_parameter_descriptions() -> None:
    async def _run() -> dict:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "fetch":
                return tool.inputSchema
        raise AssertionError("fetch tool missing from list_tools()")

    schema = asyncio.run(_run())
    assert schema["properties"]["urls"].get("description")
    assert schema["properties"]["content_format"].get("description")


def test_page_search_tool_schema_carries_parameter_descriptions() -> None:
    async def _run() -> dict:
        server = create_server()
        tools = await server.list_tools()
        for tool in tools:
            if tool.name == "page_search":
                return tool.inputSchema
        raise AssertionError("page_search tool missing from list_tools()")

    schema = asyncio.run(_run())
    assert schema["properties"]["url"].get("description")
    assert schema["properties"]["query"].get("description")


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


def test_all_four_tools_are_advertised() -> None:
    async def _run() -> set[str]:
        server = create_server()
        return {tool.name for tool in await server.list_tools()}

    assert asyncio.run(_run()) == {"search", "fetch", "page_search", "providers"}


def test_tool_descriptions_teach_the_status_contract() -> None:
    """A model reads tool descriptions, not the source.

    The contract that an empty result is not evidence of an empty web has to
    be stated where the model will actually see it.
    """

    async def _run() -> dict[str, str]:
        server = create_server()
        return {tool.name: tool.description or "" for tool in await server.list_tools()}

    descriptions = asyncio.run(_run())
    assert "no_results" in descriptions["search"]
    assert "not evidence that the topic is empty" in descriptions["search"]
    assert "suggested_providers" in descriptions["search"]
    assert "Partial success is normal" in descriptions["fetch"]
    assert "refine_query" in descriptions["page_search"]


def test_server_instructions_state_the_empty_is_not_absent_rule() -> None:
    server = create_server()
    instructions = server._mcp_server.create_initialization_options().instructions or ""
    assert "status" in instructions
    assert "NOT" in instructions
    assert "Use tools silently" in instructions


def test_providers_tool_takes_no_arguments() -> None:
    async def _run() -> dict:
        server = create_server()
        for tool in await server.list_tools():
            if tool.name == "providers":
                return tool.inputSchema
        raise AssertionError("providers tool missing from list_tools()")

    schema = asyncio.run(_run())
    assert schema.get("properties") == {}
