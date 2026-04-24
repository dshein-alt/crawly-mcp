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


@dataclass(frozen=True)
class AlgoliaHit:
    app_id: str
    api_key: str
    index_name: str


class AlgoliaTier:
    name = "algolia"

    def __init__(
        self, *, http_client_factory: Callable[[], httpx.AsyncClient]
    ) -> None:
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
        url = (
            f"https://{hit.app_id.lower()}-dsn.algolia.net"
            f"/1/indexes/{hit.index_name}/query"
        )
        await URLSafetyGuard().validate_user_url(url)
        body = {
            "params": f"query={quote(query, safe='')}&hitsPerPage={MAX_SEARCH_RESULTS}"
        }
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
        snippet = re.sub(r"</?(em|mark)>", "", snippet_value)
        return PageSearchResult(
            snippet=snippet,
            url=raw.get("url"),
            title=" › ".join(title_parts) if title_parts else None,
        )
