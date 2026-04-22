from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup

from web_search_mcp.errors import ChallengeBlockedError


class PageLike(Protocol):
    @property
    def url(self) -> str: ...

    async def title(self) -> str: ...

    async def content(self) -> str: ...


CHALLENGE_MARKERS = (
    "access denied",
    "are you human",
    "aubis",
    "captcha",
    "challenge",
    "checking if the site connection is secure",
    "checking your browser",
    "enable javascript and cookies to continue",
    "just a moment",
    "please verify you are a human",
    "security check",
    "unusual traffic",
    "verify you are human",
)


@dataclass(slots=True)
class PageSnapshot:
    url: str
    title: str
    html: str


def looks_like_challenge(url: str, title: str, html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:4000]
    haystack = " ".join((url, title, text)).lower()
    return any(marker in haystack for marker in CHALLENGE_MARKERS)


async def snapshot_page(page: PageLike) -> PageSnapshot:
    title = await page.title()
    html = await page.content()
    return PageSnapshot(url=page.url, title=title, html=html)


async def resolve_fetch_content(page: PageLike, *, settle_timeout_seconds: float) -> str:
    await asyncio.sleep(0.35)
    snapshot = await snapshot_page(page)
    if not looks_like_challenge(snapshot.url, snapshot.title, snapshot.html):
        return snapshot.html

    deadline = time.monotonic() + settle_timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        snapshot = await snapshot_page(page)
        if not looks_like_challenge(snapshot.url, snapshot.title, snapshot.html):
            return snapshot.html

    raise ChallengeBlockedError("page stayed on a browser challenge screen")
