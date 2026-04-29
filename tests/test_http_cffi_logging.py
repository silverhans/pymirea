"""Tests for logging behavior in the TLS impersonation path.

When TLS spoofing is canaried in production, these log signals are how
operators detect "wrapper is active", "wrapper has bugs", or "we missed
an exception type". Without them, silent failures in the canary become
indistinguishable from bad luck."""

from __future__ import annotations

import base64
import logging
import secrets
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from pymirea import Config, configure

curl_cffi = pytest.importorskip("curl_cffi")

from curl_cffi import exceptions as cce  # noqa: E402

from pymirea._http_cffi import CurlCffiAsyncClient  # noqa: E402


def _setup() -> None:
    configure(Config(
        session_keys=base64.b64encode(secrets.token_bytes(32)).decode(),
        tls_impersonate="chrome120",
    ))


# ──────────────────────────────────────────────────────────────────────
# Init log: confirms TLS is actually active in production
# ──────────────────────────────────────────────────────────────────────


def test_constructor_logs_info_on_init(caplog):
    """One INFO line per client — operators check for this in logs to
    confirm TLS spoofing is active. Without it, the flag could silently
    be unset and we'd never know."""
    _setup()
    with caplog.at_level(logging.INFO, logger="pymirea._http_cffi"):
        CurlCffiAsyncClient(impersonate="chrome120")

    init_logs = [r for r in caplog.records if "TLS impersonation enabled" in r.message]
    assert len(init_logs) == 1
    assert init_logs[0].levelno == logging.INFO
    assert "chrome120" in init_logs[0].message
    assert "curl_cffi" in init_logs[0].message


def test_constructor_log_includes_impersonate_value(caplog):
    """Different profiles should show in the log message verbatim."""
    _setup()
    with caplog.at_level(logging.INFO, logger="pymirea._http_cffi"):
        CurlCffiAsyncClient(impersonate="safari17")

    init_logs = [r for r in caplog.records if "TLS impersonation enabled" in r.message]
    assert len(init_logs) == 1
    assert "safari17" in init_logs[0].message


# ──────────────────────────────────────────────────────────────────────
# Exception translation logging
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_translated_exception_logs_at_debug(caplog):
    """When we translate a known curl_cffi exception, log at DEBUG with
    both source and target type — useful for confirming translation is
    happening when diagnosing weird production failures."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")
    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = cce.Timeout("read timeout")
        with caplog.at_level(logging.DEBUG, logger="pymirea._http_cffi"):
            with pytest.raises(Exception):
                await c.get("https://x.test/")

    debug_logs = [r for r in caplog.records if "translated curl_cffi exception" in r.message]
    assert len(debug_logs) >= 1
    assert "Timeout" in debug_logs[0].message
    assert "TimeoutException" in debug_logs[0].message


@pytest.mark.asyncio
async def test_untranslated_curl_cffi_exception_logs_warning(caplog):
    """If curl_cffi raises a type we DON'T know how to translate (e.g.
    a future version adds a new exception class), warn loudly so we can
    extend _translate_exception."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")

    # Fabricate an exception that lives in curl_cffi.* but isn't in our
    # translation table.
    class FutureCurlException(Exception):
        pass

    FutureCurlException.__module__ = "curl_cffi.exceptions"

    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = FutureCurlException("new exception type from future curl_cffi")
        with caplog.at_level(logging.WARNING, logger="pymirea._http_cffi"):
            with pytest.raises(FutureCurlException):
                await c.get("https://x.test/")

    warnings = [r for r in caplog.records if "untranslated curl_cffi exception" in r.message]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "FutureCurlException" in warnings[0].message


@pytest.mark.asyncio
async def test_unrelated_exception_does_not_log_warning(caplog):
    """ValueError from caller code propagates without the wrapper crying
    wolf about untranslated curl_cffi exceptions — that warning is
    reserved for actual curl_cffi escapes."""
    _setup()
    c = CurlCffiAsyncClient(impersonate="chrome120")

    with mock.patch.object(c._session, "request", new_callable=AsyncMock) as req:
        req.side_effect = ValueError("not a network error")
        with caplog.at_level(logging.WARNING, logger="pymirea._http_cffi"):
            with pytest.raises(ValueError):
                await c.get("https://x.test/")

    warnings = [r for r in caplog.records if "untranslated curl_cffi exception" in r.message]
    assert len(warnings) == 0


# ──────────────────────────────────────────────────────────────────────
# Factory backend logging
# ──────────────────────────────────────────────────────────────────────


def test_factory_logs_curl_cffi_backend_at_debug(caplog):
    """Factory logs which transport it picked — useful for debugging
    why a particular client behaves a certain way."""
    _setup()
    from pymirea._http import make_async_client

    with caplog.at_level(logging.DEBUG, logger="pymirea._http"):
        make_async_client()

    debug_logs = [r for r in caplog.records if "backend=curl_cffi" in r.message]
    assert len(debug_logs) == 1


def test_factory_logs_httpx_backend_when_no_impersonation(caplog):
    configure(Config(
        session_keys=base64.b64encode(secrets.token_bytes(32)).decode(),
    ))
    from pymirea._http import make_async_client

    with caplog.at_level(logging.DEBUG, logger="pymirea._http"):
        make_async_client()

    debug_logs = [r for r in caplog.records if "backend=httpx" in r.message]
    assert len(debug_logs) == 1
