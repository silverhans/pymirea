"""Integration tests for MireaAuth.login — full Keycloak SSO flow + 2FA detection."""

from __future__ import annotations

import base64
import secrets
from typing import Optional

import httpx
import pytest
import respx

from pymirea import Config, MireaAuth, configure


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


AUTH_URL = "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth"
TOKEN_URL = "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/token"
LOGIN_ACTION_BASE = "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"
LOGIN_ACTION = f"{LOGIN_ACTION_BASE}?session_code=SC&execution=EX&client_id=attendance-app&tab_id=T1"


def _kc_login_form(action_url: str = LOGIN_ACTION) -> str:
    escaped = action_url.replace("&", "\\u0026")
    return (
        '<html><body><script>'
        f'var kcContext = {{"loginAction": "{escaped}", "pageId": "login-username"}};'
        '</script></body></html>'
    )


def _kc_email_code_form(action_url: str) -> str:
    escaped = action_url.replace("&", "\\u0026")
    return (
        '<script>'
        f'var kcContext = {{"loginAction": "{escaped}", "pageId": "email-code-form"}};'
        '</script>'
    )


def _kc_otp_form(action_url: str) -> str:
    escaped = action_url.replace("&", "\\u0026")
    return (
        '<script>'
        f'var kcContext = {{"loginAction": "{escaped}", "pageId": "otp-form", "otpLogin": {{}}}};'
        '</script>'
    )


@pytest.mark.asyncio
@respx.mock
async def test_login_happy_path_returns_tokens():
    _setup()
    respx.get(AUTH_URL).mock(return_value=httpx.Response(200, text=_kc_login_form()))
    # The POST is to the base URL — respx matches on path, query string is separate.
    respx.post(LOGIN_ACTION_BASE).mock(
        return_value=httpx.Response(302, headers={"Location": "https://pulse.mirea.ru/?code=AUTH-CODE&session_state=abc"})
    )
    respx.get("https://pulse.mirea.ru/").mock(return_value=httpx.Response(200, text="<html>pulse</html>"))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "ACC", "refresh_token": "REF", "token_type": "Bearer", "expires_in": 600,
        })
    )

    auth = MireaAuth()
    try:
        result = await auth.login("ivanov.i@edu.mirea.ru", "password123")
    finally:
        await auth.close()

    assert result.success is True, f"expected success, got: {result.message}"
    assert result.cookies is not None
    assert result.cookies.get("access_token") == "ACC"
    assert result.cookies.get("refresh_token") == "REF"


@pytest.mark.asyncio
@respx.mock
async def test_login_sends_pkce_s256_challenge():
    _setup()
    captured: dict[str, Optional[str]] = {}

    def _auth_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.url.params.get("code_challenge_method")
        captured["challenge"] = request.url.params.get("code_challenge")
        return httpx.Response(200, text=_kc_login_form())

    respx.get(AUTH_URL).mock(side_effect=_auth_handler)
    respx.post(LOGIN_ACTION_BASE).mock(
        return_value=httpx.Response(302, headers={"Location": "https://pulse.mirea.ru/?code=X"})
    )
    respx.get("https://pulse.mirea.ru/").mock(return_value=httpx.Response(200))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "ACC", "refresh_token": "REF"}))

    auth = MireaAuth()
    try:
        await auth.login("a", "b")
    finally:
        await auth.close()

    assert captured.get("method") == "S256"
    assert captured.get("challenge") is not None
    assert len(captured["challenge"]) >= 43


@pytest.mark.asyncio
async def test_login_empty_username_returns_failure():
    _setup()
    auth = MireaAuth()
    try:
        result = await auth.login("", "pw")
    finally:
        await auth.close()
    assert result.success is False


@pytest.mark.asyncio
async def test_login_empty_password_returns_failure():
    _setup()
    auth = MireaAuth()
    try:
        result = await auth.login("user", "")
    finally:
        await auth.close()
    assert result.success is False


@pytest.mark.asyncio
@respx.mock
async def test_login_detects_email_code_challenge():
    _setup()
    otp_action = f"{LOGIN_ACTION_BASE}?session_code=OTP-SC&execution=OTP-EX"
    respx.get(AUTH_URL).mock(return_value=httpx.Response(200, text=_kc_login_form()))
    respx.post(LOGIN_ACTION_BASE).mock(
        return_value=httpx.Response(200, text=_kc_email_code_form(otp_action))
    )

    auth = MireaAuth()
    try:
        result = await auth.login("ivanov", "pw")
    finally:
        await auth.close()

    assert result.success is False
    assert result.challenge is not None
    assert result.challenge.kind == "email_code"
    assert result.challenge.field_name == "emailCode"
    assert result.challenge.action_url == otp_action


@pytest.mark.asyncio
@respx.mock
async def test_login_detects_totp_challenge():
    _setup()
    otp_action = f"{LOGIN_ACTION_BASE}?session_code=O"
    respx.get(AUTH_URL).mock(return_value=httpx.Response(200, text=_kc_login_form()))
    respx.post(LOGIN_ACTION_BASE).mock(
        return_value=httpx.Response(200, text=_kc_otp_form(otp_action))
    )

    auth = MireaAuth()
    try:
        result = await auth.login("ivanov", "pw")
    finally:
        await auth.close()

    assert result.success is False
    assert result.challenge is not None
    assert result.challenge.kind == "otp"
    assert result.challenge.field_name == "otp"


@pytest.mark.asyncio
@respx.mock
async def test_login_wrong_credentials_returns_failure():
    _setup()
    # Keycloak renders a page that stays on login-actions (not a redirect away)
    # and contains an error message in kcContext.
    err_html = (
        '<script>var kcContext = {"loginAction":"'
        + LOGIN_ACTION.replace("&", "\\u0026")
        + '","pageId":"login-username","message":"Invalid username or password"};</script>'
    )
    respx.get(AUTH_URL).mock(return_value=httpx.Response(200, text=_kc_login_form()))
    respx.post(LOGIN_ACTION_BASE).mock(return_value=httpx.Response(200, text=err_html))

    auth = MireaAuth()
    try:
        result = await auth.login("ivanov", "wrong")
    finally:
        await auth.close()

    assert result.success is False
    assert result.challenge is None


@pytest.mark.asyncio
@respx.mock
async def test_login_missing_login_action_returns_failure():
    _setup()
    respx.get(AUTH_URL).mock(return_value=httpx.Response(200, text="<html>no kcContext</html>"))

    auth = MireaAuth()
    try:
        result = await auth.login("x", "y")
    finally:
        await auth.close()

    assert result.success is False
