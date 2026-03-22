"""Error message sanitization to prevent leaking internal details.

This module provides utilities to sanitize error messages before returning
them to clients, preventing leakage of:
- Internal URLs (API endpoints, internal services)
- File paths
- API keys and tokens
- Stack traces
- Database connection strings

OWASP Compliance:
- A4:2021 Insecure Design - Don't expose internal architecture
- A5:2021 Security Misconfiguration - Proper error handling
"""

import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Patterns for sensitive information that should be stripped from errors
SENSITIVE_PATTERNS = [
    # URLs with protocol (OpenAI, internal services, etc.)
    (r"https?://[^\s\"'<>\]]+", "[REDACTED_URL]"),
    # Hostnames without protocol (api.openai.com, service.internal.net)
    (r"[a-zA-Z0-9\-]+\.(openai|azure|anthropic|cohere|voyageai|jina)\.[a-z]+", "[REDACTED_SERVICE]"),
    # Generic API-like hostnames
    (r"api\.[a-zA-Z0-9\-]+\.[a-z]+(?::\d+)?", "[REDACTED_ENDPOINT]"),
    # File paths
    (r"/[a-zA-Z0-9_\-./]+\.py", "[REDACTED_PATH]"),
    (r"/[a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-./]+", "[REDACTED_PATH]"),
    # API keys (common patterns) - be aggressive, catch partial keys too
    (r"sk-[a-zA-Z0-9\-_.]+", "[REDACTED_API_KEY]"),
    (r"pk-[a-zA-Z0-9\-_.]+", "[REDACTED_API_KEY]"),
    (r"Bearer\s+[a-zA-Z0-9\-_\.]+", "[REDACTED_TOKEN]"),
    # Connection strings
    (r"(?:postgres|mysql|redis|mongodb)://[^\s\"']+", "[REDACTED_CONNECTION]"),
    # IP addresses with ports
    (r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+", "[REDACTED_ENDPOINT]"),
    # Server hostnames with port (hostname:443, localhost:5432)
    (r"[a-zA-Z0-9\-_.]+:\d{2,5}", "[REDACTED_ENDPOINT]"),
    # Stack trace indicators
    (r"Traceback \(most recent call last\):.*", "[REDACTED_TRACEBACK]"),
    (r'File "[^"]+", line \d+', "[REDACTED_LOCATION]"),
]

# Compiled patterns for performance
_COMPILED_PATTERNS = [(re.compile(pattern, re.IGNORECASE | re.DOTALL), replacement) for pattern, replacement in SENSITIVE_PATTERNS]

# Known safe error messages (can be passed through)
# NOTE: These are EXACT safe messages, not prefixes that might contain sensitive data
SAFE_ERROR_PREFIXES = [
    "query must not be empty",
    "content must not be empty",
    "Cannot embed empty text",
    "Authentication required",
    "Permission denied",
    "Memory not found",
    "Invalid request",
    "Invalid memory ID",
    "Invalid user ID",
    "Invalid project ID",
    "Rate limit exceeded",
    "Request validation failed",
]


def sanitize_error_message(error: str | Exception) -> str:
    """
    Sanitize an error message to remove sensitive internal details.

    Args:
        error: The error message string or exception object

    Returns:
        A sanitized error message safe for client display

    Examples:
        >>> sanitize_error_message("Connection to https://api.openai.com/v1 failed")
        'An internal error occurred. Please try again or contact support.'

        >>> sanitize_error_message("File /app/src/service.py not found")
        'An internal error occurred. Please try again or contact support.'
    """
    error_str = str(error) if isinstance(error, Exception) else error

    # ALWAYS check for sensitive patterns FIRST, before allowing safe prefixes
    # This prevents attacks like "Invalid API key: sk-xxx" from leaking
    contains_sensitive = False
    for pattern, _ in _COMPILED_PATTERNS:
        if pattern.search(error_str):
            contains_sensitive = True
            break

    # If sensitive data was found, return a generic message immediately
    if contains_sensitive:
        log.warning(
            "sensitive_data_in_error",
            original_length=len(error_str),
            patterns_matched=sum(1 for p, _ in _COMPILED_PATTERNS if p.search(error_str)),
        )
        return "An internal error occurred. Please try again or contact support."

    # Only check safe prefixes AFTER confirming no sensitive data
    for prefix in SAFE_ERROR_PREFIXES:
        if error_str.startswith(prefix):
            return error_str

    # Log for monitoring
    log.debug("sanitizing_error_message", original_error=error_str[:200])

    # Truncate very long error messages
    if len(error_str) > 500:
        error_str = error_str[:500] + "..."

    return error_str


def create_safe_error_response(
    error: str | Exception,
    status: str = "error",
    include_type: bool = False,
) -> dict[str, Any]:
    """
    Create a safe error response dictionary.

    Args:
        error: The error message or exception
        status: Status string for the response
        include_type: If True, include a sanitized error type

    Returns:
        Dict with sanitized error response
    """
    response: dict[str, Any] = {
        "status": status,
        "error": sanitize_error_message(error),
    }

    if include_type:
        # Only include generic error types, not specific exception classes
        error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
        # Map specific types to generic ones
        type_mapping = {
            "httpx.HTTPStatusError": "ServiceError",
            "httpx.RequestError": "ConnectionError",
            "httpx.TimeoutException": "TimeoutError",
            "MemoryError": "MemoryError",
            "ValueError": "ValidationError",
            "RuntimeError": "ServiceError",
        }
        response["error_type"] = type_mapping.get(error_type, "Error")

    return response


def is_safe_to_expose(error_message: str) -> bool:
    """
    Check if an error message is safe to expose to clients.

    Args:
        error_message: The error message to check

    Returns:
        True if safe to expose, False if should be sanitized
    """
    # Check for known safe prefixes
    for prefix in SAFE_ERROR_PREFIXES:
        if error_message.startswith(prefix):
            return True

    # Check for sensitive patterns
    for pattern, _ in _COMPILED_PATTERNS:
        if pattern.search(error_message):
            return False

    # Default to safe if short and no patterns matched
    return len(error_message) < 200
