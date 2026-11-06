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
    initial: PageSnapshot,
    provider: str,
    query: str,
    classify: ClassifyPage,
    window_seconds: float,
    sample_seconds: float,
    deadline: float | None = None,
) -> tuple[PageSnapshot, SearchPageEvidence]:
    """Wait out a provider's own automatic check; report the page it ends on.

    Start from the caller's last successful reading. Bound both polling sleeps
    and page reads by `deadline` (loop time), retaining that reading if the next
    read stalls or fails during navigation. Stop when results render or a human
    verification wall appears. Cancellation by the caller still propagates.
    """
    loop = asyncio.get_running_loop()
    window_end = loop.time() + window_seconds
    if deadline is not None:
        window_end = min(window_end, deadline)

    snapshot = initial
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
        try:
            async with asyncio.timeout_at(window_end):
                await asyncio.sleep(min(sample_seconds, remaining))
                candidate = await snapshot_page(page)
        except TimeoutError:
            break
        except Exception as exc:  # a navigation in flight destroys the context
            logger.debug(
                "settle read failed provider={} type={}", provider, type(exc).__name__
            )
            continue
        snapshot = candidate
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
