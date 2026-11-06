from pathlib import Path

import pytest

from crawly_mcp.parsing import (
    SearchFormHit,
    build_search_url,
    build_snippets,
    classify_search_page,
    detect_algolia_config,
    detect_opensearch_href,
    detect_search_form,
    extract_opaque_result_urls,
    extract_search_results,
    is_search_blocked,
    normalize_result_url,
    parse_defer_redirect_target,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_build_search_url_builds_duckduckgo_url() -> None:
    url = build_search_url("duckduckgo", "python testing")

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
    result = build_snippets(
        "the quick brown fox", "zebra", max_matches=5, context_chars=60
    )
    assert result == []


def test_build_snippets_case_insensitive_match() -> None:
    text = (
        "First line.\nThe QUICK brown fox jumps.\nAnother line about something else.\n"
    )
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


def test_detect_opensearch_href_absolute() -> None:
    html = """
    <html><head>
      <link rel="search" type="application/opensearchdescription+xml"
            href="https://example.com/osd.xml" title="Example">
    </head></html>
    """
    href = detect_opensearch_href(html, base_url="https://example.com/page")
    assert href == "https://example.com/osd.xml"


def test_detect_opensearch_href_relative_resolved() -> None:
    html = """<link rel="search" type="application/opensearchdescription+xml" href="/osd.xml">"""
    href = detect_opensearch_href(html, base_url="https://docs.example.com/a/b")
    assert href == "https://docs.example.com/osd.xml"


def test_detect_opensearch_href_missing() -> None:
    html = "<html><head></head></html>"
    assert detect_opensearch_href(html, base_url="https://example.com/") is None


def test_detect_opensearch_href_wrong_type_ignored() -> None:
    html = """<link rel="search" type="application/rss+xml" href="/rss.xml">"""
    assert detect_opensearch_href(html, base_url="https://example.com/") is None


def test_detect_search_form_role_search_priority() -> None:
    html = """
    <form action="/fallback" method="get"><input name="q"></form>
    <form role="search" action="/s" method="get"><input name="query"></form>
    """
    hit = detect_search_form(html, base_url="https://example.com/")
    assert hit is not None
    assert hit.action == "https://example.com/s"
    assert hit.input_name == "query"


def test_detect_search_form_role_search_skips_hidden_inputs() -> None:
    html = """
    <form role="search" action="/s" method="get">
      <input type="hidden" name="utf8" value="✓">
      <input type="search" name="Query">
    </form>
    """
    hit = detect_search_form(html, base_url="https://example.com/")
    assert hit is not None
    assert hit.input_name == "Query"


def test_detect_search_form_input_type_search() -> None:
    html = """<form action="/go" method="get"><input type="search" name="s"></form>"""
    hit = detect_search_form(html, base_url="https://example.com/")
    assert hit is not None
    assert hit.input_name == "s"


def test_detect_search_form_input_name_fallback() -> None:
    html = """<form action="/do" method="get"><input name="query" type="text"></form>"""
    hit = detect_search_form(html, base_url="https://example.com/")
    assert hit is not None
    assert hit.input_name == "query"


def test_detect_search_form_input_name_fallback_preserves_case() -> None:
    html = """<form action="/do" method="get"><input name="Query" type="text"></form>"""
    hit = detect_search_form(html, base_url="https://example.com/")
    assert hit is not None
    assert hit.input_name == "Query"


def test_detect_search_form_skips_post_forms() -> None:
    html = """<form action="/s" method="POST"><input name="q"></form>"""
    assert detect_search_form(html, base_url="https://example.com/") is None


def test_detect_search_form_empty_action_resolves_to_source_url() -> None:
    html = """<form method="get"><input name="q"></form>"""
    hit = detect_search_form(html, base_url="https://example.com/docs/page.html")
    assert hit is not None
    assert hit.action == "https://example.com/docs/page.html"
    assert hit.input_name == "q"


def test_detect_search_form_relative_action_resolved() -> None:
    html = """<form action="search.html" method="get"><input name="q"></form>"""
    hit = detect_search_form(html, base_url="https://example.com/docs/")
    assert hit is not None
    assert hit.action == "https://example.com/docs/search.html"


def test_search_form_hit_type_available() -> None:
    assert SearchFormHit is not None


def test_zero_results_page_yields_no_urls() -> None:
    """A page that says it found nothing must never yield result URLs.

    The old parser harvested DuckDuckGo's zero-hit page chrome and returned
    five popular domains as if they were ranked hits, so a niche query came
    back looking like a healthy search.
    """
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=zzqxvplm+qkfwjzb+tmvqxoze&ia=web",
        "zzqxvplm qkfwjzb tmvqxoze at DuckDuckGo",
        _fixture("duckduckgo_zero_results.html"),
    )
    assert evidence.kind == "zero_results"
    assert "no results" in evidence.detail


