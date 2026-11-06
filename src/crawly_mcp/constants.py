from typing import Literal, get_args

PLAYWRIGHT_BROWSER_SOURCE_ENV_VAR = "PLAYWRIGHT_BROWSER_SOURCE"
SYSTEM_CHROMIUM_ENV_VAR = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
CRAWLY_HOST_ENV_VAR = "CRAWLY_HOST"
CRAWLY_PORT_ENV_VAR = "CRAWLY_PORT"
BROWSER_LANG_ENV_VAR = "CRAWLY_BROWSER_LANG"
BROWSER_LOCATION_ENV_VAR = "CRAWLY_BROWSER_LOCATION"
BROWSER_VIEWPORT_ENV_VAR = "CRAWLY_BROWSER_VIEWPORT"
CRAWLY_TRACE_DIR_ENV_VAR = "CRAWLY_TRACE_DIR"
CRAWLY_FETCH_MAX_SIZE_ENV_VAR = "CRAWLY_FETCH_MAX_SIZE"
CRAWLY_FETCH_CONTENT_FORMAT_ENV_VAR = "CRAWLY_FETCH_CONTENT_FORMAT"
# Exact hostnames (comma-separated) allowed to resolve to non-public addresses.
# Empty by default, which means the SSRF guard refuses them all.
CRAWLY_SSRF_ALLOW_ENV_VAR = "CRAWLY_SSRF_ALLOW"

BROWSER_SOURCE_SYSTEM = "system"
BROWSER_SOURCE_BUNDLED = "bundled"
ALLOWED_BROWSER_SOURCES = (BROWSER_SOURCE_SYSTEM, BROWSER_SOURCE_BUNDLED)

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000

SearchProvider = Literal["duckduckgo", "google", "yandex", "searxng"]
ALLOWED_PROVIDERS: tuple[SearchProvider, ...] = get_args(SearchProvider)

# `auto` is what a caller gets when it has no preference: start from the
# operator's configured priority order. It is a value in the schema on purpose,
# so omitting the provider and asking for `auto` mean the same thing out loud,
# and excluding a provider from the order cannot break the default call path.
SearchProviderChoice = Literal["auto", "duckduckgo", "google", "yandex", "searxng"]
PROVIDER_AUTO: SearchProviderChoice = "auto"
DEFAULT_PROVIDER_CHOICE: SearchProviderChoice = PROVIDER_AUTO

# --- shared outcome vocabulary ---
#
# Every tool answers with the same small state machine so a caller can tell
# "the web has nothing for this" apart from "this provider could not answer",
# and knows what to do next without being told by a human.
#
#   ok                — the call produced usable data
#   no_results        — the source answered and genuinely had nothing; the
#                       query is exhausted, NOT the tool
#   blocked           — the source refused us (CAPTCHA, consent wall, bot page)
#   unavailable       — the source is unusable right now: not configured, not
#                       reachable, or its page shape is no longer parseable
#   timeout           — the source did not answer in budget
#   navigation_failed — the request could not be completed at the network layer
#   unsafe_url        — the URL was refused by the safety guard
#   invalid_input     — the call arguments are wrong; fix the arguments
ToolStatus = Literal[
    "ok",
    "no_results",
    "blocked",
    "unavailable",
    "timeout",
    "navigation_failed",
    "unsafe_url",
    "invalid_input",
]

#   none               — nothing to do, report the data
#   try_other_provider — call again; a provider that can answer exists
#   refine_query       — the source is fine, the query is the problem
#   fetch_directly     — skip this step and fetch a known URL instead
#   retry_later        — transient; the same call may work later
#   report_to_user     — the caller cannot fix this; say so accurately
NextAction = Literal[
    "none",
    "try_other_provider",
    "refine_query",
    "fetch_directly",
    "retry_later",
    "report_to_user",
]

