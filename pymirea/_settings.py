"""Внутренний shim для ``settings``. Сохраняет синтаксис ``settings.X``,
которым пользуется портированный код, заменяя глобальный объект на
:class:`Config`, переданный через :func:`pymirea.configure`."""

from typing import Optional

from .config import Config

_cfg: Optional[Config] = None


def configure(config: Config) -> None:
    """Связать pymirea с конкретной runtime-конфигурацией. Должно быть
    вызвано ровно один раз при старте приложения, до использования
    любого pymirea-клиента."""
    global _cfg
    _cfg = config


class _Proxy:
    def __getattr__(self, name: str):  # type: ignore[override]
        if _cfg is None:
            raise RuntimeError(
                "pymirea не сконфигурирован: вызовите pymirea.configure(Config(...)) "
                "при старте приложения"
            )
        return getattr(_cfg, name)


settings = _Proxy()
