"""Tests for error message sanitization.

These tests ensure that internal URLs, paths, and API keys
are never leaked to clients in error responses.
"""

import pytest

from remembra.security.error_sanitizer import (
    create_safe_error_response,
    is_safe_to_expose,
    sanitize_error_message,
)


class TestSanitizeErrorMessage:
    """Tests for sanitize_error_message function."""

    def test_sanitizes_openai_urls(self):
        """OpenAI API URLs should not be exposed."""
        error = "Connection to https://api.openai.com/v1/embeddings failed"
        result = sanitize_error_message(error)
        assert "openai" not in result.lower()
        assert "api." not in result.lower()
        assert "An internal error occurred" in result

    def test_sanitizes_any_https_urls(self):
        """Any HTTPS URLs should be sanitized."""
        urls = [
            "https://api.azure.com/v1/endpoint",
            "http://localhost:8787/api/v1/memories",
            "https://internal.service:9000/path",
        ]
        for url in urls:
            error = f"Failed to connect to {url}"
            result = sanitize_error_message(error)
            assert url not in result
            assert "An internal error occurred" in result

    def test_sanitizes_file_paths(self):
        """File paths should not be exposed."""
        error = "Error in /app/src/storage/embeddings.py at line 50"
        result = sanitize_error_message(error)
        assert ".py" not in result
        assert "/app/" not in result
        assert "An internal error occurred" in result

    def test_sanitizes_api_keys(self):
        """API keys should never be exposed."""
        keys = [
            "sk-proj-abc123xyz789abc123xyz789",
            "sk-abc123xyz789abc123xyz789",
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ]
        for key in keys:
            error = f"Invalid API key: {key}"
            result = sanitize_error_message(error)
            assert key not in result
            assert "An internal error occurred" in result

    def test_sanitizes_connection_strings(self):
        """Database connection strings should not be exposed."""
        conn_strings = [
            "postgres://user:pass@localhost:5432/db",
            "redis://localhost:6379",
            "mongodb://admin:secret@cluster.mongodb.net",
        ]
        for conn in conn_strings:
            error = f"Failed to connect: {conn}"
            result = sanitize_error_message(error)
            assert conn not in result
            assert "An internal error occurred" in result

    def test_preserves_safe_messages(self):
        """Known safe error messages should pass through unchanged."""
        safe_messages = [
            "query must not be empty",
            "content must not be empty",
            "Cannot embed empty text",
            "Authentication required",
            "Permission denied: memory:store required",
            "Memory not found",
            "Invalid memory ID format",
            "Rate limit exceeded",
        ]
        for msg in safe_messages:
            result = sanitize_error_message(msg)
            assert result == msg

    def test_handles_exception_objects(self):
        """Should handle Exception objects as well as strings."""
        exc = ValueError("Connection to https://api.openai.com failed")
        result = sanitize_error_message(exc)
        assert "openai" not in result.lower()
        assert "An internal error occurred" in result

    def test_truncates_long_safe_messages(self):
        """Very long messages should be truncated."""
        long_msg = "x" * 600
        result = sanitize_error_message(long_msg)
        assert len(result) <= 503  # 500 + "..."

    def test_sanitizes_ip_with_port(self):
        """IP addresses with ports should be sanitized."""
        error = "Cannot connect to 192.168.1.100:5432"
        result = sanitize_error_message(error)
        assert "192.168" not in result
        assert "An internal error occurred" in result


class TestIsSafeToExpose:
    """Tests for is_safe_to_expose function."""

    def test_safe_prefixes_return_true(self):
        """Messages starting with safe prefixes should be safe."""
        assert is_safe_to_expose("query must not be empty")
        assert is_safe_to_expose("Authentication required")
        assert is_safe_to_expose("Permission denied: something")

    def test_urls_return_false(self):
        """URLs should not be safe to expose."""
        assert not is_safe_to_expose("https://api.openai.com/v1")
        assert not is_safe_to_expose("Error at http://localhost:8787")

    def test_short_generic_messages_safe(self):
        """Short generic messages without patterns are safe."""
        assert is_safe_to_expose("Something went wrong")
        assert is_safe_to_expose("Validation failed")


class TestCreateSafeErrorResponse:
    """Tests for create_safe_error_response function."""

    def test_basic_response(self):
        """Should create basic error response."""
        error = "Something went wrong"
        result = create_safe_error_response(error)
        assert result["status"] == "error"
        assert result["error"] == error

    def test_sanitizes_in_response(self):
        """Should sanitize error in response."""
        error = "Failed at https://api.openai.com/v1"
        result = create_safe_error_response(error)
        assert "openai" not in result["error"].lower()

    def test_includes_error_type(self):
        """Should include error type when requested."""
        exc = ValueError("Invalid input")
        result = create_safe_error_response(exc, include_type=True)
        assert "error_type" in result
        assert result["error_type"] == "ValidationError"

    def test_custom_status(self):
        """Should use custom status."""
        result = create_safe_error_response("Error", status="failed")
        assert result["status"] == "failed"


class TestRealWorldScenarios:
    """Tests for real-world error scenarios."""

    def test_openai_embedding_error(self):
        """Simulate OpenAI embedding error response."""
        # This is what an OpenAI error might look like
        error = (
            "Error code: 400 - {'error': {'message': 'Invalid API Key provided: sk-proj-abc...xyz', "
            "'type': 'invalid_request_error', 'param': None, 'code': 'invalid_api_key'}}"
        )
        result = sanitize_error_message(error)
        assert "sk-" not in result
        assert "An internal error occurred" in result

    def test_httpx_connection_error(self):
        """Simulate httpx connection error."""
        error = "All connection attempts failed. [Connect to api.openai.com:443 ssl:True server_hostname:api.openai.com]"
        result = sanitize_error_message(error)
        assert "openai" not in result.lower()
        assert "An internal error occurred" in result

    def test_empty_query_to_embedding(self):
        """The original issue: empty query exposing OpenAI URL."""
        # Simulating what might happen if empty query slips through
        error = "Request to https://api.openai.com/v1/embeddings failed with empty input"
        result = sanitize_error_message(error)
        assert "openai" not in result.lower()
        assert "api" not in result.lower()
        assert "An internal error occurred" in result
