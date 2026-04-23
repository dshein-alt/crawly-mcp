from crawly_mcp.mcp_cli import build_parser


def test_build_parser_uses_env_defaults(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("CRAWLY_PORT", "9000")

    args = build_parser().parse_args([])

    assert args.host == "0.0.0.0"  # noqa: S104
    assert args.port == 9000
    assert args.transport == "stdio"


def test_build_parser_allows_cli_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("CRAWLY_PORT", "9000")

    args = build_parser().parse_args(
        ["--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8000"]
    )

    assert args.transport == "streamable-http"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
