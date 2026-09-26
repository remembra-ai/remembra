"""Launcher package for the Remembra MCP server.

The server itself lives in :mod:`remembra.mcp.server` (installed through the
``remembra[mcp]`` dependency). This package exists so MCP registry clients can
start it with ``uvx remembra-mcp``: the registry names a PyPI package, and a
package named after the command is what ``uvx`` runs without extra flags.
"""

from __future__ import annotations


def main() -> None:
    # The same launcher as remembra's own `remembra-mcp` script: it answers --help and
    # --version, names a missing extra instead of a traceback, then runs the stdio server.
    from remembra._launch import mcp

    mcp()
