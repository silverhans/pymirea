"""Mock-based tests for CurlCffiAsyncClient kwargs translation.

The wrapper turns httpx-style kwargs into curl_cffi kwargs. This file
verifies that translation actually happens — without these tests a
silent regression in ``_request()`` would only be caught in production
when a user enables ``tls_impersonate``."""

from __future__ import annotations

import base64
import secrets
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from pymirea import Config, configure

curl_cffi = pytest.importorskip("curl_cffi")

from pymirea._http_cffi import CurlCffiAsyncClient  # noqa: E402


def _setup() -> None:
    configure(Config(
        session_keys=base64.b64encode(secrets.token_bytes(32)).decode(),
        tls_impersonate="chrome120",
    ))


# ──────────────────────────────────────────────────────────────────────
# follow_redirects → allow_redirects
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_passes_follow_redirects_false_as_allow_redirects_false():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.get("https://x.test/", follow_redirects=False)
        req.assert_called_once()
        kwargs = req.call_args.kwargs
        assert kwargs["allow_redirects"] is False
        assert "follow_redirects" not in kwargs


@pytest.mark.asyncio
async def test_get_passes_follow_redirects_true_as_allow_redirects_true():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.get("https://x.test/", follow_redirects=True)
        kwargs = req.call_args.kwargs
        assert kwargs["allow_redirects"] is True
        assert "follow_redirects" not in kwargs


@pytest.mark.asyncio
async def test_get_uses_constructor_follow_redirects_when_unset():
    """Constructor sets default; explicit kwargs override it."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120", follow_redirects=False)
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.get("https://x.test/")  # no explicit kwarg
        kwargs = req.call_args.kwargs
        assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_post_translates_follow_redirects():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.post("https://x.test/", follow_redirects=False)
        kwargs = req.call_args.kwargs
        assert kwargs["allow_redirects"] is False
        assert "follow_redirects" not in kwargs


# ──────────────────────────────────────────────────────────────────────
# content → data (raw bytes body)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_translates_content_to_data():
    """grades.py and acs.py send gRPC-Web frames via content=bytes."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        body = b"\x00\x00\x00\x00\x05hello"
        await c.post("https://x.test/", content=body)
        kwargs = req.call_args.kwargs
        assert kwargs["data"] == body
        assert "content" not in kwargs


@pytest.mark.asyncio
async def test_post_does_not_overwrite_existing_data_with_content():
    """If both data and content are passed, prefer existing data (defensive)."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.post("https://x.test/", data={"k": "v"}, content=b"raw")
        kwargs = req.call_args.kwargs
        # data wins (setdefault behavior)
        assert kwargs["data"] == {"k": "v"}


# ──────────────────────────────────────────────────────────────────────
# Pass-through kwargs (params, headers, json, etc.)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_passes_params_through():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.get("https://x.test/", params={"token": "abc"})
        assert req.call_args.kwargs["params"] == {"token": "abc"}


@pytest.mark.asyncio
async def test_get_passes_headers_through():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.get("https://x.test/", headers={"X-Custom": "v"})
        assert req.call_args.kwargs["headers"] == {"X-Custom": "v"}


@pytest.mark.asyncio
async def test_post_passes_json_through():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        payload = {"a": 1, "b": "two"}
        await c.post("https://x.test/", json=payload)
        assert req.call_args.kwargs["json"] == payload


@pytest.mark.asyncio
async def test_post_normalizes_per_request_cookies_to_dict():
    """Per-request cookies kwarg should be converted to dict for curl_cffi."""
    _setup()
    import httpx
    httpx_cookies = httpx.Cookies()
    httpx_cookies.set("k", "v", domain=".example.test")

    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.post("https://x.test/", cookies=httpx_cookies)
        cookies_arg = req.call_args.kwargs["cookies"]
        assert isinstance(cookies_arg, dict)
        assert cookies_arg.get("k") == "v"


# ──────────────────────────────────────────────────────────────────────
# Method dispatch (GET vs POST)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dispatches_GET_method():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.get("https://x.test/")
        # First positional or method kwarg is "GET"
        args, kwargs = req.call_args
        method = args[0] if args else kwargs.get("method")
        assert method == "GET"


@pytest.mark.asyncio
async def test_post_dispatches_POST_method():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        await c.post("https://x.test/")
        args, kwargs = req.call_args
        method = args[0] if args else kwargs.get("method")
        assert method == "POST"


# ──────────────────────────────────────────────────────────────────────
# Constructor: impersonate / proxy / headers reach AsyncSession
# ──────────────────────────────────────────────────────────────────────


def test_constructor_passes_impersonate_to_session():
    """Verify TLS profile string actually reaches the underlying session."""
    _setup()
    with mock.patch("pymirea._http_cffi.AsyncSession") as MockSession:
        CurlCffiAsyncClient(impersonate="chrome131")
        kwargs = MockSession.call_args.kwargs
        assert kwargs["impersonate"] == "chrome131"


def test_constructor_passes_proxy():
    _setup()
    with mock.patch("pymirea._http_cffi.AsyncSession") as MockSession:
        CurlCffiAsyncClient(impersonate="chrome120", proxy="http://proxy.test:8080")
        assert MockSession.call_args.kwargs["proxy"] == "http://proxy.test:8080"


def test_constructor_omits_proxy_when_none():
    """Don't pass proxy=None — curl_cffi should use no proxy by default."""
    _setup()
    with mock.patch("pymirea._http_cffi.AsyncSession") as MockSession:
        CurlCffiAsyncClient(impersonate="chrome120", proxy=None)
        assert "proxy" not in MockSession.call_args.kwargs