# What kind of page a search navigation actually landed on. This is the
# evidence behind `no_results`: a page is only allowed to be called empty when
# it was positively identified as a results page that says it has nothing.
#   results      — a results page with at least one extractable result
#   zero_results — a results page that states it found nothing
#   consent      — cookie/consent wall
#   captcha      — CAPTCHA / bot-check / "unusual traffic" wall
#   shell        — page did not render (blank, JS shell, interstitial)
#   unknown      — rendered page we cannot classify
PageKind = Literal[
    "results",
    "zero_results",
    "consent",
    "captcha",
    "shell",
    "unknown",
]

# Per-tier outcome for the page_search cascade.
TierOutcome = Literal["ok", "empty", "failed", "unavailable"]

MAX_SEARCH_RESULTS = 5

# Google routes every organic title through `/goto?url=<opaque>`, so a results
# page carries more result links than the cap and each destination costs its own
# round-trip. Google answers a concurrent burst of those requests with a defer
# page, so over-ask for links, resolve them a few at a time, and retry a defer
# token once before giving up on it.
OPAQUE_RESOLVE_ATTEMPTS = 12
OPAQUE_RESOLVE_CONCURRENCY = 2
OPAQUE_RESOLVE_RETRY_DELAY_SECONDS = 3.0
# Leave time for cancellation and closing the search page before its deadline.
OPAQUE_RESOLVE_CLEANUP_SECONDS = 0.25

MAX_FETCH_URLS = 5
MAX_FETCH_SIZE = 1024 * 1024
DEFAULT_MAX_FETCH_SIZE = MAX_FETCH_SIZE
FetchContentFormat = Literal["html", "text"]
DEFAULT_FETCH_CONTENT_FORMAT: FetchContentFormat = "html"

MAX_CONCURRENT_NAVIGATIONS = 3

SEARCH_PAGE_TIMEOUT_SECONDS = 15
# Budget for one provider attempt inside a cascade.
SEARCH_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 15
# Hard ceiling across the whole cascade, so a caller never waits on a chain of
# dead providers.
SEARCH_CASCADE_TIMEOUT_SECONDS = 45
# Providers tried per search call, in cascade order. Used when
# CRAWLY_SEARCH_MAX_PROVIDERS is unset.
MAX_SEARCH_PROVIDERS = 3
# Consecutive failures after which a provider is skipped in the cascade order.
PROVIDER_FAILURE_THRESHOLD = 2
FETCH_PAGE_TIMEOUT_SECONDS = 15
FETCH_TOTAL_TIMEOUT_SECONDS = 35
CHALLENGE_SETTLE_TIMEOUT_SECONDS = 10
TRACE_CAPTURE_TIMEOUT_SECONDS = 2

# How long a search page that landed on a wall gets to finish a provider's own
# automatic browser check. Yandex's `/showcaptchafast` runs a script that
# submits a fingerprint and redirects to the results page it was aiming at, and
# the fork lands within about a second of navigation; the window is wider to
# cover the results rendering after the redirect, which is where the result
# links appear. A wall that asks a human to prove something is reported at once
# and never spends this window.
SEARCH_WALL_SETTLE_SECONDS = 3.0
# Poll spacing inside the settle window.
SEARCH_WALL_SETTLE_SAMPLE_SECONDS = 0.25
# Leave room to read the settled page and raise before the attempt deadline.
SEARCH_WALL_SETTLE_CLEANUP_SECONDS = 0.5

STANDARD_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

STANDARD_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="146", "Not)A;Brand";v="8", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

# --- browser identity / persistent-profile configuration ---

CRAWLY_PROFILE_DIR_ENV_VAR = "CRAWLY_PROFILE_DIR"
CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR = "CRAWLY_PROFILE_CLEANUP_ON_START"
CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR = "CRAWLY_PROFILE_MAX_AGE_DAYS"
CRAWLY_SEARCH_JITTER_MS_ENV_VAR = "CRAWLY_SEARCH_JITTER_MS"
CRAWLY_USE_PERSISTENT_PROFILES_ENV_VAR = "CRAWLY_USE_PERSISTENT_PROFILES"

