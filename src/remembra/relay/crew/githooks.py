"""Crew git gates (spec §8.4, D22): ``pre-commit``, ``prepare-commit-msg`` and ``pre-push``.

Installed by ``remembra-crew connect --git-hooks`` after the consent step, never
committed, and always chained with whatever the repository already runs:

* **lefthook** (the active hook file runs ``lefthook``): a ``remembra-crew``
  command is added to the local, untracked ``lefthook-local.yml``;
* **Husky v9** (``core.hooksPath = .husky/_``): one guarded line in the
  untracked ``.husky/<hook>`` sources an include kept under
  ``<git-common-dir>/hooks/remembra-crew/<hook>``, which a Husky reinstall
  cannot wipe. When ``.husky/<hook>`` is tracked (the repo does not allow a
  local line), the plain fallback below is used on Husky's runner instead;
* **plain** (anything else): the crew script is written to the active hook
  path (``git rev-parse --git-path hooks``). An existing hook is moved to
  ``<hook>.remembra-prev`` and run **first** (delegation), with the same
  arguments and, for ``pre-push``, the same stdin.

Tracked files are never modified, and a hooks path outside the repository (a
global ``core.hooksPath``) is never written: those hooks are reported as
``manual`` with the exact line to add. Files created inside the worktree are
listed in a managed block of ``.git/info/exclude`` so they never show up as
untracked. Every crew line carries ``# remembra-crew`` so the gate's tamper
protection (D28) recognises it.

The hooks run the vendored gate: ``<python> -I ~/.remembra/crew/bin/crew-gate.py
precommit | trailer | prepush`` (WP-9). The gate resolves the committing session
through crewd from the process chain (never from the environment) and exits 1
to refuse. A missing gate file fails open with a note on stderr.

:func:`status` is the ``githook_state`` check crewd runs at every SessionStart
and heartbeat; :func:`ensure` reinstalls missing hooks when they were installed
with consent.
"""

from __future__ import annotations

import difflib
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from remembra.crew.schemas import CREW_HOOK_MARKER

HOOKS = ("pre-commit", "prepare-commit-msg", "pre-push")
GATE_VERBS = {"pre-commit": "precommit", "prepare-commit-msg": "trailer", "pre-push": "prepush"}
SCRIPT_VERSION = 1
SCRIPT_HEADER = "# remembra-crew git hook:"
INCLUDE_HEADER = "# remembra-crew git hook include:"
PREV_SUFFIX = ".remembra-prev"
INCLUDE_DIRNAME = "remembra-crew"
EXCLUDE_BEGIN = "# >>> remembra-crew (managed block) >>>"
EXCLUDE_END = "# <<< remembra-crew (managed block) <<<"
LEFTHOOK_COMMAND = "remembra-crew"
LEFTHOOK_CONFIGS = (
    "lefthook.yml",
    "lefthook.yaml",
    ".lefthook.yml",
    ".lefthook.yaml",
    ".config/lefthook.yml",
    ".config/lefthook.yaml",
    "lefthook.json",
    "lefthook.toml",
    ".lefthook.json",
    ".lefthook.toml",
)
LEFTHOOK_LOCALS = (
    "lefthook-local.yml",
    "lefthook-local.yaml",
    ".lefthook-local.yml",
    ".lefthook-local.yaml",
    ".config/lefthook-local.yml",
)
GIT_TIMEOUT_S = 10.0

# Method per hook, recorded in the install manifest and shown in the plan.
METHODS = ("plain", "delegate", "husky", "lefthook", "manual")


class GitHookError(RuntimeError):
    pass


@dataclass(frozen=True)
class GateCommand:
    """The interpreter and vendored gate the hooks run."""

    python: str
    gate: str

    def shell(self, verb: str) -> str:
        return f"{shlex.quote(self.python)} -I {shlex.quote(self.gate)} {verb}"


@dataclass
class FileOp:
    """One planned file change: write ``after`` (``None`` deletes the file)."""

    path: Path
    before: str | None
    after: str | None
    mode: int | None = None
    summary: list[str] = field(default_factory=list)
    backup: bool = True
    force: bool = False  # write even when the text is unchanged (e.g. restore a lost executable bit)

    @property
    def changed(self) -> bool:
        return self.force or self.before != self.after

    def diff(self) -> str:
        before = (self.before or "").splitlines(keepends=True)
        after = (self.after or "").splitlines(keepends=True)
        new = "/dev/null" if self.after is None else f"{self.path} (new)"
        old = "/dev/null" if self.before is None else f"{self.path} (current)"
        return "".join(difflib.unified_diff(before, after, fromfile=old, tofile=new))


