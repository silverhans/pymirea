"""Internal settings shim. Keeps the ``settings.X`` call-syntax that the
ported code uses, backed by a user-provided :class:`Config` injected via
:func:`pymirea.configure`."""

from typing import Optional

from .config import Config

_cfg: Optional[Config] = None


def configure(config: Config) -> None:
    """Wire pymirea to a concrete runtime configuration. Must be called
    once at application startup before any pymirea client is used."""
    global _cfg
    _cfg = config


class _Proxy:
    def __getattr__(self, name: str):  # type: ignore[override]
        if _cfg is None:
            raise RuntimeError(
                "pymirea not configured: call pymirea.configure(Config(...)) at startup"
            )
        return getattr(_cfg, name)


settings = _Proxy()
