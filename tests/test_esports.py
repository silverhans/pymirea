"""Tests for MireaEsports REST helpers and HTML scrapers."""

from __future__ import annotations

import base64
import secrets

import httpx
import pytest
import respx

from pymirea import Config, MireaEsports, configure


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


# ---------------------------------------------------------------------------
# HTML scrapers — pure static methods
# ---------------------------------------------------------------------------


def test_extract_csrf_both_orders():
    assert MireaEsports._extract_csrf('<input name="csrfmiddlewaretoken" value="abc123">') == "abc123"
    assert MireaEsports._extract_csrf('<input value="def456" name="csrfmiddlewaretoken">') == "def456"


def test_extract_csrf_missing():
    assert MireaEsports._extract_csrf("") is None
    assert MireaEsports._extract_csrf('<input name="other" value="x">') is None


def test_extract_next_decodes_amp():
    html = '<input name="next" value="/oauth2/authorize?client_id=x&amp;state=y">'
    assert MireaEsports._extract_next(html) == "/oauth2/authorize?client_id=x&state=y"


def test_extract_next_missing():
    assert MireaEsports._extract_next("<input name='other' value='x'>") is None


def test_extract_login_error_from_class():
    html = '<div class="error-message">Неверный логин или пароль</div>'
    assert MireaEsports._extract_login_error(html) == "Неверный логин или пароль"


def test_extract_login_error_missing():
    assert MireaEsports._extract_login_error("<div class='ok'>fine</div>") is None


# ---------------------------------------------------------------------------
# REST helpers with respx
# ---------------------------------------------------------------------------


ESPORTS_API = "https://esports.mirea.ru/api/v1"


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_success():
    _setup()
    respx.post(f"{ESPORTS_API}/user/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": "new-a", "refresh_token": "new-r"})
    )
    c = MireaEsports()
    try:
        tokens = await c.refresh_tokens("old-refresh")
    finally:
        await c.close()
    assert tokens is not None
    assert tokens.access_token == "new-a"
    assert tokens.refresh_token == "new-r"


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_short_field_names():
    _setup()
    # Django often emits "access"/"refresh" without _token suffix
    respx.post(f"{ESPORTS_API}/user/refresh").mock(
        return_value=httpx.Response(200, json={"access": "a", "refresh": "r"})
    )
    c = MireaEsports()
    try:
        tokens = await c.refresh_tokens("old")
    finally:
        await c.close()
    assert tokens is not None
    assert tokens.access_token == "a"


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_on_4xx_returns_none():
    _setup()
    respx.post(f"{ESPORTS_API}/user/refresh").mock(return_value=httpx.Response(401))
    c = MireaEsports()
    try:
        tokens = await c.refresh_tokens("bad")
    finally:
        await c.close()
    assert tokens is None


@pytest.mark.asyncio
@respx.mock
async def test_get_configuration_sends_bearer():
    _setup()
    seen: dict = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"categories": []})

    respx.get(f"{ESPORTS_API}/bookings/configuration").mock(side_effect=_handler)
    c = MireaEsports()
    try:
        res = await c.get_configuration("tok-42")
    finally:
        await c.close()

    assert res == {"categories": []}
    assert seen["auth"] == "Bearer tok-42"


@pytest.mark.asyncio
@respx.mock
async def test_get_slots_passes_query_params():
    _setup()
    seen: dict = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"slots": []})

    respx.get(f"{ESPORTS_API}/bookings/slots").mock(side_effect=_handler)
    c = MireaEsports()
    try:
        await c.get_slots("tok", date="2026-04-15", duration=60, start_time="12:00", category="pc")
    finally:
        await c.close()

    assert seen["params"]["date"] == "2026-04-15"
    assert seen["params"]["duration"] == "60"
    assert seen["params"]["category"] == "pc"


@pytest.mark.asyncio
@respx.mock
async def test_create_booking_sends_json_body():
    _setup()
    seen: dict = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": 7})

    respx.post(f"{ESPORTS_API}/bookings/@me/create").mock(side_effect=_handler)
    c = MireaEsports()
    try:
        res = await c.create_booking("tok", device_id="d1", booking_datetime="2026-04-15T12:00", booking_duration=60)
    finally:
        await c.close()

    assert res.get("id") == 7
    assert '"device_id":"d1"' in seen["body"]
    assert '"booking_duration":60' in seen["body"]


@pytest.mark.asyncio
@respx.mock
async def test_api_401_returns_unauthorized_sentinel():
    _setup()
    respx.get(f"{ESPORTS_API}/bookings/configuration").mock(return_value=httpx.Response(401))
    c = MireaEsports()
    try:
        res = await c.get_configuration("expired-tok")
    finally:
        await c.close()
    assert res == {"_unauthorized": True}
