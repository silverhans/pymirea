"""Tests for observability hooks (on_refresh, on_request, on_error).

Hooks are wired through Config and dispatched from pymirea hot paths.
Three guarantees under test:

1. Hooks fire when set, with the expected payload shape.
2. Sync and async hook callables both work.
3. Exceptions raised inside the user's hook never propagate to callers
   (observability must not break business logic)."""

from __future__ import annotations

import asyncio
import base64
import secrets
from unittest import mock
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from pymirea import Config, configure
from pymirea import tokens as tokens_mod
from pymirea._http import make_async_client


def _setup(**hook_overrides) -> None:
    cfg = Config(
        session_keys=base64.b64encode(secrets.token_bytes(32)).decode(),
        **hook_overrides,
    )
    configure(cfg)


def _fake_tokens() -> dict:
    return {
        "access_token": "AT",
        "refresh_token": "RT2",
        "token_type": "Bearer",
        "expires_in": 300,
    }


# ─── on_refresh ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_refresh_fires_on_success():
    captured: list[dict] = []
    _setup(on_refresh=lambda info: captured.append(info))

    cookies = {"refresh_token": "rt"}
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens",
                           new=AsyncMock(return_value=_fake_tokens())):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            ok = await tokens_mod.try_refresh_tokens(cookies)

    assert ok is True
    assert len(captured) == 1
    assert captured[0]["success"] is True
    assert captured[0]["had_refresh_token"] is True


@pytest.mark.asyncio
async def test_on_refresh_fires_on_failure():
    captured: list[dict] = []
    _setup(on_refresh=lambda info: captured.append(info))

    cookies = {"refresh_token": "rt"}
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens",
                           new=AsyncMock(return_value=None)):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            ok = await tokens_mod.try_refresh_tokens(cookies)

    assert ok is False
    assert len(captured) == 1
    assert captured[0]["success"] is False


@pytest.mark.asyncio
async def test_on_refresh_supports_async_hook():
    captured: list[dict] = []

    async def async_hook(info):
        await asyncio.sleep(0)  # actually await
        captured.append(info)

    _setup(on_refresh=async_hook)

    cookies = {"refresh_token": "rt"}
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens",
                           new=AsyncMock(return_value=_fake_tokens())):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            await tokens_mod.try_refresh_tokens(cookies)

    assert len(captured) == 1
    assert captured[0]["success"] is True


@pytest.mark.asyncio
async def test_hook_exception_does_not_break_caller():
    """User's hook raises → pymirea logs warning + continues normally."""
    def bad_hook(info):
        raise RuntimeError("boom")

    _setup(on_refresh=bad_hook)

    cookies = {"refresh_token": "rt"}
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens",
                           new=AsyncMock(return_value=_fake_tokens())):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            # The bug-free contract: refresh succeeds, hook error swallowed.
            ok = await tokens_mod.try_refresh_tokens(cookies)

    assert ok is True
    assert cookies["access_token"] == "AT"


@pytest.mark.asyncio
async def test_no_hook_set_is_noop():
    """The cleanest version of 'don't break things': if no hook configured,
    the dispatcher does nothing and refresh proceeds identically."""
    _setup()  # no hooks

    cookies = {"refresh_token": "rt"}
    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens",
                           new=AsyncMock(return_value=_fake_tokens())):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            ok = await tokens_mod.try_refresh_tokens(cookies)

    assert ok is True


# ─── on_error ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_error_fires_when_refresh_raises():
    captured: list[tuple] = []

    def hook(exc, ctx):
        captured.append((exc, ctx))

    _setup(on_error=hook)

    cookies = {"refresh_token": "rt"}

    async def boom(*args, **kwargs):
        raise httpx.NetworkError("simulated")

    with mock.patch.object(tokens_mod.MireaAuth, "refresh_tokens", new=boom):
        with mock.patch.object(tokens_mod.MireaAuth, "close", new=AsyncMock()):
            with pytest.raises(httpx.NetworkError):
                await tokens_mod.try_refresh_tokens(cookies)

    assert len(captured) == 1
    exc, ctx = captured[0]
    assert isinstance(exc, httpx.NetworkError)
    assert ctx["where"] == "try_refresh_tokens"


# ─── on_request ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_on_request_fires_per_http_call():
    captured: list[dict] = []
    _setup(on_request=lambda info: captured.append(info))

    respx.get("https://example.test/foo").mock(return_value=httpx.Response(200, text="ok"))

    client = make_async_client()
    try:
        resp = await client.get("https://example.test/foo")
        assert resp.status_code == 200
    finally:
        await client.aclose()

    assert len(captured) == 1
    info = captured[0]
    assert info["method"] == "GET"
    assert info["status"] == 200
    assert "example.test/foo" in info["url"]
    assert info["duration_ms"] >= 0


@pytest.mark.asyncio
@respx.mock
async def test_on_request_records_failure_status():
    captured: list[dict] = []
    _setup(on_request=lambda info: captured.append(info))

    respx.post("https://example.test/x").mock(return_value=httpx.Response(503))

    client = make_async_client()
    try:
        await client.post("https://example.test/x")
    finally:
        await client.aclose()

    assert len(captured) == 1
    assert captured[0]["status"] == 503
    assert captured[0]["method"] == "POST"


@pytest.mark.asyncio
@respx.mock
async def test_on_request_hook_exception_does_not_break_request():
    """If the user's hook errors, the underlying HTTP call still succeeds."""
    def bad(info):
        raise ValueError("hook bug")

    _setup(on_request=bad)

    respx.get("https://example.test/foo").mock(return_value=httpx.Response(200, text="ok"))

    client = make_async_client()
    try:
        resp = await client.get("https://example.test/foo")
    finally:
        await client.aclose()

    assert resp.status_code == 200
