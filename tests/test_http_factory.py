"""Tests for the make_async_client factory.

Verifies the default httpx path. The curl_cffi path is exercised only
when [tls] extras are installed — gated behind importorskip."""

from __future__ import annotations

import base64
import secrets

import httpx
import pytest

from pymirea import Config, configure
from pymirea._http import make_async_client


def _setup(tls: str | None = None) -> None:
    configure(Config(
        session_keys=base64.b64encode(secrets.token_bytes(32)).decode(),
        tls_impersonate=tls,
    ))


def test_default_returns_httpx_client():
    _setup(tls=None)
    client = make_async_client()
    assert isinstance(client, httpx.AsyncClient)


def test_default_passes_through_basic_args():
    _setup(tls=None)
    client = make_async_client(
        headers={"X-Test": "1"},
        timeout=httpx.Timeout(5.0),
    )
    assert isinstance(client, httpx.AsyncClient)
    assert client.headers.get("X-Test") == "1"


def test_tls_impersonate_returns_curl_cffi_wrapper():
    pytest.importorskip("curl_cffi")
    _setup(tls="chrome120")
    from pymirea._http_cffi import CurlCffiAsyncClient
    client = make_async_client()
    assert isinstance(client, CurlCffiAsyncClient)


def test_tls_impersonate_wrapper_has_required_api():
    pytest.importorskip("curl_cffi")
    _setup(tls="chrome120")
    client = make_async_client()
    assert hasattr(client, "get")
    assert hasattr(client, "post")
    assert hasattr(client, "aclose")
    assert hasattr(client, "cookies")


def test_config_default_tls_impersonate_is_none():
    cfg = Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode())
    assert cfg.tls_impersonate is None
