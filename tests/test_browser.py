import asyncio
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import patchright.async_api as playwright_api
import pytest

from crawly_mcp.browser import (
    BrowserManager,
    SearchContextHandle,
    resolve_browser_source,
    resolve_chromium_executable,
)
from crawly_mcp.errors import BrowserUnavailableError


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


def test_resolve_browser_source_defaults_to_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAYWRIGHT_BROWSER_SOURCE", raising=False)

    assert resolve_browser_source() == "system"


@pytest.mark.asyncio
async def test_bundled_browser_source_launches_without_executable_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_calls: list[dict[str, object]] = []

    class FakeBrowser:
        def on(self, *_args: object) -> None:
            return None

        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch(self, **kwargs: object) -> FakeBrowser:
            launch_calls.append(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakeAsyncPlaywrightManager:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    def fake_async_playwright() -> FakeAsyncPlaywrightManager:
        return FakeAsyncPlaywrightManager()

    monkeypatch.setenv("PLAYWRIGHT_BROWSER_SOURCE", "bundled")
    monkeypatch.setattr(playwright_api, "async_playwright", fake_async_playwright)

    manager = BrowserManager()
    await manager.start()
    await manager.close()

    assert len(launch_calls) == 1
    assert "executable_path" not in launch_calls[0]


@pytest.mark.asyncio
async def test_system_browser_source_launches_with_executable_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launch_calls: list[dict[str, object]] = []

    class FakeBrowser:
        def on(self, *_args: object) -> None:
            return None

        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch(self, **kwargs: object) -> FakeBrowser:
            launch_calls.append(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakeAsyncPlaywrightManager:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    def fake_async_playwright() -> FakeAsyncPlaywrightManager:
        return FakeAsyncPlaywrightManager()

    chromium_path = tmp_path / "chromium"
    chromium_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("PLAYWRIGHT_BROWSER_SOURCE", "system")
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", os.fspath(chromium_path))
    monkeypatch.setattr(playwright_api, "async_playwright", fake_async_playwright)

    manager = BrowserManager()
    await manager.start()
    await manager.close()

    assert len(launch_calls) == 1
    assert launch_calls[0]["executable_path"] == os.fspath(chromium_path)


def test_context_options_reads_tz_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    manager = BrowserManager()
    opts = manager._context_options()
    assert opts["timezone_id"] == "Europe/Berlin"
    assert opts["locale"] == "en-US"
    assert opts["viewport"] == {"width": 1366, "height": 768}
    assert opts["java_script_enabled"] is True
    assert "sec-ch-ua" in opts["extra_http_headers"]


def test_context_options_defaults_timezone_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    manager = BrowserManager()
    opts = manager._context_options()
    assert opts["timezone_id"] == "America/New_York"


def test_search_context_handle_is_frozen_dataclass() -> None:
    # Use real sentinels — just type-shape check
    ctx = object()
    guard = object()
    handle = SearchContextHandle(context=ctx, guard=guard, first_use=True)
    assert handle.context is ctx
    assert handle.guard is guard
    assert handle.first_use is True
    with pytest.raises(FrozenInstanceError):
        handle.first_use = False  # type: ignore[misc]


class _AsyncNoop:
    async def __call__(self, *args: Any, **kwargs: Any) -> None: ...


@pytest.mark.asyncio
async def test_search_context_returns_handle_and_tracks_first_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """First call: first_use=True. Second call for same provider: first_use=False."""
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("CRAWLY_PROFILE_CLEANUP_ON_START", "false")

    created_dirs: list[str] = []

    class FakeChromium:
        async def launch(self, **kwargs: Any) -> Any:
            return object()  # unused in this test

        async def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> Any:
            created_dirs.append(user_data_dir)
            # _AsyncNoop is an async-callable assigned where patchright
            # expects a method; `await ctx.route(...)` invokes __call__.
            return SimpleNamespace(
                route=_AsyncNoop(),
                close=_AsyncNoop(),
                on=lambda *_a, **_k: None,
                is_closed=lambda: False,
            )

    class FakePlaywright:
        chromium = FakeChromium()
        async def stop(self) -> None: ...

    async def fake_async_playwright() -> FakePlaywright:
        return FakePlaywright()

    monkeypatch.setattr(playwright_api, "async_playwright", lambda: SimpleNamespace(start=fake_async_playwright))

    manager = BrowserManager()
    h1 = await manager.search_context("duckduckgo")
    assert h1.first_use is True

    h2 = await manager.search_context("duckduckgo")
    assert h2.first_use is False
    assert h2.context is h1.context  # same cached instance
    assert len(created_dirs) == 1  # not recreated
