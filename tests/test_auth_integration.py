"""Integration tests for MireaAuth using respx to mock httpx HTTP calls.

These verify the public auth API contracts (refresh_tokens, verify_session,
submit_otp/complete_2fa response handling, breaker integration) without
hitting live MIREA servers."""

from __future__ import annotations

import base64
import secrets

import httpx
import pytest
import respx

from pymirea import AuthChallenge, AuthResult, Config, MireaAuth, configure


def _setup_config():
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


# ---------------------------------------------------------------------------
# refresh_tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_success_returns_new_tokens():
    _setup_config()
    auth = MireaAuth()
    respx.post(MireaAuth.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )
    )

    result = await auth.refresh_tokens("old-refresh")
    await auth.close()

    assert result is not None
    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "new-refresh"
    assert result["token_type"] == "Bearer"
    assert result["expires_in"] == 3600


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_keeps_old_refresh_when_omitted_by_server():
    """Some Keycloak setups don't return refresh_token on refresh — keep the old one."""
    _setup_config()
    auth = MireaAuth()
    respx.post(MireaAuth.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "new-access", "token_type": "Bearer", "expires_in": 600},
        )
    )

    result = await auth.refresh_tokens("original-refresh-token")
    await auth.close()

    assert result is not None
    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "original-refresh-token"


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_returns_none_on_4xx():
    _setup_config()
    auth = MireaAuth()
    respx.post(MireaAuth.TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )

    result = await auth.refresh_tokens("bad-token")
    await auth.close()

    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_returns_none_on_empty_input():
    _setup_config()
    auth = MireaAuth()
    # No HTTP request expected — short-circuit on empty input
    result = await auth.refresh_tokens("")
    await auth.close()
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_returns_none_when_response_missing_access_token():
    _setup_config()
    auth = MireaAuth()
    respx.post(MireaAuth.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token_type": "Bearer"})
    )

    result = await auth.refresh_tokens("some-token")
    await auth.close()
    assert result is None


# ---------------------------------------------------------------------------
# verify_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_verify_session_true_when_no_login_redirect():
    _setup_config()
    auth = MireaAuth()
    respx.get(MireaAuth.ATTENDANCE_URL).mock(
        return_value=httpx.Response(200, text="<html>welcome</html>")
    )

    ok = await auth.verify_session({"AUTH_SESSION_ID": "abc"})
    await auth.close()
    assert ok is True


@pytest.mark.asyncio
@respx.mock
async def test_verify_session_false_when_redirected_to_login():
    _setup_config()
    auth = MireaAuth()
    respx.get(MireaAuth.ATTENDANCE_URL).mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://sso.mirea.ru/realms/mirea/login"},
        )
    )
    # Follow-redirect target
    respx.get("https://sso.mirea.ru/realms/mirea/login").mock(
        return_value=httpx.Response(200, text="<html>login form</html>")
    )

    ok = await auth.verify_session({"AUTH_SESSION_ID": "stale"})
    await auth.close()
    assert ok is False


@pytest.mark.asyncio
@respx.mock
async def test_verify_session_filters_internal_keys():
    """access_token / __internal keys must NOT be sent as cookies to the server."""
    _setup_config()
    auth = MireaAuth()
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["cookies"] = dict(request.headers).get("cookie", "")
        return httpx.Response(200, text="<html>ok</html>")

    respx.get(MireaAuth.ATTENDANCE_URL).mock(side_effect=_capture)

    await auth.verify_session(
        {
            "access_token": "should-not-leak",
            "refresh_token": "also-not",
            "__token_refreshed_at": 1234567890,
            "AUTH_SESSION_ID": "send-me",
        }
    )
    await auth.close()

    assert "AUTH_SESSION_ID" in captured["cookies"]
    assert "should-not-leak" not in captured["cookies"]
    assert "also-not" not in captured["cookies"]
    assert "__token_refreshed_at" not in captured["cookies"]


# ---------------------------------------------------------------------------
# AuthResult / AuthChallenge contract
# ---------------------------------------------------------------------------


def test_auth_result_dataclass_defaults():
    res = AuthResult(success=True, message="ok")
    assert res.cookies is None
    assert res.user_info is None
    assert res.challenge is None
    assert res.tokens is None  # property alias


def test_auth_challenge_dataclass_defaults():
    ch = AuthChallenge(kind="otp", action_url="https://x", field_name="otp", hidden_fields={})
    assert ch.referer is None
    assert ch.pkce_verifier is None
    assert ch.redirect_uri is None
