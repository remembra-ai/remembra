"""TTL (Time-To-Live) parsing and expiration calculation."""

from datetime import datetime, timedelta

from remembra.client.ttl import parse_ttl_seconds
from remembra.core.time import utcnow


def parse_ttl(ttl_string: str) -> timedelta:
    """
    Parse a TTL string into a timedelta.

    The same format the server reads for a store ``ttl``
    (:mod:`remembra.client.ttl`): a number, decimals allowed, and a unit.

    - "30s" → 30 seconds
    - "5min" → 5 minutes (a bare "5m" is refused: write min or mo)
    - "24h" → 24 hours
    - "1.5d" → 36 hours
    - "2w" → 2 weeks
    - "1mo" or "1M" → 1 month (30 days)
    - "1y" → 1 year (365 days)

    Args:
        ttl_string: TTL in format "<number><unit>"

    Returns:
        timedelta representing the TTL

    Raises:
        ValueError: If format is invalid

    Examples:
        >>> parse_ttl("30d")
        timedelta(days=30)
        >>> parse_ttl("1y")
        timedelta(days=365)
    """
    if not ttl_string or not ttl_string.strip():
        raise ValueError("TTL string cannot be empty")
    return timedelta(seconds=parse_ttl_seconds(ttl_string))


def calculate_expires_at(
    ttl_string: str | None = None,
    ttl_delta: timedelta | None = None,
    from_time: datetime | None = None,
) -> datetime | None:
    """
    Calculate expiration datetime from TTL.

    Args:
        ttl_string: TTL string like "30d" (takes priority)
        ttl_delta: timedelta directly (used if ttl_string not provided)
        from_time: Base time to calculate from (default: now)

    Returns:
        datetime when the memory expires, or None if no TTL

    Examples:
        >>> calculate_expires_at("7d")
        datetime(2026, 3, 8, ...)  # 7 days from now

        >>> calculate_expires_at(ttl_delta=timedelta(hours=24))
        datetime(2026, 3, 2, ...)  # 24 hours from now
    """
    if ttl_string is None and ttl_delta is None:
        return None

    base_time = from_time or utcnow()

    delta = parse_ttl(ttl_string) if ttl_string else ttl_delta

    if delta is None:
        return None

    return base_time + delta


def ttl_to_seconds(ttl_string: str) -> int:
    """Convert TTL string to seconds."""
    delta = parse_ttl(ttl_string)
    return int(delta.total_seconds())


def format_ttl(delta: timedelta) -> str:
    """
    Format a timedelta as a human-readable TTL string.

    Args:
        delta: timedelta to format

    Returns:
        Formatted string like "7d" or "2w"
    """
    total_seconds = int(delta.total_seconds())

    if total_seconds >= 31536000 and total_seconds % 31536000 == 0:
        return f"{total_seconds // 31536000}y"
    if total_seconds >= 2592000 and total_seconds % 2592000 == 0:
        return f"{total_seconds // 2592000}M"
    if total_seconds >= 604800 and total_seconds % 604800 == 0:
        return f"{total_seconds // 604800}w"
    if total_seconds >= 86400 and total_seconds % 86400 == 0:
        return f"{total_seconds // 86400}d"
    if total_seconds >= 3600 and total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    if total_seconds >= 60 and total_seconds % 60 == 0:
        return f"{total_seconds // 60}m"

    return f"{total_seconds}s"


# Default TTL presets for common use cases
TTL_PRESETS = {
    "session": "24h",  # Temporary session context
    "conversation": "7d",  # Conversation summaries
    "short_term": "30d",  # Short-term memories
    "long_term": "1y",  # Long-term facts
    "permanent": None,  # Never expires
}


def get_preset_ttl(preset_name: str) -> str | None:
    """
    Get a TTL preset by name.

    Available presets:
    - session: 24 hours
    - conversation: 7 days
    - short_term: 30 days
    - long_term: 1 year
    - permanent: Never expires (returns None)
    """
    if preset_name not in TTL_PRESETS:
        raise ValueError(f"Unknown TTL preset: '{preset_name}'. Available presets: {list(TTL_PRESETS.keys())}")
    return TTL_PRESETS[preset_name]
