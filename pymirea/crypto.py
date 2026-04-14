"""
Encryption helpers for safe storage of user sessions (MIREA cookies) in the DB.

Recommended configuration:
- Set SESSION_KEYS in .env (comma-separated). First key is used for encryption,
  all keys are tried for decryption. Keep the old key(s) during a rotation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ._settings import settings

logger = logging.getLogger(__name__)

_HKDF_SALT = b"versiti.session.v1"
_HKDF_INFO = b"mirea-session-cookies"


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    items = [part.strip() for part in value.split(",")]
    return [item for item in items if item]


def _looks_like_fernet_key(value: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        return len(raw) == 32
    except Exception:
        return False


def _legacy_fernet_key_from_secret(secret: str) -> bytes:
    """Derive a Fernet key from a plain secret via SHA-256 (legacy/fallback)."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _hkdf_fernet_key_from_secret(secret: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    raw_key = hkdf.derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw_key)


def _fernet_key_from_config_entry(entry: str) -> bytes:
    if _looks_like_fernet_key(entry):
        return entry.encode("ascii")
    return _hkdf_fernet_key_from_secret(entry)


@dataclass(frozen=True)
class _DecryptedSession:
    cookies: dict
    key_index: int


class SessionCrypto:
    """Encryption wrapper with key rotation support."""

    def __init__(self, session_keys: str | None = None, *, legacy_bot_token: str | None = None):
        keys: list[bytes] = []

        for entry in _split_csv(session_keys):
            try:
                key = _fernet_key_from_config_entry(entry)
            except Exception:
                continue
            if key not in keys:
                keys.append(key)

        # Always include legacy key as a decryption fallback.
        if legacy_bot_token:
            legacy_key = _legacy_fernet_key_from_secret(legacy_bot_token)
            if legacy_key not in keys:
                keys.append(legacy_key)

        if not keys:
            raise RuntimeError("No session encryption keys available (configure SESSION_KEYS).")

        self._fernets = [Fernet(k) for k in keys]
        self._primary = self._fernets[0]

    def encrypt_session(self, cookies: dict) -> str:
        json_data = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
        encrypted = self._primary.encrypt(json_data.encode("utf-8"))
        return encrypted.decode("ascii")

    def _decrypt_with_key_index(self, encrypted_data: str) -> _DecryptedSession | None:
        if not encrypted_data:
            return None

        token = encrypted_data.encode("ascii", errors="ignore")
        for idx, f in enumerate(self._fernets):
            try:
                decrypted = f.decrypt(token)
                cookies = json.loads(decrypted.decode("utf-8"))
                if isinstance(cookies, dict):
                    return _DecryptedSession(cookies=cookies, key_index=idx)
                return None
            except InvalidToken:
                continue
            except Exception:
                return None
        return None

    def decrypt_session(self, encrypted_data: str) -> dict | None:
        res = self._decrypt_with_key_index(encrypted_data)
        return res.cookies if res else None

    def decrypt_session_for_db(self, encrypted_data: str) -> tuple[dict | None, str | None]:
        res = self._decrypt_with_key_index(encrypted_data)
        if not res:
            return None, None
        rotated = None
        if res.key_index != 0:
            rotated = self.encrypt_session(res.cookies)
        return res.cookies, rotated


_crypto: SessionCrypto | None = None


def get_crypto() -> SessionCrypto:
    global _crypto
    if _crypto is None:
        if not settings.session_keys:
            raise RuntimeError(
                "SESSION_KEYS is not configured. "
                "Set SESSION_KEYS in .env (comma-separated Fernet keys or passphrases)."
            )
        logger.info("Session encryption: SESSION_KEYS configured (rotation enabled).")
        _crypto = SessionCrypto(
            settings.session_keys,
            legacy_bot_token=settings.jwt_secret or None,
        )
    return _crypto
