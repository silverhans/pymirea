"""curl_cffi-based async HTTP client wrapper.

Exposes a subset of ``httpx.AsyncClient`` API so the rest of pymirea can
swap clients via :func:`pymirea._http.make_async_client` without code
changes elsewhere.

Only loaded when ``Config.tls_impersonate`` is set — keeps ``curl_cffi``
out of the default install.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

try:
    from curl_cffi.requests import AsyncSession  # type: ignore[import-not-found]
except ImportError as e:
    raise ImportError(
        "TLS impersonation requires curl_cffi. Install with: pip install pymirea[tls]"
    ) from e


class CurlCffiAsyncClient:
    """Async HTTP client backed by ``curl_cffi`` with TLS fingerprint spoofing.

    Mimics the surface of ``httpx.AsyncClient`` that pymirea uses:
    ``get/post/aclose`` and a dict-compatible ``cookies`` property.
    """

    def __init__(
        self,
        *,
        impersonate: str,
        headers: Optional[dict] = None,
        cookies: Optional[Any] = None,
        timeout: Optional[Any] = None,
        proxy: Optional[str] = None,
        follow_redirects: bool = True,
    ) -> None:
        self._follow_redirects = follow_redirects

        kwargs: dict[str, Any] = {"impersonate": impersonate}
        if headers:
            kwargs["headers"] = dict(headers)
        if proxy:
            kwargs["proxy"] = proxy
        if timeout is not None:
            kwargs["timeout"] = self._timeout_seconds(timeout)
        if cookies is not None:
            kwargs["cookies"] = self._cookies_to_dict(cookies)

        self._session = AsyncSession(**kwargs)

    # ─── Public API mirroring httpx.AsyncClient ──────────────────────────

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self._request("POST", url, **kwargs)

    async def aclose(self) -> None:
        # curl_cffi 0.7+ has async close; older versions are sync
        close = getattr(self._session, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    @property
    def cookies(self) -> Any:
        return self._session.cookies

    # ─── Internal helpers ────────────────────────────────────────────────

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        # Translate httpx kwargs → curl_cffi
        if "follow_redirects" in kwargs:
            kwargs["allow_redirects"] = kwargs.pop("follow_redirects")
        else:
            kwargs.setdefault("allow_redirects", self._follow_redirects)

        # httpx uses `content` for raw body bytes; curl_cffi uses `data`.
        if "content" in kwargs:
            kwargs.setdefault("data", kwargs.pop("content"))

        # `cookies` per-request passed as dict
        if "cookies" in kwargs and kwargs["cookies"] is not None:
            kwargs["cookies"] = self._cookies_to_dict(kwargs["cookies"])

        return await self._session.request(method, url, **kwargs)

    @staticmethod
    def _timeout_seconds(timeout: Any) -> Any:
        """Convert httpx.Timeout into something curl_cffi understands."""
        if isinstance(timeout, httpx.Timeout):
            connect = float(timeout.connect or 10.0)
            read = float(timeout.read or 30.0)
            return (connect, read)
        return timeout

    @staticmethod
    def _cookies_to_dict(cookies: Any) -> dict[str, str]:
        """Normalize cookie input to a flat dict[name → value]."""
        if cookies is None:
            return {}
        if isinstance(cookies, dict):
            return dict(cookies)
        # httpx.Cookies, RequestsCookieJar — both expose dict-like protocol
        try:
            return dict(cookies)
        except Exception:
            return {}
