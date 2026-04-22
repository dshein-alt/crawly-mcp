from __future__ import annotations

from typing import Any

from mcp import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData


class WebSearchError(Exception):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        code: int = INTERNAL_ERROR,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.code = code
        self.data = data or {}

    def to_payload(self) -> dict[str, Any]:
        payload = {"type": self.error_type, "message": self.message}
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


class TimeoutExceededError(WebSearchError):
    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


class ProviderBlockedError(WebSearchError):
    def __init__(self, message: str) -> None:
        super().__init__("provider_blocked", message)


class ChallengeBlockedError(WebSearchError):
    def __init__(self, message: str) -> None:
        super().__init__("challenge_blocked", message)


class NavigationFailedError(WebSearchError):
    def __init__(self, message: str) -> None:
        super().__init__("navigation_failed", message)


class URLSafetyError(WebSearchError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(error_type, message, code=INVALID_PARAMS)
