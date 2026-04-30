"""Tests for ``get_token_exp`` and ``ensure_fresh_token``.

Proactive refresh saves the wasted "request → 401 → refresh → retry"
round-trip when we already know the token is about to expire. These tests
cover the JWT decoding path (no signature check) and the orchestration of
calling refresh ahead of expiry."""

from __future__ import annotations

import base64
import json
import secrets
import time
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from pymirea import (
    Config,
    MireaSessionExpired,
    configure,
    ensure_fresh_token,
    get_token_exp,
)
from pymirea import tokens as tokens_mod


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


def _make_jwt(*, exp: int | None = None, extra_claims: dict | None = None) -> str:
    """Build a JWT-like string with the given exp claim. Signature is
    'sig' — we don't verify, the consumer (МИРЭА) does."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_dict: dict = {}
    if exp is not None:
        payload_dict["exp"] = exp
    if extra_claims:
        payload_dict.update(extra_claims)
    payload_json = json.dumps(payload_dict).encode()
    payload = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


# ─── get_token_exp ───────────────────────────────────────────────────


def test_get_token_exp_reads_valid_jwt():
    cookies = {"access_token": _make_jwt(exp=1_700_000_000)}
    assert get_token_exp(cookies) == 1_700_000_000


def test_get_token_exp_returns_none_for_missing_token():
    assert get_token_exp(None) is None
    assert get_token_exp({}) is None
    assert get_token_exp({"access_token": ""}) is None


def test_get_token_exp_returns_none_for_malformed_jwt():
    assert get_token_exp({"access_token": "not.a.jwt"}) is None
    assert get_token_exp({"access_token": "only-one-segment"}) is None
    assert get_token_exp({"access_token": "two.segments"}) is None


def test_get_token_exp_returns_none_when_no_exp_claim():
    """Token may be valid JWT but without exp (rare, but possible)."""
    cookies = {"access_token": _make_jwt(extra_claims={"sub": "user"})}
    assert get_token_exp(cookies) is None


def test_get_token_exp_handles_padding_variations():
    """JWT base64url is padding-stripped — our decoder must restore it."""
    # exp=1 produces a tiny payload that is more likely to need padding restored
    cookies = {"access_token": _make_jwt(exp=1)}
    assert get_token_exp(cookies) == 1


# ─── ensure_fresh_token ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_fresh_no_op_when_token_far_from_expiry():
    """Token expires in 1 hour, buffer is 30s — no refresh needed."""
    _setup()
    far_future = int(time.time()) + 3600
    cookies = {
        "access_token": _make_jwt(exp=far_future),
        "refresh_token": "rt",
    }

    refresh_mock = AsyncMock()
    with mock.patch.object(tokens_mod, "try_refresh_tokens", new=refresh_mock):
        result = await ensure_fresh_token(cookies)

    assert result is True
    refresh_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_fresh_refreshes_when_inside_buffer():
    """Token expires in 5s, buffer is 30s — must refresh before any request."""
    _setup()
    near_future = int(time.time()) + 5
    cookies = {
        "access_token": _make_jwt(exp=near_future),
        "refresh_token": "rt",
    }

    refresh_mock = AsyncMock(return_value=True)
    with mock.patch.object(tokens_mod, "try_refresh_tokens", new=refresh_mock):
        result = await ensure_fresh_token(cookies)

    assert result is True
    refresh_mock.assert_called_once_with(cookies)


@pytest.mark.asyncio
async def test_ensure_fresh_refreshes_when_already_expired():
    _setup()
    past = int(time.time()) - 100
    cookies = {
        "access_token": _make_jwt(exp=past),
        "refresh_token": "rt",
    }

    refresh_mock = AsyncMock(return_value=True)
    with mock.patch.object(tokens_mod, "try_refresh_tokens", new=refresh_mock):
        result = await ensure_fresh_token(cookies)

    assert result is True
    refresh_mock.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_fresh_raises_session_expired_when_refresh_fails():
    """Refresh returned False → no recovery path → typed exception."""
    _setup()
    past = int(time.time()) - 100
    cookies = {
        "access_token": _make_jwt(exp=past),
        "refresh_token": "rt-dead",
    }

    refresh_mock = AsyncMock(return_value=False)
    with mock.patch.object(tokens_mod, "try_refresh_tokens", new=refresh_mock):
        with pytest.raises(MireaSessionExpired):
            await ensure_fresh_token(cookies)


@pytest.mark.asyncio
async def test_ensure_fresh_returns_false_when_no_jwt():
    """Without an access_token (or unparseable one) we have no way to be
    proactive — return False, let the regular request flow hit 401 and
    refresh on the reactive path."""
    _setup()
    refresh_mock = AsyncMock()
    with mock.patch.object(tokens_mod, "try_refresh_tokens", new=refresh_mock):
        assert await ensure_fresh_token({}) is False
        assert await ensure_fresh_token({"access_token": "garbage"}) is False
        assert await ensure_fresh_token(None) is False

    refresh_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_fresh_custom_buffer():
    """Caller can tune the 'how-soon-is-too-soon' threshold."""
    _setup()
    soon = int(time.time()) + 60
    cookies = {
        "access_token": _make_jwt(exp=soon),
        "refresh_token": "rt",
    }

    refresh_mock = AsyncMock(return_value=True)
    with mock.patch.object(tokens_mod, "try_refresh_tokens", new=refresh_mock):
        # buffer=10 → 60s margin is plenty, no refresh
        assert await ensure_fresh_token(cookies, buffer_s=10) is True
        refresh_mock.assert_not_called()

        # buffer=120 → 60s margin is too little, refresh
        assert await ensure_fresh_token(cookies, buffer_s=120) is True
        refresh_mock.assert_called_once()
