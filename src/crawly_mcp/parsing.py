from __future__ import annotations

import json as _json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeAlias
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from loguru import logger
from soupsieve import SelectorSyntaxError, match as _match_element

from crawly_mcp.constants import MAX_SEARCH_RESULTS, PageKind

# (page kind, marker that proved it, one-line explanation)
Verdict: TypeAlias = tuple[PageKind, str | None, str]

# The mappings below (PROVIDER_HOST_SUFFIXES, SEARCH_URL_TEMPLATES,
# RESULT_SELECTORS, RESULT_CONTAINERS, SEARCH_CONSENT_MARKERS,
# SEARCH_CAPTCHA_MARKERS, SEARCH_ZERO_RESULT_MARKERS) are keyed by
# browser-driven providers only ("duckduckgo", "google", "yandex"). The searxng
# provider uses the JSON API in crawly_mcp.searxng and never traverses this
# module.
PROVIDER_HOST_SUFFIXES = {
    "duckduckgo": ("duckduckgo.com", "duck.com"),
    "google": ("google.com", "google.ru"),
    "yandex": ("yandex.com", "yandex.ru", "yandex.kz", "yandex.by"),
}

SEARCH_URL_TEMPLATES = {
    "duckduckgo": "https://duckduckgo.com/?q={query}&ia=web",
    "google": "https://www.google.com/search?q={query}&hl=en",
    "yandex": "https://yandex.ru/search/?text={query}",
}

# Anchors that name a result. They are only trusted when they sit inside one of
# RESULT_CONTAINERS below; on their own they also match navigation, footer, and
# "related searches" chrome, which is how a zero-hit page used to be reported as
# five plausible results.
RESULT_SELECTORS = {
    "duckduckgo": [
        "a.result__a[href]",
        "article a[data-testid='result-title-a'][href]",
    ],
    "google": [
        "div.yuRUbf a[href]",
        "a.zReHs[href]",
        "div.yuRUbf > a[href]",
        "a[jsname][href]",
    ],
    "yandex": [
        "a.OrganicTitle-Link[href]",
        "li.serp-item a.Link[href]",
    ],
}

# Markup that wraps a single organic result. An anchor outside every one of
# these is page chrome, not a result, and is never returned as a result URL.
RESULT_CONTAINERS = {
    "duckduckgo": [
        "article",
        "div.result__body",
        "section[data-testid='web-result']",
    ],
    "google": ["div.yuRUbf", "div.g", "div.Mffcpf", "div.vGQaTc"],
    "yandex": ["li.serp-item", "div.organics-item2", "div.serp-item"],
}

# Result links whose target is not present in the markup. Google routes organic
# titles through `/goto?url=<opaque>`; the redirect response supplies the real
# destination. Count these separately so the service can resolve them without
# reconstructing URLs from display text.
OPAQUE_RESULT_PATHS = ("/goto?url=",)

SEARCH_CONSENT_MARKERS = {
    "duckduckgo": ("consent", "accept all", "agree and continue"),
    "google": (
        "before you continue",
        "consent",
        "agree and continue",
        "not redirected within a few seconds",
    ),
    "yandex": ("соглашение", "принять все", "consent"),
}

SEARCH_CAPTCHA_MARKERS = {
    "duckduckgo": (
        "anomaly",
        "automated requests",
        "static-pages",  # bot-detection redirect target, e.g. /static-pages/418.html
        "verify you are a human",
    ),
    "google": ("detected unusual traffic", "unusual traffic", "sorry", "recaptcha"),
    "yandex": (
        "robot",
        "captcha",
        "проверка",
        "unusual",
        "подтвердите, что вы не робот",
    ),
}

# Kept as one flat map for the block-marker API used by service.py; the split
# above is what lets a page be reported as a consent wall rather than a CAPTCHA.
SEARCH_BLOCK_MARKERS = {
    provider: tuple(
        dict.fromkeys(
            SEARCH_CONSENT_MARKERS[provider] + SEARCH_CAPTCHA_MARKERS[provider]
        )
    )
    for provider in SEARCH_CONSENT_MARKERS
}

# Copy a provider puts on a results page that genuinely has nothing to show.
# Only these make `no_results` a defensible claim.
SEARCH_ZERO_RESULT_MARKERS = {
    "duckduckgo": (
        "no results",
        "not many results",
        "nothing to show",
        "did not return any results",
    ),
    "google": (
        "did not match any documents",
        "no results found",
        "итогов не найдено",
    ),
    "yandex": (
        "ничего не найдено",
        "нет результатов",
        "по вашему запросу ничего не найдено",
        "no results found",
    ),
}

