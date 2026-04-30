"""Internal hook dispatcher.

User-provided observability callbacks (set on :class:`pymirea.Config`) are
called from pymirea hot paths via the helpers in this module. Two
guarantees apply:

1. **Hook errors never propagate.** If the user's callback raises, we log
   a warning and swallow it. Observability must never break business logic.
2. **Sync and async hooks both work.** If the callback returns a coroutine,
   we await it; otherwise it's called as a regular function.

Hook firing is best-effort: if pymirea isn't configured yet, we simply
skip — useful during library import and shutdown when state is partial.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_hook(name: str) -> Any:
    """Return the configured hook callable, or None."""
    try:
        from ._settings import settings
        return getattr(settings, name, None)
    except Exception:
        # Config not loaded yet, or attribute missing — silently skip.
        return None


async def dispatch(name: str, *args: Any, **kwargs: Any) -> None:
    """Call hook ``name`` if set, with the given args. Awaits coroutine
    return values. Swallows any exception from the hook itself."""
    hook = _get_hook(name)
    if hook is None:
        return
    try:
        result = hook(*args, **kwargs)
        if inspect.iscoroutine(result):
            await result
    except Exception as e:
        logger.warning("pymirea hook %r raised %s; ignoring", name, type(e).__name__)


def dispatch_sync(name: str, *args: Any, **kwargs: Any) -> None:
    """Sync version — for paths that aren't async (rare). Coroutine return
    values from hooks are NOT awaited; callers should prefer ``dispatch``."""
    hook = _get_hook(name)
    if hook is None:
        return
    try:
        result = hook(*args, **kwargs)
        if inspect.iscoroutine(result):
            # Caller is sync — close the unawaited coroutine to avoid
            # "coroutine was never awaited" warnings.
            result.close()
    except Exception as e:
        logger.warning("pymirea hook %r raised %s; ignoring", name, type(e).__name__)
