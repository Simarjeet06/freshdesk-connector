"""Error types. Every message is written so that an LLM agent can relay it or act on it."""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class. `retryable` tells the agent whether trying again later can help."""

    retryable = False

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class ConfigError(ConnectorError):
    pass


class AuthError(ConnectorError):
    """401: the API key is missing, wrong or revoked."""


class PermissionDeniedError(ConnectorError):
    """403: the key is valid but the agent it belongs to cannot see this resource."""


class NotFoundError(ConnectorError):
    pass


class ValidationError(ConnectorError):
    """400 from Freshdesk, or arguments rejected before any request was sent."""


class RateLimitedError(ConnectorError):
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None):
        super().__init__(message, status=429)
        self.retry_after = retry_after


class UpstreamError(ConnectorError):
    """5xx or network failure that survived our retries."""

    retryable = True
