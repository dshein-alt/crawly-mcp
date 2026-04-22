import asyncio
import os
from pathlib import Path

import pytest

from web_search_mcp.browser import BrowserManager, resolve_chromium_executable
from web_search_mcp.errors import BrowserUnavailableError


class Tracker:
    def __init__(self) -> None:
        self.current = 0
        self.maximum = 0
        self.lock = asyncio.Lock()


class FakeNavPage:
    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        del url, wait_until, timeout
        async with self.tracker.lock:
            self.tracker.current += 1
            self.tracker.maximum = max(self.tracker.maximum, self.tracker.current)
        await asyncio.sleep(0.01)
        async with self.tracker.lock:
            self.tracker.current -= 1


@pytest.mark.asyncio
async def test_goto_respects_global_navigation_limit() -> None:
    manager = BrowserManager(max_concurrent_navigations=3)
    tracker = Tracker()
    pages = [FakeNavPage(tracker) for _ in range(5)]

    await asyncio.gather(
        *(manager.goto(page, f"https://example.com/{index}", timeout_ms=100) for index, page in enumerate(pages))
    )

    assert tracker.maximum == 3


def test_resolve_chromium_executable_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chromium_path = tmp_path / "chromium"
    chromium_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", str(chromium_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert resolve_chromium_executable() == os.fspath(chromium_path)


def test_resolve_chromium_executable_uses_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/chromium-browser" if name == "chromium-browser" else None,
    )

    assert resolve_chromium_executable() == "/usr/bin/chromium-browser"


def test_resolve_chromium_executable_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(BrowserUnavailableError, match="system Chromium was not found"):
        resolve_chromium_executable()
