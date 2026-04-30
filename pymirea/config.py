"""Конфигурация pymirea. Приложения передают её в библиотеку вместо
импорта глобального ``settings``-модуля — это разрывает связь с
конкретным host-приложением."""

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

# Type alias for hook callables: either a sync function or an async one
# returning a coroutine. Implementations swallow any exception, so the hook
# may raise freely without affecting pymirea's hot path.
HookFn = Optional[Callable[..., Union[None, Awaitable[None]]]]


@dataclass(frozen=True)
class Config:
    """Runtime-конфигурация pymirea.

    ``session_keys`` — единственное обязательное поле. Это base64-строка
    HKDF-сида, из которого выводится Fernet-ключ для шифрования
    cookies в БД. Сгенерируйте один раз::

        python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

    Использование::

        from pymirea import Config, configure
        configure(Config(session_keys="..."))
    """

    session_keys: str
    """Base64-строка с HKDF-сидом (≥32 байта энтропии)."""

    mirea_proxy: Optional[str] = None
    """Опциональный HTTP/SOCKS-прокси для pulse.mirea.ru
    (датацентры заблокированы)."""

    legacy_bot_token: Optional[str] = None
    """Старый HMAC-секрет — принимается ``decrypt_session()``-ом для
    миграционного grace-period после ротации ключей."""

    # Опциональный Oplexx-style Go-бинарник для batch-обработки
    # attendance-detail. По умолчанию выключен — pymirea использует
    # чисто-Python-путь.
    attendance_core_enabled: bool = False
    attendance_core_shadow: bool = False
    attendance_core_bin: str = ""
    attendance_core_timeout_s: float = 5.0

    request_timeout_s: float = 15.0
    """Таймаут одного HTTP-запроса к МИРЭА."""

    breaker_failure_threshold: int = 5
    """Circuit-breaker: сколько подряд ошибок до открытия."""

    breaker_recovery_s: float = 30.0
    """Circuit-breaker: сколько держать открытым перед half-open пробой."""

    tls_impersonate: Optional[str] = None
    """Имитировать TLS fingerprint браузера (например, ``"chrome120"``).

    По умолчанию ``None`` — используется стандартный httpx (Python
    fingerprint). Если задано — pymirea подменяет HTTP-клиент на
    ``curl_cffi`` с указанным impersonation-профилем, чтобы JA3/JA4 был
    как у Chrome/Safari/Firefox. Полезно если МИРЭА начнут блокировать
    по TLS-fingerprinting через DDoS-Guard.

    Требует установки extras::

        pip install pymirea[tls]

    Поддерживаемые профили — см. документацию curl_cffi
    (chrome120, safari17, firefox133 и др.)."""

    on_refresh: HookFn = None
    """Optional callback invoked after every token-refresh attempt.

    Receives a dict like::

        {"success": True, "age_s": 1234, "had_refresh_token": True}

    Hook may be sync or async (a returned coroutine is awaited). Exceptions
    from the hook are logged and swallowed — observability never breaks
    request flow. Useful for emitting Prometheus counters / Grafana series
    without parsing pymirea's logs."""

    on_request: HookFn = None
    """Optional callback invoked after every HTTP request to МИРЭА.

    Receives a dict like::

        {"method": "POST", "url": "https://...", "status": 200, "duration_ms": 432}

    Same sync/async + swallow-exception contract as ``on_refresh``."""

    on_error: HookFn = None
    """Optional callback invoked when pymirea catches an exception in a
    critical path (refresh failure, parse error, etc.).

    Receives ``(exception, context_dict)``. Same sync/async + swallow
    contract."""
