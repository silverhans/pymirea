from __future__ import annotations

from .circuit_breaker import CircuitBreaker


# Keep thresholds conservative: do not penalize the core "scan" flow too aggressively.
_BREAKERS: dict[str, CircuitBreaker] = {
    # Keycloak/SSO pages + token exchange.
    "mirea_sso": CircuitBreaker("mirea_sso", failure_threshold=6, open_cooldown_s=20.0, half_open_max_calls=2),
    # pulse.mirea.ru (HTML selfapprove fallback).
    "mirea_attendance_app": CircuitBreaker(
        "mirea_attendance_app", failure_threshold=7, open_cooldown_s=20.0, half_open_max_calls=2
    ),
    # pulse.mirea.ru gRPC-web endpoints (BRS + selfapprove gRPC).
    "mirea_attendance_grpc": CircuitBreaker(
        "mirea_attendance_grpc", failure_threshold=7, open_cooldown_s=20.0, half_open_max_calls=2
    ),
    # ACS gRPC calls are less critical; fail fast sooner to avoid piling up load.
    "mirea_acs_grpc": CircuitBreaker("mirea_acs_grpc", failure_threshold=5, open_cooldown_s=25.0, half_open_max_calls=1),
    # External schedule aggregator.
    "schedule_api": CircuitBreaker("schedule_api", failure_threshold=5, open_cooldown_s=25.0, half_open_max_calls=1),
}


def get_breaker(name: str) -> CircuitBreaker:
    return _BREAKERS[name]


async def snapshot_all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, breaker in _BREAKERS.items():
        try:
            out[name] = await breaker.snapshot()
        except Exception:
            out[name] = {"name": name, "state": "unknown"}
    return out