def test_shell_page_is_not_reported_as_empty_results() -> None:
    """DuckDuckGo's no-JavaScript interstitial is a provider failure.

    It renders ~1 KB of text and no results, which the old parser reported as
    `urls: []` — indistinguishable from "the web has nothing on this topic".
    """
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=patchright&ia=web",
        "patchright at DuckDuckGo",
        _fixture("duckduckgo_shell.html"),
    )
    assert evidence.kind == "shell"
    assert "interstitial" in evidence.detail


def test_captcha_page_is_classified_as_a_wall() -> None:
    evidence = classify_search_page(
        "google",
        "https://www.google.com/sorry/index",
        "Unusual traffic from your computer network",
        _fixture("google_sorry.html"),
    )
    assert evidence.kind == "captcha"
    assert evidence.marker == "detected unusual traffic"


def test_goto_only_results_page_reports_unextractable_urls() -> None:
    """Static parsing counts opaque links without inventing their targets.

    The service resolves the real redirect responses separately; the parser
    itself must neither guess targets nor report the results page as empty.
    """
    html = _fixture("google_goto_only.html")
    evidence = classify_search_page(
        "google",
        "https://www.google.com/search?q=python&hl=en",
        "python - Google Search",
        html,
    )
    assert evidence.kind == "results"
    assert evidence.result_anchors == 0
    assert evidence.opaque_anchors == 2
    assert (
        extract_search_results(
            "google", html, "https://www.google.com/search?q=python&hl=en"
        )
        == []
    )


def test_results_page_is_classified_with_anchor_counts() -> None:
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=python&ia=web",
        "python at DuckDuckGo",
        _fixture("duckduckgo_results.html"),
    )
    assert evidence.kind == "results"
    assert evidence.result_anchors == 3


def test_links_outside_result_containers_are_not_results() -> None:
    urls = extract_search_results(
        "duckduckgo",
        """
        <html><body>
          <nav><a class="result__a" href="https://duckduckgo.com/ads">Ads</a></nav>
          <footer><a class="result__a" href="https://example.com/footer">f</a></footer>
          <article><a class="result__a" href="https://example.com/real">r</a></article>
        </body></html>
        """,
        "https://duckduckgo.com/?q=python&ia=web",
    )
    assert urls == ["https://example.com/real"]


def test_build_snippets_matches_multi_word_queries() -> None:
    """A natural-language query must not be matched as one literal phrase.

    The page text separates the query words with markup, so the old
    phrase-anchored match returned nothing for every multi-word query.
    """
    text = (
        "Installing the browser\n"
        "Run the install command to add the chromium browser to your system. "
        "Chromium downloads are large."
    )
    snippets = build_snippets(
        text,
        "how to install chromium browser",
        max_matches=3,
        context_chars=120,
    )
    assert snippets
    assert "chromium" in snippets[0].lower()


def test_build_snippets_prefers_an_exact_phrase_match() -> None:
    text = "The network idle option waits. Nothing else here about idle."
    snippets = build_snippets(text, "network idle", max_matches=2, context_chars=80)
    assert snippets[0].startswith("The network idle option")


def test_query_echo_in_the_title_cannot_fake_a_zero_hit_page() -> None:
    """The caller's own words are subtracted before markers run.

    Searching "sqlite no results" puts that phrase in the page title, and a
    marker matched against the whole page then reads a page with three real
    results as a zero-hit page — which also stops a cascade that should have
    continued.
    """
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=sqlite+no+results&ia=web",
        "sqlite no results at DuckDuckGo",
        _fixture("duckduckgo_results.html"),
        query="sqlite no results",
    )
    assert evidence.kind == "results"
    assert evidence.result_anchors == 3


def test_result_snippets_cannot_trigger_a_consent_wall() -> None:
    """A page about cookie consent is content about consent, not a consent wall."""
    html = """
    <html><head><title>cookie consent at DuckDuckGo</title></head><body>
      <article class="result">
        <h2><a class="result__a" href="https://gdpr.example/cookie-consent">
          Cookie consent guide</a></h2>
        <p>How to implement cookie consent and manage the cookie consent banner.</p>
      </article>
    </body></html>
    """
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=cookie+consent&ia=web",
        "cookie consent at DuckDuckGo",
        html,
        query="cookie consent",
    )
    assert evidence.kind == "results"
    assert evidence.result_anchors == 1


def test_genuine_zero_hit_page_stays_zero_results_when_the_query_echoes() -> None:
    """Removing the echo must not remove the provider's own statement."""
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=zzqxvplm+qkfwjzb&ia=web",
        "zzqxvplm qkfwjzb at DuckDuckGo",
        _fixture("duckduckgo_zero_results.html"),
        query="zzqxvplm qkfwjzb",
    )
    assert evidence.kind == "zero_results"


def test_a_real_wall_stays_a_wall_when_the_query_names_one() -> None:
    evidence = classify_search_page(
        "google",
        "https://www.google.com/sorry/index",
        "Unusual traffic from your computer network",
        _fixture("google_sorry.html"),
        query="unusual traffic",
    )
    assert evidence.kind == "captcha"


