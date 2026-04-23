import pytest

from crawly_mcp.challenge import looks_like_challenge, resolve_fetch_content
from crawly_mcp.errors import ChallengeBlockedError


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
