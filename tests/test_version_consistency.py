"""AGT-12: every surface that reports a Remembra version must agree.

The installed MCP server reported 0.13.2 while the API was 0.16.0 because the
SDK fell back to a hard-coded version string and the MCP handshake reported the
MCP SDK's version. These tests pin all of them to pyproject.toml.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import remembra
from remembra.client import memory as sdk_memory
from remembra.mcp import server as mcp_server

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def test_package_version_matches_pyproject() -> None:
    assert remembra.__version__ == _pyproject_version()


def test_sdk_user_agent_version_matches() -> None:
    assert sdk_memory.USER_AGENT_VERSION == _pyproject_version()
    client = sdk_memory.Memory(base_url="http://x")
    try:
        assert client._headers["User-Agent"] == f"remembra-python/{_pyproject_version()}"
    finally:
        client.close()


def test_mcp_handshake_reports_package_version() -> None:
    opts = mcp_server.mcp._mcp_server.create_initialization_options()
    assert opts.server_version == _pyproject_version()


def test_registry_manifests_match() -> None:
    manifest = json.loads((ROOT / "server.json").read_text())
    assert manifest["version"] == _pyproject_version()
    assert all(p["version"] == _pyproject_version() for p in manifest["packages"])
    card = json.loads((ROOT / "src/remembra/api/well_known/server-card.json").read_text())
    assert card["serverInfo"]["version"] == _pyproject_version()
