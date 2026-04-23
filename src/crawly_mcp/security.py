from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import TypeAlias
from urllib.parse import urlsplit

from loguru import logger
from patchright.async_api import BrowserContext, Page, Route

from crawly_mcp.errors import URLSafetyError

SAFE_NETWORK_SCHEMES = {"http", "https"}
SAFE_LOCAL_SCHEMES = {"about", "blob", "data"}
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(slots=True)
class BlockedRequest:
    url: str
    error: URLSafetyError


class URLSafetyGuard:
    def __init__(self) -> None:
        self._resolve_cache: dict[str, tuple[IPAddress, ...]] = {}
        self._cache_lock = asyncio.Lock()
        self._blocked_requests: dict[Page, list[BlockedRequest]] = {}

    async def attach(self, context: BrowserContext) -> None:
        await context.route("**/*", self.handle_route)

    async def validate_user_url(self, url: str) -> None:
        await self._validate(url, allow_local_schemes=False)

    async def handle_route(self, route: Route) -> None:
        request_url = route.request.url
        try:
            await self._validate(request_url, allow_local_schemes=True)
        except URLSafetyError as exc:
            logger.warning(
                "ssrf reject url={!r} reason={} message={}",
                request_url, exc.error_type, exc.message,
            )
            page = route.request.frame.page
            bucket = self._blocked_requests.get(page)
            if bucket is None:
                bucket = []
                self._blocked_requests[page] = bucket
                # Subscribe once per page so the dict entry is released when
                # the page closes, even if the caller never inspected errors.
                page.on("close", lambda p=page: self._blocked_requests.pop(p, None))
            bucket.append(BlockedRequest(url=request_url, error=exc))
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    def pop_blocked_error(self, page: Page) -> URLSafetyError | None:
        bucket = self._blocked_requests.get(page)
        if not bucket:
            return None
        error = bucket.pop(0).error
        if not bucket:
            # The `close` handler will also release the dict entry eventually.
            # Dropping it here too is safe because `pop_blocked_error` is
            # idempotent — a subsequent call returns None.
            del self._blocked_requests[page]
        return error

    async def _validate(self, url: str, *, allow_local_schemes: bool) -> None:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()

        if scheme in SAFE_LOCAL_SCHEMES and allow_local_schemes:
            return
        if scheme not in SAFE_NETWORK_SCHEMES:
            raise URLSafetyError("invalid_url", f"unsupported URL scheme: {scheme or '<missing>'}")
        if parsed.username or parsed.password:
            raise URLSafetyError("invalid_url", "URLs with embedded credentials are not allowed")
        if not parsed.hostname:
            raise URLSafetyError("invalid_url", "URL must include a hostname")

        host = parsed.hostname.rstrip(".").lower()
        if host in BLOCKED_HOSTNAMES:
            raise URLSafetyError("blocked_target", f"hostname {host!r} is not allowed")

        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            addresses = await self._resolve_host(host, parsed.port)
        else:
            addresses = (literal_ip,)

        blocked = [str(address) for address in addresses if not address.is_global]
        if blocked:
            joined = ", ".join(blocked)
            raise URLSafetyError("blocked_target", f"URL resolves to non-public address(es): {joined}")

    async def _resolve_host(
        self,
        host: str,
        port: int | None,
    ) -> tuple[IPAddress, ...]:
        async with self._cache_lock:
            cached = self._resolve_cache.get(host)
            if cached is not None:
                return cached

        try:
            info = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port or 443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise URLSafetyError("invalid_url", f"failed to resolve hostname {host!r}") from exc

        addresses: list[IPAddress] = []
        for family, _, _, _, sockaddr in info:
            raw_ip = sockaddr[0]
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            address = ipaddress.ip_address(raw_ip)
            if address not in addresses:
                addresses.append(address)

        if not addresses:
            raise URLSafetyError("invalid_url", f"hostname {host!r} did not resolve to a usable IP address")

        result = tuple(addresses)
        async with self._cache_lock:
            self._resolve_cache[host] = result
        return result
