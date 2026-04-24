from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crawly_mcp.constants import (
    DEFAULT_FETCH_CONTENT_FORMAT,
    DEFAULT_PROVIDER,
    MAX_FETCH_URLS,
    FetchContentFormat,
    PageSearchMode,
    SearchProvider,
)


class SearchRequest(BaseModel):
    provider: SearchProvider | None = DEFAULT_PROVIDER
    context: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("provider", mode="before")
    @classmethod
    def default_provider(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return DEFAULT_PROVIDER
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("context must be a non-empty search query")
        return trimmed


class SearchResponse(BaseModel):
    urls: list[str]


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


class FetchResponse(BaseModel):
    content_format: FetchContentFormat = DEFAULT_FETCH_CONTENT_FORMAT
    pages: dict[str, str] = Field(default_factory=dict)
    errors: dict[str, FetchError] = Field(default_factory=dict)
    truncated: list[str] = Field(default_factory=list)


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


class PageSearchResponse(BaseModel):
    mode: PageSearchMode
    attempted: list[PageSearchMode]
    source_url: str
    results_url: str | None = None
    results: list[PageSearchResult] = Field(default_factory=list)
    truncated: bool = False