DEFAULT_PROFILE_DIR = "~/.cache/crawly/profiles"
DEFAULT_PROFILE_MAX_AGE_DAYS = 14
DEFAULT_SEARCH_JITTER_MS: tuple[int, int] = (500, 1500)
DEFAULT_BROWSER_LANG = "ru-RU"
DEFAULT_BROWSER_LOCATION = "Europe/Moscow"
DEFAULT_BROWSER_VIEWPORT = "1366x768"
DEFAULT_TIMEZONE_ID = DEFAULT_BROWSER_LOCATION
DEFAULT_USE_PERSISTENT_PROFILES = True

WARMUP_PAGE_TIMEOUT_SECONDS = 3
SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS = 10

# Keyed by browser-driven providers only; searxng is handled separately and
# does not use the warm-up hop or homepage hint.
PROVIDER_HOMEPAGE: dict[SearchProvider, str] = {
    "duckduckgo": "https://duckduckgo.com/",
    "google": "https://www.google.com/",
    "yandex": "https://yandex.ru/",
}

# --- SearXNG provider configuration ---
#
# SearXNG is a configured provider: it is only available when CRAWLY_SEARXNG_URL
# is set to the URL of a SearXNG instance that exposes the JSON API
# (format=json). Public instances on https://searx.space generally return
# 429 / redirect / empty results to non-browser clients via their botdetection
# middleware, so this provider is intended for self-hosted or privately-known
# instances. When configured it leads the cascade order, because it is the only
# provider here that is not fighting a bot wall. `search` cascades across
# providers; see DEFAULT_SEARCH_PROVIDER_ORDER.

CRAWLY_SEARXNG_URL_ENV_VAR = "CRAWLY_SEARXNG_URL"
SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS = 8

# --- provider cascade configuration ---
#
# One `search` call walks the priority order until a provider answers, so the
# caller does not have to be told to "try google". An explicitly requested
# provider is always tried first; the rest of the order follows. Providers that
# are not configured (searxng without CRAWLY_SEARXNG_URL) are reported as
# unavailable and never entered into the cascade.

CRAWLY_SEARCH_PROVIDER_ORDER_ENV_VAR = "CRAWLY_SEARCH_PROVIDER_ORDER"

# Providers the operator forbids entirely. An excluded provider is never tried,
# not even when a caller asks for it by name, and is reported as excluded in
# the `providers` tool. Exclusion is stronger than ordering: a provider listed
# in both is excluded.
CRAWLY_SEARCH_PROVIDER_EXCLUDE_ENV_VAR = "CRAWLY_SEARCH_PROVIDER_EXCLUDE"

# How many providers one search call may try. Set to 1 to disable cascading.
CRAWLY_SEARCH_MAX_PROVIDERS_ENV_VAR = "CRAWLY_SEARCH_MAX_PROVIDERS"

DEFAULT_SEARCH_PROVIDER_ORDER: tuple[SearchProvider, ...] = (
    "searxng",
    "duckduckgo",
    "google",
    "yandex",
)

PageSearchMode = Literal["algolia", "opensearch", "readthedocs", "form", "text"]

# `source` is not a search facility: it names the page we were handed, so a
# wall on the source page can be reported without inventing a tier.
PageSearchTier = Literal[
    "source", "algolia", "opensearch", "readthedocs", "form", "text"
]
PAGE_SEARCH_TIER_TIMEOUT_SECONDS = 10
PAGE_SEARCH_SNIPPET_CONTEXT_CHARS = 240
# A wall announces itself in the title and has nothing else to say. Past this
# many characters of text, a page is content that mentions a wall, not a wall.
PAGE_SEARCH_WALL_MAX_TEXT_CHARS = 1500
