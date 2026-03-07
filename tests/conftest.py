"""Shared pytest fixtures for the Remembra test suite."""

import os

# Disable auth and rate limiting for all tests
os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

import aiosqlite
import pytest
from unittest.mock import AsyncMock, MagicMock

from remembra.services.memory import MemoryService


# ---------------------------------------------------------------------------
# Application fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings():
    """Create mock settings for testing."""
    settings = MagicMock()
    settings.openai_api_key = "test-key"
    settings.azure_openai_api_key = "test-azure-key"
    settings.azure_openai_endpoint = "https://test.openai.azure.com"
    settings.azure_openai_deployment = "text-embedding-3-small"
    settings.azure_openai_api_version = "2024-02-01"
    settings.cohere_api_key = "test-cohere-key"
    settings.voyage_api_key = "test-voyage-key"
    settings.jina_api_key = "test-jina-key"
    settings.ollama_url = "http://localhost:11434"
    settings.embedding_provider = "openai"
    settings.embedding_model = "text-embedding-3-small"
    settings.embedding_dimensions = None
    settings.extraction_model = "gpt-4o-mini"
    settings.sanitization_enabled = False
    return settings


@pytest.fixture()
def mock_memory_service():
    """Create a mock memory service for testing."""
    service = MagicMock(spec=MemoryService)
    service.store = AsyncMock()
    service.recall = AsyncMock()
    service.forget = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def in_memory_db():
    """Create a real in-memory SQLite database via aiosqlite.

    Returns a thin wrapper whose ``.conn`` attribute is the raw
    aiosqlite connection, matching the interface that ConflictManager
    and SpaceManager expect.
    """

    class _DB:
        def __init__(self, conn: aiosqlite.Connection) -> None:
            self.conn = conn

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    db = _DB(conn)
    yield db
    await conn.close()


# ---------------------------------------------------------------------------
# HTTP mock helpers
# ---------------------------------------------------------------------------


def make_httpx_response(json_data: dict, status_code: int = 200):
    """Build a mock httpx.Response with the given JSON payload."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp
