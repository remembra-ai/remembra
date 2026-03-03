"""TTL (Time-To-Live) parsing and expiration calculation."""

import re
from datetime import datetime, timedelta

# TTL format: <number><unit> where unit is s/m/h/d/w/M/y
TTL_PATTERN = re.compile(r'^(\d+)([smhdwMy])$')

# Unit to seconds mapping
UNIT_SECONDS = {
    's': 1,           # seconds
    'm': 60,          # minutes
    'h': 3600,        # hours
    'd': 86400,       # days
    'w': 604800,      # weeks
    'M': 2592000,     # months (30 days)
    'y': 31536000,    # years (365 days)
}


def parse_ttl(ttl_string: str) -> timedelta:
    """
    Parse a TTL string into a timedelta.
    
    Supported formats:
    - "30s" → 30 seconds
    - "5m" → 5 minutes
    - "24h" → 24 hours
    - "7d" → 7 days
    - "2w" → 2 weeks
    - "1M" → 1 month (30 days)
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
    if not ttl_string:
        raise ValueError("TTL string cannot be empty")
    
    ttl_string = ttl_string.strip()
    match = TTL_PATTERN.match(ttl_string)
    
    if not match:
        raise ValueError(
            f"Invalid TTL format: '{ttl_string}'. "
            f"Expected format: <number><unit> where unit is s/m/h/d/w/M/y. "
            f"Examples: '30d', '1y', '24h'"
        )
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if value <= 0:
        raise ValueError(f"TTL value must be positive, got: {value}")
    
    seconds = value * UNIT_SECONDS[unit]
    return timedelta(seconds=seconds)


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
    
    base_time = from_time or datetime.utcnow()
    
    if ttl_string:
        delta = parse_ttl(ttl_string)
    else:
        delta = ttl_delta
    
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
    "session": "24h",      # Temporary session context
    "conversation": "7d",  # Conversation summaries
    "short_term": "30d",   # Short-term memories
    "long_term": "1y",     # Long-term facts
    "permanent": None,     # Never expires
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
        raise ValueError(
            f"Unknown TTL preset: '{preset_name}'. "
            f"Available presets: {list(TTL_PRESETS.keys())}"
        )
    return TTL_PRESETS[preset_name]
