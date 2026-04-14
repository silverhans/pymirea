from __future__ import annotations

import logging
import time

from .auth import MireaAuth

logger = logging.getLogger(__name__)

# Proactive refresh: attempt refresh if token older than 7 days
PROACTIVE_REFRESH_AGE_S = 7 * 24 * 3600
# Background refresh: process users with tokens older than 14 days
BACKGROUND_REFRESH_AGE_S = 14 * 24 * 3600


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


async def try_refresh_tokens(session_cookies: dict | None) -> bool:
    """
    Best-effort refresh for Keycloak tokens. Updates `session_cookies` in-place.
    Returns True on successful refresh, otherwise False.
    """
    if not session_cookies:
        return False
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

