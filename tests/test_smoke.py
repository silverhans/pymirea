"""Smoke-тесты, которые можно прогнать без живого МИРЭА-аккаунта.
Проверяют что пакет импортируется, ``configure``-shim работает и
``SessionCrypto`` корректно шифрует/расшифровывает. Реальные
интеграционные тесты против pulse.mirea.ru живут в downstream-приложениях."""

import base64
import secrets

import pytest

import pymirea
from pymirea import Config, SessionCrypto, configure


def _fresh_session_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def test_public_api_exposes_expected_names():
    expected = {
        "Config",
        "configure",
        "MireaAuth",
        "MireaAPI",
        "MireaACS",
        "MireaEsports",
        "SessionCrypto",
        "AuthChallenge",
        "AuthResult",
        "get_authorization_header",
        "get_token_age_seconds",
        "try_refresh_tokens",
    }
    missing = expected - set(pymirea.__all__)
    assert not missing, f"missing exports: {missing}"


def test_settings_proxy_raises_before_configure(monkeypatch):
    # Reset the module-level _cfg so the proxy raises on access
    monkeypatch.setattr(pymirea._settings, "_cfg", None)
    from pymirea._settings import settings

    with pytest.raises(RuntimeError, match="pymirea не сконфигурирован"):
        _ = settings.session_keys


def test_configure_then_settings_resolves():
    configure(Config(session_keys=_fresh_session_key()))
    from pymirea._settings import settings

    # Resolves to the injected Config now
    assert isinstance(settings.session_keys, str)
    assert settings.session_keys


def test_session_crypto_roundtrips_cookie_dict():
    key = _fresh_session_key()
    configure(Config(session_keys=key))
    crypto = SessionCrypto(key)
    cookies = {"sso_session": "abc.def.ghi", "kc_token": "xyz"}
    encrypted = crypto.encrypt_session(cookies)
    assert isinstance(encrypted, str)
    assert "abc.def.ghi" not in encrypted  # actually encrypted

    decrypted = crypto.decrypt_session(encrypted)
    assert decrypted == cookies


def test_session_crypto_decrypt_rejects_garbage():
    key = _fresh_session_key()
    crypto = SessionCrypto(key)
    assert crypto.decrypt_session("not-a-valid-token") is None


def test_get_authorization_header_returns_none_for_empty():
    from pymirea import get_authorization_header

    assert get_authorization_header(None) is None
    assert get_authorization_header({}) is None


def test_auth_result_tokens_alias_matches_cookies():
    """README/examples reference ``result.tokens`` — must stay in sync with ``cookies``."""
    from pymirea import AuthResult

    cookies = {"access_token": "abc", "refresh_token": "def"}
    res = AuthResult(success=True, message="ok", cookies=cookies)
    assert res.tokens is res.cookies
    assert res.tokens == cookies

    empty = AuthResult(success=False, message="nope")
    assert empty.tokens is None


def test_complete_2fa_alias_exists():
    """README/examples call ``auth.complete_2fa(...)`` — must be a real method."""
    from pymirea import MireaAuth

    assert hasattr(MireaAuth, "complete_2fa")
    assert callable(MireaAuth.complete_2fa)


def test_session_crypto_logs_warning_on_corrupt_payload(caplog):
    """If decryption succeeds but JSON is mangled — log a warning, return None."""
    import logging

    key = _fresh_session_key()
    crypto = SessionCrypto(key)

    # Encrypt non-JSON garbage with a key Fernet will accept on decryption
    fernet = crypto._fernets[0]
    bad_token = fernet.encrypt(b"\xff\xfe not json").decode("ascii")

    with caplog.at_level(logging.WARNING, logger="pymirea.crypto"):
        result = crypto.decrypt_session(bad_token)

    assert result is None
    assert any("payload is corrupt" in r.message for r in caplog.records)
