"""pymirea — async-клиент для Пульса МИРЭА.

Покрывает: вход + 2FA (Keycloak SSO), расписание, оценки,
посещаемость, события турникетов (ACS), регистрацию в e-sports,
шифрование cookies (Fernet+HKDF).

Быстрый старт::

    from pymirea import Config, configure, MireaAuth

    configure(Config(session_keys="base64-32-байтная-строка"))

    auth = MireaAuth()
    result = await auth.login("s12345@edu.mirea.ru", "пароль")
    if result.challenge:
        # требуется 2FA — попросите у юзера OTP, потом:
        result = await auth.complete_2fa(result.challenge, "123456")

    # result.tokens — access/refresh/id-токены для последующего API.
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

__version__ = "0.1.2"
