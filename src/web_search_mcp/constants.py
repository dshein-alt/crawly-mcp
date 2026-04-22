SYSTEM_CHROMIUM_ENV_VAR = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"

DEFAULT_PROVIDER = "duckduckgo"
ALLOWED_PROVIDERS = ("duckduckgo", "google", "yandex")
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
}
