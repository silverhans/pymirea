"""Tests for the typed exception hierarchy introduced in 0.3.4.

These exceptions are pure data — the only behavior under test is that the
inheritance relationships are correct and that attribute extras (retry_after,
status_code, response_preview) are wired up. Anything callers rely on for
``isinstance`` checks must hold."""

from __future__ import annotations

import pytest

from pymirea import (
    MireaError,
    MireaParseFailed,
    MireaRateLimited,
    MireaRefreshFailed,
    MireaServerError,
    MireaSessionExpired,
)


def test_all_inherit_from_mirea_error():
    for cls in (
        MireaSessionExpired,
        MireaRefreshFailed,
        MireaRateLimited,
        MireaServerError,
        MireaParseFailed,
    ):
        assert issubclass(cls, MireaError)
        assert issubclass(cls, Exception)


def test_session_expired_distinct_from_refresh_failed():
    """The two failure modes are deliberately separate types so callers
    can treat 'transient' vs 'terminal' differently."""
    e1 = MireaSessionExpired("dead")
    e2 = MireaRefreshFailed("transient")
    assert not isinstance(e1, MireaRefreshFailed)
    assert not isinstance(e2, MireaSessionExpired)


def test_rate_limited_carries_retry_after():
    e = MireaRateLimited("slow down", retry_after=5.0)
    assert e.retry_after == 5.0
    assert "slow down" in str(e)


def test_rate_limited_default_retry_after_is_none():
    e = MireaRateLimited()
    assert e.retry_after is None


def test_server_error_carries_status_code():
    e = MireaServerError("upstream", status_code=503)
    assert e.status_code == 503
    assert "upstream" in str(e)


def test_parse_failed_carries_response_preview():
    e = MireaParseFailed("schema drift", response_preview="<html>...</html>")
    assert e.response_preview == "<html>...</html>"


def test_can_be_raised_and_caught_as_base():
    """The whole point of the hierarchy is that callers can catch
    ``MireaError`` once and handle every typed failure."""
    with pytest.raises(MireaError):
        raise MireaSessionExpired("bye")
    with pytest.raises(MireaError):
        raise MireaRateLimited(retry_after=1)
