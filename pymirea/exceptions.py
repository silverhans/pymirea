"""Typed exceptions for pymirea.

These exceptions are raised by NEW helpers introduced in 0.3.4
(e.g. :func:`pymirea.tokens.ensure_fresh_token`). Existing functions
(``try_refresh_tokens``, ``MireaAPI.mark_attendance`` etc.) keep their
historical contract — they still return ``None``/``False`` or raise
``httpx.*`` types as before. Nothing is rewired retroactively.

Consumers who want explicit error types can opt into the new helpers;
old call sites continue to work unchanged.
"""

from __future__ import annotations


class MireaError(Exception):
    """Base class for all pymirea-typed errors."""


class MireaSessionExpired(MireaError):
    """The session is unrecoverable without user re-authentication.

    Typically raised when a refresh attempt failed and there is no
    further automatic recovery possible (e.g. refresh_token itself
    expired or Keycloak rejected it).
    """


class MireaRefreshFailed(MireaError):
    """A refresh attempt failed transiently.

    The session may still be recoverable on retry — Keycloak might be
    flaky, the network might be slow, etc. Different from
    :class:`MireaSessionExpired` which signals a terminal state.
    """


class MireaRateLimited(MireaError):
    """Upstream returned 429. ``retry_after`` is the suggested wait in seconds."""

    def __init__(self, message: str = "rate limited", *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class MireaServerError(MireaError):
    """Upstream returned a 5xx response."""

    def __init__(self, message: str = "server error", *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class MireaParseFailed(MireaError):
    """МИРЭА returned a response we could not parse.

    Usually means the upstream HTML/protobuf schema changed. The first
    ~500 chars of the offending body are attached as ``response_preview``
    to aid debugging.
    """

    def __init__(self, message: str = "failed to parse response", *, response_preview: str | None = None):
        super().__init__(message)
        self.response_preview = response_preview


__all__ = [
    "MireaError",
    "MireaSessionExpired",
    "MireaRefreshFailed",
    "MireaRateLimited",
    "MireaServerError",
    "MireaParseFailed",
]
