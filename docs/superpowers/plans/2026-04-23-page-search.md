# Page search — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a new MCP tool `page_search(url, query)` that finds content on a single page via a three-tier cascade: known site-search facilities (Algolia DocSearch / OpenSearch descriptor / Readthedocs API) → generic GET form detection → find-in-page text fallback.

**Architecture:** New self-contained `PageSearchService` in `src/crawly_mcp/page_search.py` orchestrating five tier classes (`AlgoliaTier`, `OpenSearchTier`, `ReadthedocsTier`, `FormTier`, `TextTier`), each with a pure `detect()` step and an async `execute()` step. Source HTML is fetched once via `BrowserManager.new_context()` (ephemeral, same path as `fetch()`); tier 1a/1c use direct `httpx` HTTPS with SSRF pre-validation; tiers 1b/2 reuse `BrowserManager` for navigation; tier 3 runs on the already-fetched source HTML. Outer `asyncio.timeout(FETCH_TOTAL_TIMEOUT_SECONDS)` clamps the whole call; per-tier `asyncio.wait_for(PAGE_SEARCH_TIER_TIMEOUT_SECONDS)` limits individual tiers. New pure-function helpers in `parsing.py` (`detect_algolia_config`, `detect_opensearch_href`, `detect_search_form`, `build_snippets`) keep detection unit-testable without browser fakes.

**Tech Stack:** Python 3.11+, `httpx` (promoted from transitive to explicit dependency), `patchright` (existing), `BeautifulSoup` (existing), `loguru`, `pytest` + `pytest-asyncio`, `ruff`.

**Reference spec:** [docs/superpowers/specs/2026-04-23-page-search-design.md](../specs/2026-04-23-page-search-design.md). When this plan is terse, the spec has the semantic detail.

---

## File structure

**New runtime modules:**
- `src/crawly_mcp/page_search.py` — `PageSearchService`, `DetectResult` dataclasses, five `Tier` classes, `_truncate_page_search_response`.

**Modified runtime modules:**
- `src/crawly_mcp/constants.py` — add `PageSearchMode` Literal, `PAGE_SEARCH_TIER_TIMEOUT_SECONDS = 10`, `PAGE_SEARCH_SNIPPET_CONTEXT_CHARS = 240`.
- `src/crawly_mcp/models.py` — add `PageSearchRequest`, `PageSearchResult`, `PageSearchResponse`.
- `src/crawly_mcp/parsing.py` — add `detect_algolia_config`, `detect_opensearch_href`, `detect_search_form`, `build_snippets`.
- `src/crawly_mcp/mcp_server.py` — register `page_search` tool, wire `PageSearchService` into the `lifespan`.
- `src/crawly_mcp/cli.py` — add `page-search` subcommand.

**New test files:**
- `tests/test_page_search.py` — tier execution + `PageSearchService` orchestration.

**Modified test files:**
- `tests/test_parsing.py` — detector and snippet-builder tests.
- `tests/test_constants.py` — assert new constants.
- `tests/test_models.py` — validate new request/response models + tool-schema introspection.
- `tests/test_cli.py` — `page-search` subcommand tests.

**Config & docs:**
- `pyproject.toml` — bump version `0.1.0` → `0.2.0`; promote `httpx` to explicit dependency.
- `CHANGELOG.md` — new `## [0.2.0]` section above the existing `## [0.1.0]` stub.
- `README.md` — document the new tool and CLI subcommand.

---

## Chunk 1: Foundation — dependency, version bump, constants, models

### Task 1: Add httpx as explicit dependency and bump version

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add httpx pinned to a range compatible with the transitive mcp pin**

Run: `uv add 'httpx>=0.27,<1.0'`
Expected: `httpx` appears in `pyproject.toml` `[project.dependencies]`; `uv.lock` unchanged in practice (mcp already pulls a compatible version).

- [ ] **Step 2: Bump version in pyproject.toml**

Edit `pyproject.toml`:
```toml
version = "0.2.0"
```
Expected: line 3 now reads `version = "0.2.0"`.

- [ ] **Step 3: Verify imports and version resolve**

Run: `uv run python -c "import httpx; from crawly_mcp.version import get_package_version; print(httpx.__version__, get_package_version())"`
Expected: prints an `httpx` version and `0.2.0`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: bump to 0.2.0 and promote httpx to explicit dependency"
```

### Task 2: Add page_search constants

**Files:**
- Modify: `src/crawly_mcp/constants.py`
- Modify: `tests/test_constants.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_constants.py`:
```python
def test_page_search_constants_exported() -> None:
    assert constants.PAGE_SEARCH_TIER_TIMEOUT_SECONDS == 10
    assert constants.PAGE_SEARCH_SNIPPET_CONTEXT_CHARS == 240
    assert set(get_args(constants.PageSearchMode)) == {
        "algolia", "opensearch", "readthedocs", "form", "text",
    }
```
At the top of the file (if not already imported): `from typing import get_args`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_constants.py::test_page_search_constants_exported -v`
Expected: FAIL with `AttributeError: module 'crawly_mcp.constants' has no attribute 'PAGE_SEARCH_TIER_TIMEOUT_SECONDS'`.

- [ ] **Step 3: Implement constants**

