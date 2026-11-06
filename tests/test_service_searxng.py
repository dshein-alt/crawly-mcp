from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crawly_mcp.constants import MAX_SEARCH_PROVIDERS
from crawly_mcp.errors import (
    InvalidInputError,
    ProviderBlockedError,
    ProviderUnavailableError,
)
from crawly_mcp.models import ProviderAttempt
from crawly_mcp.searxng import SearxngOutcome
from crawly_mcp.service import ProviderHealth, WebSearchService

DDG_OK = (
    ["https://ddg.example/"],
    ProviderAttempt(provider="duckduckgo", status="ok", results=1, detail="1 result"),
)


def _make_service(monkeypatch, *, adapter=None, browser=None) -> WebSearchService:
    """Build a WebSearchService that bypasses __init__.

    The real __init__ would build a BrowserManager; these tests only exercise
    the cascade, so the SearXNG adapter and the browser attempt are stubbed and
    provider health starts empty.
    """
    svc = WebSearchService.__new__(WebSearchService)
    svc._http = MagicMock()
    svc._health = {}
    svc._browser_manager = MagicMock()
    svc._attempt_browser = AsyncMock(return_value=browser or DDG_OK)
    monkeypatch.setattr(
        "crawly_mcp.service.searxng_search",
        adapter or AsyncMock(return_value=SearxngOutcome(urls=["https://r.example/"])),
    )
    return svc