def test_a_page_about_a_wall_topic_is_not_reported_as_a_wall() -> None:
    """Result text about "automated requests" is content, not a bot refusal.

    "automated requests" is a DuckDuckGo CAPTCHA marker, and it is also the
    subject of the page's own results; matching markers against result snippets
    turned a healthy results page into a refusal.
    """
    html = """
    <html><head><title>automated requests at DuckDuckGo</title></head><body>
      <article class="result">
        <h2><a class="result__a" href="https://api.example/rate-limits">
          Rate limiting automated requests</a></h2>
        <p>How to throttle automated requests from your API client.</p>
      </article>
    </body></html>
    """
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=automated+requests&ia=web",
        "automated requests at DuckDuckGo",
        html,
        query="automated requests",
    )
    assert evidence.kind == "results"
    assert evidence.result_anchors == 1


def test_a_query_that_names_the_providers_own_words_still_reads_as_empty() -> None:
    """The provider's statement survives the removal of the caller's echo.

    Stripping the query from the whole page deleted "results" from DuckDuckGo's
    own "No results for results", and the zero-hit page came back as a results
    page carrying Microsoft and Google Translate — the exact fabrication this
    classifier exists to stop.
    """
    for query in ("results", "no results"):
        evidence = classify_search_page(
            "duckduckgo",
            f"https://duckduckgo.com/?q={query.replace(' ', '+')}&ia=web",
            f"{query} at DuckDuckGo",
            _fixture("duckduckgo_zero_results.html"),
            query=query,
        )
        assert evidence.kind == "zero_results", f"query={query!r} -> {evidence.kind}"


def test_the_echo_is_removed_from_the_title_not_from_the_prose() -> None:
    """Title echo goes, provider prose stays, in the same page."""
    html = """
    <html><head><title>no results at DuckDuckGo</title></head><body>
      <p>No results found. Try checking your spelling.</p>
    </body></html>
    """
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=no+results&ia=web",
        "no results at DuckDuckGo",
        html,
        query="no results",
    )
    assert evidence.kind == "zero_results"


@pytest.mark.parametrize("query", ["Cookie Consent", "SQLite No Results"])
def test_mixed_case_title_echo_is_not_a_status(query: str) -> None:
    evidence = classify_search_page(
        "duckduckgo",
        "https://duckduckgo.com/?q=test",
        f"{query} at DuckDuckGo",
        _fixture("duckduckgo_results.html"),
        query=query,
    )
    assert evidence.kind == "results"


@pytest.mark.parametrize(
    "heading",
    [
        "Search results for <em>No Results</em>",
        "Results matching 'no results'",
        "You searched for no results",
    ],
)
def test_labelled_body_echo_cannot_fake_a_zero_hit_page(heading: str) -> None:
    html = (
        f"<html><body><h1>{heading}</h1><article>"
        '<a class="result__a" href="https://example.com/answer">Answer</a>'
        "</article></body></html>"
    )
    evidence = classify_search_page(
        "duckduckgo", "https://duckduckgo.com/", "Search", html, query="no results"
    )
    assert evidence.kind == "results"
    assert evidence.result_anchors == 1


@pytest.mark.parametrize("query", ["results", "no results", "Cookie Consent"])
def test_zero_hit_statement_survives_a_matching_body_echo(query: str) -> None:
    html = _fixture("duckduckgo_zero_results.html").replace(
        "zzqxvplm qkfwjzb tmvqxoze", query
    )
    evidence = classify_search_page(
        "duckduckgo", "https://duckduckgo.com/", query, html, query=query
    )
    assert evidence.kind == "zero_results"


def test_defer_page_target_is_followed_only_when_it_is_a_result_redirect() -> None:
    """A refresh may point the retry at the redirect endpoint, never a host."""
    base = "https://www.google.com/goto?url=TOKEN"
    throttle = (
        '<html><head><meta http-equiv="refresh" '
        'content="3;url=/goto?url=FRESH"></head><body></body></html>'
    )
    assert (
        parse_defer_redirect_target(throttle, base)
        == "https://www.google.com/goto?url=FRESH"
    )

    # A page that declares a refresh to an arbitrary site is not a throttle
    # page, and must not be able to aim the resolver at that site.
    hostile = (
        '<html><head><meta http-equiv="refresh" '
        'content="0;url=https://evil.example/pwned"></head></html>'
    )
    assert parse_defer_redirect_target(hostile, base) is None
    assert parse_defer_redirect_target("<html><body>plain</body></html>", base) is None


def test_opaque_links_can_be_collected_past_the_return_cap() -> None:
    """The caller needs a throttle budget, so collection is not capped at 5."""
    html = _fixture("google_goto_many.html")
    base = "https://www.google.com/search?q=python&hl=en"
    assert len(extract_opaque_result_urls("google", html, base)) == 5
    assert len(extract_opaque_result_urls("google", html, base, limit=12)) == 8
