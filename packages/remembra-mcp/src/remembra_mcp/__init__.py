"""Launcher package for the Remembra MCP server.

The server itself lives in :mod:`remembra.mcp.server` (installed through the
``remembra[mcp]`` dependency). This package exists so MCP registry clients can
start it with ``uvx remembra-mcp``: the registry names a PyPI package, and a
package named after the command is what ``uvx`` runs without extra flags.
"""

from __future__ import annotations


def main() -> None:
    from remembra.mcp.server import main as server_main

    server_main()
