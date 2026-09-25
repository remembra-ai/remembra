"""Rate limiter configuration.

Separated from main.py to avoid circular imports.
"""

from typing import Any

from slowapi import Limiter

from remembra.config import get_settings


def get_key_func(request: Any) -> str:
    """
    Rate-limit bucket for a request.

    Uses the *validated* account identity recorded by the auth dependency
    (``request.state.rate_limit_identity``) when the route is authenticated.
    Otherwise falls back to the real client IP, resolved through the trusted
    proxy list. A raw ``X-API-Key`` header is never used: it is attacker
    controlled, so keying on it let anyone mint fresh buckets per request and
    brute-force login/TOTP without limit.
    """
    identity = getattr(getattr(request, "state", None), "rate_limit_identity", None)
    if identity:
        return str(identity)

    from remembra.auth.middleware import get_client_ip

    return f"ip:{get_client_ip(request)}"


# Create the limiter instance
# NOTE: headers_enabled=False because we use custom middleware for header injection.
# Setting True here causes errors on endpoints without Response parameter.
settings = get_settings()
limiter = Limiter(
    key_func=get_key_func,
    enabled=settings.rate_limit_enabled,
    storage_uri=settings.rate_limit_storage if settings.rate_limit_storage != "memory" else None,
    headers_enabled=False,
)
