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
