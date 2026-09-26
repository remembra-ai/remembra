"""``remembra_setup``: the exact install and connect steps for this machine's OS and agents.

Every command comes from :mod:`remembra.marshal.commands` (the same catalog
as the dashboard's ``agents.ts``). A step that is already done on this
machine says so, from the same local reads the doctor makes (no server
call). The key step is always the user's: the key is created in the
dashboard and typed into ``remembra-install``'s hidden prompt in the user's
own terminal, never in a chat.
"""

from __future__ import annotations

import os
import platform
import shutil
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from remembra.marshal import commands as cmd
from remembra.marshal import words
from remembra.marshal.render import WIDTH
from remembra.marshal.signals import Signals
from remembra.relay.adapters import REGISTRY

OS_LABELS = {
    "macos": "macOS",
    "linux-apt": "Linux (apt)",
    "linux-dnf": "Linux (dnf)",
    "linux": "Linux",
    "windows": "Windows",
    "other": "another OS",
}
# remembra-install --all writes the MCP entry for these (Qwen Code and Kimi are added by hand).
INSTALLER_AGENTS = ("claude-code", "codex", "cursor", "gemini")


@dataclass(frozen=True)
class Step:
    n: int
    title: str
    command: str | None = None
    action: str | None = None
    runs_where: str = "agent_ok"  # agent_ok | user_terminal | codex_ui | user | none
    writes: tuple[str, ...] = ()
    needs_yes: bool = False
    done: bool = False
    note: str | None = None
    stop: bool = False  # the agent stops here until the user has done it

    def __post_init__(self) -> None:
        if self.command is not None and not cmd.is_allowed(self.command):
            raise ValueError(f"not a template command: {self.command!r}")


@dataclass(frozen=True)
class SetupPlan:
    os: str
    shell: str | None
    agents: tuple[str, ...]
    steps: tuple[Step, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"os": self.os, "shell": self.shell, "agents": list(self.agents), "steps": [asdict(s) for s in self.steps]}


def detect_os(
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    os_release: Path = Path("/etc/os-release"),
) -> tuple[str, str | None]:
    """``(os id, shell name)``: macos, linux-apt, linux-dnf, linux, windows or other; the shell from ``$SHELL``."""
    env = os.environ if environ is None else environ
    name = (system or platform.system()).lower()
    shell = os.path.basename(env.get("SHELL") or "") or None
    if name == "darwin":
        return "macos", shell
    if name == "windows":
        return "windows", shell
    if name == "linux":
        try:
            text = os_release.read_text(encoding="utf-8").lower()
        except OSError:
            return "linux", shell
        fields = {}
        for line in text.splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
        family = f"{fields.get('id', '')} {fields.get('id_like', '')}".split()
        if any(f in ("debian", "ubuntu") for f in family):
            return "linux-apt", shell
        if any(f in ("fedora", "rhel", "centos") for f in family):
            return "linux-dnf", shell
        return "linux", shell
    return "other", shell


def _selected(sig: Signals, agents: list[str] | None) -> tuple[str, ...]:
    if agents:
        unknown = [a for a in agents if a not in REGISTRY]
        if unknown:
            raise ValueError(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}")
        return tuple(a for a in REGISTRY if a in agents)
    found = tuple(n for n, a in sig.agents.items() if a.detected or a.any_hooks)
    return found or ("claude-code", "codex")


def pipx_bin_on_path(environ: Mapping[str, str], home: Path) -> bool:
    """pipx's bin directory (``PIPX_BIN_DIR``, else ``~/.local/bin``) is on ``PATH``: its commands are found."""
    bin_dir = environ.get("PIPX_BIN_DIR") or str(home / ".local" / "bin")
    wanted = os.path.normpath(os.path.expanduser(bin_dir))
    return any(os.path.normpath(os.path.expanduser(d)) == wanted for d in (environ.get("PATH") or "").split(os.pathsep) if d)


ENSUREPATH_NOTE = (
    "It changes new shells only: open a new terminal before the next step. A shell that started before it"
    " (your agent's own, for one) still won't find remembra-relay: run pipx environment --value PIPX_BIN_DIR"
    " and put that directory in front of the command, or restart the agent."
)


