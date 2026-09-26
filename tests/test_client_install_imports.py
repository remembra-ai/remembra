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


# ---------------------------------------------------------------------------
# Console scripts that need an extra explain it instead of a traceback
# ---------------------------------------------------------------------------

LAUNCH = """
    import sys
    from remembra import _launch

    sys.argv = [{argv0!r}, *{args!r}]
    try:
        getattr(_launch, {entry!r})()
    except SystemExit as exc:
        print("exit:", exc.code)
    else:
        print("exit: none")
    """


def _launch(entry: str, argv0: str, *, allowed: set[str] | None, blocked: set[str], args: tuple[str, ...] = ()):
    return _run(LAUNCH.format(entry=entry, argv0=argv0, args=list(args)), allowed=allowed, blocked=blocked)


def test_server_command_on_a_base_install_names_the_server_extra() -> None:
    for argv0 in ("remembra", "/home/u/.local/bin/remembra-server"):
        result = _launch("server", argv0, allowed=BASE_ALLOWED, blocked=set())
        assert "Traceback" not in result.stderr, result.stderr
        assert result.stdout.strip() == "exit: 1"
        name = argv0.rsplit("/", 1)[-1]
        assert result.stderr.startswith(f"{name}: this command needs the 'server' extra"), result.stderr
        assert "(missing module: structlog)" in result.stderr
        assert "pipx install --force 'remembra[server]'" in result.stderr
        assert "pip install 'remembra[server]'" in result.stderr


def test_server_command_with_only_the_mcp_extra_names_the_server_extra() -> None:
    # The bug report: `remembra` on a [mcp]-only install crashed with ModuleNotFoundError: fastapi.
    result = _launch("server", "remembra", allowed=None, blocked=SERVER_ONLY)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.stdout.strip() == "exit: 1"
    assert "needs the 'server' extra" in result.stderr and "(missing module: fastapi)" in result.stderr


def test_mcp_command_on_a_base_install_names_the_mcp_extra() -> None:
    result = _launch("mcp", "remembra-mcp", allowed=BASE_ALLOWED, blocked=set(), args=("--help",))
    assert "Traceback" not in result.stderr, result.stderr
    assert result.stdout.strip() == "exit: 1"
    assert result.stderr.startswith("remembra-mcp: this command needs the 'mcp' extra")
    assert "(missing module: mcp)" in result.stderr
    assert "pipx install --force 'remembra[mcp]>=0.16'" in result.stderr


def test_mcp_command_with_the_mcp_extra_answers_help_and_version() -> None:
    result = _launch("mcp", "remembra-mcp", allowed=None, blocked=SERVER_ONLY, args=("--help",))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: remembra-mcp")
    assert result.stdout.strip().endswith("exit: none")
    version = _launch("mcp", "remembra-mcp", allowed=None, blocked=SERVER_ONLY, args=("--version",))
    import remembra

    assert f"remembra-mcp {remembra.__version__}" in version.stdout


def test_a_missing_module_no_extra_provides_is_not_disguised() -> None:
    # A real bug (a module missing from the package itself) keeps its traceback.
    result = _run(
        """
        import sys
        from remembra import _launch

        def broken():
            import remembra_no_such_module  # noqa: F401

        try:
            _launch._load("server", _launch.SERVER_INSTALL, broken)
        except ModuleNotFoundError as exc:
            print("raised:", exc.name)
        """,
        allowed=None,
        blocked=set(),
    )
    assert result.stdout.strip() == "raised: remembra_no_such_module", result.stderr


def test_extra_module_names_cover_every_package_of_the_extras() -> None:
    """Each distribution in the server / mcp extras has its import name listed in _launch."""
    import tomllib

    from remembra._launch import EXTRA_MODULES

    import_names = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "pydantic": "pydantic",
        "pydantic-settings": "pydantic_settings",
        "qdrant-client": "qdrant_client",
        "python-ulid": "ulid",
        "pyotp": "pyotp",
        "qrcode": "qrcode",
        "structlog": "structlog",
        "aiosqlite": "aiosqlite",
        "openai": "openai",
        "tiktoken": "tiktoken",
        "bcrypt": None,  # also a base dependency
        "slowapi": None,  # also a base dependency
        "python-multipart": "multipart",
        "PyJWT": "jwt",
        "email-validator": "email_validator",
        "mcp": "mcp",
    }
    extras = tomllib.loads((SRC.parent / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    for extra in ("server", "mcp"):
        for requirement in extras[extra]:
            dist = requirement.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
            assert dist in import_names, f"new {extra} dependency {dist}: add its import name to _launch.EXTRA_MODULES"
            module = import_names[dist]
            if module:
                assert module in EXTRA_MODULES[extra], (extra, module)
