from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeAlias

from bs4 import BeautifulSoup
from loguru import logger

from crawly_mcp.errors import ChallengeBlockedError

if TYPE_CHECKING:
    from crawly_mcp.parsing import SearchPageEvidence

# `classify_search_page(provider, url, title, html, query=...)`, injected so
# this module stays free of provider knowledge and of the parser.
ClassifyPage: TypeAlias = Callable[..., "SearchPageEvidence"]


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


async def resolve_fetch_content(
    page: PageLike, *, settle_timeout_seconds: float
) -> str:
    await asyncio.sleep(0.35)
    snapshot = await snapshot_page(page)
    if not looks_like_challenge(snapshot.url, snapshot.title, snapshot.html):
        return snapshot.html

    logger.info("challenge detected url={!r} title={!r}", snapshot.url, snapshot.title)
    deadline = time.monotonic() + settle_timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        snapshot = await snapshot_page(page)
        if not looks_like_challenge(snapshot.url, snapshot.title, snapshot.html):
            logger.info("challenge settled url={!r}", snapshot.url)
            return snapshot.html

    logger.warning("challenge unsettled url={!r}", snapshot.url)
    raise ChallengeBlockedError("page stayed on a browser challenge screen")


async def settle_search_page(
    page: PageLike,
    *,
    provider: str,
    query: str,
    classify: ClassifyPage,
    window_seconds: float,
    sample_seconds: float,
    deadline: float | None = None,
) -> tuple[PageSnapshot, SearchPageEvidence]:
    """Wait out a provider's own automatic check; report the page it ends on.

    A provider that is mid-check serves a page that says it will carry on by
    itself, and the check lands within about a second: it either reaches the
    results page or reaches the wall that asks a human to click. Reading the
    page once, as search used to, reports `blocked` on a page that was about to
    answer, and names the transient interstitial as the wall.

    The window closes on the first reading that is no longer mid-flight, so a
    check that resolves costs one sample rather than the whole window, and a
    wall that demands a human is reported as soon as it is seen. A read that
    fails is a navigation in flight, not a failure of the search: the last good
    reading is kept and the poll continues.

    `deadline` is loop time, the provider attempt's own budget. A window that
    would overrun it is not started at all, because a settle cancelled
    mid-flight leaves the caller with a timeout in place of the answer the page
    was about to give.
    """
    loop = asyncio.get_running_loop()
    window_end = loop.time() + window_seconds
    if deadline is not None:
        window_end = min(window_end, deadline)

    snapshot = await snapshot_page(page)
    evidence = classify(
        provider, snapshot.url, snapshot.title, snapshot.html, query=query
    )
    if not evidence.mid_flight:
        return snapshot, evidence

    logger.info(
        "search wall mid-flight provider={} kind={} marker={!r}; settling for {:.1f}s",
        provider,
        evidence.kind,
        evidence.marker,
        max(0.0, window_end - loop.time()),
    )
    samples = 0
    while evidence.mid_flight and loop.time() < window_end:
        remaining = window_end - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(sample_seconds, remaining))
        try:
            snapshot = await snapshot_page(page)
        except Exception as exc:  # a navigation in flight destroys the context
            logger.debug(
                "settle read failed provider={} type={}", provider, type(exc).__name__
            )
            continue
        samples += 1
        evidence = classify(
            provider, snapshot.url, snapshot.title, snapshot.html, query=query
        )

    logger.info(
        "search wall settled provider={} kind={} anchors={} samples={} url={!r}",
        provider,
        evidence.kind,
        evidence.result_anchors + evidence.opaque_anchors,
        samples,
        snapshot.url,
    )
    return snapshot, evidence
