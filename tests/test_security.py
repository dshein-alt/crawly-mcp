import ipaddress

import pytest

from crawly_mcp.errors import URLSafetyError
from crawly_mcp.security import BlockedRequest, URLSafetyGuard


@pytest.mark.asyncio
async def test_validate_user_url_rejects_non_http_scheme() -> None:
    guard = URLSafetyGuard()

    with pytest.raises(URLSafetyError, match="unsupported URL scheme"):
        await guard.validate_user_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_validate_user_url_rejects_embedded_credentials() -> None:
    guard = URLSafetyGuard()

    with pytest.raises(URLSafetyError, match="embedded credentials"):
        await guard.validate_user_url("https://user:pass@example.com/")


@pytest.mark.asyncio
async def test_validate_user_url_rejects_private_dns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = URLSafetyGuard()

    async def fake_resolve(host: str, port: int | None):
        return (ipaddress.ip_address("10.0.0.5"),)

    monkeypatch.setattr(guard, "_resolve_host", fake_resolve)

    with pytest.raises(URLSafetyError, match="non-public"):
        await guard.validate_user_url("https://internal.example/")


@pytest.mark.asyncio
async def test_validate_user_url_allows_public_dns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = URLSafetyGuard()

    async def fake_resolve(host: str, port: int | None):
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr(guard, "_resolve_host", fake_resolve)

    await guard.validate_user_url("https://example.com/")


def _fake_page() -> object:
    """Opaque sentinel used as a dict key; URLSafetyGuard only uses identity."""
    return object()


def test_pop_blocked_error_returns_page_scoped_error() -> None:
    guard = URLSafetyGuard()
    page_a = _fake_page()
    page_b = _fake_page()
    err_a = URLSafetyError("blocked_target", "A")
    err_b = URLSafetyError("blocked_target", "B")

    # Simulate what handle_route() would do internally:
    guard._blocked_requests.setdefault(page_a, []).append(BlockedRequest(url="https://a/", error=err_a))
    guard._blocked_requests.setdefault(page_b, []).append(BlockedRequest(url="https://b/", error=err_b))

    assert guard.pop_blocked_error(page_a) is err_a
    assert guard.pop_blocked_error(page_b) is err_b
    # Draining the list cleans the entry:
    assert page_a not in guard._blocked_requests
    assert page_b not in guard._blocked_requests


def test_pop_blocked_error_returns_none_for_unknown_page() -> None:
    guard = URLSafetyGuard()
    assert guard.pop_blocked_error(_fake_page()) is None


def test_close_event_cleans_up_dict_entry() -> None:
    """If a page had blocked subresources but closed without the caller
    draining pop_blocked_error, the close handler still releases the dict
    entry so long-lived contexts don't leak memory."""
    guard = URLSafetyGuard()

    close_handlers: list[object] = []

    class _Page:
        def on(self, event: str, handler: object) -> None:
            assert event == "close"
            close_handlers.append(handler)

    page = _Page()
    # Simulate what handle_route's first-seen path does:
    bucket: list[BlockedRequest] = []
    guard._blocked_requests[page] = bucket
    page.on("close", lambda p=page: guard._blocked_requests.pop(p, None))
    bucket.append(BlockedRequest(url="https://blocked/", error=URLSafetyError("blocked_target", "x")))

    assert page in guard._blocked_requests
    # Fire the close handler without anyone calling pop_blocked_error:
    close_handlers[0]()
    assert page not in guard._blocked_requests
