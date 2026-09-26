"""Rate limiter configuration.

Separated from main.py to avoid circular imports.
"""

from typing import Any

from slowapi import Limiter

from remembra.cloud.ratelimit import storage_options_for, storage_uri_from_setting
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
# in_memory_fallback_enabled: when a shared backend (redis://) stops answering,
# slowapi marks it dead, keeps enforcing the same route limits in process
# memory and re-checks the backend with backoff; swallow_errors is the last
# resort so a limiter failure never turns a request into a 500.
settings = get_settings()
_storage_uri = storage_uri_from_setting(settings.rate_limit_storage)
limiter = Limiter(
    key_func=get_key_func,
    enabled=settings.rate_limit_enabled,
    storage_uri=_storage_uri,
    storage_options=storage_options_for(_storage_uri),
    headers_enabled=False,
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)
