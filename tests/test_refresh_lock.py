"""Tests for the single-flight refresh lock in ``try_refresh_tokens``.

Without serialization, N concurrent refreshes hit Keycloak in parallel.
Keycloak rotates the refresh-token on each successful exchange and
invalidates older ones — so the parallel calls win-win-lose-lose-lose,
and the surviving session ends up with whichever winner arrived last.

These tests verify exactly one upstream call regardless of concurrency."""

from __future__ import annotations

import asyncio
import base64
import secrets
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from pymirea import Config, configure
from pymirea import tokens as tokens_mod
from pymirea.tokens import try_refresh_tokens


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


def _fake_tokens(suffix: str = "1") -> dict:
    return {
        "access_token": f"new-access-{suffix}",
        "refresh_token": f"new-refresh-{suffix}",
        "token_type": "Bearer",
        "expires_in": 300,
    }


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_upstream_once():
    _setup()
    cookies = {"refresh_token": "rt-original"}

    call_count = 0
    completed = asyncio.Event()

    async def slow_refresh(self, rt: str):
        nonlocal call_count
        call_count += 1
        # Hold the lock long enough that the second waiter must serialize behind us.
        await asyncio.sleep(0.05)
        completed.set()
        return _fake_tokens()

    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens", new=slow_refresh):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            results = await asyncio.gather(
                try_refresh_tokens(cookies),
                try_refresh_tokens(cookies),
                try_refresh_tokens(cookies),
                try_refresh_tokens(cookies),
                try_refresh_tokens(cookies),
            )

    assert all(results), "all callers should report success"
    assert call_count == 1, f"expected 1 upstream call, got {call_count}"
    assert cookies["access_token"] == "new-access-1"


@pytest.mark.asyncio
async def test_lock_is_per_session_dict():
    """Two distinct session_cookies dicts must NOT serialize against each
    other — different users share nothing."""
    _setup()
    cookies_a = {"refresh_token": "rt-A"}
    cookies_b = {"refresh_token": "rt-B"}

    in_flight = 0
    max_concurrent = 0

    async def tracking_refresh(self, rt: str):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _fake_tokens(suffix=rt[-1])

    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens", new=tracking_refresh):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            await asyncio.gather(
                try_refresh_tokens(cookies_a),
                try_refresh_tokens(cookies_b),
            )

    assert max_concurrent == 2, "different sessions should refresh in parallel"


@pytest.mark.asyncio
async def test_recently_refreshed_skips_upstream():
    """If a refresh completed within the stampede window, a subsequent call
    after lock acquisition should detect the fresh timestamp and skip the
    upstream HTTP entirely."""
    _setup()
    import time
    cookies = {
        "refresh_token": "rt-fresh",
        "__token_refreshed_at": int(time.time()),  # just refreshed
    }

    refresh_mock = AsyncMock(return_value=_fake_tokens())
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens", new=refresh_mock):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            ok = await try_refresh_tokens(cookies)

    assert ok is True
    refresh_mock.assert_not_called()


@pytest.mark.asyncio
async def test_old_refreshed_at_does_not_skip():
    """If __token_refreshed_at is older than the stampede window, refresh
    must run. Verifies the double-check guard is not too aggressive."""
    _setup()
    import time
    cookies = {
        "refresh_token": "rt-stale",
        "__token_refreshed_at": int(time.time()) - 3600,  # 1h ago
    }

    refresh_mock = AsyncMock(return_value=_fake_tokens())
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens", new=refresh_mock):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            ok = await try_refresh_tokens(cookies)

    assert ok is True
    refresh_mock.assert_called_once()


@pytest.mark.asyncio
async def test_no_refresh_token_returns_false_without_locking():
    """Empty cookies dict short-circuits before touching the lock — no
    accidental new lock entries leak from rejected calls."""
    _setup()
    locks_before = len(tokens_mod._refresh_locks)

    assert await try_refresh_tokens(None) is False
    assert await try_refresh_tokens({}) is False
    assert await try_refresh_tokens({"refresh_token": ""}) is False

    locks_after = len(tokens_mod._refresh_locks)
    assert locks_after == locks_before
