"""The client commands must start on the installs the site tells people to run.

`pipx install remembra` pulls only the base dependencies (httpx, bcrypt,
slowapi); `remembra[mcp]` adds mcp and structlog. The dev venv has every
extra, so a stray server import (structlog, aiosqlite, the storage layer)
never fails here unless we take those packages away. Each case runs in a
fresh interpreter with an import hook that refuses whatever that install
would not have.

CI also installs the built wheel with no extras (the "wheel-install" job).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# Import roots of the base dependencies and what they pull in.
BASE_ALLOWED = {
    "remembra",
    "httpx",
    "httpcore",
    "h11",
    "h2",
    "hpack",
    "hyperframe",
    "anyio",
    "sniffio",
    "idna",
    "certifi",
    "bcrypt",
    "slowapi",
    "limits",
    "deprecated",
    "wrapt",
    "packaging",
    "typing_extensions",
    "exceptiongroup",
}

# Server-only packages that remembra[mcp] does not bring.
SERVER_ONLY = {
    "aiosqlite",
    "qdrant_client",
    "openai",
    "tiktoken",
    "pyotp",
    "qrcode",
    "ulid",
    "fastapi",
    "email_validator",
    "resend",
    "redis",
    "dateparser",
}

HOOK = textwrap.dedent(
    """
    import sys

    ALLOWED = {allowed!r}
    BLOCKED = {blocked!r}

    class Refuse:
        def find_spec(self, name, path=None, target=None):
            root = name.partition(".")[0]
            if root in sys.stdlib_module_names or root.startswith("_"):
                return None
            if (ALLOWED is not None and root not in ALLOWED) or root in BLOCKED:
                raise ModuleNotFoundError(f"No module named {{root!r}} (not in this install)", name=root)
            return None

    sys.meta_path.insert(0, Refuse())
    """
)


def _run(body: str, *, allowed: set[str] | None, blocked: set[str]) -> subprocess.CompletedProcess[str]:
    code = HOOK.format(allowed=allowed, blocked=blocked) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin", "HOME": str(SRC.parent)},
    )


def test_base_install_runs_the_relay_and_the_installers() -> None:
    result = _run(
        """
        import contextlib, io, sys
        import remembra
        from remembra.relay import cli
        from remembra.tools import agents, codex, doctor, bridge

        sys.argv = ["remembra-relay", "--help"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                cli.entrypoint()
            except SystemExit as exc:
                assert exc.code == 0, exc.code
        assert "brief" in out.getvalue()
        assert "codex" in agents.build_parser().format_help()
        from remembra.security.secrets import redact_secrets
        assert "[REDACTED" in redact_secrets("key sk-abcdefghijklmnopqrstuvwxyz0123456789").text
        loaded = sorted(m for m in ("structlog", "aiosqlite", "mcp", "remembra.storage.database") if m in sys.modules)
        print("loaded:", loaded)
        """,
        allowed=BASE_ALLOWED,
        blocked=set(),
    )
    assert result.returncode == 0, result.stderr
    assert "loaded: []" in result.stdout


def test_security_package_names_still_load_on_first_use() -> None:
    # The lazy package __init__ keeps `from remembra.security import AuditLogger` working on a server install.
    import remembra.security as security

    assert security.AuditLogger.__name__ == "AuditLogger"
    assert security.redact_pii("mail me at a@b.co") != "mail me at a@b.co"
    assert set(security.__all__) <= set(dir(security))


def test_mcp_extra_runs_the_mcp_server_without_server_packages() -> None:
    result = _run(
        """
        import sys
        import remembra.mcp.server as server
        from remembra.relay import cli
        assert callable(server.main) and callable(cli.entrypoint)
        print("aiosqlite" in sys.modules, "remembra.storage.database" in sys.modules)
        """,
        allowed=None,
        blocked=SERVER_ONLY,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"
