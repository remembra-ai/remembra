"""``remembra-crew connect``: wire crew mode into this machine, only with consent (spec §8.2, §8.4, §11, D22).

``connect`` builds one plan of every file it would write, shows it as a dry-run
diff, and writes **nothing** until the owner confirms at an interactive TTY:

* the vendored gate ``~/.remembra/crew/bin/crew-gate.py`` (from WP-9's runtime);
* Claude Code crew hooks (§8.2) in ``~/.claude/settings.json`` (global, the
  default: the gate exits in <10 ms in repos without ``.remembra/``) or, with
  ``--scope project --repo R``, in ``R/.claude/settings.json`` only
  (``settings.local.json`` when ``settings.json`` is tracked by git);
* with ``--include-unverified``: observe-mode crew hooks for Codex, Cursor,
  Gemini, Qwen and Kimi (§8.3; ``remembra-crew verify`` switches one to enforce);
* the crewd supervisor: a launchd LaunchAgent (``KeepAlive``) on macOS or a
  systemd ``--user`` unit on Linux, loaded after writing;
* with ``--git-hooks``: ``pre-commit``, ``prepare-commit-msg`` and ``pre-push``
  in each ``--repo``, chained with Husky/lefthook/existing hooks (§8.4);
* with ``--agents-md PATH``: the relay + crew section of an AGENTS.md;
* ``~/.remembra/crew/adapters.json`` and the install manifest ``install.json``
  (which records the consent crewd needs to repair a wiped git hook).

Without ``--apply`` it is a dry run. ``--apply`` shows the same diff and then
asks; it refuses outright when stdin is not a TTY, and ``--yes`` only skips the
question, never the TTY check. Existing files are backed up. Re-running is
idempotent. ``--uninstall`` plans the reverse under the same consent rule.

Interface to WP-9: the gate text comes from
``remembra.relay.crew.gate.vendored_source()`` (``--gate-source FILE`` overrides
it), crewd runs as ``remembra-crewd`` (or ``python -m remembra.relay.crew.crewd``),
and crewd calls :func:`ensure_git_hooks` at SessionStart and every heartbeat.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from remembra.crew import schemas as S
from remembra.relay.adapters import REGISTRY, agents_md, relay_command
from remembra.relay.adapters.claude_code import CREW_LEGACY_MARKERS
from remembra.relay.adapters.crew_hooks import CrewCommands, CrewSpec, crew_specs, render
from remembra.relay.crew import githooks
from remembra.relay.crew.githooks import FileOp, GateCommand, apply_op
from remembra.relay.crew.verify import adapters_path, crew_home, read_adapters, render_adapters

MANIFEST_VERSION = 1
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class InstallError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Commands the hooks and the service run
# ---------------------------------------------------------------------------


def default_crew_command() -> str:
    found = shutil.which("remembra-crew")
    if found:
        return shlex.quote(str(Path(found).resolve()))
    return f"{shlex.quote(sys.executable)} -m remembra.relay.crew.cli"


def default_crewd_command() -> list[str] | None:
    found = shutil.which("remembra-crewd")
    if found:
        return [str(Path(found).resolve())]
    if importlib.util.find_spec("remembra.relay.crew.crewd") is not None:
        return [sys.executable, "-m", "remembra.relay.crew.crewd"]
    return None


def gate_source_text(path: str | None) -> str:
    """The vendored gate: ``--gate-source`` or WP-9's ``remembra.relay.crew.gate.vendored_source()``."""
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    try:
        module = importlib.import_module("remembra.relay.crew.gate")
    except ImportError as e:
        raise InstallError("the crew runtime is not installed (remembra.relay.crew.gate is missing)") from e
    source = getattr(module, "vendored_source", None)
    if not callable(source):
        raise InstallError("remembra.relay.crew.gate has no vendored_source(); update remembra")
    text = source()
    if not isinstance(text, str) or not text.strip():
        raise InstallError("remembra.relay.crew.gate.vendored_source() returned nothing")
    return text


def gate_path(home: Path) -> Path:
    return crew_home(home) / "bin" / "crew-gate.py"


