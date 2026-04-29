"""HTTP client factory.

When ``tls_impersonate`` is set in :class:`Config`, returns a
``curl_cffi``-based async client with the requested TLS fingerprint
(Chrome/Safari/Firefox). Otherwise returns a stock ``httpx.AsyncClient``.

The wrapper is implemented in :mod:`pymirea._http_cffi` and is imported
lazily so that ``curl_cffi`` is not a hard dependency — install it via
``pip install pymirea[tls]`` only if you need TLS spoofing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from ._settings import settings

logger = logging.getLogger(__name__)


def make_async_client(
    *,
    headers: Optional[dict] = None,
    cookies: Optional[Any] = None,
    timeout: Optional[Any] = None,
    limits: Optional[Any] = None,
    transport: Optional[Any] = None,
    proxy: Optional[str] = None,
    follow_redirects: bool = True,
) -> Any:
    """Create an async HTTP client honoring ``Config.tls_impersonate``.

    Returns either ``httpx.AsyncClient`` (default) or a wrapper around
    ``curl_cffi.requests.AsyncSession`` with matching API surface.
    """
    impersonate: Optional[str] = None
    try:
        impersonate = settings.tls_impersonate
    except (RuntimeError, AttributeError):
        # Not configured yet, or older Config without the field — fall through
        impersonate = None

    if impersonate:
        # Lazy import — keeps curl_cffi optional.
        from ._http_cffi import CurlCffiAsyncClient

        logger.debug("make_async_client: backend=curl_cffi, impersonate=%s", impersonate)
        return CurlCffiAsyncClient(
            impersonate=impersonate,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            proxy=proxy,
            follow_redirects=follow_redirects,
        )

    logger.debug("make_async_client: backend=httpx (no TLS impersonation)")
    return httpx.AsyncClient(
        headers=headers,
        cookies=cookies,
        timeout=timeout if timeout is not None else httpx.Timeout(15.0, connect=10.0),
        transport=transport,
        proxy=proxy,
        follow_redirects=follow_redirects,
    )
