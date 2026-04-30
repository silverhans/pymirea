"""Unified async client.

A façade over the existing service classes (:class:`MireaAuth`,
:class:`MireaACS`, :class:`MireaGrades`, :class:`MireaAPI`,
:class:`MireaEsports`) with one consolidated lifetime. Use it as an
async context manager so all underlying HTTP pools (httpx + curl_cffi)
are closed deterministically::

    async with pymirea.Client(session_cookies=cookies) as c:
        await c.ensure_fresh_token()
        grades = await c.grades.get_grades()
        events = await c.acs.get_today_events()

The existing per-service classes still work standalone and are
unaffected by this façade — Client is purely additive.

Service objects are created lazily on first attribute access, so
constructing a ``Client`` is cheap and you only pay for the services
you actually use.
"""

from __future__ import annotations

import logging
from typing import Optional

from .acs import MireaACS
from .auth import MireaAuth
from .esports import MireaEsports
from .grades import MireaGrades
from .session import MireaAPI
from .tokens import ensure_fresh_token as _ensure_fresh_token

logger = logging.getLogger(__name__)


class Client:
    """Unified pymirea client.

    Pass ``session_cookies`` to use the authenticated services
    (grades/acs/attendance). Without it, only :attr:`auth` is meaningful
    — use it for the login flow, then construct a new ``Client`` with
    the resulting cookies.
    """

    def __init__(self, *, session_cookies: Optional[dict] = None) -> None:
        # Note: we DON'T copy the dict — the services mutate it in-place
        # (refresh writes new tokens), and the caller often wants those
        # mutations visible (so they can persist the rotated session).
        self._session_cookies: dict = session_cookies if session_cookies is not None else {}
        self._auth: Optional[MireaAuth] = None
        self._acs: Optional[MireaACS] = None
        self._grades: Optional[MireaGrades] = None
        self._attendance: Optional[MireaAPI] = None
        self._esports: Optional[MireaEsports] = None
        self._closed = False

    @property
    def session_cookies(self) -> dict:
        """The mutable cookies dict shared across all sub-services."""
        return self._session_cookies

    @property
    def auth(self) -> MireaAuth:
        if self._auth is None:
            self._auth = MireaAuth()
        return self._auth

    @property
    def acs(self) -> MireaACS:
        if self._acs is None:
            self._acs = MireaACS(self._session_cookies)
        return self._acs

    @property
    def grades(self) -> MireaGrades:
        if self._grades is None:
            self._grades = MireaGrades(self._session_cookies)
        return self._grades

    @property
    def attendance(self) -> MireaAPI:
        if self._attendance is None:
            self._attendance = MireaAPI(session_cookies=self._session_cookies)
        return self._attendance

    @property
    def esports(self) -> MireaEsports:
        if self._esports is None:
            self._esports = MireaEsports()
        return self._esports

    async def ensure_fresh_token(self, *, buffer_s: int = 30) -> bool:
        """Convenience wrapper around :func:`pymirea.ensure_fresh_token` that
        targets this client's session cookies."""
        return await _ensure_fresh_token(self._session_cookies, buffer_s=buffer_s)

    async def close(self) -> None:
        """Close all instantiated services. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for name, svc in (
            ("auth", self._auth),
            ("acs", self._acs),
            ("grades", self._grades),
            ("attendance", self._attendance),
            ("esports", self._esports),
        ):
            if svc is None:
                continue
            try:
                await svc.close()
            except Exception as e:
                logger.warning("error closing %s: %s", name, e)

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


__all__ = ["Client"]