def gate_bundle_ops(
    home: Path, python: str, crewd_argv: Sequence[str] | None, *, remove: bool
) -> tuple[list[FileOp], list[Path]]:
    """The gate's ``bin/lib`` decision core and ``bin/crewd.json`` (WP-9 ``vendor_gate`` layout), as file ops.

    The launcher that :func:`gate_source_text` returns imports everything from ``bin/lib``, and its sha
    header covers those files, so they are written with it (and removed with it). Returns the ops and
    the directories to create 0700 first.
    """
    from remembra.relay.crew import gate as G

    layout = G.Layout(Path(home))
    lib = layout.lib
    files = {} if remove else G.bundle_files()
    ops: list[FileOp] = []
    dirs: list[Path] = [lib] if files else []
    for rel, content in sorted(files.items()):
        path = lib / rel
        ops.append(FileOp(path, _read(path), content.decode("utf-8"), 0o600, [], backup=False, quiet=True))
        for depth in range(1, len(Path(rel).parts)):  # every level under lib, parents first, all 0700
            directory = lib.joinpath(*Path(rel).parts[:depth])
            if directory not in dirs:
                dirs.append(directory)
    if lib.is_dir():
        for path in sorted(lib.rglob("*.py")):
            if "__pycache__" not in path.parts and path.relative_to(lib).as_posix() not in files:
                ops.append(FileOp(path, _read(path), None, None, [], backup=False, quiet=True))
    cmd_path = layout.crewd_cmd
    if remove:
        if cmd_path.exists():
            ops.append(FileOp(cmd_path, _read(cmd_path), None, None, [f"remove {cmd_path}"], backup=False))
    else:
        argv = list(crewd_argv) if crewd_argv else [python, "-m", "remembra.relay.crew.crewd"]
        text = json.dumps({"argv": argv, "python": python}, separators=(",", ":"), sort_keys=True)
        before = _read(cmd_path)
        if before is not None and _same_json(before, text):
            before = text  # same content, other formatting: no change
        ops.append(FileOp(cmd_path, before, text, 0o600, [f"record how hooks respawn crewd ({cmd_path.name})"], backup=False))
    return ops, dirs


def _same_json(a: str, b: str) -> bool:
    try:
        return bool(json.loads(a) == json.loads(b))
    except ValueError:
        return False


def manifest_path(home: Path) -> Path:
    return crew_home(home) / "install.json"


# ---------------------------------------------------------------------------
# Service units
# ---------------------------------------------------------------------------


def launchd_plist_path(home: Path) -> Path:
    return Path(home) / "Library" / "LaunchAgents" / f"{S.CREWD_LAUNCHD_LABEL}.plist"


def systemd_unit_path(home: Path) -> Path:
    return Path(home) / ".config" / "systemd" / "user" / S.CREWD_SYSTEMD_UNIT


def _service_path_env(python: str) -> str:
    parts = [str(Path(python).parent), *SERVICE_PATH.split(":")]
    return ":".join(dict.fromkeys(parts))


def render_launchd_plist(home: Path, program: Sequence[str], python: str) -> str:
    log = crew_home(home) / "log" / "crewd.launchd.log"
    body = {
        "Label": S.CREWD_LAUNCHD_LABEL,
        "ProgramArguments": list(program),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": _service_path_env(python)},
    }
    return plistlib.dumps(body, sort_keys=False).decode("utf-8")


def _systemd_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/._-+=:@%," for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_systemd_unit(program: Sequence[str], python: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Remembra crew daemon (remembra-crewd)",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "ExecStart=" + " ".join(_systemd_quote(a) for a in program),
            "Restart=always",
            "RestartSec=5",
            f"Environment={_systemd_quote('PATH=' + _service_path_env(python))}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


@dataclass(frozen=True)
class ServiceCommand:
    argv: tuple[str, ...]
    allow_fail: bool = False  # e.g. bootout of an agent that is not loaded yet


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class Section:
    title: str
    ops: list[FileOp] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(op.changed for op in self.ops)


@dataclass
class InstallPlan:
    sections: list[Section]
    pre_commands: list[ServiceCommand] = field(default_factory=list)  # before writing (uninstall: unload first)
    post_commands: list[ServiceCommand] = field(default_factory=list)  # after writing (install: load)
    ensure_dirs: list[Path] = field(default_factory=list)

    @property
    def ops(self) -> list[FileOp]:
        return [op for s in self.sections for op in s.ops if op.changed]

    @property
    def changed(self) -> bool:
        return bool(self.ops)