# A page that rendered this little text and no result containers never reached
# a results view at all: DuckDuckGo serves a "redirecting to the non-JavaScript
# site" interstitial, and Google serves a redirect interstitial, both with the
# real results absent. That is a provider failure, not an empty web.
SHELL_TEXT_CHARS = 2500
SEARCH_SHELL_MARKERS = (
    "you are being redirected to the non-javascript site",
    "redirected to the non-javascript",
    "please click here if you are not redirected",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies to continue",
)


def build_search_url(provider: str, context: str) -> str:
    resolved_provider = provider
    if resolved_provider not in SEARCH_URL_TEMPLATES:
        raise KeyError(
            f"build_search_url supports only browser-driven providers "
            f"{sorted(SEARCH_URL_TEMPLATES)}; got {resolved_provider!r}"
        )
    template = SEARCH_URL_TEMPLATES[resolved_provider]
    return template.format(query=quote_plus(context))


@dataclass(slots=True)
class SearchPageEvidence:
    """What a search navigation actually landed on, with counts as proof."""

    kind: PageKind = "unknown"
    result_anchors: int = 0
    opaque_anchors: int = 0
    text_chars: int = 0
    marker: str | None = None
    detail: str = ""

    @property
    def extractable(self) -> bool:
        return self.kind == "results" and self.result_anchors > 0


def classify_search_page(
    provider: str,
    url: str,
    title: str,
    html: str,
    *,
    query: str = "",
) -> SearchPageEvidence:
    """Name the kind of page a search landed on, and show the counts behind it.

    The checks run in order of severity: a wall is reported before a results
    page, and a page that never rendered is reported before a page that is
    empty, so `zero_results` is only ever returned for a page that positively
    states it found nothing.

    `query` is the caller's own words. It is subtracted from the text markers
    are matched against, because a search page repeats those words back at us:
    searching "sqlite no results" puts "no results" in the page title, and the
    page is then indistinguishable from a zero-hit page unless the echo is
    removed before the markers run.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    haystack = _status_haystack(html, provider, url, title, query)
    anchors, opaque = _count_result_anchors(soup, provider, base_url=url)
    evidence = SearchPageEvidence(
        result_anchors=anchors,
        opaque_anchors=opaque,
        text_chars=len(text),
    )
    for check in (_wall_check, _shell_check, _zero_result_check, _content_check):
        verdict = check(provider, haystack, evidence)
        if verdict is not None:
            evidence.kind, evidence.marker, evidence.detail = verdict
            return evidence
    evidence.kind, evidence.marker, evidence.detail = _fallback_check(evidence)
    return evidence


def _status_haystack(html: str, provider: str, url: str, title: str, query: str) -> str:
    """Text the provider wrote about its own state, with the caller's words out.

    Markers describe the provider: "no results found", "unusual traffic",
    "just a moment". Matched against the whole page they also match the
    caller's query echoed in the title and result snippets about cookies, and a
    page with three real results becomes a zero-hit page or a consent wall —
    which also stops a cascade that should have continued.

    So result containers (the provider quoting other sites) and the echo
    positions — the search box, and the <title> node that get_text() would
    otherwise fold into the body — come out, the URL is stripped to its path,
    and query echoes are removed from the title and labelled summaries such
    as "Search results for ...". Provider status statements stay intact.
    """
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(", ".join(RESULT_CONTAINERS[provider])):
        node.decompose()
    for node in soup(
        ["form", "input", "textarea", "script", "style", "noscript", "title"]
    ):
        node.decompose()
    path = urlsplit(url)._replace(query="", fragment="").geturl()
    # The caller's words are removed where the provider quotes them back — the
    # address, the title, the search box — and left alone in the body. Stripping
    # the body too deletes the provider's own statement: searching "results"
    # removes that word from "No results for results", and the zero-hit page is
    # then read as a results page full of unrelated links.
    body = _strip_body_query_echoes(soup.get_text(" ", strip=True), query)[:4000]
    return " ".join((path, _strip_query_text(title, query), body)).lower()


def _strip_query_text(text: str, query: str) -> str:
    """Delete the caller's phrase, plain and URL-encoded, from one echo position."""
    cleaned = text.lower()
    phrase = query.strip().lower()
    if not phrase:
        return cleaned
    encoded = quote_plus(phrase).lower()
    for needle in (
        phrase,
        encoded,
        encoded.replace("+", "%20"),
        phrase.replace(" ", "+"),
    ):
        cleaned = re.sub(rf"(?<!\w){re.escape(needle)}(?!\w)", " ", cleaned)
    return cleaned