@pytest.mark.asyncio
async def test_searxng_without_env_raises_invalid_input(monkeypatch) -> None:
    """Asking for a provider this deployment cannot use is an input problem."""
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    adapter = AsyncMock()
    svc = _make_service(monkeypatch, adapter=adapter)

    with pytest.raises(InvalidInputError):
        await svc.search(provider="searxng", context="x")
    adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_without_env_reports_unavailable_in_available_providers(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    svc = _make_service(monkeypatch)

    resp = await svc.search(context="x")
    assert "searxng" not in resp.available_providers
    assert "duckduckgo" in resp.available_providers


@pytest.mark.asyncio
async def test_searxng_with_env_calls_adapter_once(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(return_value=SearxngOutcome(urls=["https://r.example/"]))
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.urls == ["https://r.example/"]
    assert resp.status == "ok"
    assert resp.provider == "searxng"
    adapter.assert_awaited_once()
    assert adapter.await_args.args[0] == "https://pinned.example/"
    svc._attempt_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_normalizes_trailing_slash(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example")  # no slash
    adapter = AsyncMock(return_value=SearxngOutcome(urls=["https://r.example/"]))
    svc = _make_service(monkeypatch, adapter=adapter)

    await svc.search(provider="searxng", context="x")
    assert adapter.await_args.args[0] == "https://pinned.example/"


@pytest.mark.asyncio
async def test_searxng_zero_results_stops_the_cascade(monkeypatch) -> None:
    """An honest empty answer is an answer: cascading on it wastes calls."""
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(return_value=SearxngOutcome())
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="obscure")
    assert resp.urls == []
    assert resp.status == "no_results"
    assert resp.next_action == "refine_query"
    assert resp.suggested_providers == []
    svc._attempt_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_searxng_upstream_error_cascades_instead_of_reporting_empty(
    monkeypatch,
) -> None:
    """HTTP 200 + an `error` field is a provider that could not answer.

    Read as an empty URL list it stopped the cascade and told the caller to
    reword the query, which is the ambiguity this whole change exists to kill.
    """
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(return_value=SearxngOutcome(error="too many requests"))
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.status == "ok"
    assert resp.urls == ["https://ddg.example/"]
    assert resp.attempted[0].provider == "searxng"
    assert resp.attempted[0].status == "unavailable"
    assert "too many requests" in resp.attempted[0].detail
    svc._attempt_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_searxng_unresponsive_engines_cascade(monkeypatch) -> None:
    """Empty results plus failed engines is an engine failure, not an empty web."""
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(
        return_value=SearxngOutcome(unresponsive_engines=("google:bot",))
    )
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.status == "ok"
    assert resp.attempted[0].status == "unavailable"
    assert "google:bot" in resp.attempted[0].detail
    assert resp.provider == "duckduckgo"


@pytest.mark.asyncio
async def test_searxng_answer_with_no_results_field_cascades(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(
        return_value=SearxngOutcome(error="response carried no 'results' field")
    )
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.attempted[0].status == "unavailable"
    svc._attempt_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_searxng_blocked_cascades_to_the_browser(monkeypatch) -> None:
    """The headline change: a refused provider no longer ends the search."""
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    adapter = AsyncMock(side_effect=ProviderBlockedError("searxng refused"))
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.status == "ok"
    assert resp.provider == "duckduckgo"
    assert resp.urls == ["https://ddg.example/"]
    assert resp.attempted[0].provider == "searxng"
    assert resp.attempted[0].status == "blocked"
    svc._attempt_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_providers_failing_is_reported_as_a_failure(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "searxng")
    adapter = AsyncMock(side_effect=ProviderUnavailableError("instance is down"))
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.urls == []
    assert resp.status == "unavailable"
    assert resp.next_action == "report_to_user"
    assert "instance is down" in resp.detail


@pytest.mark.asyncio
async def test_searxng_rejects_non_http_scheme(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "ftp://wrong.example/")
    svc = _make_service(monkeypatch)

    with pytest.raises(InvalidInputError):
        await svc.search(provider="searxng", context="x")


@pytest.mark.asyncio
async def test_searxng_accepts_plain_http(monkeypatch) -> None:
    """http:// is allowed so users can point at a localhost / private SearXNG."""
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "http://searxng.local:8080/")
    adapter = AsyncMock(return_value=SearxngOutcome(urls=["https://r.example/"]))
    svc = _make_service(monkeypatch, adapter=adapter)

    resp = await svc.search(provider="searxng", context="x")
    assert resp.urls == ["https://r.example/"]
    assert adapter.await_args.args[0] == "http://searxng.local:8080/"


@pytest.mark.asyncio
async def test_non_searxng_provider_starts_where_it_is_told(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    svc = _make_service(monkeypatch)
    monkeypatch.setattr(
        "crawly_mcp.service.searxng_search",
        AsyncMock(side_effect=AssertionError("must not be called")),
    )
    resp = await svc.search(provider="duckduckgo", context="x")
    assert resp.urls == ["https://ddg.example/"]
    assert resp.provider == "duckduckgo"
    svc._attempt_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_closes_http_client(monkeypatch) -> None:
    svc = _make_service(monkeypatch)
    client = svc._http  # capture before aclose() drops the reference
    client.aclose = AsyncMock()
    await svc.aclose()
    client.aclose.assert_awaited_once()
    assert svc._http is None


@pytest.mark.asyncio
async def test_aclose_is_idempotent(monkeypatch) -> None:
    svc = _make_service(monkeypatch)
    client = svc._http
    client.aclose = AsyncMock()
    await svc.aclose()
    # Second call must not blow up even though _http is now None.
    await svc.aclose()
    client.aclose.assert_awaited_once()
    assert svc._http is None


@pytest.mark.asyncio
async def test_http_client_is_lazily_recreated_after_aclose(monkeypatch) -> None:
    """The FastMCP streamable-http transport can call aclose() per session.
    The next search must work — _http rebuilds on demand instead of staying
    closed for the rest of the process lifetime.
    """
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    captured: list[object] = []

    async def fake_adapter(instance_url, query, *, client, timeout):
        captured.append(client)
        return SearxngOutcome(urls=["https://r.example/"])

    svc = _make_service(monkeypatch)
    # _make_service installs its own stub; swap to our capturing adapter.
    monkeypatch.setattr("crawly_mcp.service.searxng_search", fake_adapter)
    svc._http = None  # simulate post-aclose state

    resp = await svc.search(provider="searxng", context="x")
    assert resp.urls == ["https://r.example/"]
    assert svc._http is not None
    assert captured[0] is svc._http


def test_providers_report_marks_unconfigured_searxng_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    svc = _make_service(monkeypatch)
    svc._browser_manager.is_ready = True

    report = svc.providers()
    by_name = {p.provider: p for p in report.providers}
    assert report.browser_ready is True
    assert report.searxng_configured is False
    assert by_name["searxng"].available is False
    assert by_name["duckduckgo"].available is True
    assert "searxng" not in report.cascade_order


def test_providers_report_carries_observed_health(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARXNG_URL", "https://pinned.example/")
    svc = _make_service(monkeypatch)
    svc._health["google"] = ProviderHealth(
        failures=3,
        consecutive_failures=3,
        last_status="blocked",
        last_detail="consent wall",
    )

    report = svc.providers()
    google = next(p for p in report.providers if p.provider == "google")
    assert google.failures == 3
    assert google.status == "blocked"
    assert google.skipped is True
    assert "google" not in report.cascade_order or google.skipped


def test_provider_priority_is_configurable(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google,yandex,duckduckgo")
    svc = _make_service(monkeypatch)

    assert svc.provider_order() == ["google", "yandex", "duckduckgo"]
    assert svc.providers().cascade_order == ["google", "yandex", "duckduckgo"]


def test_excluded_providers_are_never_available(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_EXCLUDE", "google,yandex")
    svc = _make_service(monkeypatch)

    assert svc.available_providers() == ["duckduckgo"]
    assert svc.providers().cascade_order == ["duckduckgo"]


@pytest.mark.asyncio
async def test_requesting_an_excluded_provider_is_refused(monkeypatch) -> None:
    """Asking for a provider the operator forbade is an input problem, and the
    answer says so instead of quietly substituting another provider."""
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_EXCLUDE", "google")
    svc = _make_service(monkeypatch)

    with pytest.raises(InvalidInputError, match="excluded by"):
        await svc.search(provider="google", context="x")
    svc._attempt_browser.assert_not_awaited()


def test_exclusion_wins_over_priority(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "google,duckduckgo")
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_EXCLUDE", "google")
    svc = _make_service(monkeypatch)

    assert svc.provider_order() == ["duckduckgo"]


def test_providers_report_names_the_excluded_and_the_reason(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_EXCLUDE", "yandex")
    svc = _make_service(monkeypatch)

    report = {p.provider: p for p in svc.providers().providers}
    assert report["yandex"].excluded is True
    assert report["yandex"].available is False
    assert "CRAWLY_SEARCH_PROVIDER_EXCLUDE" in report["yandex"].detail
    assert "excluded by configuration: yandex" in svc.providers().detail


def test_max_providers_caps_the_cascade(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")
    svc = _make_service(monkeypatch)

    assert svc.max_providers() == 1
    assert svc._cascade_order("duckduckgo") == ["duckduckgo"]


@pytest.mark.asyncio
async def test_max_providers_one_disables_cascading(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "1")
    monkeypatch.setattr(
        "crawly_mcp.service.searxng_search", AsyncMock(return_value=SearxngOutcome())
    )
    svc = _make_service(
        monkeypatch,
        browser=(
            [],
            ProviderAttempt(provider="duckduckgo", status="blocked", detail="captcha"),
        ),
    )

    resp = await svc.search(provider="duckduckgo", context="x")
    assert resp.status == "blocked"
    assert len(resp.attempted) == 1
    # The cap is a budget, not a claim that nothing else exists: the response
    # names the providers a follow-up call could ask for by name.
    assert resp.next_action == "try_other_provider"
    assert resp.suggested_providers == ["google", "yandex"]
    assert "at most 1 provider" in resp.detail


def test_invalid_max_providers_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLY_SEARCH_MAX_PROVIDERS", "many")
    svc = _make_service(monkeypatch)
    assert svc.max_providers() == MAX_SEARCH_PROVIDERS


def test_unknown_provider_names_in_configuration_are_ignored(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLY_SEARXNG_URL", raising=False)
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_ORDER", "duckduckgo,askjeeves")
    monkeypatch.setenv("CRAWLY_SEARCH_PROVIDER_EXCLUDE", "altavista")
    svc = _make_service(monkeypatch)

    assert svc.provider_order() == ["duckduckgo"]
    assert svc.excluded_providers() == frozenset()