@dataclass
class ConnectOptions:
    home: Path
    agents: list[str] = field(default_factory=list)
    include_unverified: bool = False
    scope: str = "global"
    repos: list[Path] = field(default_factory=list)
    git_hooks: bool = False
    agents_md: Path | None = None
    service: bool = True
    load_service: bool = True
    uninstall: bool = False
    python: str = sys.executable
    crew_command: str | None = None
    crewd_command: list[str] | None = None
    gate_source: str | None = None
    platform: str = sys.platform
    uid: int = field(default_factory=os.getuid)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_manifest(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(manifest_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claude_targets(opts: ConnectOptions) -> list[tuple[Path, str]]:
    """(settings file, note) for Claude Code: global, or project-level per repo."""
    spec = crew_specs()["claude-code"]
    if opts.scope == "global":
        return [(REGISTRY["claude-code"].spec.config_file(opts.home), "")]
    targets = []
    for repo in opts.repos:
        top = githooks.inspect(repo).toplevel
        assert spec.project_config is not None
        path = spec.project_config(top)
        info = githooks.inspect(top)
        if path.exists() and info.tracked(path):
            local = path.with_name("settings.local.json")
            targets.append((local, f"{path} is tracked by git, so the crew hooks go to {local.name} (never committed)"))
        else:
            targets.append((path, ""))
    return targets


def _selected_agents(opts: ConnectOptions, section: Section) -> list[str]:
    specs = crew_specs()
    wanted = [a.lower() for a in opts.agents]
    unknown = [a for a in wanted if a not in specs]
    if unknown:
        raise InstallError(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(specs)}")
    chosen: list[str] = []
    for name, spec in specs.items():
        named = name in wanted
        if wanted and not named:
            continue
        if not spec.verified and not opts.include_unverified:
            if named:
                section.notes.append(
                    f"{name}: unverified adapter, not written (add --include-unverified; it then runs in observe mode)"
                )
            continue
        if not named and not REGISTRY[name].detect(opts.home):
            section.notes.append(f"{name}: not detected, skipped")
            continue
        chosen.append(name)
    return chosen


def _agent_op(path: Path, spec: CrewSpec, cmds: CrewCommands, remove: bool) -> FileOp:
    before = _read(path)
    if remove and before is None:
        return FileOp(path, None, None, None, [])
    legacy = CREW_LEGACY_MARKERS if spec.adapter == "claude-code" else ()
    after, summary = render(before, spec, cmds, remove=remove, legacy_markers=legacy, path_hint=str(path))
    if path.is_symlink() and after != before:
        # a dotfiles-managed config stays a link: the change is written to the file it points to
        summary = [*summary, f"{path} is a symlink: the change is written to its target {os.path.realpath(path)}"]
    return FileOp(path, before, after, None if before is not None else 0o600, summary)


def build_plan(opts: ConnectOptions) -> InstallPlan:
    """Everything ``connect`` (or ``connect --uninstall``) would do. Reads only; writes nothing."""
    home = Path(opts.home)
    manifest = read_manifest(home)
    python = opts.python
    crew_cmd = opts.crew_command or default_crew_command()
    gate = gate_path(home)
    cmds = CrewCommands(python=python, gate=str(gate), crew=crew_cmd)
    remove = opts.uninstall
    recorded_agents = manifest.get("agents")
    recorded: dict[str, Any] = recorded_agents if isinstance(recorded_agents, dict) else {}
    # A partial uninstall (--uninstall --agent X) keeps the gate, crewd and git hooks for the others.
    full_remove = remove and (not opts.agents or set(recorded) <= set(opts.agents))
    sections: list[Section] = []
    plan = InstallPlan(sections)

    # 1. The vendored gate (hooks point at it).
    gate_section = Section("Crew gate")
    if not remove:
        text = gate_source_text(opts.gate_source)
        gate_section.ops.append(
            FileOp(gate, _read(gate), text, 0o600, [f"install the vendored crew gate at {gate}"], backup=False)
        )
        plan.ensure_dirs += [crew_home(home), crew_home(home) / "bin", crew_home(home) / "log"]
        if opts.gate_source is None:  # the packaged gate: its decision core goes to bin/lib next to it
            bundle_ops, bundle_dirs = gate_bundle_ops(home, python, opts.crewd_command or default_crewd_command(), remove=False)
            gate_section.ops += bundle_ops
            plan.ensure_dirs += bundle_dirs
            gate_section.notes.append(
                f"the gate's decision core ({sum(1 for op in bundle_ops if op.quiet)} package source files)"
                f" goes to {gate.parent / 'lib'}; package sources are listed without a diff"
            )
    elif full_remove:
        if gate.exists():
            gate_section.ops.append(FileOp(gate, _read(gate), None, None, [f"remove {gate}"], backup=False))
        gate_section.ops += gate_bundle_ops(home, python, None, remove=True)[0]
    sections.append(gate_section)

    # 2. Agent hooks.
    agent_section = Section("Agent hooks")
    adapter_updates: dict[str, dict[str, Any] | None] = {}
    existing_adapters = read_adapters(home)["adapters"]
    manifest_agents: dict[str, list[str]] = {}
    if remove:
        for name, paths in recorded.items():
            if name not in crew_specs() or (opts.agents and name not in opts.agents):
                continue
            for p in paths if isinstance(paths, list) else []:
                op = _agent_op(Path(p), crew_specs()[name], cmds, remove=True)
                agent_section.ops.append(op)
            adapter_updates[name] = {"installed": False}
    else:
        for name in _selected_agents(opts, agent_section):
            spec = crew_specs()[name]
            targets = _claude_targets(opts) if name == "claude-code" else [(REGISTRY[name].spec.config_file(home), "")]
            for path, note in targets:
                if note:
                    agent_section.notes.append(note)
                agent_section.ops.append(_agent_op(path, spec, cmds, remove=False))
                manifest_agents.setdefault(name, []).append(str(path))
            prior = existing_adapters.get(name) if isinstance(existing_adapters.get(name), dict) else {}
            verified_local = bool(prior.get("verified_local"))
            adapter_updates[name] = {
                "installed": True,
                "config": [str(p) for p, _ in targets],
                "spec_verified": spec.verified,
                "verified_local": verified_local,
                "enforcement": "enforce" if (spec.verified or verified_local) else "observe",
                "output": spec.output,
            }
            if not spec.verified and not verified_local:
                agent_section.notes.append(
                    f"{name}: runs in OBSERVE mode (logs would-deny, injects context)"
                    f" until `remembra-crew verify --agent {name}` passes"
                )
        if opts.scope == "project" and "claude-code" in manifest_agents:
            agent_section.notes.append(
                "project-level install: global ~/.claude/settings.json is not changed; if it still has remembra-relay"
                " SessionStart/SessionEnd hooks, crew repos get the relay brief twice"
            )
    sections.append(agent_section)

    # 3. crewd supervisor.
    service_section = Section("crewd supervisor")
    if opts.service and (full_remove or not remove):
        _plan_service(opts, service_section, plan, remove)
    elif remove:
        service_section.notes.append("kept: other agents still use crewd")
    else:
        service_section.notes.append("skipped (--no-service): hooks respawn crewd, but nothing restarts it after a crash")
    sections.append(service_section)

    # 4. Git hooks.
    git_section = Section("Git hooks")
    repo_records: dict[str, Any] = {}
    repos = list(opts.repos)
    if full_remove and not repos:
        repos = [Path(p) for p in (manifest.get("repos") or {})]
    if (opts.git_hooks and not remove) or (full_remove and repos):
        for repo in repos:
            try:
                hp = githooks.plan_uninstall(repo) if remove else githooks.plan_install(repo, GateCommand(python, str(gate)))
            except githooks.GitHookError as e:
                git_section.notes.append(f"{repo}: {e}")
                continue
            git_section.ops += hp.ops
            git_section.notes += [f"{hp.repo.toplevel}: {n}" for n in hp.notes]
            methods = ", ".join(f"{h}={m}" for h, m in hp.methods.items())
            git_section.notes.append(f"{hp.repo.toplevel}: {methods or 'nothing to change'}")
            repo_records[str(hp.repo.toplevel)] = None if remove else {"methods": hp.methods}
    sections.append(git_section)

    # 5. AGENTS.md block.
    md_section = Section("AGENTS.md")
    if opts.agents_md is not None:
        change = agents_md.plan(Path(opts.agents_md).expanduser(), relay_command(), None if remove else crew_cmd)
        md_section.ops.append(
            FileOp(change.path, change.before, change.after, None if change.before is not None else 0o644, change.summary)
        )
    sections.append(md_section)

    # 6. Status files.
    status_section = Section("Crew status files")
    if adapter_updates:
        before = _read(adapters_path(home))
        status_section.ops.append(
            FileOp(
                adapters_path(home),
                before,
                render_adapters(before, adapter_updates),
                0o600,
                ["record adapter status (adapters.json)"],
                backup=False,
            )
        )
    status_section.ops.append(_manifest_op(home, manifest, opts, cmds, manifest_agents, repo_records))
    sections.append(status_section)
    if remove:
        # Uninstall takes the hooks out before the gate they run: if a later write fails ("Stopped: …
        # re-run to finish"), no agent or git hook is left pointing at a missing crew-gate.py. The
        # manifest (no agents left) is written before the gate goes, so crewd stops restoring it.
        order = {s.title: i for i, s in enumerate(sections)}
        wanted = ["Agent hooks", "Git hooks", "AGENTS.md", "Crew status files", "crewd supervisor", "Crew gate"]
        sections.sort(key=lambda s: wanted.index(s.title) if s.title in wanted else len(wanted) + order[s.title])
    return plan


def _plan_service(opts: ConnectOptions, section: Section, plan: InstallPlan, remove: bool) -> None:
    home = Path(opts.home)
    if opts.platform == "darwin":
        path = launchd_plist_path(home)
        target = f"gui/{opts.uid}"
        if remove:
            if path.exists():
                plan.pre_commands.append(ServiceCommand(("launchctl", "bootout", f"{target}/{S.CREWD_LAUNCHD_LABEL}"), True))
                section.ops.append(FileOp(path, _read(path), None, None, [f"remove the LaunchAgent {path}"], backup=False))
            return
        program = opts.crewd_command or default_crewd_command()
        if not program:
            raise InstallError("remembra-crewd is not installed (the crew runtime is missing); use --no-service to skip it")
        text = render_launchd_plist(home, program, opts.python)
        # No backup next to it: launchd would load a second plist with the same label.
        summary = [f"LaunchAgent {S.CREWD_LAUNCHD_LABEL} (KeepAlive) runs {' '.join(program)}"]
        op = FileOp(path, _read(path), text, 0o644, summary, backup=False)
        section.ops.append(op)
        if op.changed and opts.load_service:
            plan.post_commands += [
                ServiceCommand(("launchctl", "bootout", f"{target}/{S.CREWD_LAUNCHD_LABEL}"), True),
                ServiceCommand(("launchctl", "bootstrap", target, str(path))),
            ]
        elif op.changed:
            section.notes.append(f"not loaded (--no-service-load); load it with: launchctl bootstrap {target} {path}")
    elif opts.platform.startswith("linux"):
        path = systemd_unit_path(home)
        unit = S.CREWD_SYSTEMD_UNIT
        if remove:
            if path.exists():
                plan.pre_commands.append(ServiceCommand(("systemctl", "--user", "disable", "--now", unit), True))
                section.ops.append(FileOp(path, _read(path), None, None, [f"remove the systemd user unit {path}"], backup=False))
                plan.post_commands.append(ServiceCommand(("systemctl", "--user", "daemon-reload"), True))
            return
        program = opts.crewd_command or default_crewd_command()
        if not program:
            raise InstallError("remembra-crewd is not installed (the crew runtime is missing); use --no-service to skip it")
        op = FileOp(
            path,
            _read(path),
            render_systemd_unit(program, opts.python),
            0o644,
            [f"systemd --user unit {unit} (Restart=always)"],
            backup=False,
        )
        section.ops.append(op)
        if op.changed and opts.load_service:
            plan.post_commands += [
                ServiceCommand(("systemctl", "--user", "daemon-reload")),
                ServiceCommand(("systemctl", "--user", "enable", unit)),
                ServiceCommand(("systemctl", "--user", "restart", unit)),
            ]
        elif op.changed:
            section.notes.append(f"not loaded (--no-service-load); start it with: systemctl --user enable --now {unit}")
    else:
        section.notes.append(f"crewd supervision is not supported on {opts.platform} (macOS and Linux only)")


def _manifest_op(
    home: Path,
    manifest: dict[str, Any],
    opts: ConnectOptions,
    cmds: CrewCommands,
    agents: dict[str, list[str]],
    repos: dict[str, Any],
) -> FileOp:
    path = manifest_path(home)
    before = _read(path)
    data: dict[str, Any] = json.loads(json.dumps(manifest)) if manifest else {}
    data["version"] = MANIFEST_VERSION
    if opts.uninstall:
        for name in list(data.get("agents") or {}):
            if not opts.agents or name in opts.agents:
                data["agents"].pop(name, None)
        for repo in repos:
            (data.get("repos") or {}).pop(repo, None)
    else:
        data.update({"python": cmds.python, "gate": cmds.gate, "crew_command": cmds.crew, "scope": opts.scope})
        recorded = data.setdefault("agents", {})
        for name, paths in agents.items():
            recorded[name] = sorted(set(recorded.get(name) or []) | set(paths))
        known = data.setdefault("repos", {})
        for repo, record in repos.items():
            prior = known.get(repo) or {}
            known[repo] = {"methods": record["methods"], "consented_at": prior.get("consented_at") or _now()}
    if before is not None:
        try:
            if json.loads(before) == data:
                return FileOp(path, before, before, 0o600, [])
        except ValueError:
            pass
    return FileOp(
        path,
        before,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        0o600,
        ["record what was installed, with consent (install.json)"],
        backup=False,
    )


# ---------------------------------------------------------------------------
# Show, consent, apply
# ---------------------------------------------------------------------------


def show_plan(plan: InstallPlan, out: TextIO) -> None:
    for section in plan.sections:
        if not section.ops and not section.notes:
            continue
        print(f"\n== {section.title} ==", file=out)
        for note in section.notes:
            print(f"  note: {note}", file=out)
        for op in section.ops:
            if not op.changed:
                print(f"  {op.path}: no change", file=out)
                continue
            for line in op.summary:
                print(f"  - {line}", file=out)
            if op.quiet:
                verb = "remove" if op.after is None else ("add" if op.before is None else "update")
                print(f"  {verb}: {op.path}", file=out)
                continue
            diff = op.diff()
            if diff:
                print("  " + diff.replace("\n", "\n  ").rstrip(), file=out)
            if op.after is not None and op.mode is not None and op.before is None:
                print(f"  (new file, mode {op.mode:o})", file=out)
    for cmd in plan.pre_commands + plan.post_commands:
        print(f"  will run: {' '.join(shlex.quote(a) for a in cmd.argv)}", file=out)


def consent(stdin: TextIO, out: TextIO, *, count: int, assume_yes: bool) -> tuple[bool, str]:
    """Interactive confirmation. A non-TTY stdin always refuses, with or without ``--yes``."""
    try:
        interactive = stdin.isatty()
    except (AttributeError, ValueError, OSError):
        interactive = False
    if not interactive:
        return False, "consent needs an interactive terminal (stdin is not a TTY)"
    if assume_yes:
        return True, "confirmed with --yes at a TTY"
    out.write(f"\nWrite these {count} change(s)? Backups are kept. Type 'yes' to confirm: ")
    out.flush()
    answer = stdin.readline().strip().lower()
    if answer in ("y", "yes"):
        return True, "confirmed at the terminal"
    return False, "not confirmed"


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)


def apply_plan(plan: InstallPlan, *, runner: Runner, out: TextIO, stamp: str | None = None) -> int:
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    code = 0
    for cmd in plan.pre_commands:
        code |= _run(cmd, runner, out)
    for directory in plan.ensure_dirs:
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    for op in plan.ops:
        backup = apply_op(op, stamp)
        verb = "removed" if op.after is None else "written"
        print(f"  {verb}: {op.path}{f' (backup: {backup})' if backup else ''}", file=out)
    for cmd in plan.post_commands:
        code |= _run(cmd, runner, out)
    return code


def _run(cmd: ServiceCommand, runner: Runner, out: TextIO) -> int:
    shown = " ".join(shlex.quote(a) for a in cmd.argv)
    try:
        proc = runner(cmd.argv)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  ran: {shown} -> {e.__class__.__name__}", file=out)
        return 0 if cmd.allow_fail else 1
    ok = proc.returncode == 0
    detail = "ok" if ok else f"exit {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:200]}"
    print(f"  ran: {shown} -> {detail}", file=out)
    return 0 if ok or cmd.allow_fail else 1


# ---------------------------------------------------------------------------
# crewd interface: repair a wiped git hook (only where the owner consented)
# ---------------------------------------------------------------------------


def ensure_git_hooks(home: Path, repo: Path) -> dict[str, str]:
    """``githook_state`` per hook for ``repo``, reinstalling missing ones if ``connect`` recorded consent for it."""
    manifest = read_manifest(home)
    try:
        top = str(githooks.inspect(repo).toplevel)
    except githooks.GitHookError:
        return dict.fromkeys(githooks.HOOKS, "unknown")
    consented = top in (manifest.get("repos") or {})
    python = str(manifest.get("python") or sys.executable)
    gate = str(manifest.get("gate") or gate_path(home))
    return githooks.ensure(Path(top), GateCommand(python, gate), consented=consented)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remembra-crew connect",
        description="Install crew mode on this machine. Dry run by default; --apply asks for consent at a terminal.",
    )
    p.add_argument("--crew", action="store_true", help="(accepted for compatibility; crew mode is what connect installs)")
    p.add_argument("--agent", action="append", default=[], help=f"Only these agents ({', '.join(crew_specs())}); repeatable")
    p.add_argument("--include-unverified", action="store_true", help="Also wire unverified adapters (observe mode)")
    p.add_argument(
        "--scope", choices=["global", "project"], default="global", help="Claude Code hooks: global (default) or per repo"
    )
    p.add_argument("--repo", action="append", default=[], help="Repository for --scope project and --git-hooks (repeatable)")
    p.add_argument("--git-hooks", action="store_true", help="Install the pre-commit, prepare-commit-msg and pre-push gates")
    p.add_argument("--agents-md", help="Write the relay + crew section into this AGENTS.md")
    p.add_argument("--no-service", action="store_true", help="Do not install the crewd LaunchAgent / systemd unit")
    p.add_argument("--no-service-load", action="store_true", help="Write the service file but do not load it")
    p.add_argument("--apply", action="store_true", help="Write after showing the diff (asks for confirmation at a TTY)")
    p.add_argument("--yes", action="store_true", help="Skip the question (still requires an interactive terminal)")
    p.add_argument("--uninstall", action="store_true", help="Remove what connect installed (same consent rule)")
    p.add_argument("--python", help=argparse.SUPPRESS)
    p.add_argument("--crew-command", help=argparse.SUPPRESS)
    p.add_argument("--crewd-command", help=argparse.SUPPRESS)
    p.add_argument("--gate-source", help=argparse.SUPPRESS)
    return p