def _strip_body_query_echoes(text: str, query: str) -> str:
    """Remove labelled query echoes without changing the provider's statement.

    In both "Search results for no results" and "No results for no results",
    only the words after "results for" quote the query. The latter must keep
    its leading "No results", even when that is also what the caller typed.
    """
    phrase = r"\s+".join(re.escape(word) for word in query.split())
    if not phrase:
        return text
    pattern = (
        r"(\b(?:results\s+(?:for|matching)|searched\s+for|search\s+for|"
        r"your\s+search\s*[:\-])\s*[\"'\u201c\u2018]?)"
        rf"{phrase}(?!\w)"
    )
    return re.sub(pattern, r"\1 ", text, flags=re.IGNORECASE)


def _wall_check(
    provider: str, haystack: str, evidence: SearchPageEvidence
) -> Verdict | None:
    del evidence
    marker = _first_marker(haystack, SEARCH_CAPTCHA_MARKERS.get(provider, ()))
    if marker is not None:
        return "captcha", marker, _wall_detail(marker)
    marker = _first_marker(haystack, SEARCH_CONSENT_MARKERS.get(provider, ()))
    if marker is not None:
        return "consent", marker, _wall_detail(marker)
    return None


def _shell_check(
    provider: str, haystack: str, evidence: SearchPageEvidence
) -> Verdict | None:
    del provider
    marker = _first_marker(haystack, SEARCH_SHELL_MARKERS)
    if marker is None or evidence.result_anchors:
        return None
    return (
        "shell",
        marker,
        f"provider served an interstitial (marker={marker!r}) with no results "
        "rendered; the page never reached a results view",
    )


def _zero_result_check(
    provider: str, haystack: str, evidence: SearchPageEvidence
) -> Verdict | None:
    """A page that states it found nothing is empty, whatever its DOM contains.

    DuckDuckGo's zero-hit page ships article-wrapped links to popular sites, so
    trusting the anchors over the provider's own statement is exactly how a
    niche query used to come back as five plausible, unrelated URLs.
    """
    del evidence
    marker = _first_marker(haystack, SEARCH_ZERO_RESULT_MARKERS.get(provider, ()))
    if marker is None:
        return None
    return (
        "zero_results",
        marker,
        f"results page states it found nothing (marker={marker!r})",
    )


def _content_check(
    provider: str, haystack: str, evidence: SearchPageEvidence
) -> Verdict | None:
    del provider, haystack
    if evidence.result_anchors > 0:
        return "results", None, ""
    if evidence.opaque_anchors > 0:
        return (
            "results",
            None,
            f"{evidence.opaque_anchors} result link(s) present but their targets "
            "are not in the markup (click-through redirect); URLs are not "
            "statically extractable",
        )
    return None


def _fallback_check(evidence: SearchPageEvidence) -> Verdict:
    if evidence.text_chars < SHELL_TEXT_CHARS:
        return (
            "shell",
            None,
            f"page rendered only {evidence.text_chars} characters of text with no "
            "result containers; it did not reach a results view",
        )
    return (
        "unknown",
        None,
        f"page rendered {evidence.text_chars} characters of text but no result "
        "containers matched; the provider layout may have changed",
    )


def _wall_detail(marker: str) -> str:
    return (
        f"provider returned a wall page (marker={marker!r}); it refused the "
        "request rather than answering it"
    )


def _count_result_anchors(
    soup: BeautifulSoup, provider: str, *, base_url: str
) -> tuple[int, int]:
    extractable = 0
    opaque = 0
    for anchor in _result_anchors(soup, provider):
        href = anchor.get("href") or ""
        absolute = urljoin(base_url, href)
        if any(path in absolute for path in OPAQUE_RESULT_PATHS):
            opaque += 1
            continue
        if normalize_result_url(provider, href, base_url=base_url) is not None:
            extractable += 1
    return extractable, opaque


def _result_anchors(soup: BeautifulSoup, provider: str) -> list:
    """Result anchors that live inside a result container.

    Chrome links (nav, footer, related searches, ads) match the same selectors
    as real results. Requiring a container is what stops a page with nothing on
    it from being reported as a page full of results.
    """
    combined = ", ".join(RESULT_SELECTORS[provider])
    containers = RESULT_CONTAINERS[provider]
    hits: list = []
    for anchor in soup.select(combined):
        if not _in_result_container(anchor, containers):
            continue
        hits.append(anchor)
    return hits


def _in_result_container(anchor, containers: list[str]) -> bool:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        for selector in containers:
            try:
                if _match_element(selector, parent):
                    return True
            except SelectorSyntaxError:
                logger.debug("ignoring unusable container selector {!r}", selector)
    return False


