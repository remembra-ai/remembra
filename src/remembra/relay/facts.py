"""Deterministic session facts from git and (optionally) an agent transcript.

Nothing here asks a model anything: facts are read from ``git`` and from the
agent's own tool-call log. The raw transcript never leaves the machine — only
the extracted commands (clipped), exit codes, test verdicts, edited paths and
open todo items are sent, and every string is passed through
``redact_secrets`` before it is returned.

Stdlib only. Every subprocess call is bounded by a shared deadline so a close
hook can never hang the agent.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from remembra.relay.handoff import is_probe_command
from remembra.security.secrets import redact_secrets

MAX_COMMITS = 30
MAX_COMMANDS = 60
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
CMD_CLIP = 300
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_TEST_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:"
    r"pytest|py\.test|python[\d.]*\s+-m\s+(?:pytest|unittest)|tox|nox|jest|vitest|mocha|ava|rspec|phpunit|"
    r"go\s+test|cargo\s+(?:test|nextest)|swift\s+test|xcodebuild\b[^;&|]*\btest\b|mvn\b[^;&|]*\btest\b|"
    r"gradlew?\b[^;&|]*\btest\b|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|deno\s+test|bun\s+test|make\s+(?:test|check)\b|"
    r"ctest|dotnet\s+test|mix\s+test|flutter\s+test|dart\s+test"
    r")(?=$|[\s;&|)])"
)
_TEST_SUMMARY_RE = re.compile(
    r"\b\d+\s+(?:passed|failed|errors?|failures?)\b|^Tests?:\s|\bTEST (?:SUCCEEDED|FAILED)\b|^(?:ok|FAIL)\s|\btest result:",
    re.IGNORECASE,
)
_EXIT_RE = re.compile(r"^\s*(?:Error:\s*)?Exit code (\d+)")
# `git commit` / cherry-pick / revert output: "[branch 1a2b3c4] subject" (or "(root-commit)").
_COMMIT_LINE_RE = re.compile(r"^\[[^\]\n]+? (?:\(root-commit\) )?([0-9a-f]{7,40})\] \S", re.MULTILINE)
_LEADING_CD_RE = re.compile(r"""^\s*cd\s+("[^"]+"|'[^']+'|[^\s;&|]+)\s*(?:&&|;|$)""")


