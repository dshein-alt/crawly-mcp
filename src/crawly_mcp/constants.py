from typing import Literal, get_args

PLAYWRIGHT_BROWSER_SOURCE_ENV_VAR = "PLAYWRIGHT_BROWSER_SOURCE"
SYSTEM_CHROMIUM_ENV_VAR = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
CRAWLY_HOST_ENV_VAR = "CRAWLY_HOST"
CRAWLY_PORT_ENV_VAR = "CRAWLY_PORT"

BROWSER_SOURCE_SYSTEM = "system"
BROWSER_SOURCE_BUNDLED = "bundled"
ALLOWED_BROWSER_SOURCES = (BROWSER_SOURCE_SYSTEM, BROWSER_SOURCE_BUNDLED)

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000

SearchProvider = Literal["duckduckgo", "google", "yandex"]
ALLOWED_PROVIDERS: tuple[SearchProvider, ...] = get_args(SearchProvider)
DEFAULT_PROVIDER: SearchProvider = "duckduckgo"
MAX_SEARCH_RESULTS = 5
MAX_FETCH_URLS = 5
MAX_HTML_BYTES = 1024 * 1024

MAX_CONCURRENT_NAVIGATIONS = 3

SEARCH_PAGE_TIMEOUT_SECONDS = 15
SEARCH_TOTAL_TIMEOUT_SECONDS = 20
FETCH_PAGE_TIMEOUT_SECONDS = 15
FETCH_TOTAL_TIMEOUT_SECONDS = 35
CHALLENGE_SETTLE_TIMEOUT_SECONDS = 10

STANDARD_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

STANDARD_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="146", "Not)A;Brand";v="8", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

# --- stealth / persistent-profile configuration ---

CRAWLY_USE_XVFB_ENV_VAR = "CRAWLY_USE_XVFB"
CRAWLY_XVFB_GEOMETRY_ENV_VAR = "CRAWLY_XVFB_GEOMETRY"
CRAWLY_PROFILE_DIR_ENV_VAR = "CRAWLY_PROFILE_DIR"
CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR = "CRAWLY_PROFILE_CLEANUP_ON_START"
CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR = "CRAWLY_PROFILE_MAX_AGE_DAYS"
CRAWLY_SEARCH_JITTER_MS_ENV_VAR = "CRAWLY_SEARCH_JITTER_MS"

DEFAULT_XVFB_GEOMETRY = "1280x720x24"
DEFAULT_PROFILE_DIR = "~/.cache/crawly/profiles"
DEFAULT_PROFILE_MAX_AGE_DAYS = 14
DEFAULT_SEARCH_JITTER_MS: tuple[int, int] = (500, 1500)
DEFAULT_TIMEZONE_ID = "America/New_York"

WARMUP_PAGE_TIMEOUT_SECONDS = 3
SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS = 10

PROVIDER_HOMEPAGE: dict[SearchProvider, str] = {
    "duckduckgo": "https://duckduckgo.com/",
    "google": "https://www.google.com/",
    "yandex": "https://yandex.ru/",
}
