from __future__ import annotations

import json as _json
import re
from collections.abc import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from loguru import logger

from crawly_mcp.constants import DEFAULT_PROVIDER, MAX_SEARCH_RESULTS

PROVIDER_HOST_SUFFIXES = {
    "duckduckgo": ("duckduckgo.com", "duck.com"),
    "google": ("google.com",),
    "yandex": ("yandex.com", "yandex.ru", "yandex.kz", "yandex.by"),
}

SEARCH_URL_TEMPLATES = {
    "duckduckgo": "https://duckduckgo.com/?q={query}&ia=web",
    "google": "https://www.google.com/search?q={query}&hl=en",
    "yandex": "https://yandex.ru/search/?text={query}",
}

RESULT_SELECTORS = {
    "duckduckgo": [
        "a.result__a[href]",
        "article a[data-testid='result-title-a'][href]",
    ],
    "google": [
        "div.yuRUbf > a[href]",
        "a[jsname][href]",
    ],
    "yandex": [
        "a.OrganicTitle-Link[href]",
        "li.serp-item a.Link[href]",
    ],
}

SEARCH_BLOCK_MARKERS = {
    "duckduckgo": (
        "anomaly",
        "automated requests",
        "verify",
        "static-pages",  # bot-detection redirect target, e.g. /static-pages/418.html
    ),
    "google": (
        "before you continue",
        "detected unusual traffic",
        "sorry",
        "unusual traffic",
    ),
    "yandex": ("robot", "captcha", "проверка", "unusual"),
}


def build_search_url(provider: str | None, context: str) -> str:
    resolved_provider = provider or DEFAULT_PROVIDER
    template = SEARCH_URL_TEMPLATES[resolved_provider]
    return template.format(query=quote_plus(context))


def extract_search_results(provider: str, html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []

    combined_selector = ", ".join(RESULT_SELECTORS[provider])
    raw_anchors = soup.select(combined_selector)
    for anchor in raw_anchors:
        href = anchor.get("href")
        if not href:
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
        len(raw_anchors),
        len(html),
        len(urls),
    )
    if not urls:
        logger.warning(
            "parsing {} returned zero results raw_anchors={} html_bytes={}",
            provider,
            len(raw_anchors),
            len(html),
        )
    return urls


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
    """Return the marker that flagged this page as a provider block, or None.

    Checking the URL, title, and first 4 KiB of rendered text is enough to
    catch DuckDuckGo `/static-pages/418.html` redirects, Google `/sorry`
    interstitials, and Yandex CAPTCHA pages without false-positives on real
    result pages.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:4000].lower()
    haystack = " ".join((url.lower(), title.lower(), text))
    for marker in SEARCH_BLOCK_MARKERS[provider]:
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


def build_snippets(
    text: str,
    query: str,
    *,
    max_matches: int,
    context_chars: int,
) -> list[str]:
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

        raw = text[start:end].strip()
        normalized = re.sub(r"\s+", " ", raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            snippets.append(normalized)

    return snippets


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
