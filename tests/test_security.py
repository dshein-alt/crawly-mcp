import ipaddress

import pytest

from crawly_mcp.errors import URLSafetyError
from crawly_mcp.security import URLSafetyGuard


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
