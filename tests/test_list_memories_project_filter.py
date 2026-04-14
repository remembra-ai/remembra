"""
Tests for list_memories project_id filter (Addendum C).

Until this fix, the MCP `list_memories(project_id=...)` tool accepted
project_id as a parameter but never passed it to the underlying client —
it silently dropped the arg. Users got the same results regardless of
what project they asked for.

This test pins three layers:
  1. Memory.list() SDK method forwards project_id into query params
  2. Memory.list() omits project_id from params when None (don't send the key)
  3. MCP list_memories() passes project_id through to client.list()
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1 + 2) Client SDK — Memory.list() query-param shaping
# ---------------------------------------------------------------------------


def _make_client_with_mocked_request():
    from remembra.client.memory import Memory

    m = Memory.__new__(Memory)
    m.base_url = "http://x"
    m.api_key = None
    m.user_id = "user_x"
    m.project = "default"
    m._headers = {}
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []
    mock_http.request.return_value = mock_resp
    m._client = mock_http
    return m, mock_http


class TestClientListMemoriesProjectFilter:
    def test_forwards_project_id_when_set(self):
        m, http = _make_client_with_mocked_request()
        m.list(limit=5, offset=0, project_id="trademind")
        params = http.request.call_args.kwargs["params"]
        assert params == {"limit": 5, "offset": 0, "project_id": "trademind"}

    def test_omits_project_id_when_none(self):
        m, http = _make_client_with_mocked_request()
        m.list(limit=5, offset=0, project_id=None)
        params = http.request.call_args.kwargs["params"]
        assert "project_id" not in params, (
            "When project_id is None, the SDK must not send the key at all "
            "so the server can perform cross-project listing."
        )
        assert params == {"limit": 5, "offset": 0}

    def test_returns_list_even_if_server_returns_non_list(self):
        m, http = _make_client_with_mocked_request()
        http.request.return_value.json.return_value = {"oops": "not a list"}
        assert m.list() == []


# ---------------------------------------------------------------------------
# 3) MCP tool — list_memories forwards project_id to client.list
# ---------------------------------------------------------------------------


class TestMCPListMemoriesForwardsProjectId:
    def test_mcp_passes_project_id_through_to_client(self):
        import remembra.mcp.server as mcp_server

        fake_client = MagicMock()
        fake_client.list.return_value = [
            {"id": "a", "content": "x", "created_at": "2026-01-01T00:00:00", "project_id": "trademind"},
        ]

        with patch.object(mcp_server, "_get_client", return_value=fake_client):
            import json
            raw = mcp_server.list_memories(limit=5, project_id="trademind")
            parsed = json.loads(raw)

        # Verify the call landed on the SDK
        fake_client.list.assert_called_once()
        kwargs = fake_client.list.call_args.kwargs
        assert kwargs["project_id"] == "trademind"
        assert kwargs["limit"] == 5
        assert kwargs["offset"] == 0

        # Verify the response carries the project_id back to the caller
        assert parsed["status"] == "ok"
        assert parsed["project_id"] == "trademind"
        assert parsed["count"] == 1

    def test_mcp_passes_none_project_id_through_to_client(self):
        """No project_id = cross-project listing; None must propagate."""
        import remembra.mcp.server as mcp_server

        fake_client = MagicMock()
        fake_client.list.return_value = []

        with patch.object(mcp_server, "_get_client", return_value=fake_client):
            mcp_server.list_memories(limit=10)

        kwargs = fake_client.list.call_args.kwargs
        assert kwargs["project_id"] is None
