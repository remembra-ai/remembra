"""Console-script launchers for the commands that need an optional extra.

``pipx install remembra`` brings only the base dependencies. The server
(``remembra``, ``remembra-server``) needs the ``server`` extra and
``remembra-mcp`` needs the ``mcp`` extra. Without them the old entry points
died with a ``ModuleNotFoundError`` traceback. These launchers import the real
module first and, when a package that belongs to an extra is missing, print
which extra to install and exit 1. A missing module that no extra provides is
a real bug and is re-raised unchanged.

This module imports nothing outside the standard library at import time.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

# Import names (not distribution names) of each extra's packages; see
# [project.optional-dependencies] in pyproject.toml (a test keeps them in step).
EXTRA_MODULES: dict[str, frozenset[str]] = {
    "server": frozenset(
        {
            "fastapi",
            "starlette",
            "uvicorn",
            "pydantic",
            "pydantic_core",
            "pydantic_settings",
            "qdrant_client",
            "ulid",
            "pyotp",
            "qrcode",
            "PIL",
            "structlog",
            "aiosqlite",
            "openai",
            "tiktoken",
            "multipart",
            "python_multipart",
            "jwt",
            "email_validator",
        }
    ),
    "mcp": frozenset({"mcp", "structlog", "pydantic", "pydantic_core", "starlette", "anyio"}),
}

# The install line the site and the dashboard give (tests keep them identical).
MCP_INSTALL = "pipx install --force 'remembra[mcp]>=0.16'"
SERVER_INSTALL = "pipx install --force 'remembra[server]'"

MCP_USAGE = """usage: remembra-mcp [--help] [--version]

Remembra MCP server (stdio by default). It is started by your agent from its
MCP config, not by hand; remembra-install writes that config.

Configuration comes from the environment:
  REMEMBRA_URL         server URL (required)
  REMEMBRA_API_KEY     API key
  REMEMBRA_AGENT_ID    this agent's id, e.g. claude-code
  REMEMBRA_PROJECT     project namespace (default: default)
  REMEMBRA_MCP_TRANSPORT  stdio | sse | streamable-http (default: stdio)
"""


def _command_name() -> str:
    return os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "remembra"


def missing_extra(exc: ModuleNotFoundError, extra: str) -> str | None:
    """The missing top-level module when it belongs to ``extra``, else None."""
    name = exc.name or ""
    root = name.partition(".")[0]
    return root if root and root in EXTRA_MODULES[extra] else None


def explain_missing_extra(command: str, extra: str, module: str, install: str) -> str:
    return (
        f"{command}: this command needs the '{extra}' extra, which is not installed (missing module: {module}).\n"
        f"  Install it with:  {install}\n"
        f"  or, in a virtualenv:  pip install 'remembra[{extra}]'\n"
        "  remembra-relay and remembra-install work without it."
    )


def _load(extra: str, install: str, loader: Callable[[], Callable[[], object]]) -> Callable[[], object]:
    try:
        return loader()
    except ModuleNotFoundError as exc:
        module = missing_extra(exc, extra)
        if module is None:
            raise
        print(explain_missing_extra(_command_name(), extra, module, install), file=sys.stderr)
        raise SystemExit(1) from None


def _server_entry() -> Callable[[], object]:
    from remembra.main import run

    return run


def _mcp_entry() -> Callable[[], object]:
    from remembra.mcp.server import main

    return main


def server() -> None:
    """``remembra`` / ``remembra-server``."""
    run = _load("server", SERVER_INSTALL, _server_entry)
    run()


def mcp() -> None:
    """``remembra-mcp``. ``--help`` and ``--version`` answer after the imports succeed."""
    main = _load("mcp", MCP_INSTALL, _mcp_entry)
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(MCP_USAGE, end="")
        return
    if args and args[0] == "--version":
        from remembra import __version__

        print(f"remembra-mcp {__version__}")
        return
    main()
