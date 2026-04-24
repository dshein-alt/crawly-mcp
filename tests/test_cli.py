import json

import pytest

from crawly_mcp.cli import build_parser, main
from crawly_mcp.errors import InvalidInputError


def test_build_parser_accepts_search_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["search", "--context", "python"])

    assert args.command == "search"
    assert args.provider is None
    assert args.context == "python"


def test_main_prints_structured_error_for_search(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(provider: str | None, context: str) -> int:
        del provider, context
        raise InvalidInputError("context must be a non-empty search query")

    monkeypatch.setattr("crawly_mcp.cli.run_search_command", fake_search)

    exit_code = main(["search", "--context", "python"])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    payload = json.loads(stderr)
    assert payload["error"]["type"] == "invalid_input"


def test_build_parser_accepts_page_search_command() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["page-search", "--url", "https://example.com/", "--query", "hello"]
    )
    assert args.command == "page-search"
    assert args.url == "https://example.com/"
    assert args.query == "hello"


def test_main_prints_structured_error_for_page_search(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_page_search(url: str, query: str) -> int:
        del url, query
        raise InvalidInputError("query must be a non-empty string")

    monkeypatch.setattr("crawly_mcp.cli._run_page_search", fake_page_search)

    exit_code = main(["page-search", "--url", "https://example.com/", "--query", ""])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "query must be a non-empty string" in captured.err