def plan(
    sig: Signals,
    agents: list[str] | None = None,
    *,
    os_id: str | None = None,
    shell: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    server_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SetupPlan:
    """The steps for ``agents`` (default: those found here). ``server_url`` overrides the configured server."""
    env = os.environ if environ is None else environ
    if os_id is None:
        os_id, detected_shell = detect_os()
        shell = shell or detected_shell
    chosen = _selected(sig, agents)
    steps: list[Step] = []

    def add(**kwargs: Any) -> None:
        steps.append(Step(n=len(steps) + 1, **kwargs))

    if os_id == "windows":
        add(
            title="Windows setup is not tested",
            action="Stop here and follow docs.remembra.dev/guides/relay/ by hand; the hooks have not been run on Windows.",
            runs_where="user",
            stop=True,
        )
        return SetupPlan(os=os_id, shell=shell, agents=chosen, steps=tuple(steps))

    has_pipx = bool(which("pipx"))
    installed = bool(which("remembra-relay")) and bool(which("remembra-mcp"))
    if not has_pipx:
        add(
            title="Install pipx",
            command=cmd.PIPX_BOOTSTRAP.get(os_id, cmd.PIPX_BOOTSTRAP["other"]),
            runs_where="agent_ok",
            writes=("pipx (system package)", "your shell's startup file (PATH)"),
            needs_yes=True,
            note=ENSUREPATH_NOTE,
        )
    elif installed or pipx_bin_on_path(env, sig.home):
        add(title="pipx is installed", runs_where="agent_ok", done=True)
    else:
        # pipx is there but its bin directory is not on PATH (Homebrew's pipx, ensurepath never run): the
        # commands pipx installs next would not be found.
        add(
            title="pipx is installed; put the commands it installs on your PATH",
            command=cmd.PIPX_ENSUREPATH,
            runs_where="agent_ok",
            writes=("your shell's startup file (PATH), only when the directory is missing",),
            needs_yes=True,
            note=ENSUREPATH_NOTE,
        )
    primary = next((k for k in sig.keys if k.primary), None)
    has_key = primary is not None and primary.state != "missing"
    add(
        title=f"A key is saved ({primary.source})" if has_key and primary else "Get a free key",
        action=None if has_key else f"Create a free key at {cmd.SIGNUP_URL} (API keys). Stop here until you have it.",
        runs_where="user",
        done=has_key,
        stop=not has_key,
    )
    add(
        title="remembra is installed" if installed else "Install remembra (with the MCP server)",
        command=None if installed else cmd.PIPX_INSTALL,
        runs_where="agent_ok",
        writes=() if installed else ("the pipx environment of remembra",),
        needs_yes=not installed,
        done=installed,
    )
    needs_mcp = [a for a in chosen if a in INSTALLER_AGENTS and sig.agents[a].mcp != "configured"]
    key_command = cmd.key_step(server_url or sig.server_url)
    saved = has_key and not needs_mcp
    add(
        title="Key saved and MCP server added" if saved else "Save the key and add the Remembra MCP server",
        command=None if saved else key_command,
        runs_where="user_terminal",
        writes=() if saved else ("~/.remembra/credentials", "the remembra MCP entry of each agent it finds"),
        needs_yes=not saved,
        done=saved,
        note=None if saved else "It asks for the key at a hidden prompt. Never paste a key into a chat.",
    )
    unwritten = [a for a in chosen if not sig.agents[a].hooks_written]
    verified_new = [a for a in unwritten if REGISTRY[a].spec.verified]
    unverified_new = [a for a in unwritten if not REGISTRY[a].spec.verified]
    # `remembra-relay connect` with no --agent looks at every agent found here, and --apply then writes the
    # verified ones: the lines remembra.dev and setup.md show. They are used unless they would miss a chosen
    # agent (one not found here yet) or write one that was left out; then each agent is named.
    left_out = [n for n, a in sig.agents.items() if n not in chosen and a.verified and a.detected and not a.hooks_written]
    whole = not left_out and all(sig.agents[a].detected for a in verified_new)
    add(
        title="Hooks are written" if not unwritten else "See what connect would change (dry run)",
        command=None if not unwritten else cmd.connect(() if whole else unwritten, apply=False),
        runs_where="agent_ok",
        done=not unwritten,
        note=None if not unwritten else "Writes nothing; show the output and ask before the next step.",
    )
    if verified_new:
        add(
            title="Write the hooks",
            command=cmd.connect(() if whole else verified_new),
            runs_where="agent_ok",
            writes=tuple(sig.agents[a].config_path for a in verified_new),
            needs_yes=True,
            note="A backup of each file is kept.",
        )
    for agent in unverified_new:
        add(
            title=f"Only if you want them: {words.agent_name(agent)}'s hooks",
            command=cmd.agent_connect(agent),
            runs_where="agent_ok",
            writes=(sig.agents[agent].config_path,),
            needs_yes=True,
            note=f"{words.unverified_line(agent)} Without --include-unverified, connect --apply leaves it out.",
        )
    if "codex" in chosen:
        trust = sig.codex.trust
        trusted = trust is not None and trust.all_trusted and "codex" not in unwritten
        add(
            title="Codex trusts the hooks" if trusted else "Trust the hooks in Codex",
            action=None if trusted else words.codex_trust_fix(),
            runs_where="codex_ui",
            done=trusted,
            note=None if trusted else words.say("CODEX_TRUST_MISSING", "detail"),
        )
    by_hand = [a for a in chosen if a in ("qwen", "kimi")]
    if by_hand:
        add(
            title="Add the MCP server by hand: " + ", ".join(words.agent_name(a) for a in by_hand),
            action="remembra-install does not write these yet; see docs.remembra.dev/guides/relay/#mcp-by-hand.",
            runs_where="user",
        )
    if not saved or not installed:
        add(
            title="Restart your agents",
            action="Restart each agent so it loads the Remembra MCP server and the hooks.",
            runs_where="user",
        )
    add(
        title="Check",
        command=cmd.doctor(),
        runs_where="agent_ok",
        note="Then end this session: the next agent starts with the brief.",
    )
    return SetupPlan(os=os_id, shell=shell, agents=chosen, steps=tuple(steps))


def render_plan(sig: Signals, setup: SetupPlan) -> str:
    head = f"remembra setup · {OS_LABELS.get(setup.os, setup.os)}" + (f" · {setup.shell}" if setup.shell else "")
    agents = ", ".join(setup.agents)
    lines = [head + " " * max(1, WIDTH - len(head) - len("marshal")) + "marshal"]
    lines += textwrap.wrap(f"agents: {agents}", width=WIDTH, initial_indent="  ", subsequent_indent="          ")
    indent = " " * 12

    def wrap(text: str) -> None:
        lines.extend(textwrap.wrap(text, width=WIDTH, initial_indent=indent, subsequent_indent=indent) or [indent])

    for step in setup.steps:
        mark = "[ok]" if step.done else "    "
        lines.append(f"  {mark} {step.n:>2}  {step.title}")
        if step.done:
            continue
        if step.command:
            lines.append(f"{indent}{step.command}")  # never wrapped: a broken command would not paste
        if step.action:
            wrap(step.action)
        where = {
            "agent_ok": "your agent may run it after you say yes" if step.needs_yes else "your agent may run it",
            "user_terminal": "run it yourself in your own terminal: it involves your key",
            "codex_ui": "done in Codex",
            "user": "yours to do",
        }.get(step.runs_where, "")
        if step.writes:
            where += "; writes " + ", ".join(step.writes)
        if where:
            wrap(where)
        if step.note:
            wrap(step.note)
    left = sum(1 for s in setup.steps if not s.done)
    lines.append(
        f"  {left} step{'s' if left != 1 else ''} left." if left else "  Nothing left: run remembra-relay doctor any time."
    )
    footer = (
        f"Built by rules in remembra {sig.version} from Remembra's adapter specs and this machine's files."
        " No model wrote this. Nothing was changed."
    )
    lines += textwrap.wrap(footer, width=WIDTH, initial_indent="  ", subsequent_indent="  ")
    return "\n".join(lines) + "\n"
