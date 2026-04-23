import asyncio
import os
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import patchright.async_api as playwright_api
import pytest

from crawly_mcp.browser import (
    BrowserManager,
    SearchContextHandle,
    build_standard_headers,
    persistent_profiles_enabled,
    resolve_browser_language,
    resolve_browser_location,
    resolve_browser_source,
    resolve_browser_viewport,
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


def test_context_options_reads_browser_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLY_BROWSER_LANG", "de-DE")
    monkeypatch.setenv("CRAWLY_BROWSER_LOCATION", "Europe/Berlin")
    monkeypatch.setenv("CRAWLY_BROWSER_VIEWPORT", "1600x900")
    manager = BrowserManager()
    opts = manager._context_options()
    assert opts["timezone_id"] == "Europe/Berlin"
    assert opts["locale"] == "de-DE"
    assert opts["viewport"] == {"width": 1600, "height": 900}
    assert opts["java_script_enabled"] is True
    assert opts["extra_http_headers"]["Accept-Language"] == "de-DE,de;q=0.9"
    assert "sec-ch-ua" in opts["extra_http_headers"]


def test_context_options_defaults_timezone_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRAWLY_BROWSER_LANG", raising=False)
    monkeypatch.delenv("CRAWLY_BROWSER_LOCATION", raising=False)
    monkeypatch.delenv("CRAWLY_BROWSER_VIEWPORT", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    manager = BrowserManager()
    opts = manager._context_options()
    assert opts["timezone_id"] == "Europe/Moscow"
    assert opts["locale"] == "ru-RU"
    assert opts["viewport"] == {"width": 1366, "height": 768}


def test_resolve_browser_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLY_BROWSER_LANG", "ru-RU")
    monkeypatch.setenv("CRAWLY_BROWSER_LOCATION", "Europe/Moscow")
    monkeypatch.setenv("CRAWLY_BROWSER_VIEWPORT", "1920x1080")

    assert resolve_browser_language() == "ru-RU"
    assert resolve_browser_location() == "Europe/Moscow"
    assert resolve_browser_viewport() == {"width": 1920, "height": 1080}


def test_resolve_browser_viewport_falls_back_on_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLY_BROWSER_VIEWPORT", "garbage")
    assert resolve_browser_viewport() == {"width": 1366, "height": 768}


def test_build_standard_headers_sets_primary_language() -> None:
    headers = build_standard_headers("ru-RU")
    assert headers["Accept-Language"] == "ru-RU,ru;q=0.9"
    assert "sec-ch-ua" in headers


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


@pytest.mark.asyncio
async def test_profile_cleanup_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("CRAWLY_PROFILE_CLEANUP_ON_START", raising=False)

    old = tmp_path / "stale"
    old.mkdir()
    os.utime(old, (time.time() - 60 * 24 * 3600, time.time() - 60 * 24 * 3600))

    manager = BrowserManager()
    await manager._cleanup_stale_profiles()  # direct call under test
    assert old.exists()  # NOT deleted, cleanup gate off


@pytest.mark.asyncio
async def test_profile_cleanup_when_enabled_deletes_old_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("CRAWLY_PROFILE_CLEANUP_ON_START", "true")
    monkeypatch.setenv("CRAWLY_PROFILE_MAX_AGE_DAYS", "14")

    old = tmp_path / "ddg-stale"
    old.mkdir()
    (old / "file").write_text("data")
    os.utime(old, (time.time() - 30 * 24 * 3600, time.time() - 30 * 24 * 3600))

    fresh = tmp_path / "ddg-fresh"
    fresh.mkdir()

    manager = BrowserManager()
    await manager._cleanup_stale_profiles()
    assert not old.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_close_closes_persistent_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    class FakeCtx:
        def __init__(self, name: str) -> None:
            self.name = name
        async def close(self) -> None:
            closed.append(self.name)

    manager = BrowserManager()
    manager._search_contexts = {"duckduckgo": FakeCtx("ddg"), "google": FakeCtx("g")}
    manager._search_guards = {"duckduckgo": object(), "google": object()}
    await manager.close()
    assert sorted(closed) == ["ddg", "g"]
    assert manager._search_contexts == {}


def test_launch_options_shared_between_paths() -> None:
    manager = BrowserManager()
    opts = manager._launch_options()
    assert "--disable-dev-shm-usage" in opts["args"]
    assert "--headless=new" in opts["args"]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", True),
        ("true", True),
        ("1", True),
        ("on", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("off", False),
        ("no", False),
        ("garbage", True),  # unknown values fall back to default
    ],
)
def test_persistent_profiles_enabled_parses_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    if value:
        monkeypatch.setenv("CRAWLY_USE_PERSISTENT_PROFILES", value)
    else:
        monkeypatch.delenv("CRAWLY_USE_PERSISTENT_PROFILES", raising=False)
    assert persistent_profiles_enabled() is expected


@pytest.mark.asyncio
async def test_search_context_returns_ephemeral_handle_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the toggle off, each call returns a distinct fresh context that
    must be closed by the caller (should_close_context=True, first_use=True)."""
    monkeypatch.setenv("CRAWLY_USE_PERSISTENT_PROFILES", "false")

    class FakeCtx:
        def __init__(self) -> None:
            self.routes: list[tuple[str, Any]] = []
            self.closed = False

        async def route(self, pattern: str, handler: Any) -> None:
            self.routes.append((pattern, handler))

        async def close(self) -> None:
            self.closed = True

    created: list[FakeCtx] = []

    async def fake_new_context(self: BrowserManager) -> FakeCtx:
        ctx = FakeCtx()
        created.append(ctx)
        return ctx

    monkeypatch.setattr(BrowserManager, "new_context", fake_new_context)

    manager = BrowserManager()
    h1 = await manager.search_context("duckduckgo")
    h2 = await manager.search_context("duckduckgo")

    assert h1.first_use is True and h2.first_use is True
    assert h1.should_close_context is True and h2.should_close_context is True
    assert h1.context is not h2.context  # no caching when ephemeral
    assert manager._search_contexts == {}  # cache untouched


@pytest.mark.asyncio
async def test_search_context_uses_persistent_path_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sanity: with the toggle on (default), should_close_context stays False."""
    monkeypatch.setenv("CRAWLY_USE_PERSISTENT_PROFILES", "true")
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("CRAWLY_PROFILE_CLEANUP_ON_START", "false")

    class FakeChromium:
        async def launch(self, **kwargs: Any) -> Any:
            return object()

        async def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> Any:
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
    handle = await manager.search_context("duckduckgo")
    assert handle.should_close_context is False
    assert handle.first_use is True
