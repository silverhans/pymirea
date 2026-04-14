"""Tests for MireaAPI.extract_token_from_qr and mark_attendance (HTTP path)."""

from __future__ import annotations

import base64
import secrets

import pytest

from pymirea import Config, MireaAPI, configure


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


# ---------------------------------------------------------------------------
# extract_token_from_qr — pure function, no HTTP
# ---------------------------------------------------------------------------


def test_extract_token_raw_uuid():
    token, err = MireaAPI.extract_token_from_qr("abcdef12-3456-7890-abcd-ef1234567890")
    assert err is None
    assert token == "abcdef12-3456-7890-abcd-ef1234567890"


def test_extract_token_uuid_case_insensitive():
    token, err = MireaAPI.extract_token_from_qr("ABCDEF12-3456-7890-ABCD-EF1234567890")
    assert err is None
    assert token == "ABCDEF12-3456-7890-ABCD-EF1234567890"


def test_extract_token_url_with_token():
    token, err = MireaAPI.extract_token_from_qr("https://pulse.mirea.ru/selfapprove?token=abc-123")
    assert err is None
    assert token == "abc-123"


def test_extract_token_all_allowed_domains():
    for host in ("pulse.mirea.ru", "attendance-app.mirea.ru", "attendance.mirea.ru", "att.mirea.ru"):
        token, err = MireaAPI.extract_token_from_qr(f"https://{host}/selfapprove?token=t-{host}")
        assert err is None, f"{host} should be allowed"
        assert token == f"t-{host}"


def test_extract_token_rejects_unknown_domain():
    token, err = MireaAPI.extract_token_from_qr("https://evil.example.com/selfapprove?token=xyz")
    assert token is None
    assert err is not None


def test_extract_token_url_without_scheme():
    token, err = MireaAPI.extract_token_from_qr("pulse.mirea.ru/selfapprove?token=noScheme")
    assert err is None
    assert token == "noScheme"


def test_extract_token_url_with_port():
    token, err = MireaAPI.extract_token_from_qr("https://pulse.mirea.ru:8080/selfapprove?token=withPort")
    assert err is None
    assert token == "withPort"


def test_extract_token_url_missing_token_param():
    token, err = MireaAPI.extract_token_from_qr("https://pulse.mirea.ru/selfapprove?foo=bar")
    assert token is None
    assert err is not None


def test_extract_token_empty_input():
    token, err = MireaAPI.extract_token_from_qr("")
    assert token is None
    assert err is not None


def test_extract_token_whitespace_stripped():
    token, err = MireaAPI.extract_token_from_qr("  https://pulse.mirea.ru/selfapprove?token=trim  ")
    assert err is None
    assert token == "trim"


def test_extract_token_garbage_returns_error():
    token, err = MireaAPI.extract_token_from_qr("this is not a QR at all")
    assert token is None
    assert err is not None


# ---------------------------------------------------------------------------
# mark_attendance — HTTP selfapprove path (gRPC path is tested separately)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_attendance_invalid_qr_returns_failure_result():
    _setup()
    api = MireaAPI(session_cookies={".AspNetCore.Cookies": "x"})
    try:
        result = await api.mark_attendance("not a valid qr")
    finally:
        await api.close()
    assert result.success is False
    assert result.message


@pytest.mark.asyncio
async def test_mark_attendance_empty_qr_returns_failure():
    _setup()
    api = MireaAPI(session_cookies={".AspNetCore.Cookies": "x"})
    try:
        result = await api.mark_attendance("")
    finally:
        await api.close()
    assert result.success is False


# ---------------------------------------------------------------------------
# export_cookies / import_cookies — serialization roundtrip
# ---------------------------------------------------------------------------


def test_export_import_cookies_roundtrip():
    _setup()
    original = {".AspNetCore.Cookies": "blob", "KEYCLOAK_IDENTITY": "kcid"}
    api = MireaAPI(session_cookies=original)
    exported = api.export_cookies()  # JSON string
    assert isinstance(exported, str)

    imported = MireaAPI.import_cookies(exported)
    assert imported.get(".AspNetCore.Cookies") == "blob"
    assert imported.get("KEYCLOAK_IDENTITY") == "kcid"


def test_import_cookies_returns_empty_on_garbage():
    assert MireaAPI.import_cookies("not json at all") == {}
    assert MireaAPI.import_cookies("") == {}
