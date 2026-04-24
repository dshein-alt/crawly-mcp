from pathlib import Path

from crawly_mcp.parsing import (
    build_search_url,
    build_snippets,
    detect_algolia_config,
    extract_search_results,
    is_search_blocked,
    normalize_result_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_build_search_url_uses_duckduckgo_by_default() -> None:
    url = build_search_url(None, "python testing")

    assert url == "https://duckduckgo.com/?q=python+testing&ia=web"


def test_duckduckgo_fixture_extracts_unwrapped_links() -> None:
    urls = extract_search_results(
        "duckduckgo",
        _fixture("duckduckgo_results.html"),
        "https://duckduckgo.com/?q=python",
    )

    assert urls == [
        "https://example.com/alpha",
        "https://example.org/beta",
        "https://example.net/gamma",
    ]


def test_google_fixture_extracts_unwrapped_links() -> None:
    urls = extract_search_results(
        "google",
        _fixture("google_results.html"),
        "https://www.google.com/search?q=python",
    )

    assert urls == [
        "https://www.python.org/",
        "https://palletsprojects.com/p/flask/",
        "https://fastapi.tiangolo.com/",
    ]


def test_yandex_fixture_extracts_links() -> None:
    urls = extract_search_results(
        "yandex",
        _fixture("yandex_results.html"),
        "https://yandex.ru/search/?text=python",
    )

    assert urls == [
        "https://docs.python.org/3/",
        "https://www.djangoproject.com/",
        "https://pypi.org/",
    ]


def test_normalize_result_url_handles_known_redirect_wrappers() -> None:
    assert (
        normalize_result_url(
            "duckduckgo",
            "/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc",
            base_url="https://duckduckgo.com/?q=python",
        )
        == "https://example.com/doc"
    )
    assert (
        normalize_result_url(
            "google",
            "/url?q=https://example.org/path&sa=U",
            base_url="https://www.google.com/search?q=python",
        )
        == "https://example.org/path"
    )


def test_is_search_blocked_detects_challenge_copy() -> None:
    html = (
        "<html><body>Detected unusual traffic from your computer network.</body></html>"
    )

    assert (
        is_search_blocked("google", "https://www.google.com/sorry/index", "Sorry", html)
        is True
    )


def test_is_search_blocked_detects_duckduckgo_static_pages_redirect() -> None:
    # DuckDuckGo serves its bot-detection page at /static-pages/418.html
    # (observed in the wild). The markers must catch it even when the page
    # text itself has no suspicious keywords.
    url = "https://duckduckgo.com/static-pages/418.html?bno=84f2&is_tor=0&is_ar=0&is_netp=0"
    html = "<html><body>Something went wrong.</body></html>"

    assert is_search_blocked("duckduckgo", url, "DuckDuckGo", html) is True


def test_build_snippets_returns_empty_for_no_matches() -> None:
    result = build_snippets("the quick brown fox", "zebra", max_matches=5, context_chars=60)
    assert result == []


def test_build_snippets_case_insensitive_match() -> None:
    text = "First line.\nThe QUICK brown fox jumps.\nAnother line about something else.\n"
    snippets = build_snippets(text, "quick", max_matches=5, context_chars=40)
    assert len(snippets) == 1
    assert "quick" in snippets[0].lower()


def test_build_snippets_word_boundary_filters_substring_hits() -> None:
    text = "the keyboard has a spacer between keys"
    snippets = build_snippets(text, "space", max_matches=5, context_chars=50)
    assert snippets == []


def test_build_snippets_deduplicates_identical_snippets() -> None:
    text = "alpha beta\nalpha beta\nalpha beta\n"
    snippets = build_snippets(text, "alpha", max_matches=5, context_chars=50)
    assert len(snippets) == 1


def test_build_snippets_caps_at_max_matches() -> None:
    text = "\n".join(f"line {i} with target inside" for i in range(10))
    snippets = build_snippets(text, "target", max_matches=3, context_chars=40)
    assert len(snippets) == 3


def test_build_snippets_bounds_each_snippet_length() -> None:
    text = "before " * 500 + " target " + "after " * 500
    snippets = build_snippets(text, "target", max_matches=1, context_chars=100)
    assert len(snippets) == 1
    assert len(snippets[0]) <= 140


def test_detect_algolia_config_inline_json() -> None:
    html = """
    <html><head>
      <script type="application/json" id="docsearch-config">
        {"appId": "APPID123", "apiKey": "KEY456", "indexName": "my-docs"}
      </script>
      <script src="https://cdn.jsdelivr.net/npm/@docsearch/js@3"></script>
    </head></html>
    """
    config = detect_algolia_config(html)
    assert config is not None
    assert config["appId"] == "APPID123"
    assert config["apiKey"] == "KEY456"
    assert config["indexName"] == "my-docs"


def test_detect_algolia_config_inline_call() -> None:
    html = """
    <script>
      docsearch({
        appId: "X1",
        apiKey: "Y2",
        indexName: "docs",
        container: "#docsearch",
      });
    </script>
    """
    config = detect_algolia_config(html)
    assert config is not None
    assert config == {"appId": "X1", "apiKey": "Y2", "indexName": "docs"}


def test_detect_algolia_config_missing_returns_none() -> None:
    html = "<html><body>nothing here</body></html>"
    assert detect_algolia_config(html) is None


def test_detect_algolia_config_missing_required_field_returns_none() -> None:
    html = """
    <script>
      window.docSearchConfig = { appId: "A", apiKey: "K" };
    </script>
    """
    assert detect_algolia_config(html) is None
