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

    async def execute(
        self, hit: OpenSearchHit, query: str
    ) -> list[PageSearchResult]:
        await URLSafetyGuard().validate_user_url(hit.descriptor_url)
        async with self._client_factory() as client:
            response = await client.get(
                hit.descriptor_url, timeout=PAGE_SEARCH_TIER_TIMEOUT_SECONDS
            )
        response.raise_for_status()
        template = self._first_html_template(response.text)
        if template is None:
            return []
        results_url = self._substitute(template, query)
        html = await self._fetch_page(results_url)
        return _snippets_from_html(html, query, results_url=results_url)

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
    del results_url
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


_RTD_API = "https://readthedocs.org/api/v2/search/"
_RTD_HOSTS = (".readthedocs.io", ".readthedocs-hosted.com")


@dataclass(frozen=True)
class ReadthedocsHit:
    project: str
    version: str


class ReadthedocsTier:
    name = "readthedocs"

    def __init__(
        self, *, http_client_factory: Callable[[], httpx.AsyncClient]
    ) -> None:
        self._client_factory = http_client_factory

    def detect(self, source_html: str, source_url: str) -> ReadthedocsHit | None:
        del source_html
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower()
        if not any(host.endswith(suffix) for suffix in _RTD_HOSTS):
            return None
        parts = host.split(".")
        if len(parts) < 3 or not parts[0]:
            return None
        slug = parts[0]
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) < 2:
            return None
        version = segments[1]
        return ReadthedocsHit(project=slug, version=version)

    async def execute(
        self, hit: ReadthedocsHit, query: str
    ) -> list[PageSearchResult]:
        await URLSafetyGuard().validate_user_url(_RTD_API)
        params = {"q": query, "project": hit.project, "version": hit.version}
        async with self._client_factory() as client:
            response = await client.get(
                _RTD_API,
                params=params,
                timeout=PAGE_SEARCH_TIER_TIMEOUT_SECONDS,
            )
        response.raise_for_status()
        payload = response.json()
        out: list[PageSearchResult] = []
        for result in payload.get("results", []):
            for block in result.get("blocks", [])[:MAX_SEARCH_RESULTS]:
                url = self._block_url(result, block)
                snippet = re.sub(r"</?[a-zA-Z][^>]*>", "", block.get("content") or "")
                out.append(
                    PageSearchResult(
                        snippet=snippet.strip(),
                        url=url,
                        title=block.get("title"),
                    )
                )
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
