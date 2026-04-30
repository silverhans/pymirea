from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from .auth import MireaAuth
from .exceptions import MireaSessionExpired

logger = logging.getLogger(__name__)

# Proactive refresh: attempt refresh if token older than 7 days
PROACTIVE_REFRESH_AGE_S = 7 * 24 * 3600
# Background refresh: process users with tokens older than 14 days
BACKGROUND_REFRESH_AGE_S = 14 * 24 * 3600

# Single-flight refresh: if a refresh completed within the last STAMPEDE_WINDOW_S
# seconds, subsequent waiters skip making another HTTP call. Prevents 5 concurrent
# requests from each consuming a Keycloak refresh-token rotation slot.
_STAMPEDE_WINDOW_S = 5

# Per-session refresh locks. Keyed by id(session_cookies) — same dict instance
# means same lock. Locks accumulate over the process lifetime (each is ~100 bytes,
# negligible). Distinct dicts that happen to reuse a freed id() will still
# serialize correctly against themselves; the worst case is one false-share with
# a long-gone session, which is harmless.
_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock(session_cookies: dict) -> asyncio.Lock:
    key = id(session_cookies)
    lock = _refresh_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[key] = lock
    return lock


def get_token_age_seconds(session_cookies: dict | None) -> int | None:
    """Return seconds since last token refresh, or None if unknown."""
    if not session_cookies:
        return None
    refreshed_at = session_cookies.get("__token_refreshed_at")
    if not refreshed_at:
        return None
    try:
        return int(time.time()) - int(refreshed_at)
    except (ValueError, TypeError):
        return None


def get_authorization_header(session_cookies: dict | None) -> str | None:
    """
    Build Authorization header value from stored Keycloak tokens (if present).
    Returns e.g. "Bearer <access_token>" or None.
    """
    cookies = session_cookies or {}
    access_token = (cookies.get("access_token") or "").strip()
    if not access_token:
        return None
    token_type = (cookies.get("token_type") or "Bearer").strip() or "Bearer"
    return f"{token_type} {access_token}"


def get_token_exp(session_cookies: dict | None) -> int | None:
    """Decode the JWT ``exp`` claim from ``access_token`` and return it as a
    Unix timestamp. Returns ``None`` if the token is missing, malformed, or
    has no ``exp`` claim.

    Signature is **not verified** — we only need to read the claim, and we
    don't have Keycloak's public key locally. The MIREA server still
    validates signatures upstream.
    """
    if not session_cookies:
        return None
    token = (session_cookies.get("access_token") or "").strip()
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        # JWT base64url, may have stripped padding
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        claims = json.loads(decoded)
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def try_refresh_tokens(session_cookies: dict | None) -> bool:
    """
    Best-effort refresh for Keycloak tokens. Updates `session_cookies` in-place.
    Returns True on successful refresh, otherwise False.

    Concurrent calls with the same ``session_cookies`` dict are serialized: only
    the first caller hits Keycloak, the rest reuse the freshly-rotated tokens.
    This prevents the refresh-token rotation race (Keycloak invalidates
    older refresh tokens on each rotation, so 5 parallel refreshes leave 4
    sessions broken).
    """
    if not session_cookies:
        return False
    refresh_token = (session_cookies.get("refresh_token") or "").strip()
    if not refresh_token:
        return False

    lock = _get_refresh_lock(session_cookies)
    async with lock:
        # Double-check: another waiter may have already refreshed for us.
        last = session_cookies.get("__token_refreshed_at")
        if last:
            try:
                if (int(time.time()) - int(last)) < _STAMPEDE_WINDOW_S:
                    return True
            except (ValueError, TypeError):
                pass

        # Re-read refresh_token in case it rotated while we waited.
        refresh_token = (session_cookies.get("refresh_token") or "").strip()
        if not refresh_token:
            return False

        auth = MireaAuth()
        try:
            tokens = await auth.refresh_tokens(refresh_token)
        finally:
            try:
                await auth.close()
            except Exception:
                pass

        if not tokens:
            logger.warning("token refresh failed — no tokens returned")
            return False

        session_cookies.update(tokens)
        session_cookies["__token_refreshed_at"] = int(time.time())
        return True


async def ensure_fresh_token(
    session_cookies: dict | None,
    *,
    buffer_s: int = 30,
) -> bool:
    """Make sure ``access_token`` will still be valid for at least ``buffer_s``
    more seconds. If not, refresh proactively before the next API call.

    Returns ``True`` if the token is fresh on exit (either was already, or was
    successfully refreshed). Returns ``False`` if no JWT/exp could be parsed
    and there's nothing actionable to do.

    Raises:
        MireaSessionExpired: token is expired or near-expired AND refresh
            failed. The session needs the user to re-authenticate.

    This helper is **opt-in**: existing pymirea code paths do not call it.
    Call it before a batch of API requests to skip the wasted "request →
    401 → refresh → retry" round-trip when the token is already known stale.
    """
    if not session_cookies:
        return False
    exp = get_token_exp(session_cookies)
    if exp is None:
        # Can't make a decision without exp claim; let downstream 401 handle it.
        return False
    now = int(time.time())
    if exp - now > buffer_s:
        return True

    refreshed = await try_refresh_tokens(session_cookies)
    if not refreshed:
        raise MireaSessionExpired(
            f"proactive refresh failed (token expired {now - exp}s ago); session unrecoverable"
        )
    return True