class Deadline:
    """A wall-clock budget shared by every subprocess call."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self, cap: float = 5.0) -> float:
        return max(0.0, min(cap, self.end - time.monotonic()))

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.end


def _scrub(value: str) -> str:
    return redact_secrets(value).text if value else value


def _clip(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


class Git:
    def __init__(self, cwd: Path, deadline: Deadline) -> None:
        self.cwd = cwd
        self.deadline = deadline
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",  # never take index.lock from a hook
            "LC_ALL": "C",
        }

    def run(self, *args: str) -> str | None:
        """stdout of ``git -C cwd <args>`` or None on any failure/timeout."""
        timeout = self.deadline.remaining()
        if timeout <= 0.05:
            return None
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.cwd), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.env,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def line(self, *args: str) -> str | None:
        out = self.run(*args)
        value = (out or "").strip().splitlines()
        return value[0].strip() if value else None


@dataclass
class RepoInfo:
    toplevel: str | None = None
    git_remote: str | None = None
    root_commit: str | None = None
    repo_name: str | None = None
    branch: str | None = None
    head_commit: str | None = None
    is_git: bool = False

    def locator(self, cwd: Path, host: str | None = None) -> dict[str, Any]:
        loc: dict[str, Any] = {
            "git_remote": self.git_remote,
            "root_commit": self.root_commit,
            "root_path": self.toplevel or str(cwd),
            "repo_name": self.repo_name,
            "host": host or socket.gethostname(),
        }
        return {k: v for k, v in loc.items() if v}


def repo_info(cwd: Path, deadline: Deadline) -> RepoInfo:
    git = Git(cwd, deadline)
    toplevel = git.line("rev-parse", "--show-toplevel")
    if not toplevel:
        return RepoInfo(repo_name=cwd.name or None)
    info = RepoInfo(toplevel=toplevel, is_git=True)
    remote = git.line("remote", "get-url", "origin")
    if not remote:
        names = (git.run("remote") or "").split()
        remote = git.line("remote", "get-url", names[0]) if names else None
    info.git_remote = _strip_url_credentials(remote) if remote else None
    roots = sorted((git.run("rev-list", "--max-parents=0", "HEAD") or "").split())
    info.root_commit = roots[0] if roots else None
    common = git.line("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and Path(common).name == ".git":
        info.repo_name = Path(common).parent.name  # main checkout's name, also from a worktree
    else:
        info.repo_name = Path(toplevel).name
    branch = git.line("rev-parse", "--abbrev-ref", "HEAD")
    info.branch = "(detached)" if branch == "HEAD" else branch
    info.head_commit = git.line("rev-parse", "HEAD")
    return info


def _strip_url_credentials(url: str) -> str:
    return re.sub(r"(://)[^/@\s]+@", r"\1", url.strip())


def _reachable(git: Git, shas: list[str]) -> list[str]:
    """Full ids of the given (short) commit ids that exist and are ancestors of HEAD."""
    out: list[str] = []
    for sha in dict.fromkeys(shas):
        full = git.line("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        if full and full not in out and git.run("merge-base", "--is-ancestor", full, "HEAD") is not None:
            out.append(full)
    return out


def _session_range(
    git: Git, info: RepoInfo, start_head: str | None, session_commits: list[str] | None, hours: float
) -> tuple[list[str], str]:
    """``git log`` args for "this session's commits" and how they were chosen.

    Evidence order: the HEAD recorded at session start (``brief``), then the
    commits the agent's own transcript shows it creating, then the branch's
    merge-base with the default branch, then a time window. With a
    transcript but no commit evidence, the agent committed nothing.
    """
    if start_head and info.head_commit:
        if start_head == info.head_commit:
            return [], "session-start"
        if git.run("merge-base", "--is-ancestor", start_head, "HEAD") is not None:
            return [f"{start_head}..HEAD"], "session-start"
    if session_commits is not None:
        reachable = _reachable(git, session_commits[-MAX_COMMITS:])
        return (["--no-walk=sorted", *reachable], "transcript-commits") if reachable else ([], "transcript-no-commits")
    default = git.line("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if default and info.branch and default.split("/", 1)[-1] != info.branch:
        base = git.line("merge-base", "HEAD", default)
        if base and base != info.head_commit:
            return [f"{base}..HEAD"], f"merge-base:{default}"
    return ["--since", f"{hours:g} hours ago", "HEAD"], f"last-{hours:g}h"


def git_facts(
    cwd: Path,
    deadline: Deadline,
    start_head: str | None = None,
    session_commits: list[str] | None = None,
    hours: float = 12.0,
    info: RepoInfo | None = None,
) -> dict[str, Any]:
    """Branch, head, this session's commits + files, uncommitted files, diff stat, push state."""
    info = info or repo_info(cwd, deadline)
    facts: dict[str, Any] = {}
    if not info.is_git:
        return facts
    git = Git(cwd, deadline)
    facts["branch"] = info.branch
    facts["head_commit"] = info.head_commit

    commits: list[dict[str, str]] = []
    files: list[str] = []
    range_args, how = _session_range(git, info, start_head, session_commits, hours) if info.head_commit else ([], "empty-repo")
    facts["commit_range"] = how
    if range_args:
        log = git.run("log", f"--max-count={MAX_COMMITS}", "--no-merges", "--name-only", "--format=%x1e%H%x1f%s", *range_args)
        for block in (log or "").split("\x1e"):
            if not block.strip():
                continue
            header, _, rest = block.partition("\n")
            sha, _, subject = header.partition("\x1f")
            commits.append({"sha": sha.strip(), "subject": _scrub(subject.strip())})
            files.extend(p.strip() for p in rest.splitlines() if p.strip())
    commits.reverse()  # oldest first
    facts["commits"] = commits

    uncommitted: list[str] = []
    status = git.run("status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if status:
        entries = status.split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            if len(entry) > 3:
                code, path = entry[:2], entry[3:]
                uncommitted.append(path)
                if "R" in code or "C" in code:
                    i += 1  # the next NUL field is the rename source
            i += 1
    facts["uncommitted_files"] = sorted(dict.fromkeys(uncommitted))
    facts["files_changed"] = sorted(dict.fromkeys(files + uncommitted))

    if commits:
        first = commits[0]["sha"]
        base = git.line("rev-parse", f"{first}^") or EMPTY_TREE
        stat = git.line("diff", "--shortstat", base)
    else:
        stat = git.line("diff", "--shortstat", "HEAD") if info.head_commit else None
    if stat:
        facts["diff_stat"] = stat

    upstream = git.line("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        facts["upstream"] = upstream
        ahead = git.line("rev-list", "--count", "@{u}..HEAD")
        if ahead is not None and ahead.isdigit():
            facts["unpushed_commits"] = int(ahead)
    elif info.head_commit and info.branch != "(detached)":
        facts["no_upstream"] = True
    return facts


# ---------------------------------------------------------------------------
# Claude Code transcript (JSONL)
# ---------------------------------------------------------------------------


@dataclass
class TranscriptFacts:
    commands: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    todos_open: list[str] = field(default_factory=list)
    commit_shas: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    session_id: str | None = None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _test_summary(output: str) -> str | None:
    for line in reversed([ln.strip(" =") for ln in output.splitlines() if ln.strip()]):
        if _TEST_SUMMARY_RE.search(line):
            return _clip(line, 200)
    return None


def _last_meaningful_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip() and not _EXIT_RE.match(ln)]
    return _clip(lines[-1], 200) if lines else ""


def _iter_lines(path: Path) -> Iterable[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > MAX_TRANSCRIPT_BYTES:
            fh.seek(size - MAX_TRANSCRIPT_BYTES)
            fh.readline()  # drop the partial line
        for raw in fh:
            yield raw.decode("utf-8", errors="replace")


def _outside_root(command: str, root: str | None) -> bool:
    """True when the command starts with ``cd <dir>`` to a directory outside ``root``."""
    if not root:
        return False
    match = _LEADING_CD_RE.match(command)
    if not match:
        return False
    target = match.group(1).strip("\"'")
    if not target.startswith(("/", "~")):
        return False  # relative cd stays within the session's cwd
    target_path = os.path.realpath(os.path.expanduser(target))
    root_path = os.path.realpath(root)
    return not (target_path == root_path or target_path.startswith(root_path.rstrip(os.sep) + os.sep))


def parse_claude_transcript(path: Path, deadline: Deadline | None = None, root: str | None = None) -> TranscriptFacts:
    """Extract Bash commands + exit codes, test runs, edited files and open todos.

    Format (Claude Code JSONL): ``assistant`` lines carry ``tool_use`` blocks
    (``name``, ``id``, ``input``); ``user`` lines carry the matching
    ``tool_result`` (``tool_use_id``, ``is_error``, ``content``) and a
    ``toolUseResult`` object. A failed Bash result starts with ``Exit code N``.

    ``root`` (the repository top level) drops commands that ``cd`` elsewhere
    first, so another repo's test runs are not reported for this project.
    Commit ids printed by ``git commit`` are collected as commit evidence.
    """
    facts = TranscriptFacts()
    uses: dict[str, tuple[str, dict[str, Any]]] = {}
    todo_snapshot: list[dict[str, Any]] | None = None
    tasks: dict[str, dict[str, str]] = {}
    pending_task_creates: dict[str, str] = {}
    command_log: list[dict[str, Any]] = []
    files: list[str] = []

    for line in _iter_lines(path):
        if deadline is not None and deadline.expired:
            break
        if (
            '"tool_use"' not in line
            and '"tool_result"' not in line
            and (facts.started_at is not None or '"timestamp"' not in line)
        ):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        ts = entry.get("timestamp")
        if isinstance(ts, str) and facts.started_at is None:
            try:
                facts.started_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
        if facts.session_id is None and isinstance(entry.get("sessionId"), str):
            facts.session_id = entry["sessionId"]
        content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                raw_input = block.get("input")
                tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
                uses[str(block.get("id"))] = (name, tool_input)
                if name == "TodoWrite" and isinstance(tool_input.get("todos"), list):
                    todo_snapshot = [t for t in tool_input["todos"] if isinstance(t, dict)]
                elif name == "TaskUpdate" and tool_input.get("taskId") is not None:
                    task = tasks.setdefault(str(tool_input["taskId"]), {"subject": "", "status": "pending"})
                    if tool_input.get("status"):
                        task["status"] = str(tool_input["status"])
                    if tool_input.get("subject"):
                        task["subject"] = str(tool_input["subject"])
                elif name == "TaskCreate":
                    pending_task_creates[str(block.get("id"))] = str(tool_input.get("subject") or "")
            elif block.get("type") == "tool_result":
                use_id = str(block.get("tool_use_id"))
                if use_id not in uses:
                    continue
                name, tool_input = uses[use_id]
                is_error = bool(block.get("is_error"))
                text = _result_text(block.get("content"))
                if name == "Bash":
                    command = str(tool_input.get("command") or "").strip()
                    if not command or tool_input.get("run_in_background"):
                        continue
                    match = _EXIT_RE.match(text)
                    if is_error and not match:
                        continue  # rejected / blocked / interrupted: no real exit code
                    facts.commit_shas.extend(_COMMIT_LINE_RE.findall(text))
                    if _outside_root(command, root):
                        continue
                    exit_code = int(match.group(1)) if match else 0
                    command_log.append({"cmd": command, "exit_code": exit_code, "output": text})
                elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and not is_error:
                    target = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if isinstance(target, str) and target:
                        files.append(target)
                elif name == "TaskCreate" and not is_error:
                    result = entry.get("toolUseResult")
                    task_info = result.get("task") if isinstance(result, dict) else None
                    if isinstance(task_info, dict) and task_info.get("id") is not None:
                        tasks[str(task_info["id"])] = {
                            "subject": str(task_info.get("subject") or pending_task_creates.get(use_id, "")),
                            "status": "pending",
                        }

    # Tests: last result per exact command.
    tests: dict[str, dict[str, Any]] = {}
    for item in command_log:
        if _TEST_RE.search(item["cmd"]):
            tests[item["cmd"]] = {
                "cmd": item["cmd"],
                "passed": item["exit_code"] == 0,
                "summary": _test_summary(item["output"]),
            }
    # Errors: failed non-probe, non-test commands whose exact command did not later succeed.
    last_exit: dict[str, int] = {}
    for item in command_log:
        last_exit[item["cmd"]] = item["exit_code"]
    errors: list[str] = []
    seen: set[str] = set()
    for item in reversed(command_log):
        cmd = item["cmd"]
        if cmd in seen or item["exit_code"] == 0 or last_exit.get(cmd) == 0 or _TEST_RE.search(cmd) or is_probe_command(cmd):
            continue
        seen.add(cmd)
        detail = _last_meaningful_line(item["output"])
        errors.append(f"`{_clip(cmd, 160)}` exited {item['exit_code']}" + (f": {detail}" if detail else ""))
        if len(errors) >= 8:
            break
    errors.reverse()

    open_todos: list[str] = []
    if todo_snapshot is not None:
        open_todos.extend(str(t.get("content") or "") for t in todo_snapshot if t.get("status") != "completed")
    open_todos.extend(
        t["subject"] for _, t in sorted(tasks.items(), key=lambda kv: kv[0]) if t["status"] not in ("completed", "deleted")
    )

    facts.commands = [
        {"cmd": _scrub(_clip(c["cmd"], CMD_CLIP)), "exit_code": c["exit_code"]} for c in command_log[-MAX_COMMANDS:]
    ]
    facts.tests = [
        {
            "cmd": _scrub(_clip(t["cmd"], CMD_CLIP)),
            "passed": t["passed"],
            "summary": _scrub(t["summary"]) if t["summary"] else None,
        }
        for t in tests.values()
    ]
    facts.errors = [_scrub(e) for e in errors]
    facts.files = list(dict.fromkeys(files))
    facts.todos_open = [_scrub(_clip(t, 300)) for t in open_todos if t.strip()]
    return facts


def relativize(paths: Iterable[str], root: str | None) -> list[str]:
    """Paths under ``root`` become repo-relative; others stay absolute."""
    out = []
    for p in paths:
        if root:
            try:
                out.append(str(Path(p).resolve().relative_to(Path(root).resolve())))
                continue
            except (ValueError, OSError):
                pass
        out.append(p)
    return out


def merge_facts(git: dict[str, Any], transcript: TranscriptFacts | None, root: str | None) -> dict[str, Any]:
    """git facts + transcript facts -> the ``/session/close`` facts payload."""
    facts = dict(git)
    facts.pop("commit_range", None)
    if transcript is not None:
        edited = relativize(transcript.files, root)
        if root:  # files the agent edited outside this repository are not this project's changes
            edited = [p for p in edited if not os.path.isabs(p)]
        facts["files_changed"] = sorted(dict.fromkeys(list(facts.get("files_changed") or []) + edited))
        facts["commands"] = transcript.commands
        facts["tests"] = transcript.tests
        facts["errors"] = transcript.errors
        facts["todos_open"] = transcript.todos_open
    return facts
