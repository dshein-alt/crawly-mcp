from __future__ import annotations

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
