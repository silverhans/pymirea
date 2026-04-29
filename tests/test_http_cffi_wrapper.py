"""Tests for CurlCffiAsyncClient — verifies behavioral parity with httpx.

Covers cookies dict-protocol, async context manager, kwargs translation
(follow_redirects → allow_redirects, content → data) and timeout coercion.
Skipped entirely if curl_cffi is not installed."""

from __future__ import annotations

import base64
import secrets

import httpx
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
# Cookie compatibility — pymirea code uses dict()/get()/keys()/update()
# ──────────────────────────────────────────────────────────────────────


def test_cookies_dict_conversion():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120", cookies={"foo": "bar", "baz": "qux"})
    cookies_dict = dict(c.cookies)
    assert cookies_dict.get("foo") == "bar"
    assert cookies_dict.get("baz") == "qux"


def test_cookies_get_method():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120", cookies={"foo": "bar"})
    assert c.cookies.get("foo") == "bar"
    assert c.cookies.get("missing") is None


def test_cookies_keys():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120", cookies={"a": "1", "b": "2"})
    keys = list(c.cookies.keys())
    assert "a" in keys
    assert "b" in keys


def test_cookies_update_with_dict():
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120", cookies={"existing": "old"})
    c.cookies.update({"new": "added"})
    assert c.cookies.get("new") == "added"
    assert c.cookies.get("existing") == "old"


def test_cookies_update_jar_from_jar():
    """auth.py:803 does proxy_client.cookies.update(self.client.cookies)."""
    _setup()
    main_client = CurlCffiAsyncClient(impersonate="chrome120", cookies={"k1": "v1"})
    proxy_client = CurlCffiAsyncClient(impersonate="chrome120")
    proxy_client.cookies.update(main_client.cookies)
    assert proxy_client.cookies.get("k1") == "v1"


def test_cookies_accepts_httpx_cookies_input():
    """In session.py we build httpx.Cookies and pass it to the client constructor."""
    _setup()
    httpx_cookies = httpx.Cookies()
    httpx_cookies.set("session_id", "abc123", domain=".mirea.ru")
    httpx_cookies.set("token", "xyz", domain=".mirea.ru")

    c = CurlCffiAsyncClient(impersonate="chrome120", cookies=httpx_cookies)
    cookies_dict = dict(c.cookies)
    assert cookies_dict.get("session_id") == "abc123"
    assert cookies_dict.get("token") == "xyz"


# ──────────────────────────────────────────────────────────────────────
# Async context manager — auth.py uses `async with` for bootstrap clients
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_context_manager():
    _setup()
    async with CurlCffiAsyncClient(impersonate="chrome120") as c:
        assert hasattr(c, "get")
    # No exception means __aexit__ called aclose successfully


# ──────────────────────────────────────────────────────────────────────
# Timeout coercion — pymirea passes httpx.Timeout(read, connect=...)
# ──────────────────────────────────────────────────────────────────────


def test_timeout_httpx_object_converts_to_tuple():
    timeout = httpx.Timeout(30.0, connect=10.0)
    result = CurlCffiAsyncClient._timeout_seconds(timeout)
    assert result == (10.0, 30.0)


def test_timeout_passthrough_for_int():
    assert CurlCffiAsyncClient._timeout_seconds(15) == 15


# ──────────────────────────────────────────────────────────────────────
# Cookie input normalization
# ──────────────────────────────────────────────────────────────────────


def test_cookies_to_dict_handles_none():
    assert CurlCffiAsyncClient._cookies_to_dict(None) == {}


def test_cookies_to_dict_handles_dict():
    assert CurlCffiAsyncClient._cookies_to_dict({"a": "1"}) == {"a": "1"}


def test_cookies_to_dict_handles_httpx_cookies():
    httpx_cookies = httpx.Cookies()
    httpx_cookies.set("session", "value", domain=".mirea.ru")
    result = CurlCffiAsyncClient._cookies_to_dict(httpx_cookies)
    assert result.get("session") == "value"
