from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from loguru import logger

from crawly_mcp.constants import MAX_SEARCH_RESULTS, STANDARD_USER_AGENT
from crawly_mcp.errors import ProviderBlockedError

_BLOCKING_STATUS = frozenset({401, 403, 429})


@dataclass(slots=True)
class SearxngOutcome:
    """What an instance's JSON answer actually said, not just its URL list.

    An empty `results` list has two opposite readings and the URL list alone
    cannot tell them apart: the instance searched and found nothing, or the
    instance could not search and said so in a field the URL list throws away.
    Collapsing the second into the first stops a cascade on a provider that
    never answered.
    """

    urls: list[str] = field(default_factory=list)
    error: str | None = None
    unresponsive_engines: tuple[str, ...] = ()

    @property
    def answered_empty(self) -> bool:
        """A clean "we searched and found nothing" — the trustworthy empty."""
        return not self.urls and self.error is None and not self.unresponsive_engines


async def searxng_search(
    instance_url: str,
    query: str,
    *,
    client: httpx.AsyncClient,
    timeout: float,
) -> SearxngOutcome:
    """Query one SearXNG instance via its JSON API.

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

    outcome = _outcome_from_payload(payload, instance_url)
    logger.debug(
        "searxng_search instance={} returned={} error={!r} unresponsive_engines={!r}",
        instance_url,
        len(outcome.urls),
        outcome.error,
        outcome.unresponsive_engines,
    )
    return outcome


def _outcome_from_payload(payload: object, instance_url: str) -> SearxngOutcome:
    """Read an instance's JSON body into urls plus the reasons it has none.

    The URL list is the part a caller wants; the error and unresponsive-engine
    fields are the part that says whether an empty list means "found nothing"
    or "could not look". Keeping the reading in one place is what stops the
    second from being reported as the first.
    """
    if not isinstance(payload, dict):
        raise ProviderBlockedError(
            f"searxng instance {instance_url!r} returned {type(payload).__name__} "
            "where a JSON object was expected"
        )

    error = payload.get("error")
    error_text = str(error).strip() if error else None

    unresponsive = payload.get("unresponsive_engines") or []
    unresponsive_names = tuple(
        str(entry).strip()
        for entry in (unresponsive if isinstance(unresponsive, list) else [])
        if str(entry).strip()
    )

    entries, shape_error = _result_entries(payload)
    if shape_error and error_text is None:
        error_text = shape_error

    urls, malformed = _harvest_urls(entries)
    if malformed and error_text is None:
        error_text = (
            "malformed result entries: " + ", ".join(sorted(set(malformed)))[:120]
        )

    logger.debug(
        "searxng payload instance={} raw={} returned={} error={!r}",
        instance_url,
        len(entries),
        len(urls),
        error_text,
    )
    return SearxngOutcome(
        urls=urls, error=error_text, unresponsive_engines=unresponsive_names
    )


def _result_entries(payload: dict) -> tuple[list[object], str | None]:
    """Validate the `results` shape before anything iterates it.

    `{"results": "abc"}` iterates as characters and `{"results": {}}` as keys;
    reading either as a result list invents an empty search and stops the
    cascade on a provider that never answered. A missing field is the same
    unreadable shape, not a search that found nothing.
    """
    if "results" not in payload:
        return [], "response carried no 'results' field"
    raw = payload["results"]
    if raw is None:
        return [], None
    if isinstance(raw, list):
        return raw, None
    return [], f"'results' was {type(raw).__name__}, expected a list"


def _harvest_urls(entries: list[object]) -> tuple[list[str], list[str]]:
    """Take http(s) URLs out of result entries, reporting what cannot be read.

    An entry whose URL is not a string used to raise AttributeError out of the
    adapter, past the cascade's provider-error handling; it is now a named shape
    failure the cascade can move on from.
    """
    urls: list[str] = []
    malformed: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            malformed.append(f"entry is {type(entry).__name__}")
            continue
        raw_url = entry.get("url")
        if raw_url is None:
            continue
        if not isinstance(raw_url, str):
            malformed.append(f"url is {type(raw_url).__name__}")
            continue
        cleaned = raw_url.strip()
        if not cleaned:
            continue
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"}:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
        if len(urls) >= MAX_SEARCH_RESULTS:
            break
    return urls, malformed
