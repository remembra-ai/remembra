"""Every command Marshal may hand a user, built from templates and checked against them.

The strings mirror ``dashboard/src/lib/agents.ts`` (``PIPX_INSTALL``,
``saveKeyCommand``, ``oneLineInstall``, ``agentConnectCommand``,
``doctorCommand``, ``pipxRunDoctorCommand``, ``UNINSTALL_STEPS``,
``CONNECTABLE_AGENTS``); ``tests/test_marshal_commands_parity.py`` fails when
the two drift, and ``tests/test_marshal_parity.py`` holds remembra.dev/setup.md
and ``remembra_setup`` to the same lines. A finding or setup step may only carry a command that
:func:`is_allowed` accepts, so a fix can never be text copied from a brief, a
memory or an inbox note. No template takes an API key: keys are typed into
``remembra-install``'s hidden prompt by the user, in their own terminal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from remembra.relay.adapters import REGISTRY
from remembra.relay.config import DEFAULT_URL

CLOUD_URL = "https://api.remembra.dev"
SIGNUP_URL = "https://app.remembra.dev/signup"
DASHBOARD_KEYS_URL = "https://app.remembra.dev"
CONTACT_URL = "https://remembra.dev/contact"
DOCS_RELAY = "https://docs.remembra.dev/guides/relay/"

# The first release with remembra-relay; --force upgrades an older pipx install.
PIPX_INSTALL = "pipx install --force 'remembra[mcp]>=0.16'"
# The first release that ships `remembra-relay doctor` (for `pipx run` on an older install).
DOCTOR_MIN_VERSION = "0.16.1"
# Without --url, remembra-install keeps a server already set up (REMEMBRA_URL, the saved
# credentials, an existing remembra entry) and defaults to Remembra Cloud on a first install.
INSTALL_KEEP_SERVER = "remembra-install --all"

UNINSTALL_STEPS: tuple[tuple[str, str], ...] = (
    ("remembra-relay disconnect --apply", "removes the session hooks from every agent (a backup of each file is kept)"),
    (
        "remembra-install --remove --all --apply --delete-backups",
        "removes the Remembra MCP server from every agent and deletes the config backups that still hold your key",
    ),
    ("pipx uninstall remembra", "removes the commands"),
    ("rm -r ~/.remembra", "deletes the saved key, the unsent-handoff queue and the log"),
)

# pipx installs commands in its bin directory (~/.local/bin unless PIPX_BIN_DIR says otherwise); this puts it
# on the PATH of new shells, and changes nothing when it is there already. Run it even when pipx was there.
PIPX_ENSUREPATH = "pipx ensurepath"

# How to get pipx, per platform (docs: https://pipx.pypa.io). Windows is not tested.
PIPX_BOOTSTRAP: dict[str, str] = {
    "macos": "brew install pipx && pipx ensurepath",
    "linux-apt": "sudo apt install pipx && pipx ensurepath",
    "linux-dnf": "sudo dnf install pipx && pipx ensurepath",
    "other": "python3 -m pip install --user pipx && python3 -m pipx ensurepath",
}

_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?$")
_OUTBOX_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}\.json$")


def _agent(name: str) -> str:
    if not _AGENT_RE.match(name or ""):
        raise ValueError(f"not an agent id: {name!r}")
    return name


def save_key_command(server_url: str | None) -> str:
    """``saveKeyCommand`` in agents.ts: save the key (asked at a hidden prompt) and add the MCP server.

    ``server_url`` is the server the key belongs to, when it is known (the dashboard always knows its own).
    Without one, ``remembra-install`` keeps the server this machine already uses, or Remembra Cloud on a first
    install: the line remembra.dev and setup.md show.
    """
    if not server_url:
        return INSTALL_KEEP_SERVER
    if not _URL_RE.match(server_url):
        raise ValueError("not a server URL")
    return f"remembra-install --all --url {server_url}"


def key_step(configured_url: str | None) -> str:
    """The key step on a machine whose relay uses ``configured_url``: that server, or none when none is set up.

    The relay's own default (``DEFAULT_URL``) means nothing is configured yet, so ``remembra-install`` is left
    to choose (Remembra Cloud on a first install), exactly as remembra.dev and setup.md show it.
    """
    if not configured_url or configured_url.rstrip("/") == DEFAULT_URL:
        return INSTALL_KEEP_SERVER
    try:
        return save_key_command(configured_url)
    except ValueError:
        return INSTALL_KEEP_SERVER


def one_line_install(server_url: str) -> str:
    """``oneLineInstall`` in agents.ts. remembra-install exits 3 on "no", so ``&&`` stops before connect."""
    return f"{PIPX_INSTALL} && {save_key_command(server_url)} && remembra-relay connect --apply"


def connect(agents: Iterable[str] = (), *, unverified: bool = False, apply: bool = True) -> str:
    parts = ["remembra-relay connect"]
    if apply:
        parts.append("--apply")
    parts.extend(f"--agent {_agent(a)}" for a in agents)
    if unverified:
        parts.append("--include-unverified")
    return " ".join(parts)


def agent_connect(agent: str) -> str:
    """``agentConnectCommand`` in agents.ts: write one agent's hooks (an unverified adapter needs the flag)."""
    adapter = REGISTRY.get(_agent(agent))
    return connect([agent], unverified=adapter is None or not adapter.spec.verified)


