from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from loguru import logger

from crawly_mcp.constants import MAX_SEARCH_RESULTS, STANDARD_USER_AGENT
from crawly_mcp.errors import ProviderBlockedError

_BLOCKING_STATUS = frozenset({401, 403, 429})


async def searxng_search(
    instance_url: str,
    query: str,
    *,
    client: httpx.AsyncClient,
    timeout: float,
) -> list[str]:
    """Query one SearXNG instance via its JSON API and return up to 5 result URLs.

    Raises ProviderBlockedError for refused/non-JSON responses and 401/403/429.
    Propagates httpx.TimeoutException, httpx.RequestError, and httpx.HTTPStatusError
    for transport-level or other HTTP failures so the caller can surface them.
    """
    url = instance_url.rstrip("/") + "/search"
    params = {
        "q": query,
        "format": "json",
        "safesearch": "0",
        "language": "en",
    }
    headers = {
        "User-Agent": STANDARD_USER_AGENT,
        "Accept": "application/json",
    }

    response = await client.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout,
    )

    if response.status_code in _BLOCKING_STATUS:
        raise ProviderBlockedError(
            f"searxng instance {instance_url!r} returned {response.status_code}"
        )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        raise ProviderBlockedError(
            f"searxng instance {instance_url!r} returned non-JSON content-type "
            f"{content_type!r}; format=json may be disabled"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderBlockedError(
            f"searxng instance {instance_url!r} returned malformed JSON"
        ) from exc

    raw_results = payload.get("results") or []
    urls: list[str] = []
    seen: set[str] = set()
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        raw_url = (entry.get("url") or "").strip()
        if not raw_url:
            continue
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if raw_url in seen:
            continue
        seen.add(raw_url)
        urls.append(raw_url)
        if len(urls) >= MAX_SEARCH_RESULTS:
            break

    logger.debug(
        "searxng_search instance={} raw={} returned={}",
        instance_url,
        len(raw_results),
        len(urls),
    )
    return urls