def extract_search_results(provider: str, html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []

    for anchor in _result_anchors(soup, provider):
        href = anchor.get("href")
        if not href:
            continue
        if any(path in urljoin(base_url, href) for path in OPAQUE_RESULT_PATHS):
            continue
        normalized = normalize_result_url(provider, href, base_url=base_url)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
        if len(urls) >= MAX_SEARCH_RESULTS:
            break

    logger.debug(
        "parsing {} raw_anchors={} html_bytes={} returned={}",
        provider,
        len(_result_anchors(soup, provider)),
        len(html),
        len(urls),
    )
    if not urls:
        logger.warning(
            "parsing {} returned zero extractable results html_bytes={}",
            provider,
            len(html),
        )
    return urls


def extract_opaque_result_urls(
    provider: str,
    html: str,
    base_url: str,
    limit: int = MAX_SEARCH_RESULTS,
) -> list[str]:
    """Return result redirect URLs whose destinations require resolution.

    `limit` exceeds `MAX_SEARCH_RESULTS` when the caller intends to drop some:
    a provider that throttles resolution loses links, and a list trimmed to the
    cap before the attempts have a throttle budget returns fewer results than
    the page offered.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for anchor in _result_anchors(soup, provider):
        href = anchor.get("href") or ""
        absolute = urljoin(base_url, href)
        if not any(path in absolute for path in OPAQUE_RESULT_PATHS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= limit:
            break
    return urls


# A defer page names its own next hop: `<meta http-equiv="refresh"
# content="3;url=/goto?url=...">`. The destination of the result is not in the
# markup at all, so this is the only target the page yields.
_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["']?"""
    r"""\s*\d+\s*;\s*url=([^"'>\s]+)""",
    re.IGNORECASE,
)


def parse_defer_redirect_target(html: str, base_url: str) -> str | None:
    """Return the redirect endpoint a defer page points at, if it names one.

    Only a target that is itself a result redirect endpoint counts. A page
    cannot steer the resolver to an arbitrary host by declaring a refresh; the
    retry stays on the provider's own click-through path.
    """
    match = _META_REFRESH_RE.search(html)
    if match is None:
        return None
    target = urljoin(base_url, match.group(1))
    if not any(path in target for path in OPAQUE_RESULT_PATHS):
        return None
    return target


def normalize_result_url(provider: str, href: str, *, base_url: str) -> str | None:
    absolute = urljoin(base_url, href)
    parsed = urlsplit(absolute)

    redirect_url: str | None = None
    query_params = parse_qs(parsed.query)
    if provider == "duckduckgo" and parsed.path == "/l/":
        redirect_url = _first(query_params.get("uddg"))
    elif provider == "google" and parsed.path == "/url":
        redirect_url = _first(query_params.get("q")) or _first(query_params.get("url"))

    candidate = unquote(redirect_url) if redirect_url else absolute
    normalized = urlsplit(candidate)
    if normalized.scheme not in {"http", "https"}:
        return None
    if _is_internal_provider_url(provider, normalized.hostname):
        return None
    return normalized.geturl()


def is_search_blocked(provider: str, url: str, title: str, html: str) -> bool:
    return search_block_marker(provider, url, title, html) is not None


