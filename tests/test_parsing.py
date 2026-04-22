from pathlib import Path

from crawly_mcp.parsing import (
    build_search_url,
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
        "https://yandex.com/search/?text=python",
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
    html = "<html><body>Detected unusual traffic from your computer network.</body></html>"

    assert is_search_blocked("google", "https://www.google.com/sorry/index", "Sorry", html) is True


def test_is_search_blocked_detects_duckduckgo_static_pages_redirect() -> None:
    # DuckDuckGo serves its bot-detection page at /static-pages/418.html
    # (observed in the wild). The markers must catch it even when the page
    # text itself has no suspicious keywords.
    url = "https://duckduckgo.com/static-pages/418.html?bno=84f2&is_tor=0&is_ar=0&is_netp=0"
    html = "<html><body>Something went wrong.</body></html>"

    assert is_search_blocked("duckduckgo", url, "DuckDuckGo", html) is True
