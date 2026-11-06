from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crawly_mcp.constants import (
    DEFAULT_FETCH_CONTENT_FORMAT,
    MAX_FETCH_URLS,
    PROVIDER_AUTO,
    FetchContentFormat,
    NextAction,
    PageKind,
    PageSearchMode,
    PageSearchTier,
    SearchProvider,
    TierOutcome,
    ToolStatus,
)


class ToolOutcome(BaseModel):
    """Fields every tool result carries.

    `status` names what happened, `next_action` names what to do about it, and
    `detail` says why in one sentence. A caller that reads these three never
    has to guess whether an empty result means "the web has nothing" or "this
    provider could not answer", and never needs a human to say "try google".
    """

    status: ToolStatus
    next_action: NextAction
    detail: str = ""


class SearchRequest(BaseModel):
    # None means "no preference": start from the operator's priority order.
    # It used to default to a named provider, which made an omitted provider an
    # explicit request for that provider, so excluding it broke the default
    # call path with an input error.
    provider: SearchProvider | None = None
    context: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str):
            name = value.strip().lower()
            if name == PROVIDER_AUTO:
                return None
            return name
        return value

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("context must be a non-empty search query")
        return trimmed


class ProviderAttempt(BaseModel):
    """One provider's turn at a search, reported whether it answered or not."""

    provider: SearchProvider
    status: ToolStatus
    page_kind: PageKind | None = None
    final_url: str | None = None
    results: int = 0
    detail: str = ""


class SearchResponse(ToolOutcome):
    # `urls` stays the top-level key it has always been.
    urls: list[str] = Field(default_factory=list)
    query: str = ""
    # The provider that produced `urls`; None when none of them did.
    provider: SearchProvider | None = None
    attempted: list[ProviderAttempt] = Field(default_factory=list)
    available_providers: list[SearchProvider] = Field(default_factory=list)
    # Providers worth calling again, in the order to try them. Empty means
    # there is no point calling again: the query is the problem, not the tool.
    suggested_providers: list[SearchProvider] = Field(default_factory=list)


class FetchRequest(BaseModel):
    urls: list[str]
    content_format: FetchContentFormat = DEFAULT_FETCH_CONTENT_FORMAT

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_urls(self) -> Self:
        if not self.urls:
            raise ValueError("urls must contain at least one URL")
        if len(self.urls) > MAX_FETCH_URLS:
            raise ValueError(f"urls accepts at most {MAX_FETCH_URLS} URLs")
        for url in self.urls:
            if not isinstance(url, str) or not url.strip():
                raise ValueError("urls must contain non-empty URL strings")
        return self


class FetchError(BaseModel):
    type: str
    message: str
    next_action: NextAction = "none"


class FetchResponse(ToolOutcome):
    content_format: FetchContentFormat = DEFAULT_FETCH_CONTENT_FORMAT
    pages: dict[str, str] = Field(default_factory=dict)
    errors: dict[str, FetchError] = Field(default_factory=dict)
    truncated: list[str] = Field(default_factory=list)
    succeeded: int = 0
    failed: int = 0


class PageSearchRequest(BaseModel):
    url: str
    query: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("url")
    @classmethod
    def _url_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url must be a non-empty string")
        return value

    @field_validator("query")
    @classmethod
    def _query_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must be a non-empty string")
        return value


class PageSearchResult(BaseModel):
    snippet: str
    url: str | None = None
    title: str | None = None


class TierAttempt(BaseModel):
    """One cascade tier, with the reason it did not produce results.

    `unavailable` means the site exposes no facility of that kind, `failed`
    means it has one and the call broke, `empty` means it answered with
    nothing. Collapsing those three is what made a broken site-search look
    like a site with nothing on it.
    """

    tier: PageSearchTier
    outcome: TierOutcome
    detail: str = ""


class PageSearchResponse(ToolOutcome):
    mode: PageSearchMode
    attempted: list[TierAttempt] = Field(default_factory=list)
    source_url: str
    results_url: str | None = None
    results: list[PageSearchResult] = Field(default_factory=list)
    truncated: bool = False


class ProviderStatus(BaseModel):
    provider: SearchProvider
    available: bool
    configured: bool
    # Last outcome observed for this provider in this process, if any.
    status: ToolStatus | None = None
    detail: str = ""
    successes: int = 0
    failures: int = 0
    skipped: bool = False
    # Excluded by CRAWLY_SEARCH_PROVIDER_EXCLUDE: never tried, even on request.
    excluded: bool = False


class ProvidersResponse(BaseModel):
    """What this deployment can actually search with, before anyone calls it."""

    browser_ready: bool
    searxng_configured: bool
    cascade_order: list[SearchProvider] = Field(default_factory=list)
    max_providers: int = 0
    providers: list[ProviderStatus] = Field(default_factory=list)
    detail: str = ""