def close(agent: str) -> str:
    return f"remembra-relay close --agent {_agent(agent)}"


def resolve_bind(project_id: str) -> str:
    if not _PROJECT_RE.match(project_id or ""):
        raise ValueError(f"not a project id: {project_id!r}")
    return f"remembra-relay resolve --project {project_id} --bind"


# A dry run: lists the repositories 0.16.0 bound to the configured project and where each would go.
PROJECTS_SPLIT = "remembra-relay projects split"


def status_json() -> str:
    return "remembra-relay status --format json"


def doctor(agent: str | None = None) -> str:
    return "remembra-relay doctor" + (f" --agent {_agent(agent)}" if agent else "")


def pipx_run_doctor(agent: str | None = None) -> str:
    """Runs the doctor from PyPI on a machine whose install predates it."""
    tail = f" --agent {_agent(agent)}" if agent else ""
    return f"pipx run --spec 'remembra>={DOCTOR_MIN_VERSION}' remembra-relay doctor{tail}"


# The Codex CLI command that lists the hooks to trust (typed into Codex, not a shell).
CODEX_HOOKS = "/hooks"


def ask_agent_doctor(agent: str | None = None) -> str:
    """``askAgentDoctor`` in agents.ts: what to ask an agent that has the Remembra MCP server."""
    return f"run remembra_doctor for {agent}" if agent else "run remembra_doctor"


def remove_outbox_file(name: str) -> str:
    """Drop ONE named queued handoff (the path is fixed; only the file name varies)."""
    if not _OUTBOX_FILE_RE.match(name or "") or name.startswith("."):
        raise ValueError(f"not an outbox file name: {name!r}")
    return f"rm ~/.remembra/relay/outbox/{name}"


_A = r"[a-z0-9][a-z0-9._-]{0,63}"
ALLOWED: tuple[re.Pattern[str], ...] = (
    re.compile(r"^" + re.escape(PIPX_INSTALL) + r"$"),
    re.compile(r"^" + re.escape(PIPX_ENSUREPATH) + r"$"),
    re.compile(r"^remembra-install --all(?: --url https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?)?$"),
    re.compile(rf"^remembra-relay connect(?: --apply)?(?: --agent {_A})*(?: --include-unverified)?$"),
    re.compile(rf"^remembra-relay close --agent {_A}$"),
    re.compile(r"^remembra-relay resolve --project [A-Za-z0-9][A-Za-z0-9._-]{0,127} --bind$"),
    re.compile(r"^remembra-relay status --format json$"),
    re.compile("^" + re.escape(PROJECTS_SPLIT) + "$"),
    re.compile(rf"^remembra-relay doctor(?: --agent {_A})?$"),
    re.compile(rf"^pipx run --spec 'remembra>={re.escape(DOCTOR_MIN_VERSION)}' remembra-relay doctor(?: --agent {_A})?$"),
    re.compile(r"^rm ~/\.remembra/relay/outbox/[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}\.json$"),
    *(re.compile("^" + re.escape(command) + "$") for command, _ in UNINSTALL_STEPS),
    *(re.compile("^" + re.escape(command) + "$") for command in PIPX_BOOTSTRAP.values()),
)


