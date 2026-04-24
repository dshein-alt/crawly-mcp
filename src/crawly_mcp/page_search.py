from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)

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

_CLIENT_SEARCH_SETTLE_TIMEOUT_MS = 2_000


@dataclass(frozen=True)
class TextHit:
    text: str
    title: str | None


@dataclass(frozen=True)
class TierExecutionResult:
    results: list[PageSearchResult]
    results_url: str | None = None


class TextTier:
    name = "text"

    def detect(self, source_html: str, source_url: str) -> TextHit:
        del source_url
        soup = BeautifulSoup(source_html, "html.parser")
        title_tag = soup.title
        title = title_tag.string.strip() if title_tag and title_tag.string else None
        text = extract_readable_text(source_html)
        return TextHit(text=text, title=title)

    async def execute(self, hit: TextHit, query: str) -> TierExecutionResult:
        snippets = build_snippets(
            hit.text,
            query,
            max_matches=MAX_SEARCH_RESULTS,
            context_chars=PAGE_SEARCH_SNIPPET_CONTEXT_CHARS,
        )
        return TierExecutionResult(
            results=[
                PageSearchResult(snippet=snippet, url=None, title=hit.title)
                for snippet in snippets
            ]
        )


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
        del source_url
        config = detect_algolia_config(source_html)
        if config is None:
            return None
        return AlgoliaHit(
            app_id=config["appId"],
            api_key=config["apiKey"],
            index_name=config["indexName"],
        )

    async def execute(self, hit: AlgoliaHit, query: str) -> TierExecutionResult:
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
        return TierExecutionResult(
            results=[self._map_hit(h) for h in payload.get("hits", [])]
        )

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
            title=" > ".join(title_parts) if title_parts else None,
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
    ) -> TierExecutionResult:
        await URLSafetyGuard().validate_user_url(hit.descriptor_url)
        async with self._client_factory() as client:
            response = await client.get(
                hit.descriptor_url, timeout=PAGE_SEARCH_TIER_TIMEOUT_SECONDS
            )
        response.raise_for_status()
        template = self._first_html_template(response.text)
        if template is None:
            return TierExecutionResult(results=[])
        results_url = self._substitute(template, query)
        html = await self._fetch_page(results_url)
        return TierExecutionResult(
            results=_snippets_from_html(html, query, base_url=results_url),
            results_url=results_url,
        )

    @staticmethod
    def _first_html_template(xml_text: str) -> str | None:
        try:
            root = ET.fromstring(xml_text)  # noqa: S314 - descriptors are small and fetched from origin under tier timeout
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
    base_url: str | None = None,
) -> list[PageSearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    linked_results = _linked_results_from_search_html(soup, base_url=base_url)
    if linked_results:
        return linked_results

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


def _linked_results_from_search_html(
    soup: BeautifulSoup,
    *,
    base_url: str | None,
) -> list[PageSearchResult]:
    containers = list(soup.select("#search-results, .search-results, main"))
    if not containers:
        containers = [soup]

    results: list[PageSearchResult] = []
    seen_urls: set[str] = set()
    for container in containers:
        for anchor in container.select("a[href]"):
            raw_href = (anchor.get("href") or "").strip()
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = urljoin(base_url, raw_href) if base_url else raw_href
            if url in seen_urls:
                continue
            title = anchor.get_text(" ", strip=True)
            snippet = _result_snippet(anchor)
            if not title and not snippet:
                continue
            seen_urls.add(url)
            results.append(
                PageSearchResult(
                    snippet=snippet or title,
                    url=url,
                    title=title or None,
                )
            )
            if len(results) >= MAX_SEARCH_RESULTS:
                return results
    return results


def _result_snippet(anchor) -> str:
    result_root = anchor.find_parent(["li", "article", "div"]) or anchor.parent
    if result_root is None:
        return anchor.get_text(" ", strip=True)
    text = result_root.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


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
        if len(parts) < 3 or not parts[0]:  # noqa: PLR2004 - subdomain + rtd apex (two labels)
            return None
        slug = parts[0]
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) < 2:  # noqa: PLR2004 - /<lang>/<version>/ prefix is two segments
            return None
        version = segments[1]
        return ReadthedocsHit(project=slug, version=version)

    async def execute(
        self, hit: ReadthedocsHit, query: str
    ) -> TierExecutionResult:
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
                    return TierExecutionResult(results=out)
        return TierExecutionResult(results=out)

    @staticmethod
    def _block_url(result: dict, block: dict) -> str | None:
        domain = (result.get("domain") or "").rstrip("/")
        path = (result.get("path") or "").lstrip("/")
        anchor = block.get("id")
        if not domain or not path:
            return None
        base = f"{domain}/{path}"
        return f"{base}#{anchor}" if anchor else base


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

    async def execute(self, hit: FormHit, query: str) -> TierExecutionResult:
        results_url = self._append_query(hit.form.action, hit.form.input_name, query)
        html = await self._fetch_page(results_url)
        return TierExecutionResult(
            results=_snippets_from_html(html, query, base_url=results_url),
            results_url=results_url,
        )

    @staticmethod
    def _append_query(action: str, param_name: str, query: str) -> str:
        split = urlsplit(action)
        pairs = parse_qsl(split.query, keep_blank_values=True)
        pairs.append((param_name, query))
        new_query = urlencode(pairs, doseq=True)
        return urlunsplit(
            (split.scheme, split.netloc, split.path, new_query, split.fragment)
        )


