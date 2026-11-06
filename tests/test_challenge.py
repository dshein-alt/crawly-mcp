from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crawly_mcp.challenge import (
    looks_like_challenge,
    resolve_fetch_content,
    settle_search_page,
)
from crawly_mcp.errors import ChallengeBlockedError
from crawly_mcp.parsing import classify_search_page

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def _no_sleep(_: float) -> None:
    """Collapse the settle's polling pauses so the test does not wait."""


class FakeChallengePage:
    def __init__(self, snapshots: list[tuple[str, str, str]]) -> None:
        self._snapshots = snapshots
        self._index = 0

    @property
    def url(self) -> str:
        return self._snapshots[self._index][0]

    async def title(self) -> str:
        return self._snapshots[self._index][1]

    async def content(self) -> str:
        html = self._snapshots[self._index][2]
        if self._index < len(self._snapshots) - 1:
            self._index += 1
        return html


@pytest.mark.asyncio
async def test_resolve_fetch_content_waits_for_challenge_to_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", no_sleep)
    page = FakeChallengePage(
        [
            (
                "https://example.com/challenge",
                "Just a moment",
                "<html>Checking your browser</html>",
            ),
            ("https://example.com/final", "Example", "<html><body>Ready</body></html>"),
        ]
    )

    html = await resolve_fetch_content(page, settle_timeout_seconds=1)

    assert "Ready" in html


@pytest.mark.asyncio
async def test_resolve_fetch_content_reports_blocked_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", no_sleep)
    page = FakeChallengePage(
        [
            (
                "https://example.com/challenge",
                "Just a moment",
                "<html>Checking your browser</html>",
            )
        ]
    )

    with pytest.raises(ChallengeBlockedError, match="challenge screen"):
        await resolve_fetch_content(page, settle_timeout_seconds=0.01)


def test_looks_like_challenge_detects_common_markers() -> None:
    assert looks_like_challenge(
        "https://example.com/challenge",
        "Just a moment",
        "<html><body>Checking your browser</body></html>",
    )


class _RealisticWallPage:
    """A page that moves under the reader, the way a check in flight does.

    The frames are the real captured pages, so the settle is exercised against
    the copy Yandex actually serves rather than a paraphrase of it.
    """

    def __init__(self, frames: list[tuple[str, str, str]]) -> None:
        self._frames = frames
        self._index = 0
        self.reads = 0

    @property
    def url(self) -> str:
        return self._frames[self._index][0]

    async def title(self) -> str:
        return self._frames[self._index][1]

    async def content(self) -> str:
        self.reads += 1
        html = self._frames[self._index][2]
        if self._index < len(self._frames) - 1:
            self._index += 1
        return html


def _wall_frames() -> list[tuple[str, str, str]]:
    auto = _fixture("yandex_captcha_auto.html")
    results = _fixture("yandex_results.html")
    return [
        ("https://yandex.ru/showcaptchafast?d=EA09&retpath=x", "Верификация", auto),
        ("https://yandex.ru/search/?text=python", "python — Яндекс Поиск", results),
    ]


def _smart_frames() -> list[tuple[str, str, str]]:
    return [
        (
            "https://yandex.ru/showcaptchafast?d=EA09&retpath=x",
            "Верификация",
            _fixture("yandex_captcha_auto.html"),
        ),
        (
            "https://yandex.ru/showcaptcha?cc=1",
            "Вы не робот?",
            _fixture("yandex_captcha_smart.html"),
        ),
    ]


@pytest.mark.asyncio
async def test_settle_search_page_returns_the_results_the_check_redirected_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline case: a wall that was about to answer is not a refusal.

    Reading the page once reported `blocked` here. The check submits a
    fingerprint and redirects, and the settled reading is the results page.
    """
    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", _no_sleep)
    page = _RealisticWallPage(_wall_frames())

    snapshot, evidence = await settle_search_page(
        page,
        provider="yandex",
        query="python",
        classify=classify_search_page,
        window_seconds=3.0,
        sample_seconds=0.25,
    )

    assert evidence.kind == "results"
    assert evidence.result_anchors > 0
    assert "/search" in snapshot.url


@pytest.mark.asyncio
async def test_settle_search_page_stops_at_the_wall_that_asks_a_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check falls through to SmartCaptcha: report the wall it ended on.

    The block is preserved, and it names the checkbox wall rather than the
    transient interstitial. The window is abandoned the moment the page stops
    promising to continue, so a doomed wait costs one sample.
    """
    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", _no_sleep)
    page = _RealisticWallPage(_smart_frames())

    _, evidence = await settle_search_page(
        page,
        provider="yandex",
        query="python",
        classify=classify_search_page,
        window_seconds=3.0,
        sample_seconds=0.25,
    )

    assert evidence.kind == "captcha"
    assert evidence.mid_flight is False
    assert page.reads == 2  # saw the wall, saw the checkbox, stopped


@pytest.mark.asyncio
async def test_settle_search_page_never_waits_on_a_human_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkbox wall is read once and reported, costing no latency at all."""
    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", _no_sleep)
    page = _RealisticWallPage(_smart_frames()[1:])

    _, evidence = await settle_search_page(
        page,
        provider="yandex",
        query="python",
        classify=classify_search_page,
        window_seconds=3.0,
        sample_seconds=0.25,
    )

    assert evidence.kind == "captcha"
    assert page.reads == 1


@pytest.mark.asyncio
async def test_settle_search_page_gives_up_within_its_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check that never finishes stays a block, and stays inside the budget."""
    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", _no_sleep)
    stuck = _wall_frames()[:1] * 3
    page = _RealisticWallPage(stuck)
    loop = asyncio.get_running_loop()

    started = loop.time()
    _, evidence = await settle_search_page(
        page,
        provider="yandex",
        query="python",
        classify=classify_search_page,
        window_seconds=0.3,
        sample_seconds=0.05,
    )
    elapsed = loop.time() - started

    assert evidence.kind == "captcha"
    assert elapsed < 1.5


@pytest.mark.asyncio
async def test_settle_search_page_skips_a_window_the_deadline_already_ate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No budget left means no settle, so a wall never becomes a timeout."""
    monkeypatch.setattr("crawly_mcp.challenge.asyncio.sleep", _no_sleep)
    page = _RealisticWallPage(_wall_frames())
    loop = asyncio.get_running_loop()

    _, evidence = await settle_search_page(
        page,
        provider="yandex",
        query="python",
        classify=classify_search_page,
        window_seconds=3.0,
        sample_seconds=0.25,
        deadline=loop.time() - 1.0,
    )

    assert evidence.kind == "captcha"
    assert page.reads == 1
