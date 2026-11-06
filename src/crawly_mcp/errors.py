from __future__ import annotations

from typing import Any

from mcp import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from crawly_mcp.constants import NextAction, ToolStatus

# Every failure names the status it maps to and the move it implies, so the
# caller is told what to do rather than left to infer it from a message string.
STATUS_BY_TYPE: dict[str, ToolStatus] = {
    "invalid_input": "invalid_input",
    "invalid_url": "invalid_input",
    "blocked_target": "unsafe_url",
    "unsafe_url": "unsafe_url",
    "browser_unavailable": "unavailable",
    "provider_unavailable": "unavailable",
    "provider_blocked": "blocked",
    "challenge_blocked": "blocked",
    "timeout": "timeout",
    "navigation_failed": "navigation_failed",
}

NEXT_ACTION_BY_TYPE: dict[str, NextAction] = {
    "invalid_input": "report_to_user",
    "invalid_url": "report_to_user",
    "blocked_target": "report_to_user",
    "unsafe_url": "report_to_user",
    "browser_unavailable": "retry_later",
    "provider_unavailable": "try_other_provider",
    "provider_blocked": "try_other_provider",
    "challenge_blocked": "try_other_provider",
    "timeout": "retry_later",
    "navigation_failed": "retry_later",
}

# "Try another provider" is search advice. Fetch has no provider parameter and
# no provider suggestions to offer, so a wall there leaves the caller exactly
# one move: come back later. Telling a fetch caller to do something it cannot
# do is worse than telling it nothing.
FETCH_NEXT_ACTION_OVERRIDES: dict[str, NextAction] = {
    "provider_unavailable": "retry_later",
    "provider_blocked": "retry_later",
    "challenge_blocked": "retry_later",
}


def next_action_for(error_type: str, *, tool: str = "search") -> NextAction:
    """The move this failure implies, for the tool that hit it."""
    if tool == "fetch":
        override = FETCH_NEXT_ACTION_OVERRIDES.get(error_type)
        if override is not None:
            return override
    return NEXT_ACTION_BY_TYPE.get(error_type, "report_to_user")


class WebSearchError(Exception):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        code: int = INTERNAL_ERROR,
        data: dict[str, Any] | None = None,
        page_kind: str | None = None,
        landed_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.code = code
        self.data = data or {}
        # What kind of page the provider served, when the failure came from
        # inspecting one. It survives into the cascade's per-provider report.
        self.page_kind = page_kind
        # The URL the browser ended up on. A wall is only evidence about the
        # address it was seen at, and the per-provider report is the caller's
        # only record of the attempt — the browser is gone by the time it reads
        # it. Losing this is losing the proof.
        self.landed_url = landed_url

    @property
    def status(self) -> ToolStatus:
        return STATUS_BY_TYPE.get(self.error_type, "unavailable")

    @property
    def next_action(self) -> NextAction:
        return NEXT_ACTION_BY_TYPE.get(self.error_type, "report_to_user")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "type": self.error_type,
            "status": self.status,
            "next_action": self.next_action,
            "message": self.message,
        }
        if self.data:
            payload["data"] = self.data
        return payload

    def to_mcp_error(self) -> McpError:
        payload = {"type": self.error_type}
        if self.data:
            payload.update(self.data)
        return McpError(ErrorData(code=self.code, message=self.message, data=payload))


class InvalidInputError(WebSearchError):
    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__("invalid_input", message, code=INVALID_PARAMS, data=data)


class BrowserUnavailableError(WebSearchError):
    def __init__(self, message: str) -> None:
        super().__init__("browser_unavailable", message)


class ProviderUnavailableError(WebSearchError):
    """The provider answered, but not with anything usable.

    Covers the states that used to be reported as "no results": an
    interstitial that never reached a results view, and a results page whose
    result links no longer carry extractable URLs.
    """

    def __init__(
        self,
        message: str,
        *,
        page_kind: str | None = None,
        landed_url: str | None = None,
    ) -> None:
        super().__init__(
            "provider_unavailable",
            message,
            page_kind=page_kind,
            landed_url=landed_url,
        )


class TimeoutExceededError(WebSearchError):
    def __init__(self, message: str, *, landed_url: str | None = None) -> None:
        super().__init__("timeout", message, landed_url=landed_url)


class ProviderBlockedError(WebSearchError):
    def __init__(
        self,
        message: str,
        *,
        page_kind: str | None = None,
        landed_url: str | None = None,
    ) -> None:
        super().__init__(
            "provider_blocked", message, page_kind=page_kind, landed_url=landed_url
        )


class ChallengeBlockedError(WebSearchError):
    def __init__(self, message: str) -> None:
        super().__init__("challenge_blocked", message)


class NavigationFailedError(WebSearchError):
    def __init__(self, message: str, *, landed_url: str | None = None) -> None:
        super().__init__("navigation_failed", message, landed_url=landed_url)


class URLSafetyError(WebSearchError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(error_type, message, code=INVALID_PARAMS)