def test_constructor_passes_headers():
    _setup()
    with mock.patch("pymirea._http_cffi.AsyncSession") as MockSession:
        CurlCffiAsyncClient(impersonate="chrome120", headers={"X-Test": "1"})
        assert MockSession.call_args.kwargs["headers"] == {"X-Test": "1"}


def test_constructor_converts_cookies_to_dict():
    """httpx.Cookies passed in must be normalized to dict for curl_cffi."""
    _setup()
    import httpx
    httpx_cookies = httpx.Cookies()
    httpx_cookies.set("session_id", "xyz", domain=".mirea.ru")

    with mock.patch("pymirea._http_cffi.AsyncSession") as MockSession:
        CurlCffiAsyncClient(impersonate="chrome120", cookies=httpx_cookies)
        cookies_arg = MockSession.call_args.kwargs["cookies"]
        assert isinstance(cookies_arg, dict)
        assert cookies_arg.get("session_id") == "xyz"


# ──────────────────────────────────────────────────────────────────────
# aclose: handle both async and sync close
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_awaits_async_close():
    """curl_cffi 0.7+ has async close() — we must await it."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    awaited = False

    async def fake_close():
        nonlocal awaited
        awaited = True

    c._session.close = fake_close
    await c.aclose()
    assert awaited


@pytest.mark.asyncio
async def test_aclose_handles_sync_close():
    """Older curl_cffi versions had sync close() — handle gracefully."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    called = False

    def fake_close():
        nonlocal called
        called = True
        return None  # sync return — not awaitable

    c._session.close = fake_close
    await c.aclose()  # must not raise
    assert called


@pytest.mark.asyncio
async def test_aclose_handles_no_close_method():
    """Defensive: if session has no close() at all, aclose is a no-op."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    c._session.close = None
    await c.aclose()  # must not raise


# ──────────────────────────────────────────────────────────────────────
# Exception translation: curl_cffi → httpx
#
# pymirea code catches httpx.TimeoutException / NetworkError. Without
# translation, those `except` blocks would never match when the curl_cffi
# backend is active, leaving every transient network error unhandled.
# ──────────────────────────────────────────────────────────────────────


import httpx  # noqa: E402
from curl_cffi import exceptions as cce  # noqa: E402


@pytest.mark.asyncio
async def test_translates_curl_timeout_to_httpx_timeout():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = cce.Timeout("connection timed out")
        with pytest.raises(httpx.TimeoutException):
            await c.get("https://x.test/")


@pytest.mark.asyncio
async def test_translates_curl_connection_error_to_httpx_connect_error():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = cce.ConnectionError("DNS failed")
        with pytest.raises(httpx.ConnectError):
            await c.get("https://x.test/")


@pytest.mark.asyncio
async def test_translates_curl_request_exception_to_httpx_network_error():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        # Use the base RequestException which doesn't match a more specific subclass.
        req.side_effect = cce.RequestException("generic curl problem")
        with pytest.raises(httpx.NetworkError):
            await c.get("https://x.test/")


@pytest.mark.asyncio
async def test_does_not_swallow_unrelated_exceptions():
    """Non-curl exceptions (e.g. ValueError from caller code) propagate as-is."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = ValueError("not a network error")
        with pytest.raises(ValueError):
            await c.get("https://x.test/")


def test_translate_exception_returns_original_when_curl_cffi_missing(monkeypatch):
    """Defensive: if curl_cffi.exceptions can't be imported, return e as-is."""
    import sys
    # Force ImportError by removing curl_cffi.exceptions from sys.modules and
    # blocking re-import via sys.path. Simpler: just verify the API contract.
    e = RuntimeError("fake")
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    monkeypatch.setitem(sys.modules, "curl_cffi.exceptions", None)
    result = CurlCffiAsyncClient._translate_exception(e)
    assert result is e


@pytest.mark.asyncio
async def test_translation_preserves_chained_cause():
    """The original curl_cffi exception is set as __cause__ for debuggability."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    original = cce.Timeout("read timeout")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = original
        try:
            await c.get("https://x.test/")
        except httpx.TimeoutException as raised:
            assert raised.__cause__ is original
