"""Rate limiter configuration.

Separated from main.py to avoid circular imports.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from remembra.config import get_settings


def get_key_func(request):
    """
    Get rate limit key - prefer API key over IP.

    This allows per-user rate limiting when authenticated,
    falling back to IP for unauthenticated requests.
    """
    # Try to get API key from header
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        # Use first 8 chars of key as identifier (don't log full key)
        return f"key:{api_key[:8]}"

    # Fall back to IP address
    return get_remote_address(request)


# Create the limiter instance
settings = get_settings()
limiter = Limiter(
    key_func=get_key_func,
    enabled=settings.rate_limit_enabled,
    storage_uri=settings.rate_limit_storage if settings.rate_limit_storage != "memory" else None,
    headers_enabled=True,  # Expose X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
)