def search_block_marker(provider: str, url: str, title: str, html: str) -> str | None:
    """Return the marker that flagged this page as a provider wall, or None.

    Checking the URL, title, and first 4 KiB of rendered text is enough to
    catch DuckDuckGo `/static-pages/418.html` redirects, Google `/sorry`
    interstitials, and Yandex CAPTCHA pages without false-positives on real
    result pages.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:4000].lower()
    haystack = " ".join((url.lower(), title.lower(), text))
    return _first_marker(haystack, SEARCH_BLOCK_MARKERS[provider])


def _first_marker(haystack: str, markers: Iterable[str]) -> str | None:
    for marker in markers:
        if marker in haystack:
            return marker
    return None


def _is_internal_provider_url(provider: str, hostname: str | None) -> bool:
    if hostname is None:
        return True
    host = hostname.lower()
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in PROVIDER_HOST_SUFFIXES[provider]
    )


def _first(values: Iterable[str] | None) -> str | None:
    if values is None:
        return None
    for value in values:
        if value:
            return value
    return None


MIN_TOKEN_CHARS = 2


def build_snippets(
    text: str,
    query: str,
    *,
    max_matches: int,
    context_chars: int,
) -> list[str]:
    """Return up to `max_matches` passages of `text` relevant to `query`.

    An exact phrase match wins outright. Otherwise the query is treated as a
    bag of tokens and windows are ranked by how many distinct tokens they
    cover, so a natural-language query like "how to install chromium browser"
    still finds the passage that talks about installing chromium. Matching the
    whole query as one literal string silently returns nothing for every
    multi-word query, because the words are separated by markup in the page.
    """
    query_stripped = query.strip()
    if not query_stripped:
        return []

    half = max(context_chars // 2, 20)
    phrase = _windowed_snippets(text, _word_boundary_pattern(query_stripped), half)
    snippets = _dedupe(phrase, max_matches)
    if snippets:
        return snippets

    tokens = [
        token
        for token in re.split(r"\W+", query_stripped)
        if len(token) >= MIN_TOKEN_CHARS
    ]
    if not tokens:
        return []

    scored: list[tuple[int, int, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for token in tokens:
        for start, end, raw in _windowed_snippets(
            text, _word_boundary_pattern(token), half, with_span=True
        ):
            if (start, end) in seen_spans:
                continue
            seen_spans.add((start, end))
            coverage = sum(
                1 for other in tokens if _word_boundary_pattern(other).search(raw)
            )
            scored.append((-coverage, start, raw))

    scored.sort()
    return _dedupe([raw for _, _, raw in scored], max_matches)


def _windowed_snippets(
    text: str,
    pattern: re.Pattern[str],
    half: int,
    *,
    with_span: bool = False,
) -> list:
    out: list = []
    for match in pattern.finditer(text):
        start = max(0, match.start() - half)
        end = min(len(text), match.end() + half)

        while (
            start > 0
            and not text[start - 1].isspace()
            and match.start() - start < half + 10
        ):
            start -= 1
        while (
            end < len(text)
            and not text[end].isspace()
            and end - match.end() < half + 10
        ):
            end += 1

        raw = re.sub(r"\s+", " ", text[start:end].strip())
        if not raw:
            continue
        out.append((start, end, raw) if with_span else raw)
    return out


def _dedupe(values: Iterable[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _word_boundary_pattern(query: str) -> re.Pattern[str]:
    left = r"\b" if query[0].isalnum() else ""
    right = r"\b" if query[-1].isalnum() else ""
    return re.compile(f"{left}{re.escape(query)}{right}", re.IGNORECASE)


def detect_algolia_config(html: str) -> dict[str, str] | None:
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
    found: dict[str, str] = {}
    for match in _ALGOLIA_KEY_VALUE.finditer(html):
        found[match.group("key")] = match.group("val")
        if _has_algolia_keys(found):
            return _algolia_subset(found)
    return None


_SEARCH_INPUT_NAMES = ("q", "query", "search", "s")


@dataclass(frozen=True)
class SearchFormHit:
    action: str
    input_name: str


def detect_search_form(html: str, *, base_url: str) -> SearchFormHit | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates = (
        _match_role_search,
        _match_input_type_search,
        _match_input_name_fallback,
    )
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
        yield form, action


def _hit_for(action: str, base_url: str, input_name: str) -> SearchFormHit:
    return SearchFormHit(action=urljoin(base_url, action), input_name=input_name)


_TEXT_LIKE_INPUT_TYPES = {"", "search", "text", "email", "url", "tel"}
_NON_QUERY_INPUT_TYPES = {
    "button",
    "checkbox",
    "file",
    "hidden",
    "image",
    "radio",
    "reset",
    "submit",
}


def _search_input_name(form) -> str | None:
    fallback_name: str | None = None
    first_text_like_name: str | None = None
    for inp in form.find_all("input"):
        raw_name = (inp.get("name") or "").strip()
        if not raw_name:
            continue
        input_type = (inp.get("type") or "").strip().lower()
        if input_type == "search":
            return raw_name
        if input_type in _NON_QUERY_INPUT_TYPES:
            continue
        if fallback_name is None and raw_name.lower() in _SEARCH_INPUT_NAMES:
            fallback_name = raw_name
        if first_text_like_name is None and input_type in _TEXT_LIKE_INPUT_TYPES:
            first_text_like_name = raw_name
    return fallback_name or first_text_like_name


def _match_role_search(soup, base_url: str) -> SearchFormHit | None:
    for form, action in _iter_get_forms(soup):
        if (form.get("role") or "").lower() != "search":
            continue
        name = _search_input_name(form)
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
            raw_name = (inp.get("name") or "").strip()
            if raw_name.lower() in _SEARCH_INPUT_NAMES:
                return _hit_for(action, base_url, raw_name)
    return None


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