def apply_op(op: FileOp, stamp: str | None = None) -> Path | None:
    """Apply one op atomically; returns the backup path (if one was kept)."""
    path = op.path
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup: Path | None = None
    exists = path.exists() or path.is_symlink()
    if exists and op.backup:
        backup = path.with_name(f"{path.name}.bak-crew-{stamp}")
        shutil.copy2(path, backup, follow_symlinks=True)
    if op.after is None:
        if exists:
            path.unlink()
        return backup
    mode = op.mode
    if mode is None:
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(op.after)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


# ---------------------------------------------------------------------------
# Repository inspection
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHookError(f"git {' '.join(args)} failed: {e.__class__.__name__}") from e
    if check and proc.returncode != 0:
        raise GitHookError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc


def _abs(repo: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else (repo / path)).resolve()


@dataclass(frozen=True)
class RepoHooks:
    toplevel: Path
    common_dir: Path
    hooks_dir: Path  # the ACTIVE hook path (honours core.hooksPath)
    hooks_path_config: str | None
    exclude_file: Path
    include_dir: Path  # <common-dir>/hooks/remembra-crew
    husky_v9: bool
    lefthook_config: Path | None

    @property
    def hooks_dir_writable_scope(self) -> bool:
        """The active hook path belongs to this repository (never a global or shared hooks path)."""
        return _is_under(self.hooks_dir, self.common_dir) or _is_under(self.hooks_dir, self.toplevel)

    def rel(self, path: Path) -> str | None:
        """Path relative to the worktree, or ``None`` when it is outside it."""
        return path.resolve().relative_to(self.toplevel).as_posix() if _is_under(path, self.toplevel) else None

    def exclude_entry(self, path: Path) -> str | None:
        """``/rel/path`` for ``.git/info/exclude`` when a created file would show as untracked, else ``None``."""
        rel = self.rel(path)
        if rel is None or _is_under(path, self.common_dir) or _is_under(path, self.toplevel / ".git"):
            return None
        return f"/{rel}"

    def tracked(self, path: Path) -> bool:
        rel = self.rel(path)
        if rel is None:
            return False
        return _git(self.toplevel, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def inspect(repo: Path) -> RepoHooks:
    """Resolve the hook paths of the repository (or worktree) containing ``repo``."""
    repo = Path(repo).expanduser()
    top = _abs(repo, _git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    common = _abs(top, _git(top, "rev-parse", "--git-common-dir").stdout.strip())
    hooks_dir = _abs(top, _git(top, "rev-parse", "--git-path", "hooks").stdout.strip())
    exclude = _abs(top, _git(top, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    configured = _git(top, "config", "--get", "core.hooksPath", check=False).stdout.strip() or None
    husky = hooks_dir == (top / ".husky" / "_").resolve() and (hooks_dir / "h").is_file()
    lefthook = next((top / name for name in LEFTHOOK_CONFIGS if (top / name).is_file()), None)
    return RepoHooks(
        toplevel=top,
        common_dir=common,
        hooks_dir=hooks_dir,
        hooks_path_config=configured,
        exclude_file=exclude,
        include_dir=common / "hooks" / INCLUDE_DIRNAME,
        husky_v9=husky,
        lefthook_config=lefthook,
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Script texts
# ---------------------------------------------------------------------------


def hook_script(hook: str, gate: GateCommand, *, source_prev: bool = False) -> str:
    """The crew hook. ``source_prev`` runs the kept hook as if it were still at this path
    (Husky's generated runners derive the hook name from ``$0``)."""
    verb = GATE_VERBS[hook]
    run_prev = "sh -c '. \"$0" + PREV_SUFFIX + '"\' "$0" "$@"' if source_prev else '"$prev" "$@"'
    lines = [
        "#!/bin/sh",
        f"{SCRIPT_HEADER} {hook} (v{SCRIPT_VERSION})",
        "# Installed by `remembra-crew connect` with the owner's consent. Local only: never commit it.",
        f"# It runs the hook it replaced ({hook}{PREV_SUFFIX}) first, then the crew gate.",
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
        f'prev="$hook_dir/{hook}{PREV_SUFFIX}"',
        f"py={shlex.quote(gate.python)}",
        f"gate={shlex.quote(gate.gate)}",
        '[ -x "$py" ] || py=python3',
    ]
    if hook == "pre-push":
        lines += [
            'if [ -x "$prev" ]; then',
            '  push_refs=$(mktemp "${TMPDIR:-/tmp}/remembra-crew-push.XXXXXX") || exit 1',
            "  trap 'rm -f \"$push_refs\"' EXIT",
            '  cat > "$push_refs"',
            f'  {run_prev} < "$push_refs" || exit $?',
            '  exec < "$push_refs"',
            "fi",
        ]
    else:
        lines += ['if [ -x "$prev" ]; then', f"  {run_prev} || exit $?", "fi"]
    lines += [
        'if [ ! -f "$gate" ]; then',
        f'  echo "remembra-crew: crew gate not found at $gate; {hook} not checked" >&2',
        "  exit 0",
        "fi",
        f'"$py" -I "$gate" {verb} "$@" {CREW_HOOK_MARKER}',
        "",
    ]
    return "\n".join(lines)


def is_crew_script(text: str | None) -> bool:
    """A hook file written by :func:`hook_script` (its header sits in the first lines)."""
    if not text:
        return False
    return any(line.startswith(SCRIPT_HEADER) for line in text.split("\n", 3)[:3])


def include_script(hook: str, gate: GateCommand) -> str:
    verb = GATE_VERBS[hook]
    return "\n".join(
        [
            f"{INCLUDE_HEADER} {hook} (v{SCRIPT_VERSION}). Sourced from .husky/{hook}; local only, never commit it.",
            f"_rc_py={shlex.quote(gate.python)}",
            f"_rc_gate={shlex.quote(gate.gate)}",
            '[ -x "$_rc_py" ] || _rc_py=python3',
            f'if [ -f "$_rc_gate" ]; then "$_rc_py" -I "$_rc_gate" {verb} "$@" || exit $?; '
            f'else echo "remembra-crew: crew gate not found; {hook} not checked" >&2; fi {CREW_HOOK_MARKER}',
            "",
        ]
    )


def husky_line(hook: str) -> str:
    """The one line in ``.husky/<hook>`` that sources the include (a no-op when the include is gone)."""
    return (
        f'_rc="$(git rev-parse --git-common-dir)/hooks/{INCLUDE_DIRNAME}/{hook}"; [ ! -f "$_rc" ] || . "$_rc" {CREW_HOOK_MARKER}'
    )


def lefthook_run(hook: str, gate: GateCommand) -> str:
    return f"test ! -f {shlex.quote(gate.gate)} || {gate.shell(GATE_VERBS[hook])} {{0}} {CREW_HOOK_MARKER}"


def _lefthook_entry(hook: str, gate: GateCommand) -> dict[str, Any]:
    entry: dict[str, Any] = {"run": lefthook_run(hook, gate)}
    if hook == "pre-push":
        entry["use_stdin"] = True
    return entry


def _json_str(value: str) -> str:
    import json

    return json.dumps(value)


def render_lefthook_local(before: str | None, hooks: list[str], gate: GateCommand | None) -> str | None:
    """New ``lefthook-local.yml`` text with our commands for ``hooks`` (``gate=None`` removes them).

    Returns ``None`` when the file should be deleted (nothing but ours was in it).
    Raises :class:`GitHookError` when an existing file cannot be merged safely.
    """
    if before is None or not before.strip():
        if gate is None or not hooks:
            return before
        lines = ["# remembra-crew: crew git gates (spec §8.4). Local and untracked; never commit this file."]
        for hook in hooks:
            lines += [f"{hook}:", "  commands:", f"    {LEFTHOOK_COMMAND}:"]
            if hook == "pre-push":
                lines.append("      use_stdin: true")
            lines.append(f"      run: {_json_str(lefthook_run(hook, gate))}")
        return "\n".join(lines) + "\n"
    try:
        import yaml  # type: ignore[import-untyped,unused-ignore]
    except ImportError as e:  # pragma: no cover - PyYAML ships with the server extras
        raise GitHookError("PyYAML is needed to merge an existing lefthook-local.yml") from e
    try:
        data = yaml.safe_load(before)
    except yaml.YAMLError as e:
        raise GitHookError(f"lefthook-local.yml is not valid YAML ({e.__class__.__name__})") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise GitHookError("lefthook-local.yml is not a mapping")
    original = yaml.safe_dump(data, sort_keys=False)
    for hook in HOOKS:
        section = data.get(hook)
        if section is not None and not isinstance(section, dict):
            raise GitHookError(f"lefthook-local.yml: '{hook}' is not a mapping")
        commands = (section or {}).get("commands")
        if commands is not None and not isinstance(commands, dict):
            raise GitHookError(f"lefthook-local.yml: '{hook}.commands' is not a mapping")
        want = gate is not None and hook in hooks
        if want:
            assert gate is not None
            section = data.setdefault(hook, {}) if section is None else section
            section.setdefault("commands", {})[LEFTHOOK_COMMAND] = _lefthook_entry(hook, gate)
        elif section is not None and commands and LEFTHOOK_COMMAND in commands:
            del commands[LEFTHOOK_COMMAND]
            if not commands:
                del section["commands"]
            if not section:
                del data[hook]
    if not data:
        return None
    if yaml.safe_dump(data, sort_keys=False) == original:
        return before
    return str(yaml.safe_dump(data, sort_keys=False, width=1000))


def _render_exclude(before: str | None, entries: list[str]) -> str:
    text = before or ""
    if EXCLUDE_BEGIN in text and EXCLUDE_END in text.split(EXCLUDE_BEGIN, 1)[1]:
        head, rest = text.split(EXCLUDE_BEGIN, 1)
        _, tail = rest.split(EXCLUDE_END, 1)
        text = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    if not entries:
        return text
    block = "\n".join([EXCLUDE_BEGIN, *entries, EXCLUDE_END]) + "\n"
    return (text.rstrip("\n") + "\n" if text.strip() else "") + block


def _exclude_entries(text: str | None) -> list[str]:
    if not text or EXCLUDE_BEGIN not in text:
        return []
    inner = text.split(EXCLUDE_BEGIN, 1)[1].split(EXCLUDE_END, 1)[0]
    return [line.strip() for line in inner.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@dataclass
class HookPlan:
    repo: RepoHooks
    ops: list[FileOp]
    methods: dict[str, str]
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(op.changed for op in self.ops)


def _local_lefthook(info: RepoHooks) -> Path:
    for name in LEFTHOOK_LOCALS:
        if (info.toplevel / name).is_file():
            return info.toplevel / name
    return info.toplevel / "lefthook-local.yml"


def _runs_lefthook(text: str | None) -> bool:
    return bool(text) and "lefthook" in (text or "") and not is_crew_script(text)


def plan_install(repo: Path, gate: GateCommand, *, hooks: tuple[str, ...] = HOOKS) -> HookPlan:
    """Everything ``connect --git-hooks`` would write in ``repo`` (nothing is written here)."""
    info = inspect(repo)
    ops: list[FileOp] = []
    methods: dict[str, str] = {}
    notes: list[str] = []
    excludes = _exclude_entries(_read(info.exclude_file))
    lefthook_hooks: list[str] = []
    local = _local_lefthook(info)
    local_ok = info.lefthook_config is not None and not info.tracked(local)

    for hook in hooks:
        active = info.hooks_dir / hook
        content = _read(active)
        user_husky = info.toplevel / ".husky" / hook
        if local_ok and _runs_lefthook(content):
            methods[hook] = "lefthook"
            lefthook_hooks.append(hook)
            continue
        if info.husky_v9:
            user_text = _read(user_husky)
            line = husky_line(hook)
            tracked = user_text is not None and info.tracked(user_husky)
            if not tracked or line in (user_text or ""):
                methods[hook] = "husky"
                if user_text is None or line not in user_text:
                    base = (user_text or "").rstrip("\n")
                    after = (base + "\n" if base else "") + line + "\n"
                    ops.append(
                        FileOp(
                            user_husky,
                            user_text,
                            after,
                            None if user_text is not None else 0o755,
                            [f"{hook}: source the crew include from .husky/{hook}"],
                        )
                    )
                    entry = info.exclude_entry(user_husky)
                    if user_text is None and entry and entry not in excludes:
                        excludes.append(entry)
                include = info.include_dir / hook
                want = include_script(hook, gate)
                current = _read(include)
                if current != want:
                    ops.append(FileOp(include, current, want, 0o644, [f"{hook}: write the crew include {include}"], backup=False))
                continue
        if not info.hooks_dir_writable_scope:
            methods[hook] = "manual"
            notes.append(
                f"{hook}: the active hooks path {info.hooks_dir} is outside this repository (a global core.hooksPath);"
                f' it is not changed. Add this line to {active} yourself: {gate.shell(GATE_VERBS[hook])} "$@"'
            )
            continue
        if content is not None and info.tracked(active):
            methods[hook] = "manual"
            notes.append(
                f"{hook}: {active} is tracked by git and is not changed. Add this line to it yourself:"
                f' {gate.shell(GATE_VERBS[hook])} "$@" {CREW_HOOK_MARKER}'
            )
            continue
        want = hook_script(hook, gate, source_prev=info.husky_v9)
        prev = active.with_name(active.name + PREV_SUFFIX)
        if content is None:
            methods[hook] = "delegate" if prev.exists() else "plain"
            ops.append(FileOp(active, None, want, 0o755, [f"{hook}: install the crew hook at {active}"], backup=False))
        elif is_crew_script(content):
            methods[hook] = "delegate" if prev.exists() else "plain"
            if content != want:
                ops.append(FileOp(active, content, want, 0o755, [f"{hook}: update the crew hook at {active}"], backup=False))
            elif not os.access(active, os.X_OK):
                ops.append(
                    FileOp(
                        active, content, want, 0o755, [f"{hook}: make the crew hook executable again"], backup=False, force=True
                    )
                )
        else:
            methods[hook] = "delegate"
            mode = active.stat().st_mode & 0o777
            prev_text = _read(prev)
            ops.append(
                FileOp(
                    prev,
                    prev_text,
                    content,
                    mode,
                    [f"{hook}: keep the existing hook as {prev.name} (it runs first)"],
                    # a different hook already kept there (something replaced ours since) is backed up, never lost
                    backup=prev_text is not None and prev_text != content,
                )
            )
            ops.append(
                FileOp(active, content, want, 0o755, [f"{hook}: chain the crew gate after the existing hook"], backup=False)
            )
            entry = info.exclude_entry(prev)
            if entry and entry not in excludes:
                excludes.append(entry)
        entry = info.exclude_entry(active)
        if entry and content is None and entry not in excludes:
            excludes.append(entry)

    if lefthook_hooks:
        local_before = _read(local)
        try:
            local_after = render_lefthook_local(local_before, lefthook_hooks, gate)
        except GitHookError as e:
            notes.append(f"lefthook: {e}; add the remembra-crew commands to {local.name} yourself")
            for hook in lefthook_hooks:
                methods[hook] = "manual"
        else:
            if local_after != local_before:
                ops.append(
                    FileOp(
                        local,
                        local_before,
                        local_after,
                        None if local_before is not None else 0o644,
                        [f"lefthook: add the {LEFTHOOK_COMMAND} commands to {local.name}"],
                    )
                )
            entry = info.exclude_entry(local)
            if entry and local_before is None and entry not in excludes:
                excludes.append(entry)

    exclude_before = _read(info.exclude_file)
    exclude_after = _render_exclude(exclude_before, excludes)
    if exclude_after != (exclude_before or "") and excludes:
        ops.append(
            FileOp(
                info.exclude_file,
                exclude_before,
                exclude_after,
                None if exclude_before is not None else 0o644,
                ["keep the local crew files out of `git status` (.git/info/exclude)"],
                backup=False,
            )
        )
    return HookPlan(info, ops, methods, notes)


def plan_uninstall(repo: Path) -> HookPlan:
    """Take the crew gates out again, restoring every hook that was chained."""
    info = inspect(repo)
    ops: list[FileOp] = []
    methods: dict[str, str] = {}
    for hook in HOOKS:
        dirs = [info.common_dir / "hooks"]
        if info.hooks_dir_writable_scope and info.hooks_dir != dirs[0]:
            dirs.append(info.hooks_dir)
        for hooks_dir in dirs:
            active = hooks_dir / hook
            content = _read(active)
            prev = active.with_name(active.name + PREV_SUFFIX)
            if is_crew_script(content):
                prev_text = _read(prev)
                if prev_text is not None:
                    mode = prev.stat().st_mode & 0o777
                    ops.append(FileOp(active, content, prev_text, mode, [f"{hook}: restore the original hook"], backup=False))
                    ops.append(FileOp(prev, prev_text, None, None, [f"{hook}: remove {prev.name}"], backup=False))
                else:
                    ops.append(FileOp(active, content, None, None, [f"{hook}: remove the crew hook"], backup=False))
                methods[hook] = "removed"
        include = info.include_dir / hook
        inc = _read(include)
        if inc is not None:
            ops.append(FileOp(include, inc, None, None, [f"{hook}: remove the crew include"], backup=False))
        user_husky = info.toplevel / ".husky" / hook
        user_text = _read(user_husky)
        line = husky_line(hook)
        if user_text is not None and line in user_text and not info.tracked(user_husky):
            rest = "\n".join(x for x in user_text.splitlines() if x != line).strip("\n")
            ops.append(
                FileOp(
                    user_husky,
                    user_text,
                    (rest + "\n") if rest.strip() else None,
                    None,
                    [f"{hook}: remove the crew line from .husky/{hook}"],
                    backup=False,
                )
            )
            methods[hook] = "removed"
    local = _local_lefthook(info)
    before = _read(local)
    if before is not None and CREW_HOOK_MARKER in before:
        try:
            after = render_lefthook_local(before, [], None)
        except GitHookError:
            after = before
        if after != before:
            ops.append(FileOp(local, before, after, None, [f"lefthook: remove the {LEFTHOOK_COMMAND} commands"]))
    exclude_before = _read(info.exclude_file)
    if exclude_before is not None and EXCLUDE_BEGIN in exclude_before:
        ops.append(
            FileOp(
                info.exclude_file,
                exclude_before,
                _render_exclude(exclude_before, []),
                None,
                ["remove the crew block from .git/info/exclude"],
                backup=False,
            )
        )
    return HookPlan(info, ops, methods, [])


def apply_plan(plan: HookPlan, stamp: str | None = None) -> list[Path]:
    """Write the plan (after consent); returns backups kept."""
    backups = []
    for op in plan.ops:
        if op.changed:
            backup = apply_op(op, stamp)
            if backup is not None:
                backups.append(backup)
    return backups


# ---------------------------------------------------------------------------
# Status (githook_state) and self-repair
# ---------------------------------------------------------------------------


def hook_state(info: RepoHooks, hook: str) -> str:
    active = info.hooks_dir / hook
    content = _read(active)
    if is_crew_script(content):
        if not os.access(active, os.X_OK):
            return "missing"  # git ignores a hook that is not executable
        return "chained" if active.with_name(active.name + PREV_SUFFIX).exists() else "ok"
    if content is not None and os.access(active, os.X_OK):
        if info.husky_v9:
            user = _read(info.toplevel / ".husky" / hook) or ""
            if husky_line(hook) in user and (info.include_dir / hook).is_file():
                return "chained"
        if _runs_lefthook(content):
            local = _read(_local_lefthook(info)) or ""
            if CREW_HOOK_MARKER in local and _lefthook_has(local, hook):
                return "chained"
    return "missing"


def _lefthook_has(text: str, hook: str) -> bool:
    try:
        import yaml  # type: ignore[import-untyped,unused-ignore]

        data = yaml.safe_load(text) or {}
    except Exception:
        return False
    section = data.get(hook) if isinstance(data, dict) else None
    commands = section.get("commands") if isinstance(section, dict) else None
    entry = commands.get(LEFTHOOK_COMMAND) if isinstance(commands, dict) else None
    return isinstance(entry, dict) and CREW_HOOK_MARKER in str(entry.get("run") or "")


def status(repo: Path) -> dict[str, str]:
    """``githook_state`` per hook: ``ok`` (crew hook), ``chained`` (through a manager or a kept hook),
    ``missing`` (the gate is not reachable from the active hook path) or ``unknown`` (not a git repo)."""
    try:
        info = inspect(repo)
    except GitHookError:
        return dict.fromkeys(HOOKS, "unknown")
    return {hook: hook_state(info, hook) for hook in HOOKS}


def overall(states: dict[str, str]) -> str:
    values = set(states.values())
    for state in ("unknown", "missing", "chained"):
        if state in values:
            return state
    return "ok"


def ensure(repo: Path, gate: GateCommand, *, consented: bool) -> dict[str, str]:
    """Reinstall missing hooks when they were installed with consent (crewd, every heartbeat)."""
    states = status(repo)
    missing = tuple(h for h, s in states.items() if s == "missing")
    if not missing or not consented:
        return states
    plan = plan_install(repo, gate, hooks=missing)
    apply_plan(plan)
    return status(repo)
