"""Smoke tests for the FastAPI application skeleton."""

import pytest
from httpx import ASGITransport, AsyncClient

from remembra.main import create_app


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_root(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "remembra"
    assert "version" in data


async def test_docs_available(client: AsyncClient) -> None:
    r = await client.get("/docs")
    assert r.status_code == 200


async def test_openapi_schema(client: AsyncClient) -> None:
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "Remembra"


async def test_store_memory_returns_201(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/memories",
        json={"user_id": "test-user", "content": "John is the CTO at Acme Corp."},
    )
    assert r.status_code == 201
    data = r.json()
    assert "id" in data
    assert isinstance(data["extracted_facts"], list)
    assert isinstance(data["entities"], list)


async def test_recall_memories(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/memories/recall",
        json={"user_id": "test-user", "query": "Who is John?"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "context" in data
    assert "memories" in data
    assert "entities" in data


async def test_store_empty_content_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/memories",
        json={"user_id": "test-user", "content": "   "},
    )
    assert r.status_code == 422


async def test_forget_requires_filter(client: AsyncClient) -> None:
    r = await client.delete("/api/v1/memories")
    # 422 Unprocessable Content (FastAPI validates the query params are missing)
    assert r.status_code in (422, 422)
