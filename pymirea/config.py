"""Configuration for pymirea. Apps pass this into the client instead of
importing a global `settings` module — decouples the library from any
specific host application."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Config:
    """pymirea runtime configuration.

    `session_keys` is the only required field — a base64-encoded seed used
    by HKDF to derive the Fernet key that encrypts session tokens at rest.
    Generate one with::

        python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

    Install::

        pip install pymirea

    Usage::

        from pymirea import Config, configure
        configure(Config(session_keys="..."))
    """

    session_keys: str
    """Base64-encoded HKDF seed (≥32 bytes of entropy)."""

    mirea_proxy: Optional[str] = None
    """Optional HTTP/SOCKS proxy URL for pulse.mirea.ru (datacenter-blocked)."""

    legacy_bot_token: Optional[str] = None
    """Legacy HMAC secret still honoured by decrypt_session() for migration."""

    # Optional Oplexx-style fast-path Go binary for bulk attendance-detail
    # processing. Safe to leave at defaults (disabled) — pymirea falls back
    # to the pure-Python path.
    attendance_core_enabled: bool = False
    attendance_core_shadow: bool = False
    attendance_core_bin: str = ""
    attendance_core_timeout_s: float = 5.0

    request_timeout_s: float = 15.0
    """Per-request timeout for Мирэа HTTP calls."""

    breaker_failure_threshold: int = 5
    """Circuit-breaker: consecutive failures before opening."""

    breaker_recovery_s: float = 30.0
    """Circuit-breaker: how long to stay open before a half-open probe."""
