#!/usr/bin/env python3
"""Inline fingerprint canary for the crawly browser stack.

Exits 0 if the browser's JS-visible fingerprint looks like a real Chrome;
non-zero on the first failing check.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from crawly_mcp.browser import BrowserManager


@dataclass
class Check:
    name: str
    js: str
    predicate: str  # JS expression evaluated server-side; must return bool


CHECKS: list[Check] = [
    Check(
        "navigator.webdriver", "navigator.webdriver", "navigator.webdriver === false"
    ),
    Check(
        "navigator.plugins.length",
        "navigator.plugins.length",
        "navigator.plugins.length > 0",
    ),
    Check(
        "navigator.languages.length",
        "navigator.languages.length",
        "navigator.languages.length > 0",
    ),
    Check(
        "window.chrome", "typeof window.chrome", "typeof window.chrome !== 'undefined'"
    ),
    Check(
        "WebGL renderer",
        """(() => {
            const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
            return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'no-webgl';
        })()""",
        """(() => {
            const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
            if (!ext) return false;
            const r = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
            return typeof r === 'string' && r.length > 0 && r !== 'no-webgl';
        })()""",
    ),
    Check(
        "permissions vs Notification",
        """(async () => {
            const p = await navigator.permissions.query({name:'notifications'});
            return `state=${p.state} notif=${Notification.permission}`;
        })()""",
        """(async () => {
            const p = await navigator.permissions.query({name:'notifications'});
            return !(p.state === 'denied' && Notification.permission === 'default');
        })()""",
    ),
]


async def run(verbose: bool) -> int:
    manager = BrowserManager()
    await manager.start()
    try:
        context = await manager.new_context()
        page = await context.new_page()
        await page.goto("about:blank")

        failures = 0
        for check in CHECKS:
            value = await page.evaluate(check.js)
            ok = await page.evaluate(check.predicate)
            status = "PASS" if ok else "FAIL"
            if verbose or not ok:
                print(f"{check.name:40s} {status:5s} {value!r}")
            if not ok:
                failures += 1
        await context.close()
        return 0 if failures == 0 else 1
    finally:
        await manager.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args.verbose))


if __name__ == "__main__":
    sys.exit(main())