def is_allowed(command: str) -> bool:
    """True when ``command`` is exactly one of the templates above (never a key, never a pipe to a shell)."""
    return isinstance(command, str) and "\n" not in command and any(p.match(command) for p in ALLOWED)


# ---------------------------------------------------------------------------
# The Marshal desk: the only commands a model-written answer may carry
# ---------------------------------------------------------------------------

DeskCommandKind = Literal["terminal", "codex_ui", "agent"]
_REGISTRY_AGENT = "(" + "|".join(re.escape(name) for name in REGISTRY) + ")"
_DESK_TERMINAL: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^remembra-relay doctor(?: --agent {_REGISTRY_AGENT})?$"),
    re.compile(
        rf"^pipx run --spec 'remembra>={re.escape(DOCTOR_MIN_VERSION)}' remembra-relay doctor(?: --agent {_REGISTRY_AGENT})?$"
    ),
    re.compile(rf"^remembra-relay connect --apply(?: --agent {_REGISTRY_AGENT})*$"),
    re.compile(rf"^remembra-relay close --agent {_REGISTRY_AGENT}$"),
    re.compile(r"^remembra-relay status --format json$"),
    re.compile("^" + re.escape(PROJECTS_SPLIT) + "$"),
    re.compile("^" + re.escape(PIPX_INSTALL) + "$"),
    re.compile("^" + re.escape(INSTALL_KEEP_SERVER) + "$"),
)
_DESK_AGENT = re.compile(rf"^run remembra_doctor(?: for {_REGISTRY_AGENT})?$")
_DESK_RESOLVE = re.compile(r"^remembra-relay resolve --project ([A-Za-z0-9][A-Za-z0-9._-]{0,127}) --bind$")
_DESK_INSTALL_URL = re.compile(r"^remembra-install --all --url (\S+)$")


def desk_command_kind(command: str, *, server_urls: Iterable[str | None], projects: Iterable[str]) -> DeskCommandKind | None:
    """Where a command from a Marshal desk answer runs, or None when the desk may not show it.

    Stricter than :func:`is_allowed`: only the fixes and checks (doctor, connect,
    close, status, resolve for a project a tool result named, the install and key
    steps for this server), the Codex ``/hooks`` step and the question for the
    user's own agent. Every agent is a relay adapter id; a URL must be one of
    ``server_urls``. Nothing that deletes (the uninstall steps, ``rm``), nothing
    with ``sudo`` (the pipx bootstrap lines) and no other URL is ever accepted.
    """
    if not isinstance(command, str) or command != command.strip() or "\n" in command or "rem_" in command:
        return None
    if command == CODEX_HOOKS:
        return "codex_ui"
    if _DESK_AGENT.match(command):
        return "agent"
    if command.startswith("remembra-relay connect"):
        selected = re.findall(r"--agent ([A-Za-z0-9-]+)", command)
        if any(agent not in REGISTRY or not REGISTRY[agent].spec.verified for agent in selected):
            return None
    if any(pattern.match(command) for pattern in _DESK_TERMINAL):
        return "terminal"
    urls = {url for url in server_urls if url}
    match = _DESK_RESOLVE.match(command)
    if match:
        return "terminal" if match.group(1) in set(projects) else None
    match = _DESK_INSTALL_URL.match(command)
    if match:
        return "terminal" if match.group(1) in urls else None
    # The one-line first run (the dashboard's oneLineInstall) for this server, or for the server already set up.
    if command in {one_line_install(url) for url in urls if _URL_RE.match(url)}:
        return "terminal"
    if command == f"{PIPX_INSTALL} && {INSTALL_KEEP_SERVER} && remembra-relay connect --apply":
        return "terminal"
    return None