class PageSearchService:
    def __init__(
        self,
        browser_manager: BrowserManager,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._browser_manager = browser_manager
        self._http_client_factory = http_client_factory
        self._tiers: list = self._build_default_tiers()

    def _build_default_tiers(self) -> list:
        async def fetch_page(url: str) -> str:
            return await self._fetch_source_html(url)

        return [
            AlgoliaTier(http_client_factory=self._http_client_factory),
            OpenSearchTier(
                http_client_factory=self._http_client_factory,
                page_fetcher=fetch_page,
            ),
            ReadthedocsTier(http_client_factory=self._http_client_factory),
            FormTier(page_fetcher=fetch_page),
            TextTier(),
        ]

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
                html = await page.content()
                if self._looks_like_client_search_shell(html, url):
                    await self._settle_client_search_page(page)
                    html = await page.content()
            except PlaywrightTimeoutError as exc:
                raise NavigationFailedError(f"source fetch timed out: {exc}") from exc
            except PlaywrightError as exc:
                raise NavigationFailedError(f"source fetch failed: {exc}") from exc
            else:
                return html
            finally:
                with suppress(Exception):
                    await page.close()
        finally:
            await browser_context.close()

    @staticmethod
    def _looks_like_client_search_shell(html: str, url: str) -> bool:
        parsed = urlparse(url)
        if not parse_qsl(parsed.query, keep_blank_values=True):
            return False
        if "search" not in parsed.path.lower():
            return False
        markers = (
            "searchtools.js",
            'id="search-results"',
            "id=\"search-documentation\"",
        )
        return any(marker in html for marker in markers)

    @staticmethod
    async def _settle_client_search_page(page) -> None:
        with suppress(PlaywrightTimeoutError, PlaywrightError):
            await page.wait_for_function(
                """
                () => {
                    const container = document.querySelector('#search-results');
                    return !!container && container.innerText.trim().length > 0;
                }
                """,
                timeout=_CLIENT_SEARCH_SETTLE_TIMEOUT_MS,
            )
            return
        with suppress(PlaywrightTimeoutError, PlaywrightError):
            await page.wait_for_load_state(
                "networkidle", timeout=_CLIENT_SEARCH_SETTLE_TIMEOUT_MS
            )

    async def search(self, *, url: str, query: str) -> PageSearchResponse:
        try:
            request = PageSearchRequest(url=url, query=query)
        except ValidationError as exc:
            raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

        await URLSafetyGuard().validate_user_url(request.url)

        logger.info(
            "page_search entry url={!r} query={!r}", request.url, request.query
        )
        started = asyncio.get_running_loop().time()

        try:
            async with asyncio.timeout(FETCH_TOTAL_TIMEOUT_SECONDS):
                source_html = await self._fetch_source_html(request.url)
                attempted: list[str] = []

                for tier in self._tiers:
                    if tier.name == "text":
                        break
                    hit = tier.detect(source_html, request.url)
                    if hit is None:
                        continue
                    attempted.append(tier.name)
                    try:
                        outcome = await asyncio.wait_for(
                            tier.execute(hit, request.query),
                            PAGE_SEARCH_TIER_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning("page_search tier={} timed out", tier.name)
                        continue
                    except Exception as exc:
                        logger.warning(
                            "page_search tier={} failed: {}: {}",
                            tier.name,
                            type(exc).__name__,
                            exc,
                        )
                        continue
                    results, results_url = self._normalize_tier_outcome(outcome)
                    if results:
                        return self._respond(
                            mode=tier.name,
                            attempted=attempted,
                            source_url=request.url,
                            results_url=results_url,
                            results=results,
                        )

                text_tier = self._tiers[-1]
                attempted.append("text")
                hit = text_tier.detect(source_html, request.url)
                text_results, text_results_url = self._normalize_tier_outcome(
                    await text_tier.execute(hit, request.query)
                )
                return self._respond(
                    mode="text",
                    attempted=attempted,
                    source_url=request.url,
                    results_url=text_results_url,
                    results=text_results,
                )
        except TimeoutError as exc:
            raise TimeoutExceededError(
                "page_search exceeded the overall timeout"
            ) from exc
        finally:
            duration = asyncio.get_running_loop().time() - started
            logger.info("page_search done duration={:.2f}s", duration)

    @staticmethod
    def _normalize_tier_outcome(
        outcome: TierExecutionResult | list[PageSearchResult],
    ) -> tuple[list[PageSearchResult], str | None]:
        if isinstance(outcome, TierExecutionResult):
            return outcome.results, outcome.results_url
        return outcome, None

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


def _truncate_page_search_response(response: PageSearchResponse) -> PageSearchResponse:
    limit = resolve_fetch_max_size()
    if len(response.model_dump_json().encode("utf-8")) <= limit:
        return response

    truncated = response.model_copy(update={"truncated": True})
    while (
        truncated.results
        and len(truncated.model_dump_json().encode("utf-8")) > limit
    ):
        truncated = truncated.model_copy(
            update={"results": truncated.results[:-1]}
        )

    if len(truncated.model_dump_json().encode("utf-8")) > limit:
        logger.warning(
            "page_search response skeleton exceeds CRAWLY_FETCH_MAX_SIZE={} "
            "even with zero results; returning anyway",
            limit,
        )
    return truncated