def main(
    argv: list[str] | None = None,
    *,
    home: Path | None = None,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
    runner: Runner | None = None,
    platform: str | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    home = Path(home or os.environ.get("HOME") or Path.home())
    if args.scope == "project" and not args.repo:
        print("remembra-crew connect: --scope project needs at least one --repo", file=sys.stderr)
        return 2
    if args.git_hooks and not args.repo:
        args.repo = [os.getcwd()]
    opts = ConnectOptions(
        home=home,
        agents=args.agent,
        include_unverified=args.include_unverified,
        scope=args.scope,
        repos=[Path(r).expanduser() for r in args.repo],
        git_hooks=args.git_hooks,
        agents_md=Path(args.agents_md) if args.agents_md else None,
        service=not args.no_service,
        load_service=not args.no_service_load,
        uninstall=args.uninstall,
        python=args.python or sys.executable,
        crew_command=args.crew_command,
        crewd_command=shlex.split(args.crewd_command) if args.crewd_command else None,
        gate_source=args.gate_source,
        platform=platform or sys.platform,
    )
    try:
        plan = build_plan(opts)
    except (InstallError, githooks.GitHookError, ValueError) as e:
        print(f"remembra-crew connect: {e}. Nothing was written.", file=sys.stderr)
        return 1
    print(f"Remembra crew {'uninstall' if opts.uninstall else 'connect'} plan (home {home})", file=out)
    show_plan(plan, out)
    if not plan.changed and not plan.post_commands and not plan.pre_commands:
        print("\nAlready up to date: nothing to write.", file=out)
        return 0
    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply in an interactive terminal to write it.", file=out)
        return 0
    ok, why = consent(stdin, out, count=len(plan.ops), assume_yes=args.yes)
    if not ok:
        print(f"\nNothing was written: {why}.", file=out)
        return 2 if "TTY" in why else 1
    print(f"\nWriting ({why}):", file=out)
    try:
        code = apply_plan(plan, runner=runner or _default_runner, out=out)
    except OSError as e:
        print(f"\nStopped: {e}. Files listed as written above were written (with backups); re-run to finish.", file=out)
        return 1
    print("\nDone." if code == 0 else "\nWritten, but a service command failed (see above).", file=out)
    return code


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
