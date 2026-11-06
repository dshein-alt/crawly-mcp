"""Behavioural end-to-end suite for the local docker compose stack.

Brings up SearXNG + crawly-mcp + a fixture site, then asserts the contract a
caller actually sees over MCP:

  * an empty result set is distinguishable from a provider that could not answer
  * a refused URL costs that URL only, not the batch
  * a site-search page yields the site's ranked links, not its navigation

Deterministic by construction: the fixture site and the pinned-dead SearXNG
make blocked / empty / no-survivor states reproducible. Live DuckDuckGo and
Google probes are opt-in with --live and never fail the suite, because they
depend on what those providers are doing to this network today.

Usage:
    uv run python scripts/e2e_stack.py [--live] [--keep] [--skip-build]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

COMPOSE_DIR = Path(__file__).resolve().parent.parent / "examples" / "searxng-compose"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.test.yml"]
GOOD_ENDPOINT = "http://127.0.0.1:10000/mcp"
BROKEN_ENDPOINT = "http://127.0.0.1:10001/mcp"
FIXTURE = "http://fixture-site"
GIBBERISH = "zzqxvplm qkfwjzb tmvqxoze"


@dataclass(slots=True)
class Check:
    name: str
    endpoint: str
    run: Callable[[ClientSession], Any]


class CheckError(Exception):
    pass


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


async def _payload(session: ClientSession, tool: str, args: dict) -> dict:
    result = await session.call_tool(tool, args)
    if result.isError:
        text = " ".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        raise CheckError(f"{tool} returned a tool error: {text[:300]}")
    payload = getattr(result, "structuredContent", None)
    if not isinstance(payload, dict):
        raise CheckError(f"{tool} returned no structured content")
    return payload


# --- checks -----------------------------------------------------------------


async def check_tool_surface(session: ClientSession) -> None:
    tools = {tool.name for tool in (await session.list_tools()).tools}
    _expect(
        tools == {"search", "fetch", "page_search", "providers"},
        f"unexpected tool surface: {sorted(tools)}",
    )


async def check_providers_report(session: ClientSession) -> None:
    report = await _payload(session, "providers", {})
    _expect(report["searxng_configured"] is True, "searxng should be configured here")
    _expect(report["browser_ready"] is True, "browser should be ready")
    _expect(
        report["cascade_order"] and report["cascade_order"][0] == "searxng",
        f"searxng should lead the cascade: {report['cascade_order']}",
    )
    by_name = {entry["provider"]: entry for entry in report["providers"]}
    _expect("searxng" in by_name, "providers report is missing searxng")


async def check_searxng_returns_results(session: ClientSession) -> None:
    payload = await _payload(
        session, "search", {"provider": "searxng", "context": "python programming"}
    )
    _expect(payload["status"] == "ok", f"expected ok, got {payload['status']}")
    _expect(payload["provider"] == "searxng", f"winner should be searxng: {payload}")
    _expect(payload["urls"], "searxng returned no URLs for a common query")
    _expect(
        payload["attempted"][0]["provider"] == "searxng",
        "the requested provider must be tried first",
    )


async def check_blank_query_is_refused_as_input(session: ClientSession) -> None:
    """A blank query is a bad argument, so it is a tool error, not a result."""
    result = await session.call_tool("search", {"provider": "searxng", "context": " "})
    _expect(bool(result.isError), f"blank query should be refused: {result}")


async def check_fetch_isolates_a_refused_url(session: ClientSession) -> None:
    payload = await _payload(
        session,
        "fetch",
        {
            "urls": [f"{FIXTURE}/index.html", "http://127.0.0.1:9/"],
            "content_format": "text",
        },
    )
    _expect(
        f"{FIXTURE}/index.html" in payload["pages"],
        f"a refused URL must not cost the batch: {sorted(payload['pages'])}",
    )
    _expect(
        "http://127.0.0.1:9/" in payload["errors"],
        "the refused URL must be listed in errors",
    )
    error = payload["errors"]["http://127.0.0.1:9/"]
    _expect(error["type"] == "blocked_target", f"unexpected error type: {error}")
    _expect(error["next_action"] == "report_to_user", f"unexpected action: {error}")
    _expect(payload["status"] == "ok", f"partial success is ok: {payload['status']}")
    _expect(payload["succeeded"] == 1 and payload["failed"] == 1, f"counts: {payload}")


async def check_fetch_all_refused_is_a_failure(session: ClientSession) -> None:
    payload = await _payload(session, "fetch", {"urls": ["http://127.0.0.1:9/"]})
    _expect(payload["pages"] == {}, "nothing should have been fetched")
    _expect(
        payload["status"] == "unsafe_url",
        f"expected unsafe_url, got {payload['status']}",
    )
    _expect(payload["next_action"] == "report_to_user", f"action: {payload}")


async def check_fetch_challenge_is_a_block(session: ClientSession) -> None:
    payload = await _payload(session, "fetch", {"urls": [f"{FIXTURE}/captcha.html"]})
    _expect(payload["pages"] == {}, f"challenge page must not be returned: {payload}")
    _expect(
        payload["status"] == "blocked",
        f"a challenge wall is a block, got {payload['status']}",
    )


async def check_page_search_uses_the_results_container(session: ClientSession) -> None:
    payload = await _payload(
        session,
        "page_search",
        {"url": f"{FIXTURE}/index.html", "query": "install chromium browser"},
    )
    _expect(payload["status"] == "ok", f"expected ok, got {payload['status']}")
    urls = [entry.get("url") for entry in payload["results"]]
    _expect(
        any(url and url.endswith("/docs/install.html") for url in urls),
        f"the site's own ranked link is missing: {urls}",
    )
    _expect(
        not any(url and "community.html" in url for url in urls),
        f"navigation links must not be harvested as results: {urls}",
    )
    outcomes = {entry["tier"]: entry["outcome"] for entry in payload["attempted"]}
    _expect("form" in outcomes, f"form tier missing from attempts: {outcomes}")


async def check_page_search_empty_is_not_absent(session: ClientSession) -> None:
    payload = await _payload(
        session,
        "page_search",
        {"url": f"{FIXTURE}/zero.html", "query": GIBBERISH},
    )
    _expect(payload["results"] == [], f"expected no results: {payload}")
    _expect(
        payload["status"] == "no_results",
        f"expected no_results, got {payload['status']}",
    )
    _expect(payload["next_action"] == "refine_query", f"action: {payload}")


async def check_no_provider_answers(session: ClientSession) -> None:
    """A deployment whose only provider is unreachable must say so plainly.

    Called with no provider named, which must mean "use the configured priority
    order" (here: the one dead SearXNG), not "use duckduckgo".
    """
    payload = await _payload(session, "search", {"context": "python programming"})
    _expect(payload["urls"] == [], f"nothing should have answered: {payload}")
    _expect(
        payload["status"] in {"unavailable", "timeout", "navigation_failed"},
        f"expected an unavailable-family status, got {payload['status']}",
    )
    _expect(
        payload["next_action"] == "report_to_user",
        f"with no providers left, the caller must be told: {payload['next_action']}",
    )
    _expect(payload["attempted"], "every attempted provider must be reported")
    _expect(
        [a["provider"] for a in payload["attempted"]] == ["searxng"],
        f"only the configured provider should be tried: {payload['attempted']}",
    )


async def check_auto_uses_the_configured_order(session: ClientSession) -> None:
    """No provider named must mean the configured order, not a hidden default."""
    payload = await _payload(session, "search", {"context": "python programming"})
    _expect(
        payload["attempted"][0]["provider"] == "searxng",
        f"searxng leads the configured order: {payload['attempted']}",
    )
    _expect(payload["urls"], "the configured order should answer a common query")


CHECKS: list[Check] = [
    Check("tool surface advertises four tools", GOOD_ENDPOINT, check_tool_surface),
    Check("providers reports availability", GOOD_ENDPOINT, check_providers_report),
    Check(
        "searxng answers a common query", GOOD_ENDPOINT, check_searxng_returns_results
    ),
    Check(
        "blank query is refused as input",
        GOOD_ENDPOINT,
        check_blank_query_is_refused_as_input,
    ),
    Check(
        "fetch isolates a refused URL",
        GOOD_ENDPOINT,
        check_fetch_isolates_a_refused_url,
    ),
    Check(
        "fetch reports an all-refused batch",
        GOOD_ENDPOINT,
        check_fetch_all_refused_is_a_failure,
    ),
    Check(
        "fetch reports a challenge wall",
        GOOD_ENDPOINT,
        check_fetch_challenge_is_a_block,
    ),
    Check(
        "page_search harvests the results container, not the nav bar",
        GOOD_ENDPOINT,
        check_page_search_uses_the_results_container,
    ),
    Check(
        "page_search empty is reported as empty",
        GOOD_ENDPOINT,
        check_page_search_empty_is_not_absent,
    ),
    Check(
        "auto uses the configured priority order",
        GOOD_ENDPOINT,
        check_auto_uses_the_configured_order,
    ),
    Check(
        "no provider answering is a failure", BROKEN_ENDPOINT, check_no_provider_answers
    ),
]


# --- live probes (reported, never asserted) ---------------------------------


async def probe_live_provider(endpoint: str, provider: str) -> str:
    async with (
        streamable_http_client(endpoint) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        payload = await _payload(
            session, "search", {"provider": provider, "context": "python"}
        )
        return (
            f"{provider}: status={payload['status']} "
            f"provider_used={payload.get('provider')} "
            f"urls={len(payload['urls'])} "
            f"attempted={[a['provider'] + ':' + a['status'] for a in payload['attempted']]}"
        )


# --- stack lifecycle --------------------------------------------------------


def _compose(*args: str, check: bool = True, capture: bool = False) -> int:
    """Run `docker compose` against the test overlay; return its exit code."""
    # Fixed argv: the docker compose CLI, no shell, no interpolated input.
    cmd = ["docker", "compose"]
    for name in COMPOSE_FILES:
        cmd += ["-f", name]
    cmd += list(args)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no interpolation
        cmd,
        cwd=COMPOSE_DIR,
        check=check,
        capture_output=capture,
        text=True,
    )
    return result.returncode


def _build() -> None:
    """Build the stack, without pulling base layers we do not need to refresh.

    `--pull=false` keeps the build offline-friendly and docker compose accepts
    it; podman-compose's docker shim rejects the flag and exits before the
    stack ever starts. A rejected flag is not a build failure, so fall back to
    a plain build rather than telling the operator the stack is broken.
    """
    if _compose("build", "--pull=false", check=False) == 0:
        return
    print("compose rejected `build --pull=false`; retrying with a plain build")
    _compose("build")


async def _wait_ready(endpoint: str, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            async with (
                streamable_http_client(endpoint) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                return
        except Exception:  # poll through any startup failure
            await asyncio.sleep(2)
    raise RuntimeError(f"endpoint {endpoint} never became ready")


async def _run_checks() -> list[tuple[Check, str]]:
    """Run every check, keeping one MCP session per endpoint.

    A failing check is recorded, not raised, so one broken expectation reports
    itself and the rest of the suite still runs.
    """
    results: list[tuple[Check, str]] = []
    sessions: dict[str, ClientSession] = {}
    async with contextlib.AsyncExitStack() as stack:
        for check in CHECKS:
            if check.endpoint not in sessions:
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(check.endpoint)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions[check.endpoint] = session
            try:
                await check.run(sessions[check.endpoint])
            except Exception as exc:  # one check must not abort the suite
                results.append((check, f"FAIL: {type(exc).__name__}: {exc}"))
            else:
                results.append((check, "PASS"))
    return results


async def _run_live_probes(endpoint: str) -> None:
    print("\nLive provider probes (reported, not asserted):")
    for provider in ("duckduckgo", "google", "yandex"):
        try:
            print(f"  {await probe_live_provider(endpoint, provider)}")
        except Exception as exc:
            print(f"  {provider}: probe failed: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also probe live providers")
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if not args.skip_build:
        print("Building the stack from this checkout...")
        _build()
    print("Starting the stack...")
    _compose("up", "-d")
    exit_code = 1
    try:
        asyncio.run(_wait_ready(GOOD_ENDPOINT))
        asyncio.run(_wait_ready(BROKEN_ENDPOINT))
        results = asyncio.run(_run_checks())
        print("\nBehavioural checks:")
        for check, outcome in results:
            print(f"  [{'PASS' if outcome == 'PASS' else 'FAIL'}] {check.name}")
            if outcome != "PASS":
                print(f"         {outcome}")
        failed = sum(1 for _, outcome in results if outcome != "PASS")
        print(f"\n{len(results) - failed}/{len(results)} checks passed")
        exit_code = 1 if failed else 0
        if args.live and exit_code == 0:
            asyncio.run(_run_live_probes(GOOD_ENDPOINT))
    finally:
        if not args.keep:
            print("Stopping the stack...")
            with contextlib.suppress(subprocess.CalledProcessError):
                _compose("down", "-v")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
