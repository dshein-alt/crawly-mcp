"""Ask a provider's wall whether it was ever going to answer.

Yandex serves two walls, and only one of them is a refusal. `/showcaptchafast`
runs a script that submits a browser fingerprint to `/checkcaptchafast`, which
redirects to the results page named in `retpath`; it lands on the results,
or on `/showcaptcha`, the SmartCaptcha
checkbox that asks a human to click and does not move. Reading the page once
reports `blocked` on a page that was about to answer.

This probe navigates the real search URL through the production browser manager
and samples the page at fixed offsets, so the t=0 sample is what a single
reading would have classified and the later samples are what a settle window
sees. Each run is its own control: Yandex's willingness varies over time, so
comparing runs made at different moments measures the weather, not the setting.

    PLAYWRIGHT_BROWSER_SOURCE=bundled \\
    CRAWLY_PROFILE_DIR=/tmp/crawly-yandex-probe \\
    uv run python scripts/yandex_wall_probe.py python 'документация Python'

One fresh profile per run reproduces the cold-profile condition, where the
automatic check is served; a warm profile usually lands on results directly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import PROVIDER_HOMEPAGE
from crawly_mcp.parsing import build_search_url, classify_search_page

DEFAULT_OFFSETS = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0)


def wall_of(url: str) -> str:
    """Name what the browser stands on, by address alone."""
    path = urlsplit(url).path
    if "showcaptchafast" in path:
        return "auto-check"
    if "showcaptcha" in path:
        return "smartcaptcha"
    if "/search" in path:
        return "results"
    return path or "/"


async def _read(page: Any) -> tuple[str, str]:
    """Read the page, tolerating a navigation that destroys it mid-sample."""
    with contextlib.suppress(Exception):
        return await page.title(), await page.content()
    return "", ""


async def _sample(
    page: Any, query: str, *, started: float, scheduled_offset: float
) -> dict[str, Any]:
    title, html = await _read(page)
    evidence = classify_search_page("yandex", page.url, title, html, query=query)
    return {
        "t": time.monotonic() - started,
        "scheduled_offset": scheduled_offset,
        "wall": wall_of(page.url),
        "kind": evidence.kind,
        "anchors": evidence.result_anchors + evidence.opaque_anchors,
        "mid_flight": evidence.mid_flight,
    }


async def _trajectory(
    manager: BrowserManager,
    query: str,
    *,
    warmup: bool,
    offsets: tuple[float, ...],
) -> list[dict[str, Any]]:
    """Land on the search page and record where it goes over `offsets`."""
    handle = await manager.search_context("yandex")
    page = await handle.context.new_page()
    try:
        if warmup:
            with contextlib.suppress(Exception):
                await manager.goto(page, PROVIDER_HOMEPAGE["yandex"], timeout_ms=8000)
        await asyncio.sleep(0.6)
        with contextlib.suppress(Exception):
            await manager.goto(
                page, build_search_url("yandex", query), timeout_ms=15000
            )
        started = time.monotonic()
        samples: list[dict[str, Any]] = []
        for offset in offsets:
            target = started + offset
            while (remaining := target - time.monotonic()) > 0:
                await asyncio.sleep(min(remaining, 0.05))
            samples.append(
                await _sample(page, query, started=started, scheduled_offset=offset)
            )
        return samples
    finally:
        with contextlib.suppress(Exception):
            await page.close()
        if handle.should_close_context:
            with contextlib.suppress(Exception):
                await handle.context.close()


def _report(query: str, samples: list[dict[str, Any]]) -> None:
    first = samples[0]
    landed = next(
        (s["t"] for s in samples if s["kind"] == "results" and s["anchors"]), None
    )
    verdict = "ANSWERED" if landed is not None else "STUCK"
    print(
        f"  {query[:22]:24} t0={first['wall']:12} -> {verdict:8} resolved_at={landed}"
    )
    print(
        "      "
        + "  ".join(
            f"{s['t']:.3f}:{s['wall']}/{s['kind']}/{s['anchors']}" for s in samples
        )
    )


def _summarize(runs: list[list[dict[str, Any]]]) -> None:
    walls = [r for r in runs if r and r[0]["wall"] in ("auto-check", "smartcaptcha")]
    print(f"\n{len(runs)} runs, {len(walls)} walls at the first reading")
    for kind in ("auto-check", "smartcaptcha"):
        subset = [r for r in walls if r[0]["wall"] == kind]
        if not subset:
            continue
        resolved = [
            next((s["t"] for s in r if s["kind"] == "results" and s["anchors"]), None)
            for r in subset
        ]
        times = sorted(t for t in resolved if t is not None)
        print(
            f"  {kind:12} n={len(subset):3}  reached results={len(times):3}"
            f"  times={times}"
        )


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("queries", nargs="*", default=["python"])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--fresh-profile",
        action="store_true",
        help="one profile per run, the cold condition that draws the check",
    )
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument(
        "--offsets",
        type=float,
        nargs="+",
        default=list(DEFAULT_OFFSETS),
        help="seconds after navigation at which to read the page",
    )
    args = parser.parse_args(argv)
    offsets = tuple(args.offsets)

    runs: list[list[dict[str, Any]]] = []
    for run in range(args.runs):
        for query in args.queries:
            if args.fresh_profile:
                profile = Path(f"/tmp/crawly-yandex-probe-{run}")  # noqa: S108
                shutil.rmtree(profile, ignore_errors=True)
                os.environ["CRAWLY_PROFILE_DIR"] = str(profile)
            manager = BrowserManager()
            try:
                samples = await _trajectory(
                    manager, query, warmup=not args.no_warmup, offsets=offsets
                )
            finally:
                with contextlib.suppress(Exception):
                    await manager.close()
            if samples:
                runs.append(samples)
                _report(query, samples)
        await asyncio.sleep(1.0)

    _summarize(runs)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
