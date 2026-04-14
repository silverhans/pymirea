"""pymirea — async Python client for Мирэа LKS (Личный Кабинет Студента).

Covers login + 2FA (Keycloak SSO), schedule, grades, attendance,
ACS entry/exit events, e-sports registration, and session-token
encryption (Fernet+HKDF).

Quick start::

    from pymirea import Config, configure, MireaAuth

    configure(Config(session_keys="base64-hkdf-seed"))

    auth = MireaAuth()
    result = await auth.login("s12345@edu.mirea.ru", "password")
    if result.challenge:
        # 2FA required — prompt user for OTP, then:
        result = await auth.complete_2fa(result.challenge, "123456")

    # result.tokens carries access/refresh/id tokens for subsequent API use.
"""

from ._settings import configure
from .acs import MireaACS
from .auth import AuthChallenge, AuthResult, MireaAuth
from .config import Config
from .crypto import SessionCrypto
from .esports import MireaEsports
from .session import MireaAPI
from .tokens import (
    get_authorization_header,
    get_token_age_seconds,
    try_refresh_tokens,
)

__all__ = [
    "Config",
    "configure",
    "MireaAPI",
    "MireaACS",
    "MireaAuth",
    "MireaEsports",
    "SessionCrypto",
    "AuthChallenge",
    "AuthResult",
    "get_authorization_header",
    "get_token_age_seconds",
    "try_refresh_tokens",
]

__version__ = "0.1.1"