Append to `src/crawly_mcp/constants.py`:
```python
PageSearchMode = Literal["algolia", "opensearch", "readthedocs", "form", "text"]
PAGE_SEARCH_TIER_TIMEOUT_SECONDS = 10
PAGE_SEARCH_SNIPPET_CONTEXT_CHARS = 240
```
(`Literal` is already imported at the top of the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_constants.py::test_page_search_constants_exported -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/constants.py tests/test_constants.py
git commit -m "feat(constants): add page_search tier timeout and snippet width"
```

### Task 3: Add PageSearchRequest / Result / Response models

**Files:**
- Modify: `src/crawly_mcp/models.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_models.py`:
```python
from crawly_mcp.models import PageSearchRequest, PageSearchResponse, PageSearchResult


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -k page_search -v`
Expected: FAIL on import — `ImportError: cannot import name 'PageSearchRequest'`.

- [ ] **Step 3: Implement the models**

Append to `src/crawly_mcp/models.py`:
```python
from crawly_mcp.constants import PageSearchMode


class PageSearchRequest(BaseModel):
    url: str
    query: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("url")
    @classmethod
    def _url_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url must be a non-empty string")
        return value

    @field_validator("query")
    @classmethod
    def _query_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must be a non-empty string")
        return value


class PageSearchResult(BaseModel):
    snippet: str
    url: str | None = None
    title: str | None = None


class PageSearchResponse(BaseModel):
    mode: PageSearchMode
    attempted: list[PageSearchMode]
    source_url: str
    results_url: str | None = None
    results: list[PageSearchResult] = Field(default_factory=list)
    truncated: bool = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -k page_search -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all 81+ tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/models.py tests/test_models.py
git commit -m "feat(models): add PageSearchRequest, PageSearchResult, PageSearchResponse"
```

---

## Chunk 2: Parsing helpers — detectors and snippet builder

Pure functions, pure TDD. No browser, no HTTP, no services. Each helper is unit-tested in isolation.

### Task 4: Implement `build_snippets`

**Files:**
- Modify: `src/crawly_mcp/parsing.py`
- Modify: `tests/test_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_parsing.py`:
```python
from crawly_mcp.parsing import build_snippets


def test_build_snippets_returns_empty_for_no_matches() -> None:
    result = build_snippets("the quick brown fox", "zebra", max_matches=5, context_chars=60)
    assert result == []


def test_build_snippets_case_insensitive_match() -> None:
    text = "First line.\nThe QUICK brown fox jumps.\nAnother line about something else.\n"
    snippets = build_snippets(text, "quick", max_matches=5, context_chars=40)
    assert len(snippets) == 1
    assert "quick" in snippets[0].lower()


def test_build_snippets_word_boundary_filters_substring_hits() -> None:
    # "spacer" should not match query "space"
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
    # snippet length should be roughly context_chars; allow slack for boundary clamping
    assert len(snippets[0]) <= 140
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsing.py -k build_snippets -v`
Expected: FAIL on import — `ImportError: cannot import name 'build_snippets'`.

- [ ] **Step 3: Implement `build_snippets`**

First, add `import re` to the top of `src/crawly_mcp/parsing.py` alongside the other stdlib imports (the module does not currently import `re`).

Then append to `src/crawly_mcp/parsing.py`:
```python
def build_snippets(
    text: str,
    query: str,
    *,
    max_matches: int,
    context_chars: int,
) -> list[str]:
    """Return up to `max_matches` de-duplicated text snippets around matches of
    `query` in `text`. Matches are case-insensitive with word-boundary enforcement
    on each end of the query that looks like a word character. Each snippet is
    approximately `context_chars` wide, clamped to whitespace boundaries when
    possible to avoid cutting mid-word.
    """
    query_stripped = query.strip()
    if not query_stripped:
        return []

    pattern = _word_boundary_pattern(query_stripped)
    half = max(context_chars // 2, 20)

    snippets: list[str] = []
    seen: set[str] = set()

    for match in pattern.finditer(text):
        if len(snippets) >= max_matches:
            break
        start = max(0, match.start() - half)
        end = min(len(text), match.end() + half)

        # Clamp start/end to whitespace when it's nearby, so we don't cut words.
        while start > 0 and not text[start - 1].isspace() and match.start() - start < half + 10:
            start -= 1
        while end < len(text) and not text[end].isspace() and end - match.end() < half + 10:
            end += 1

        raw = text[start:end].strip()
        normalized = re.sub(r"\s+", " ", raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            snippets.append(normalized)

    return snippets


def _word_boundary_pattern(query: str) -> "re.Pattern[str]":
    left = r"\b" if query[0].isalnum() else ""
    right = r"\b" if query[-1].isalnum() else ""
    return re.compile(f"{left}{re.escape(query)}{right}", re.IGNORECASE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsing.py -k build_snippets -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/parsing.py tests/test_parsing.py
git commit -m "feat(parsing): add build_snippets for find-in-page tier"
```

### Task 5: Implement `detect_algolia_config`

**Files:**
- Modify: `src/crawly_mcp/parsing.py`
- Modify: `tests/test_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_parsing.py`:
```python
from crawly_mcp.parsing import detect_algolia_config


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
    # indexName missing → detection fails
    html = """
    <script>
      window.docSearchConfig = { appId: "A", apiKey: "K" };
    </script>
    """
    assert detect_algolia_config(html) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsing.py -k algolia -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `detect_algolia_config`**

Append to `src/crawly_mcp/parsing.py`:
```python
import json as _json  # only for the JSON <script> branch


def detect_algolia_config(html: str) -> dict[str, str] | None:
    """Return {'appId', 'apiKey', 'indexName'} if DocSearch config is on the
    page, else None. Tries (in order): JSON <script> islands, inline
    `docsearch({...})` calls, `window.docSearchConfig = {...}` assignments.
    """
    # 1. JSON <script> island (e.g. type="application/json")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", {"type": "application/json"}):
        content = tag.string or tag.get_text()
        if not content:
            continue
        try:
            parsed = _json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and _has_algolia_keys(parsed):
            return _algolia_subset(parsed)

    # 2. Inline JS heuristics over raw text
    return _scan_algolia_inline(html)


_ALGOLIA_REQUIRED = ("appId", "apiKey", "indexName")


def _has_algolia_keys(payload: dict) -> bool:
    return all(payload.get(key) for key in _ALGOLIA_REQUIRED)


def _algolia_subset(payload: dict) -> dict[str, str]:
    return {key: str(payload[key]) for key in _ALGOLIA_REQUIRED}


_ALGOLIA_KEY_VALUE = re.compile(
    r"""(?P<key>appId|apiKey|indexName)\s*:\s*["'](?P<val>[^"']+)["']""",
    re.VERBOSE,
)


def _scan_algolia_inline(html: str) -> dict[str, str] | None:
    # Grep the raw HTML for `key: "value"` pairs inside any <script> block
    # (BeautifulSoup loses non-text script content on some parsers).
    found: dict[str, str] = {}
    for match in _ALGOLIA_KEY_VALUE.finditer(html):
        found[match.group("key")] = match.group("val")
        if _has_algolia_keys(found):
            return _algolia_subset(found)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsing.py -k algolia -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/parsing.py tests/test_parsing.py
git commit -m "feat(parsing): detect Algolia DocSearch config on page"
```

### Task 6: Implement `detect_opensearch_href`

**Files:**
- Modify: `src/crawly_mcp/parsing.py`
- Modify: `tests/test_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_parsing.py`:
```python
from crawly_mcp.parsing import detect_opensearch_href


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsing.py -k opensearch -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `detect_opensearch_href`**

`parsing.py` already imports `urljoin` from `urllib.parse` at the top — reuse that. Append to `src/crawly_mcp/parsing.py`:
```python
def detect_opensearch_href(html: str, *, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", rel=True):
        rels = link.get("rel") or []
        if "search" not in [r.lower() for r in rels]:
            continue
        if (link.get("type") or "").lower() != "application/opensearchdescription+xml":
            continue
        href = (link.get("href") or "").strip()
        if not href:
            continue
        return urljoin(base_url, href)
    return None
```
(`BeautifulSoup` already imported in `parsing.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsing.py -k opensearch -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/parsing.py tests/test_parsing.py
git commit -m "feat(parsing): detect OpenSearch descriptor link"
```

### Task 7: Implement `detect_search_form`

**Files:**
- Modify: `src/crawly_mcp/parsing.py`
- Modify: `tests/test_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_parsing.py`:
```python
from crawly_mcp.parsing import detect_search_form, SearchFormHit


def test_detect_search_form_role_search_priority() -> None:
    html = """
    <form action="/fallback" method="get"><input name="q"></form>
    <form role="search" action="/s" method="get"><input name="query"></form>
    """
    hit = detect_search_form(html, base_url="https://example.com/")
    assert hit is not None
    assert hit.action == "https://example.com/s"
    assert hit.input_name == "query"


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


def test_detect_search_form_skips_post_forms() -> None:
    html = """<form action="/s" method="POST"><input name="q"></form>"""
    assert detect_search_form(html, base_url="https://example.com/") is None


def test_detect_search_form_requires_non_empty_action() -> None:
    html = """<form method="get"><input name="q"></form>"""
    assert detect_search_form(html, base_url="https://example.com/") is None


def test_detect_search_form_relative_action_resolved() -> None:
    html = """<form action="search.html" method="get"><input name="q"></form>"""
    hit = detect_search_form(html, base_url="https://example.com/docs/")
    assert hit is not None
    assert hit.action == "https://example.com/docs/search.html"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsing.py -k search_form -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `detect_search_form`**

Append to `src/crawly_mcp/parsing.py`:
```python
from dataclasses import dataclass


_SEARCH_INPUT_NAMES = ("q", "query", "search", "s")


@dataclass(frozen=True)
class SearchFormHit:
    action: str      # absolute URL
    input_name: str  # query parameter name


def detect_search_form(html: str, *, base_url: str) -> SearchFormHit | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates = [
        _match_role_search,
        _match_input_type_search,
        _match_input_name_fallback,
    ]
    for matcher in candidates:
        hit = matcher(soup, base_url)
        if hit is not None:
            return hit
    return None


def _iter_get_forms(soup: BeautifulSoup):
    for form in soup.find_all("form"):
        method = (form.get("method") or "get").lower()
        if method != "get":
            continue
        action = (form.get("action") or "").strip()
        if not action:
            continue
        yield form, action


def _hit_for(action: str, base_url: str, input_name: str) -> SearchFormHit:
    return SearchFormHit(action=urljoin(base_url, action), input_name=input_name)


def _first_named_input(form) -> str | None:
    for inp in form.find_all("input"):
        name = (inp.get("name") or "").strip()
        if name:
            return name
    return None


def _match_role_search(soup, base_url: str) -> SearchFormHit | None:
    for form, action in _iter_get_forms(soup):
        if (form.get("role") or "").lower() != "search":
            continue
        name = _first_named_input(form)
        if name:
            return _hit_for(action, base_url, name)
    return None


def _match_input_type_search(soup, base_url: str) -> SearchFormHit | None:
    for form, action in _iter_get_forms(soup):
        for inp in form.find_all("input"):
            if (inp.get("type") or "").lower() != "search":
                continue
            name = (inp.get("name") or "").strip()
            if name:
                return _hit_for(action, base_url, name)
    return None


def _match_input_name_fallback(soup, base_url: str) -> SearchFormHit | None:
    for form, action in _iter_get_forms(soup):
        for inp in form.find_all("input"):
            name = (inp.get("name") or "").strip().lower()
            if name in _SEARCH_INPUT_NAMES:
                return _hit_for(action, base_url, name)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsing.py -k search_form -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Run the full parsing suite and lint**

Run: `uv run pytest tests/test_parsing.py -v && uv run ruff check .`
Expected: all parsing tests pass; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/parsing.py tests/test_parsing.py
git commit -m "feat(parsing): detect generic GET search forms"
```

---

## Chunk 3: Tier implementations

Each tier is a class exposing `detect(html, source_url) -> DetectResult | None` and `async execute(hit, query) -> list[PageSearchResult]`. Implemented in dependency order: simplest first, so downstream tests can reuse the simpler tiers' helpers.

### Task 8: TextTier (tier 3)

**Files:**
- Create: `src/crawly_mcp/page_search.py`
- Create: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_page_search.py`:
```python
from __future__ import annotations

import pytest

from crawly_mcp.models import PageSearchResult
from crawly_mcp.page_search import TextTier, TextHit


def test_text_tier_detect_always_returns_hit() -> None:
    tier = TextTier()
    hit = tier.detect("<html><title>T</title><body>hello</body></html>", "https://example.com/")
    assert isinstance(hit, TextHit)
    assert hit.title == "T"


@pytest.mark.asyncio
async def test_text_tier_execute_returns_snippets() -> None:
    html = "<html><title>Docs</title><body><p>fetch returns structured content</p></body></html>"
    tier = TextTier()
    hit = tier.detect(html, "https://example.com/")
    results = await tier.execute(hit, "structured")
    assert len(results) == 1
    assert isinstance(results[0], PageSearchResult)
    assert "structured" in results[0].snippet.lower()
    assert results[0].url is None
    assert results[0].title == "Docs"


@pytest.mark.asyncio
async def test_text_tier_execute_empty_on_no_match() -> None:
    html = "<html><title>T</title><body>nothing relevant here</body></html>"
    tier = TextTier()
    hit = tier.detect(html, "https://example.com/")
    results = await tier.execute(hit, "structured")
    assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -v`
Expected: FAIL on import — `ImportError: cannot import name 'TextTier'`.

- [ ] **Step 3: Create `page_search.py` with TextTier skeleton**

Create `src/crawly_mcp/page_search.py` with the full import block up-front — tiers across Tasks 8-15 will reuse these imports, so establishing them now avoids scattering imports mid-file later:
```python
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from patchright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import ValidationError

from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import (
    FETCH_PAGE_TIMEOUT_SECONDS,
    FETCH_TOTAL_TIMEOUT_SECONDS,
    MAX_SEARCH_RESULTS,
    PAGE_SEARCH_SNIPPET_CONTEXT_CHARS,
    PAGE_SEARCH_TIER_TIMEOUT_SECONDS,
)
from crawly_mcp.errors import (
    InvalidInputError,
    NavigationFailedError,
    TimeoutExceededError,
)
from crawly_mcp.models import (
    PageSearchRequest,
    PageSearchResponse,
    PageSearchResult,
)
from crawly_mcp.parsing import (
    SearchFormHit,
    build_snippets,
    detect_algolia_config,
    detect_opensearch_href,
    detect_search_form,
)
from crawly_mcp.security import URLSafetyGuard
from crawly_mcp.service import extract_readable_text, resolve_fetch_max_size


@dataclass(frozen=True)
class TextHit:
    text: str
    title: str | None


class TextTier:
    name = "text"

    def detect(self, source_html: str, source_url: str) -> TextHit:
        soup = BeautifulSoup(source_html, "html.parser")
        title_tag = soup.title
        title = title_tag.string.strip() if title_tag and title_tag.string else None
        text = extract_readable_text(source_html)
        return TextHit(text=text, title=title)

    async def execute(self, hit: TextHit, query: str) -> list[PageSearchResult]:
        snippets = build_snippets(
            hit.text,
            query,
            max_matches=MAX_SEARCH_RESULTS,
            context_chars=PAGE_SEARCH_SNIPPET_CONTEXT_CHARS,
        )
        return [
            PageSearchResult(snippet=snippet, url=None, title=hit.title)
            for snippet in snippets
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): add TextTier (find-in-page fallback)"
```

### Task 9: AlgoliaTier (tier 1a)

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_page_search.py`:
```python
import httpx

from crawly_mcp.page_search import AlgoliaTier, AlgoliaHit


def _mock_algolia_transport(hits: list[dict], *, expect_app_id: str = "APP") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == f"{expect_app_id.lower()}-dsn.algolia.net"
        assert request.headers["X-Algolia-Application-Id"] == expect_app_id
        return httpx.Response(200, json={"hits": hits})
    return httpx.MockTransport(handler)


def test_algolia_tier_detect_returns_config_when_present() -> None:
    html = """
    <script>
      docsearch({ appId: "APP", apiKey: "KEY", indexName: "docs" });
    </script>
    """
    tier = AlgoliaTier(http_client_factory=lambda: httpx.AsyncClient())
    hit = tier.detect(html, "https://example.com/")
    assert isinstance(hit, AlgoliaHit)
    assert hit.app_id == "APP"
    assert hit.api_key == "KEY"
    assert hit.index_name == "docs"


def test_algolia_tier_detect_none_when_absent() -> None:
    tier = AlgoliaTier(http_client_factory=lambda: httpx.AsyncClient())
    assert tier.detect("<html><body>no docsearch</body></html>", "https://example.com/") is None


@pytest.mark.asyncio
async def test_algolia_tier_execute_maps_hits_to_results() -> None:
    hits = [
        {
            "url": "https://docs.example.com/page#anchor",
            "hierarchy": {"lvl0": "Guide", "lvl1": "Auth", "lvl2": "Login"},
            "_snippetResult": {"content": {"value": "Login flow <em>works</em> like this"}},
        }
    ]
    transport = _mock_algolia_transport(hits)
    tier = AlgoliaTier(http_client_factory=lambda: httpx.AsyncClient(transport=transport))
    hit = AlgoliaHit(app_id="APP", api_key="KEY", index_name="docs")

    results = await tier.execute(hit, "login")

    assert len(results) == 1
    r = results[0]
    assert r.url == "https://docs.example.com/page#anchor"
    assert "Guide" in (r.title or "")
    assert "<em>" not in r.snippet  # highlight markers stripped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k algolia -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `AlgoliaTier`**

All imports were added to the top of `page_search.py` in Task 8. Append only the new code:
```python
@dataclass(frozen=True)
class AlgoliaHit:
    app_id: str
    api_key: str
    index_name: str


class AlgoliaTier:
    name = "algolia"

    def __init__(self, *, http_client_factory: Callable[[], httpx.AsyncClient]) -> None:
        self._client_factory = http_client_factory

    def detect(self, source_html: str, source_url: str) -> AlgoliaHit | None:
        config = detect_algolia_config(source_html)
        if config is None:
            return None
        return AlgoliaHit(
            app_id=config["appId"],
            api_key=config["apiKey"],
            index_name=config["indexName"],
        )

    async def execute(self, hit: AlgoliaHit, query: str) -> list[PageSearchResult]:
        url = f"https://{hit.app_id.lower()}-dsn.algolia.net/1/indexes/{hit.index_name}/query"
        await URLSafetyGuard().validate_user_url(url)  # spec mandates SSRF check on API URL
        body = {"params": f"query={quote(query, safe='')}&hitsPerPage={MAX_SEARCH_RESULTS}"}
        headers = {
            "X-Algolia-Application-Id": hit.app_id,
            "X-Algolia-API-Key": hit.api_key,
            "Content-Type": "application/json",
        }
        async with self._client_factory() as client:
            response = await client.post(
                url,
                json=body,
                headers=headers,
                timeout=PAGE_SEARCH_TIER_TIMEOUT_SECONDS,
            )
        response.raise_for_status()
        payload = response.json()
        return [self._map_hit(h) for h in payload.get("hits", [])]

    @staticmethod
    def _map_hit(raw: dict) -> PageSearchResult:
        hierarchy = raw.get("hierarchy") or {}
        title_parts = [v for v in hierarchy.values() if isinstance(v, str) and v]
        snippet_value = (
            raw.get("_snippetResult", {}).get("content", {}).get("value")
            or raw.get("content")
            or ""
        )
        # Strip Algolia's <em>/<mark> highlight tags.
        snippet = re.sub(r"</?(em|mark)>", "", snippet_value)
        return PageSearchResult(
            snippet=snippet,
            url=raw.get("url"),
            title=" › ".join(title_parts) if title_parts else None,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -k algolia -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): add AlgoliaTier via direct API calls"
```

### Task 10: OpenSearchTier (tier 1b)

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_page_search.py`:
```python
from crawly_mcp.page_search import OpenSearchTier, OpenSearchHit


_OSD = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>Docs</ShortName>
  <Url type="text/html" template="https://example.com/search?q={searchTerms}&amp;lang=en"/>
</OpenSearchDescription>
"""


def test_opensearch_tier_detect_returns_href_when_link_present() -> None:
    html = """<link rel="search" type="application/opensearchdescription+xml" href="/osd.xml">"""
    tier = OpenSearchTier(http_client_factory=lambda: httpx.AsyncClient(), page_fetcher=_UnusedFetcher())
    hit = tier.detect(html, "https://example.com/")
    assert isinstance(hit, OpenSearchHit)
    assert hit.descriptor_url == "https://example.com/osd.xml"


class _UnusedFetcher:
    async def __call__(self, url: str) -> str:
        raise AssertionError("should not be invoked")


@pytest.mark.asyncio
async def test_opensearch_tier_execute_substitutes_query_and_fetches_results() -> None:
    descriptor_transport = httpx.MockTransport(lambda req: httpx.Response(200, text=_OSD))
    captured: list[str] = []

    async def fake_fetch(url: str) -> str:
        captured.append(url)
        return "<html><title>Results</title><body><p>Hello world match here</p></body></html>"

    tier = OpenSearchTier(
        http_client_factory=lambda: httpx.AsyncClient(transport=descriptor_transport),
        page_fetcher=fake_fetch,
    )
    hit = OpenSearchHit(descriptor_url="https://example.com/osd.xml")

    results = await tier.execute(hit, "match")

    assert len(captured) == 1
    assert "q=match" in captured[0] or "q=match&" in captured[0] or captured[0].endswith("q=match&lang=en")
    assert any("match" in r.snippet.lower() for r in results)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k opensearch -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `OpenSearchTier`**

All imports are already at the top of `page_search.py` from Task 8. Append only the new code:
```python
class _PageFetcher(Protocol):
    def __call__(self, url: str) -> Awaitable[str]: ...


_OSD_NS = "{http://a9.com/-/spec/opensearch/1.1/}"


@dataclass(frozen=True)
class OpenSearchHit:
    descriptor_url: str


class OpenSearchTier:
    name = "opensearch"

    def __init__(
        self,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient],
        page_fetcher: _PageFetcher,
    ) -> None:
        self._client_factory = http_client_factory
        self._fetch_page = page_fetcher

    def detect(self, source_html: str, source_url: str) -> OpenSearchHit | None:
        href = detect_opensearch_href(source_html, base_url=source_url)
        if href is None:
            return None
        return OpenSearchHit(descriptor_url=href)

    async def execute(self, hit: OpenSearchHit, query: str) -> list[PageSearchResult]:
        # Descriptor URL came from a <link href> on an attacker-influenced page;
        # spec mandates SSRF validation before the fetch.
        await URLSafetyGuard().validate_user_url(hit.descriptor_url)
        async with self._client_factory() as client:
            response = await client.get(hit.descriptor_url, timeout=PAGE_SEARCH_TIER_TIMEOUT_SECONDS)
        response.raise_for_status()
        template = self._first_html_template(response.text)
        if template is None:
            return []
        results_url = self._substitute(template, query)
        html = await self._fetch_page(results_url)
        return _snippets_from_html(
            html,
            query,
            results_url=results_url,
        )

    @staticmethod
    def _first_html_template(xml_text: str) -> str | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None
        for url in root.findall(f"{_OSD_NS}Url"):
            if (url.get("type") or "").lower() == "text/html":
                template = (url.get("template") or "").strip()
                if template:
                    return template
        return None

    @staticmethod
    def _substitute(template: str, query: str) -> str:
        replacements = {
            "searchTerms": quote(query, safe=""),
            "startIndex": "1",
            "count": str(MAX_SEARCH_RESULTS),
            "language": "*",
            "inputEncoding": "UTF-8",
            "outputEncoding": "UTF-8",
        }
        out = template
        for key, value in replacements.items():
            out = out.replace(f"{{{key}}}", value).replace(f"{{{key}?}}", value)
        return out


def _snippets_from_html(
    html: str,
    query: str,
    *,
    results_url: str,
) -> list[PageSearchResult]:
    """Shared extractor for tier 1b and tier 2: run build_snippets on the
    rendered text of a navigated results page. url=None per result (we cannot
    generically identify result links); results_url is set on the response
    level by the caller, not per result."""
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.title
    title = title_tag.string.strip() if title_tag and title_tag.string else None
    text = extract_readable_text(html)
    snippets = build_snippets(
        text,
        query,
        max_matches=MAX_SEARCH_RESULTS,
        context_chars=PAGE_SEARCH_SNIPPET_CONTEXT_CHARS,
    )
    return [PageSearchResult(snippet=snippet, url=None, title=title) for snippet in snippets]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -k opensearch -v`
Expected: all 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): add OpenSearchTier via descriptor + browser nav"
```

### Task 11: ReadthedocsTier (tier 1c)

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_page_search.py`:
```python
from crawly_mcp.page_search import ReadthedocsTier, ReadthedocsHit


def test_readthedocs_tier_detect_parses_slug_and_version() -> None:
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient())
    hit = tier.detect("", "https://myproject.readthedocs.io/en/stable/guide/intro.html")
    assert isinstance(hit, ReadthedocsHit)
    assert hit.project == "myproject"
    assert hit.version == "stable"


def test_readthedocs_tier_detect_skips_root_path() -> None:
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient())
    assert tier.detect("", "https://myproject.readthedocs.io/") is None


def test_readthedocs_tier_detect_skips_non_readthedocs_host() -> None:
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient())
    assert tier.detect("", "https://example.com/en/stable/page.html") is None


@pytest.mark.asyncio
async def test_readthedocs_tier_execute_maps_blocks_to_results() -> None:
    payload = {
        "results": [
            {
                "path": "guide/intro.html",
                "project": "myproject",
                "domain": "https://myproject.readthedocs.io",
                "blocks": [
                    {
                        "type": "section",
                        "id": "intro",
                        "title": "Introduction",
                        "content": "This introduces the <span>project</span> and its goals",
                    }
                ],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=intro" in str(request.url)
        assert "project=myproject" in str(request.url)
        assert "version=stable" in str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    tier = ReadthedocsTier(http_client_factory=lambda: httpx.AsyncClient(transport=transport))
    hit = ReadthedocsHit(project="myproject", version="stable")

    results = await tier.execute(hit, "intro")

    assert len(results) == 1
    r = results[0]
    assert r.url is not None and "myproject.readthedocs.io" in r.url
    assert r.title == "Introduction"
    assert "<span>" not in r.snippet  # markup stripped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k readthedocs -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `ReadthedocsTier`**

All imports are at the top of `page_search.py` from Task 8. Append only the new code:
```python
_RTD_API = "https://readthedocs.org/api/v2/search/"
_RTD_HOSTS = (".readthedocs.io", ".readthedocs-hosted.com")


@dataclass(frozen=True)
class ReadthedocsHit:
    project: str
    version: str


class ReadthedocsTier:
    name = "readthedocs"

    def __init__(self, *, http_client_factory: Callable[[], httpx.AsyncClient]) -> None:
        self._client_factory = http_client_factory

    def detect(self, source_html: str, source_url: str) -> ReadthedocsHit | None:
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower()
        if not any(host.endswith(suffix) for suffix in _RTD_HOSTS):
            return None
        # slug is the subdomain
        parts = host.split(".")
        if len(parts) < 3 or not parts[0]:
            return None
        slug = parts[0]
        # path: /<language>/<version>/<...>
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) < 2:
            return None
        version = segments[1]
        return ReadthedocsHit(project=slug, version=version)

    async def execute(self, hit: ReadthedocsHit, query: str) -> list[PageSearchResult]:
        await URLSafetyGuard().validate_user_url(_RTD_API)  # spec mandates SSRF check
        params = {"q": query, "project": hit.project, "version": hit.version}
        async with self._client_factory() as client:
            response = await client.get(
                _RTD_API, params=params, timeout=PAGE_SEARCH_TIER_TIMEOUT_SECONDS
            )
        response.raise_for_status()
        payload = response.json()
        out: list[PageSearchResult] = []
        for result in payload.get("results", []):
            for block in result.get("blocks", [])[:MAX_SEARCH_RESULTS]:
                url = self._block_url(result, block)
                snippet = re.sub(r"</?[a-zA-Z][^>]*>", "", block.get("content") or "")
                out.append(PageSearchResult(
                    snippet=snippet.strip(),
                    url=url,
                    title=block.get("title"),
                ))
                if len(out) >= MAX_SEARCH_RESULTS:
                    return out
        return out

    @staticmethod
    def _block_url(result: dict, block: dict) -> str | None:
        domain = (result.get("domain") or "").rstrip("/")
        path = (result.get("path") or "").lstrip("/")
        anchor = block.get("id")
        if not domain or not path:
            return None
        base = f"{domain}/{path}"
        return f"{base}#{anchor}" if anchor else base
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -k readthedocs -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): add ReadthedocsTier via search API"
```

### Task 12: FormTier (tier 2)

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_page_search.py`:
```python
from crawly_mcp.page_search import FormTier, FormHit
from crawly_mcp.parsing import SearchFormHit


def test_form_tier_detect_returns_hit_when_form_present() -> None:
    html = """<form role="search" action="/s" method="get"><input name="q"></form>"""
    tier = FormTier(page_fetcher=_UnusedFetcher())
    hit = tier.detect(html, "https://example.com/")
    assert isinstance(hit, FormHit)
    assert hit.form.action == "https://example.com/s"
    assert hit.form.input_name == "q"


def test_form_tier_detect_none_when_no_form() -> None:
    tier = FormTier(page_fetcher=_UnusedFetcher())
    assert tier.detect("<html><body>nothing</body></html>", "https://example.com/") is None


@pytest.mark.asyncio
async def test_form_tier_execute_constructs_url_and_fetches() -> None:
    captured: list[str] = []

    async def fake_fetch(url: str) -> str:
        captured.append(url)
        return "<html><title>Results</title><body>term found here</body></html>"

    tier = FormTier(page_fetcher=fake_fetch)
    hit = FormHit(form=SearchFormHit(action="https://example.com/search", input_name="q"))

    results = await tier.execute(hit, "term")

    assert captured == ["https://example.com/search?q=term"]
    assert len(results) == 1
    assert "term" in results[0].snippet.lower()


@pytest.mark.asyncio
async def test_form_tier_execute_preserves_existing_action_query_params() -> None:
    captured: list[str] = []

    async def fake_fetch(url: str) -> str:
        captured.append(url)
        return "<html><body>ok</body></html>"

    tier = FormTier(page_fetcher=fake_fetch)
    hit = FormHit(form=SearchFormHit(action="https://example.com/s?lang=en", input_name="q"))
    await tier.execute(hit, "hello world")

    # Either ordering is acceptable as long as both params end up in the URL.
    assert "lang=en" in captured[0]
    assert "q=hello+world" in captured[0] or "q=hello%20world" in captured[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k form_tier -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `FormTier`**

All imports are at the top of `page_search.py` from Task 8. Append only the new code:
```python
@dataclass(frozen=True)
class FormHit:
    form: SearchFormHit


class FormTier:
    name = "form"

    def __init__(self, *, page_fetcher: _PageFetcher) -> None:
        self._fetch_page = page_fetcher

    def detect(self, source_html: str, source_url: str) -> FormHit | None:
        form = detect_search_form(source_html, base_url=source_url)
        if form is None:
            return None
        return FormHit(form=form)

    async def execute(self, hit: FormHit, query: str) -> list[PageSearchResult]:
        results_url = self._append_query(hit.form.action, hit.form.input_name, query)
        html = await self._fetch_page(results_url)
        return _snippets_from_html(html, query, results_url=results_url)

    @staticmethod
    def _append_query(action: str, param_name: str, query: str) -> str:
        split = urlsplit(action)
        pairs = parse_qsl(split.query, keep_blank_values=True)
        pairs.append((param_name, query))
        new_query = urlencode(pairs, doseq=True)
        return urlunsplit((split.scheme, split.netloc, split.path, new_query, split.fragment))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -k form_tier -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Run the full page_search + parsing test suite**

Run: `uv run pytest tests/test_page_search.py tests/test_parsing.py -v`
Expected: all tests pass. (Skip `ruff check .` here — several imports added in Task 8's top-of-file block are not used until Tasks 14-15, and ruff's F401 would flag them. The full ruff gate runs in Task 16 Step 5 after the file is complete.)

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): add FormTier (generic GET form)"
```

---

## Chunk 4: Orchestration and MCP integration

### Task 13: Source-page fetch helper

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_page_search.py`:
```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

from crawly_mcp.errors import NavigationFailedError
from crawly_mcp.page_search import PageSearchService


class _FakeBrowser:
    """Minimal BrowserManager fake for source-fetch unit tests."""
    def __init__(self, *, html: str, navigate_raises: BaseException | None = None) -> None:
        self._html = html
        self._navigate_raises = navigate_raises
        self.close_calls = 0

    async def new_context(self):
        outer = self
        class _Ctx:
            async def new_page(self_inner):
                async def content() -> str:
                    return outer._html
                async def close() -> None:
                    return None
                async def goto(url, **kwargs):
                    if outer._navigate_raises is not None:
                        raise outer._navigate_raises
                return SimpleNamespace(content=content, close=close, goto=goto, url="")
            async def close(self_inner):
                outer.close_calls += 1
        return _Ctx()

    async def goto(self, page, url, timeout_ms=None):
        await page.goto(url)


@pytest.mark.asyncio
async def test_source_fetch_returns_html() -> None:
    browser = _FakeBrowser(html="<html><body>hello</body></html>")
    service = PageSearchService(browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient())
    html = await service._fetch_source_html("https://example.com/")
    assert "hello" in html
    assert browser.close_calls == 1


@pytest.mark.asyncio
async def test_source_fetch_raises_navigation_failed_on_error() -> None:
    from patchright.async_api import Error as PlaywrightError
    browser = _FakeBrowser(html="", navigate_raises=PlaywrightError("nope"))
    service = PageSearchService(browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient())
    with pytest.raises(NavigationFailedError):
        await service._fetch_source_html("https://example.com/")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k source_fetch -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `_fetch_source_html` and `PageSearchService` scaffold**

All imports are at the top of `page_search.py` from Task 8. Append only the new code. **Task 14 will extend this class body with the `search()` method using `Edit` — do not duplicate the class definition when Task 14 runs.**

```python
class PageSearchService:
    def __init__(
        self,
        browser_manager: BrowserManager,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._browser_manager = browser_manager
        self._http_client_factory = http_client_factory

    async def _fetch_source_html(self, url: str) -> str:
        browser_context = await self._browser_manager.new_context()
        guard = URLSafetyGuard()
        await guard.attach(browser_context)
        try:
            page = await browser_context.new_page()
            try:
                await self._browser_manager.goto(
                    page, url, timeout_ms=FETCH_PAGE_TIMEOUT_SECONDS * 1000
                )
                return await page.content()
            except PlaywrightTimeoutError as exc:
                raise NavigationFailedError(f"source fetch timed out: {exc}") from exc
            except PlaywrightError as exc:
                raise NavigationFailedError(f"source fetch failed: {exc}") from exc
            finally:
                with suppress(Exception):
                    await page.close()
        finally:
            await browser_context.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -k source_fetch -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): fetch source HTML via ephemeral browser context"
```

### Task 14: Cascade orchestration and public `search()`

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_page_search.py`:
```python
from crawly_mcp.errors import InvalidInputError, TimeoutExceededError
from crawly_mcp.models import PageSearchResponse


class _FakeTier:
    def __init__(self, name: str, *, hit: object | None, results: list[PageSearchResult], raises: BaseException | None = None) -> None:
        self.name = name
        self._hit = hit
        self._results = results
        self._raises = raises
        self.execute_calls = 0

    def detect(self, source_html: str, source_url: str):
        return self._hit

    async def execute(self, hit, query: str) -> list[PageSearchResult]:
        self.execute_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._results


class _PassthroughBrowser(_FakeBrowser):
    """Identical to _FakeBrowser; alias for readability."""


def _make_service_with_tiers(browser, tiers: list) -> PageSearchService:
    service = PageSearchService(browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient())
    service._tiers = tiers  # injection point for tests
    return service


@pytest.mark.asyncio
async def test_cascade_first_tier_wins() -> None:
    browser = _PassthroughBrowser(html="<html><body>src</body></html>")
    algolia = _FakeTier("algolia", hit=object(), results=[PageSearchResult(snippet="A")])
    opensearch = _FakeTier("opensearch", hit=None, results=[])
    service = _make_service_with_tiers(browser, [algolia, opensearch])

    response = await service.search(url="https://example.com/", query="q")

    assert response.mode == "algolia"
    assert response.attempted == ["algolia"]
    assert len(response.results) == 1
    assert opensearch.execute_calls == 0


@pytest.mark.asyncio
async def test_cascade_skips_non_detected_tiers() -> None:
    browser = _PassthroughBrowser(html="<html><body>src</body></html>")
    algolia = _FakeTier("algolia", hit=None, results=[])
    form = _FakeTier("form", hit=object(), results=[PageSearchResult(snippet="F")])
    text = _FakeTier("text", hit=object(), results=[PageSearchResult(snippet="T")])
    service = _make_service_with_tiers(browser, [algolia, form, text])

    response = await service.search(url="https://example.com/", query="q")

    assert response.mode == "form"
    assert response.attempted == ["form"]


@pytest.mark.asyncio
async def test_cascade_continues_past_raising_tier() -> None:
    browser = _PassthroughBrowser(html="<html><body>src</body></html>")
    algolia = _FakeTier("algolia", hit=object(), results=[], raises=RuntimeError("boom"))
    text = _FakeTier("text", hit=object(), results=[PageSearchResult(snippet="T")])
    service = _make_service_with_tiers(browser, [algolia, text])

    response = await service.search(url="https://example.com/", query="q")

    assert response.mode == "text"
    assert response.attempted == ["algolia", "text"]


@pytest.mark.asyncio
async def test_cascade_zero_results_from_text_still_valid() -> None:
    browser = _PassthroughBrowser(html="<html><body>src</body></html>")
    text = _FakeTier("text", hit=object(), results=[])
    service = _make_service_with_tiers(browser, [text])

    response = await service.search(url="https://example.com/", query="q")

    assert response.mode == "text"
    assert response.attempted == ["text"]
    assert response.results == []


@pytest.mark.asyncio
async def test_search_rejects_empty_query() -> None:
    browser = _PassthroughBrowser(html="")
    service = PageSearchService(browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient())
    with pytest.raises(InvalidInputError):
        await service.search(url="https://example.com/", query="")


@pytest.mark.asyncio
async def test_search_ssrf_rejects_private_url() -> None:
    browser = _PassthroughBrowser(html="")
    service = PageSearchService(browser_manager=browser, http_client_factory=lambda: httpx.AsyncClient())
    from crawly_mcp.errors import URLSafetyError
    with pytest.raises(URLSafetyError):
        await service.search(url="http://127.0.0.1/", query="hello")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k "cascade or search_rejects or search_ssrf" -v`
Expected: FAIL — `AttributeError: 'PageSearchService' object has no attribute 'search'`.

- [ ] **Step 3: Extend `PageSearchService` with tier list and `search()` via Edit**

Use the `Edit` tool (not append) on `src/crawly_mcp/page_search.py` to grow the existing class body. Two separate Edits:

**Edit 3a — add `self._tiers` to `__init__`:**

old_string:
```python
        self._browser_manager = browser_manager
        self._http_client_factory = http_client_factory

    async def _fetch_source_html(self, url: str) -> str:
```

new_string:
```python
        self._browser_manager = browser_manager
        self._http_client_factory = http_client_factory
        self._tiers: list = self._build_default_tiers()

    def _build_default_tiers(self) -> list:
        async def fetch_page(url: str) -> str:
            return await self._fetch_source_html(url)
        return [
            AlgoliaTier(http_client_factory=self._http_client_factory),
            OpenSearchTier(http_client_factory=self._http_client_factory, page_fetcher=fetch_page),
            ReadthedocsTier(http_client_factory=self._http_client_factory),
            FormTier(page_fetcher=fetch_page),
            TextTier(),
        ]

    async def _fetch_source_html(self, url: str) -> str:
```

**Edit 3b — append `search()` and `_respond()` methods at the end of the class.** Identify the last line of `_fetch_source_html` (the outer `finally: await browser_context.close()`). Append the following, *inside* the class body (matching indentation):

old_string:
```python
        finally:
            await browser_context.close()
```

new_string (same first two lines, then the new methods):
```python
        finally:
            await browser_context.close()

    async def search(self, *, url: str, query: str) -> PageSearchResponse:
        try:
            request = PageSearchRequest(url=url, query=query)
        except ValidationError as exc:
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        # SSRF pre-check on the user-supplied URL
        await URLSafetyGuard().validate_user_url(request.url)

        logger.info("page_search entry url={!r} query={!r}", request.url, request.query)
        started = asyncio.get_running_loop().time()

        try:
            async with asyncio.timeout(FETCH_TOTAL_TIMEOUT_SECONDS):
                source_html = await self._fetch_source_html(request.url)
                attempted: list[str] = []

                for tier in self._tiers:
                    if tier.name == "text":
                        break  # text tier handled after the loop
                    hit = tier.detect(source_html, request.url)
                    if hit is None:
                        continue
                    attempted.append(tier.name)
                    try:
                        results = await asyncio.wait_for(
                            tier.execute(hit, request.query),
                            PAGE_SEARCH_TIER_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("page_search tier={} timed out", tier.name)
                        continue
                    except Exception as exc:
                        logger.warning(
                            "page_search tier={} failed: {}: {}",
                            tier.name, type(exc).__name__, exc,
                        )
                        continue
                    if results:
                        return self._respond(
                            mode=tier.name,
                            attempted=attempted,
                            source_url=request.url,
                            results_url=None,
                            results=results,
                        )

                # Text tier (always last)
                text_tier = self._tiers[-1]
                attempted.append("text")
                hit = text_tier.detect(source_html, request.url)
                text_results = await text_tier.execute(hit, request.query)
                return self._respond(
                    mode="text",
                    attempted=attempted,
                    source_url=request.url,
                    results_url=None,
                    results=text_results,
                )
        except asyncio.TimeoutError as exc:
            raise TimeoutExceededError("page_search exceeded the overall timeout") from exc
        finally:
            duration = asyncio.get_running_loop().time() - started
            logger.info("page_search done duration={:.2f}s", duration)

    def _respond(
        self,
        *,
        mode: str,
        attempted: list[str],
        source_url: str,
        results_url: str | None,
        results: list[PageSearchResult],
    ) -> PageSearchResponse:
        response = PageSearchResponse(
            mode=mode,
            attempted=attempted,
            source_url=source_url,
            results_url=results_url,
            results=results[:MAX_SEARCH_RESULTS],
            truncated=False,
        )
        return _truncate_page_search_response(response)
```

**Do NOT create a new `class PageSearchService:` block in a separate append — that would shadow the Task-13 definition and drop `_fetch_source_html`.**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -v`
Expected: all page_search tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): cascade orchestration with timeouts and attempted tracking"
```

### Task 15: Response truncation

**Files:**
- Modify: `src/crawly_mcp/page_search.py`
- Modify: `tests/test_page_search.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_page_search.py`:
```python
from crawly_mcp.page_search import _truncate_page_search_response


def _response_with(n_snippets: int, snippet: str) -> PageSearchResponse:
    return PageSearchResponse(
        mode="text",
        attempted=["text"],
        source_url="https://example.com/",
        results_url=None,
        results=[PageSearchResult(snippet=snippet) for _ in range(n_snippets)],
        truncated=False,
    )


def test_truncate_noop_under_limit(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_FETCH_MAX_SIZE", "10000")
    response = _response_with(3, "short")
    out = _truncate_page_search_response(response)
    assert out.truncated is False
    assert len(out.results) == 3


def test_truncate_drops_trailing_results_when_over_limit(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_FETCH_MAX_SIZE", "300")
    response = _response_with(5, "x" * 100)
    out = _truncate_page_search_response(response)
    assert out.truncated is True
    assert len(out.results) < 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_page_search.py -k truncate -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement `_truncate_page_search_response`**

`resolve_fetch_max_size` is already imported at the top of `page_search.py` from Task 8. Append only the function:
```python
def _truncate_page_search_response(response: PageSearchResponse) -> PageSearchResponse:
    limit = resolve_fetch_max_size()
    serialized = response.model_dump_json().encode("utf-8")
    if len(serialized) <= limit:
        return response

    truncated = response.model_copy(update={"truncated": True})
    while truncated.results and len(truncated.model_dump_json().encode("utf-8")) > limit:
        truncated = truncated.model_copy(
            update={"results": truncated.results[:-1]}
        )
    return truncated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_page_search.py -k truncate -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/page_search.py tests/test_page_search.py
git commit -m "feat(page_search): truncate response when CRAWLY_FETCH_MAX_SIZE exceeded"
```

### Task 16: MCP tool registration

**Files:**
- Modify: `src/crawly_mcp/mcp_server.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_models.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py::test_page_search_tool_schema_advertises_required_fields -v`
Expected: FAIL — tool not registered yet.

- [ ] **Step 3: Register the tool in `mcp_server.py`**

Edit `src/crawly_mcp/mcp_server.py`:

Add imports:
```python
from crawly_mcp.models import FetchResponse, PageSearchResponse, SearchResponse
from crawly_mcp.page_search import PageSearchService
```

In `create_server`, after `service = WebSearchService(browser_manager)`, add:
```python
    page_search_service = PageSearchService(browser_manager)
```

After the `fetch` tool, register:
```python
    @server.tool(
        name="page_search",
        description=(
            "Search for content on a single page using a three-tier cascade: "
            "known site-search facilities (Algolia DocSearch, OpenSearch, "
            "Readthedocs) first, then generic GET form detection, then "
            "find-in-page text fallback."
        ),
    )
    async def page_search(url: str, query: str) -> PageSearchResponse:
        try:
            return await page_search_service.search(url=url, query=query)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py::test_page_search_tool_schema_advertises_required_fields -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/mcp_server.py tests/test_models.py
git commit -m "feat(mcp): register page_search tool"
```

### Task 17: CLI subcommand

**Files:**
- Modify: `src/crawly_mcp/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_cli.py`:
```python
def test_build_parser_accepts_page_search_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["page-search", "--url", "https://example.com/", "--query", "hello"])
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k page_search -v`
Expected: FAIL — `ArgumentError: invalid choice: 'page-search'`.

- [ ] **Step 3: Implement the subcommand**

Edit `src/crawly_mcp/cli.py`. Add to `build_parser()` after the `fetch` parser:
```python
    page_search_parser = subparsers.add_parser(
        "page-search",
        help="Search for content on a single page via the cascade.",
    )
    page_search_parser.add_argument("--url", required=True, help="Page URL to search.")
    page_search_parser.add_argument("--query", required=True, help="Search query text.")
```

Add the runner helper (private, so tests can monkeypatch it). Match the existing `_run_search` / `_run_fetch` pattern — `BrowserManager.new_context()` lazily starts Playwright, so no explicit `start()` is needed:

```python
async def _run_page_search(url: str, query: str) -> int:
    from crawly_mcp.browser import BrowserManager
    from crawly_mcp.page_search import PageSearchService

    browser_manager = BrowserManager()
    try:
        service = PageSearchService(browser_manager)
        response = await service.search(url=url, query=query)
        print(response.model_dump_json())
    finally:
        await browser_manager.close()
    return 0
```

In `main()`, add an `elif args.command == "page-search":` branch before the final `parser.error(...)` line. The branch should call `asyncio.run(_run_page_search(args.url, args.query))` wrapped in the same `try: ... except WebSearchError as exc: print(json.dumps({"type": exc.error_type, "message": exc.message}), file=sys.stderr); return 1` pattern used by the existing `search`/`fetch` branches. Inspect the existing `cli.py` if unsure of the exact error-printing idiom — copy it verbatim for consistency.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k page_search -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/cli.py tests/test_cli.py
git commit -m "feat(cli): add page-search subcommand"
```

---

## Chunk 5: Documentation and release

### Task 18: Document the new tool in README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add tool entry under "Tools"**

Edit `README.md`. Under the "## Tools" section (the bullet list that currently lists `search` and `fetch`), add:
```markdown
- `page_search(url, query)` searches for content on a single page. Tries known site-search facilities first (Algolia DocSearch, OpenSearch descriptor, Readthedocs API), then generic GET form detection, then find-in-page text as a fallback. Returns a `mode` discriminator plus up to 5 results with snippets and optional result URLs.
```

- [ ] **Step 2: Document the CLI subcommand**

Under an appropriate CLI subsection in README (where `search` / `fetch` are shown if present), add:
```markdown
### `page-search`

```sh
crawly-cli page-search --url https://docs.example.com/guide --query "authentication"
```

Prints a JSON `PageSearchResponse` with `mode`, `attempted`, `results_url`, and `results[]`.
```

(If the README has no CLI subsection, skip this step — the `crawly-cli --help` output covers it.)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): document page_search tool and CLI"
```

### Task 19: Update CHANGELOG for 0.2.0

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add `[0.2.0]` section above `[0.1.0]`**

Edit `CHANGELOG.md`. Insert above the existing `## [0.1.0] - 2026-04-23` heading:
```markdown
## [0.2.0] - 2026-04-23

### Added

- `page_search(url, query)` MCP tool: three-tier cascade over Algolia DocSearch, OpenSearch descriptor, Readthedocs API, generic GET forms, and find-in-page text fallback. Returns a `mode` discriminator, ordered `attempted` list, and up to 5 result snippets with optional result URLs.
- `crawly-cli page-search --url URL --query TEXT` subcommand mirroring the MCP tool.

### Changed

- Promote `httpx` from a transitive dependency to an explicit project dependency (used by `page_search` for Algolia and Readthedocs API calls).

### Fixed
```

Also add a link reference near the bottom:
```markdown
[0.2.0]: https://github.com/dshein-alt/crawly-mcp/releases/tag/v0.2.0
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): release 0.2.0"
```

### Task 20: Full CI-equivalent verification before push

**Files:** (no edits; verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 2: Full test suite**

Run: `uv run pytest -q`
Expected: every test passes; the count should be the pre-change total plus the new `test_page_search.py` tests plus the new cases in `test_parsing.py`, `test_constants.py`, `test_models.py`, `test_cli.py`.

- [ ] **Step 3: Fingerprint canary (only matters for CI on tag push, but worth confirming locally)**

Run: `uv run python scripts/fingerprint_check.py; echo "EXIT=$?"`
Expected: `EXIT=0`.

- [ ] **Step 4: Container build + HTTP MCP smoke**

Run:
```bash
docker build -t crawly-mcp:test .
docker run -d --rm --name crawly-mcp-test --init --user app \
  --add-host=host.docker.internal:host-gateway -p 8000:8000 crawly-mcp:test
for attempt in $(seq 1 20); do
  if uv run python scripts/http_mcp_smoke.py --url http://127.0.0.1:8000/mcp \
     --private-url http://host.docker.internal:12345; then
    echo "SMOKE_PASSED"
    break
  fi
  sleep 2
done
docker rm -f crawly-mcp-test
```
Expected: `SMOKE_PASSED` after one or two attempts; container logs show `version=0.2.0` on startup.

- [ ] **Step 5: Verify the reported version is 0.2.0**

Run: `uv run python -c "from crawly_mcp.version import get_package_version; print(get_package_version())"`
Expected: `0.2.0`.

- [ ] **Step 6: (Optional) push branch and open PR**

Run: `git push -u origin feature/on_page_search`
Then open a PR via `gh pr create` (or through the GitHub UI) describing the feature and linking the spec at `docs/superpowers/specs/2026-04-23-page-search-design.md`.

---

## Notes for the executor

- **Spec is the source of truth** for any ambiguity — consult [docs/superpowers/specs/2026-04-23-page-search-design.md](../specs/2026-04-23-page-search-design.md) when this plan elides details (error messages, log format specifics, etc.).
- **TDD discipline**: every task follows failing-test → minimal-implementation → passing-test → commit. Do not collapse steps.
- **Commit hygiene**: one commit per task (not per step). Keep messages imperative, terse, and subject-line-only unless context is genuinely needed.
- **The full suite runs under 2 seconds in this repo.** If a new test takes noticeably longer, something is wrong (likely accidental live network).
- **Type-checking:** this repo does not enforce `mypy`; `ruff` is the only linter. Keep the `from __future__ import annotations` header on new files for forward-reference-safe typing.
- **If a tier reveals a deeper design issue during implementation**, pause and raise it. Do not silently deviate from the spec — the spec went through a two-round review for a reason.
