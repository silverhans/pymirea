"""Tests for the unified ``Client`` façade.

Client wraps the existing per-service classes with one async context manager
so callers don't have to remember to ``await service.close()`` manually.
The standalone classes still work — Client is purely additive — so these
tests cover the new lifecycle and the lazy-instantiation guarantee."""

from __future__ import annotations

import base64
import secrets
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from pymirea import Client, Config, configure


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


# ─── Lifecycle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_context_manager_returns_self():
    _setup()
    async with Client(session_cookies={}) as c:
        assert isinstance(c, Client)


@pytest.mark.asyncio
async def test_close_is_idempotent():
    _setup()
    c = Client(session_cookies={})
    await c.close()
    await c.close()  # second close should not raise


@pytest.mark.asyncio
async def test_aexit_calls_close_on_instantiated_services_only():
    """Services that were never accessed should NOT be closed (they don't exist)."""
    _setup()
    c = Client(session_cookies={})
    # Touch only `acs` — the others must remain None on exit.
    _ = c.acs
    with mock.patch.object(c.acs, "close", new=AsyncMock()) as acs_close:
        async with c:
            pass
    acs_close.assert_called_once()


# ─── Lazy services ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_services_are_lazy():
    _setup()
    c = Client(session_cookies={})
    # Internally None until accessed.
    assert c._auth is None
    assert c._acs is None
    assert c._grades is None
    assert c._attendance is None
    assert c._esports is None
    await c.close()


@pytest.mark.asyncio
async def test_repeated_access_returns_same_instance():
    _setup()
    cookies = {"access_token": "AT"}
    async with Client(session_cookies=cookies) as c:
        a1 = c.grades
        a2 = c.grades
        assert a1 is a2


# ─── session_cookies sharing ────────────────────────────────────────


@pytest.mark.asyncio
async def test_services_share_session_cookies_dict():
    """If one service mutates cookies, others see it — that's the whole
    point of having a shared dict."""
    _setup()
    cookies = {"access_token": "AT"}
    async with Client(session_cookies=cookies) as c:
        # Trigger ACS init so it grabs the dict.
        _ = c.acs
        # Mutate via the shared reference.
        cookies["access_token"] = "AT2"
        assert c.session_cookies["access_token"] == "AT2"


@pytest.mark.asyncio
async def test_default_empty_session_cookies():
    """Constructing a Client without cookies (e.g. for the login flow)
    starts with an empty dict, not None."""
    _setup()
    async with Client() as c:
        assert c.session_cookies == {}


# ─── ensure_fresh_token integration ─────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_fresh_token_passes_through_to_module_function():
    """Client.ensure_fresh_token is a thin wrapper that targets the
    Client's own session_cookies, no surprises."""
    _setup()
    cookies = {"access_token": "no-jwt"}  # un-parseable → returns False
    async with Client(session_cookies=cookies) as c:
        result = await c.ensure_fresh_token()
        assert result is False  # No JWT to inspect → no-op


@pytest.mark.asyncio
async def test_ensure_fresh_token_custom_buffer():
    _setup()
    async with Client(session_cookies={}) as c:
        # Empty cookies → False regardless of buffer.
        assert await c.ensure_fresh_token(buffer_s=120) is False
