# tests/test_constants.py
from crawly_mcp import constants


def test_new_stealth_constants_exported() -> None:
    assert constants.WARMUP_PAGE_TIMEOUT_SECONDS == 3
    assert constants.SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS == 10
    assert constants.CRAWLY_USE_XVFB_ENV_VAR == "CRAWLY_USE_XVFB"
    assert constants.CRAWLY_XVFB_GEOMETRY_ENV_VAR == "CRAWLY_XVFB_GEOMETRY"
    assert constants.CRAWLY_PROFILE_DIR_ENV_VAR == "CRAWLY_PROFILE_DIR"
    assert constants.CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR == "CRAWLY_PROFILE_CLEANUP_ON_START"
    assert constants.CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR == "CRAWLY_PROFILE_MAX_AGE_DAYS"
    assert constants.CRAWLY_SEARCH_JITTER_MS_ENV_VAR == "CRAWLY_SEARCH_JITTER_MS"
    assert constants.DEFAULT_XVFB_GEOMETRY == "1280x720x24"
    assert constants.DEFAULT_PROFILE_DIR == "~/.cache/crawly/profiles"
    assert constants.DEFAULT_PROFILE_MAX_AGE_DAYS == 14
    assert constants.DEFAULT_SEARCH_JITTER_MS == (500, 1500)
    assert constants.DEFAULT_TIMEZONE_ID == "America/New_York"


def test_client_hint_headers_present() -> None:
    assert "sec-ch-ua" in constants.STANDARD_HEADERS
    assert "sec-ch-ua-mobile" in constants.STANDARD_HEADERS
    assert "sec-ch-ua-platform" in constants.STANDARD_HEADERS
    assert '"Linux"' in constants.STANDARD_HEADERS["sec-ch-ua-platform"]


def test_provider_homepages_present() -> None:
    assert constants.PROVIDER_HOMEPAGE["duckduckgo"] == "https://duckduckgo.com/"
    assert constants.PROVIDER_HOMEPAGE["google"] == "https://www.google.com/"
    assert constants.PROVIDER_HOMEPAGE["yandex"] == "https://yandex.ru/"
