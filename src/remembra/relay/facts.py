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
import shlex
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

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
# A command that starts by changing directory: `cd X &&`, `(cd X && ...)`, `pushd X;`.
_LEADING_CD_RE = re.compile(r"""^[\s(]*(?:cd|pushd)\s+("[^"]+"|'[^']+'|[^\s;&|()]+)\s*(?:&&|;|\)|$)""")
# `git -C DIR ...` (optionally after `-c key=value` options).
_GIT_C_RE = re.compile(r"""^[\s(]*git\s+(?:-c\s+\S+\s+)*-C\s+("[^"]+"|'[^']+'|[^\s;&|()]+)""")
# Reflog actions that create a commit in this checkout (a pull, merge, checkout or reset does not).
_OWN_REFLOG_RE = re.compile(
    r"^(?:commit(?: \((?:amend|initial)\))?|cherry-pick|revert|rebase(?: -i)? \((?:pick|reword|edit|squash|fixup|continue)\))$"
)
_KNOWN_GIT_HOSTS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "ssh.dev.azure.com",
    "vs-ssh.visualstudio.com",
    "git.sr.ht",
    "gitee.com",
}
_SCP_REMOTE_RE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[A-Za-z0-9.\-]+):(?!//)(?P<path>.+)$")


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
        self.timed_out = False
        self.timeouts = 0

    def run(self, *args: str) -> str | None:
        """stdout of ``git -C cwd <args>`` or None on any failure/timeout.

        ``timed_out`` tells the two apart for the last call: a probe that ran
        out of time must be reported as unknown, never as "nothing there".
        """
        self.timed_out = False
        timeout = self.deadline.remaining()
        if timeout <= 0.05:
            self.timed_out = True
            self.timeouts += 1
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
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.timeouts += 1
            return None
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
    branch = git.line("rev-parse", "--abbrev-ref", "HEAD")
    remote = _pick_remote(git, branch if branch and branch != "HEAD" else None)
    if remote:
        remote = _absolute_local_remote(_strip_url_credentials(remote), toplevel)
        remote = _resolve_ssh_alias(remote, deadline)
    info.git_remote = remote
    roots = sorted((git.run("rev-list", "--max-parents=0", "HEAD") or "").split())
    info.root_commit = roots[0] if roots else None
    common = git.line("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and Path(common).name == ".git":
        info.repo_name = Path(common).parent.name  # main checkout's name, also from a worktree
    else:
        info.repo_name = Path(toplevel).name
    info.branch = "(detached)" if branch == "HEAD" else branch
    info.head_commit = git.line("rev-parse", "HEAD")
    return info


def _strip_url_credentials(url: str) -> str:
    return re.sub(r"(://)[^/@\s]+@", r"\1", url.strip())


def _pick_remote(git: Git, branch: str | None) -> str | None:
    """The remote URL that identifies this repository.

    ``origin`` when it exists (stable across branches, so a fork's feature
    branches and main agree); otherwise the remote the current branch tracks,
    then ``remote.pushDefault``, then the only remote, then the first by name.
    """
    names = (git.run("remote") or "").split()
    if not names:
        return None
    chosen: str | None = "origin" if "origin" in names else None
    if chosen is None and branch:
        tracked = git.line("config", "--get", f"branch.{branch}.remote")
        chosen = tracked if tracked in names else None
    if chosen is None:
        push_default = git.line("config", "--get", "remote.pushDefault")
        chosen = push_default if push_default in names else None
    if chosen is None:
        chosen = sorted(names)[0]
    return git.line("remote", "get-url", chosen)


def _is_local_path_remote(url: str) -> bool:
    return "://" not in url and not _SCP_REMOTE_RE.match(url)


def _absolute_local_remote(url: str, toplevel: str) -> str:
    """A relative local remote (``../upstream``) made absolute, so two repos'
    ``../upstream`` remotes never collapse to one fingerprint."""
    if not _is_local_path_remote(url) or os.path.isabs(url) or url.startswith("~") or re.match(r"^[A-Za-z]:[\\/]", url):
        return url
    return os.path.normpath(os.path.join(toplevel, url))


def _resolve_ssh_alias(url: str, deadline: Deadline) -> str:
    """Replace an ssh config ``Host`` alias (``git@github-work:me/repo``) with
    the real host name from ``ssh -G``, so the alias and the plain URL resolve
    to one project. Known forge hosts are left alone; any failure keeps ``url``."""
    host: str | None = None
    rebuild = None
    match = _SCP_REMOTE_RE.match(url) if "://" not in url else None
    if match:
        host = match.group("host")
        user = f"{match.group('user')}@" if match.group("user") else ""

        def rebuild(real: str) -> str:
            return f"{user}{real}:{match.group('path')}"

    elif url.lower().startswith("ssh://"):
        parsed = re.match(r"^(ssh://(?:[^@/]+@)?)([^/:]+)(.*)$", url, re.IGNORECASE)
        if parsed:
            host = parsed.group(2)
            prefix, rest = parsed.group(1), parsed.group(3)

            def rebuild(real: str) -> str:
                return f"{prefix}{real}{rest}"

    if not host or rebuild is None or host.lower() in _KNOWN_GIT_HOSTS or not re.match(r"^[A-Za-z0-9.\-]+$", host):
        return url
    timeout = deadline.remaining(cap=1.5)
    if timeout <= 0.05:
        return url
    try:
        proc = subprocess.run(
            ["ssh", "-G", host],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return url
    if proc.returncode != 0:
        return url
    for line in proc.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() == "hostname" and value.strip() and value.strip().lower() != host.lower():
            return rebuild(value.strip())
    return url


def _reachable(git: Git, shas: list[str]) -> list[str]:
    """Full ids of the given (short) commit ids that exist and are ancestors of HEAD."""
    out: list[str] = []
    for sha in dict.fromkeys(shas):
        full = git.line("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        if full and full not in out and git.run("merge-base", "--is-ancestor", full, "HEAD") is not None:
            out.append(full)
    return out


def _own_commits_since(git: Git, start_head: str, started_at: float | None) -> list[str] | None:
    """Commits THIS checkout created since the session started, oldest first, from ``git reflog``.

    Keeps commit / amend / cherry-pick / revert / rebase-pick entries and
    drops pull, merge, checkout and reset (their commits are someone else's
    work arriving). Walks back to the session start time, or to the session's
    start HEAD when no start time was recorded. None when the reflog cannot
    be read (the caller falls back to other evidence).
    """
    out = git.run("reflog", "show", "--date=unix", "--format=%H%x1f%gd%x1f%gs", "HEAD", "--")
    if not out or not out.strip():
        return None  # no reflog (core.logAllRefUpdates=false, or it was expired): use other evidence
    shas: list[str] = []
    for line in out.splitlines():
        sha, _, rest = line.partition("\x1f")
        selector, _, subject = rest.partition("\x1f")
        stamp = re.search(r"@\{(\d+)\}", selector)
        if started_at is not None:
            if stamp is None or int(stamp.group(1)) < int(started_at):
                break
        elif sha.strip() == start_head:
            break  # the entry that produced the session's start HEAD predates the session
        action = subject.split(":", 1)[0].strip()
        if _OWN_REFLOG_RE.match(action):
            shas.append(sha.strip())
    shas.reverse()
    return list(dict.fromkeys(shas))


def _no_walk(shas_oldest_first: list[str]) -> list[str]:
    """``git log`` args listing exactly these commits, newest first (the order ``git log`` normally prints)."""
    return ["--no-walk=unsorted", *reversed(shas_oldest_first)]


def _session_range(
    git: Git,
    info: RepoInfo,
    start_head: str | None,
    session_commits: list[str] | None,
    hours: float,
    started_at: float | None = None,
) -> tuple[list[str], str]:
    """``git log`` args for "this session's commits" and how they were chosen.

    Evidence order: with the HEAD recorded at session start (``brief``), the
    commits the reflog shows this checkout creating since then (a pulled or
    merged teammate commit is not this session's work); then the commits the
    agent's own transcript shows it creating; then the branch's merge-base
    with the default branch, then a time window (both labeled as windows:
    they may include other people's commits). With a transcript but no commit
    evidence, the agent committed nothing.
    """
    if start_head and info.head_commit:
        if start_head == info.head_commit:
            return [], "session-start"
        own = _own_commits_since(git, start_head, started_at)
        if own is not None:
            reachable = _reachable(git, own[-MAX_COMMITS:])
            return (_no_walk(reachable), "session-reflog") if reachable else ([], "session-reflog")
        if session_commits is not None:
            reachable = _reachable(git, session_commits[-MAX_COMMITS:])
            return (_no_walk(reachable), "transcript-commits") if reachable else ([], "transcript-no-commits")
        if git.run("merge-base", "--is-ancestor", start_head, "HEAD") is not None:
            return [f"{start_head}..HEAD"], "session-start-range"
    if session_commits is not None:
        reachable = _reachable(git, session_commits[-MAX_COMMITS:])
        return (_no_walk(reachable), "transcript-commits") if reachable else ([], "transcript-no-commits")
    default = git.line("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if default and info.branch and default.split("/", 1)[-1] != info.branch:
        base = git.line("merge-base", "HEAD", default)
        if base and base != info.head_commit:
            return [f"{base}..HEAD"], f"merge-base:{default}"
    return ["--since", f"{hours:g} hours ago", "HEAD"], f"last-{hours:g}h"


def _numstat_totals(text: str) -> tuple[set[str], int, int]:
    files: set[str] = set()
    added = deleted = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files.add(parts[2])
        added += int(parts[0]) if parts[0].isdigit() else 0
        deleted += int(parts[1]) if parts[1].isdigit() else 0
    return files, added, deleted


def git_facts(
    cwd: Path,
    deadline: Deadline,
    start_head: str | None = None,
    session_commits: list[str] | None = None,
    hours: float = 12.0,
    info: RepoInfo | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    """Branch, head, this session's commits + files, uncommitted files, diff stat, push state.

    ``incomplete`` lists the probes that did not finish in time (``log``,
    ``status``, ``diff``, ``upstream``): their facts are unknown, not empty.
    """
    info = info or repo_info(cwd, deadline)
    facts: dict[str, Any] = {}
    if not info.is_git:
        return facts
    git = Git(cwd, deadline)
    incomplete: list[str] = []

    def probe(name: str, *args: str) -> str | None:
        out = git.run(*args)
        if git.timed_out and name not in incomplete:
            incomplete.append(name)
        return out

    facts["branch"] = info.branch
    facts["head_commit"] = info.head_commit

    commits: list[dict[str, str]] = []
    files: list[str] = []
    range_args, how = (
        _session_range(git, info, start_head, session_commits, hours, started_at) if info.head_commit else ([], "empty-repo")
    )
    if git.timeouts:
        incomplete.append("log")
    facts["commit_range"] = how
    stat_files: set[str] = set()
    added = deleted = 0
    if range_args:
        log = probe("log", "log", f"--max-count={MAX_COMMITS}", "--no-merges", "--numstat", "--format=%x1e%H%x1f%s", *range_args)
        for block in (log or "").split("\x1e"):
            if not block.strip():
                continue
            header, _, rest = block.partition("\n")
            sha, _, subject = header.partition("\x1f")
            commits.append({"sha": sha.strip(), "subject": _scrub(subject.strip())})
            block_files, block_added, block_deleted = _numstat_totals(rest)
            files.extend(sorted(block_files))
            stat_files |= block_files
            added += block_added
            deleted += block_deleted
    commits.reverse()  # oldest first
    facts["commits"] = commits

    uncommitted: list[str] = []
    status = probe("status", "status", "--porcelain=v1", "-z", "--untracked-files=normal")
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

    # Diff stat of this session's own commits plus the working tree (summed per
    # commit, so commits pulled in between are never counted).
    if info.head_commit:
        worktree = probe("diff", "diff", "--numstat", "HEAD")
        wt_files, wt_added, wt_deleted = _numstat_totals(worktree or "")
        stat_files |= wt_files
        added += wt_added
        deleted += wt_deleted
    if stat_files:
        facts["diff_stat"] = f"{len(stat_files)} files changed, {added} insertions(+), {deleted} deletions(-)"

    upstream = probe("upstream", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        upstream = upstream.strip().splitlines()[0] if upstream.strip() else None
    if upstream:
        facts["upstream"] = upstream
        ahead = probe("upstream", "rev-list", "--count", "@{u}..HEAD")
        if ahead is not None and ahead.strip().isdigit():
            facts["unpushed_commits"] = int(ahead.strip())
    elif info.head_commit and info.branch != "(detached)" and "upstream" not in incomplete:
        facts["no_upstream"] = True
    if incomplete:
        facts["incomplete"] = incomplete
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
    # The agent's own message when its last turn stopped on a plan/usage limit (Codex rollouts only).
    usage_limit: str | None = None


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


def effective_dir(command: str, cwd: str | None) -> str | None:
    """The directory ``command`` runs in: ``cwd`` (the shell's directory when the
    agent issued it), moved by a leading ``cd X &&`` / ``(cd X && …)`` /
    ``pushd X;`` or ``git -C X``. Relative targets resolve against ``cwd``.
    None when unknown (no cwd and no absolute target)."""
    target: str | None = None
    for regex in (_LEADING_CD_RE, _GIT_C_RE):
        match = regex.match(command)
        if match:
            target = match.group(1).strip("\"'")
            break
    if target is None:
        return cwd
    target = os.path.expanduser(target)
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(cwd, target)) if cwd else None


def _outside_root(command: str, root: str | None, cwd: str | None = None) -> bool:
    """True when ``command`` runs in a directory outside the repository ``root``."""
    if not root:
        return False
    where = effective_dir(command, cwd or root)
    if where is None:
        return False
    target_path = os.path.realpath(where)
    root_path = os.path.realpath(root)
    return not (target_path == root_path or target_path.startswith(root_path.rstrip(os.sep) + os.sep))


def _finish(
    facts: TranscriptFacts, command_log: list[dict[str, Any]], files: list[str], open_todos: list[str]
) -> TranscriptFacts:
    """Turn a parsed command log (oldest first: ``cmd``, ``exit_code``, ``output``) into the facts
    every transcript parser reports: commands, test verdicts, errors, files, open todos."""
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


def parse_claude_transcript(path: Path, deadline: Deadline | None = None, root: str | None = None) -> TranscriptFacts:
    """Extract Bash commands + exit codes, test runs, edited files and open todos.

    Format (Claude Code JSONL): ``assistant`` lines carry ``tool_use`` blocks
    (``name``, ``id``, ``input``); ``user`` lines carry the matching
    ``tool_result`` (``tool_use_id``, ``is_error``, ``content``) and a
    ``toolUseResult`` object. A failed Bash result starts with ``Exit code N``.

    ``root`` (the repository top level) drops commands that ran in another
    directory, so another repo's test runs and errors are not reported for
    this project. Where a command ran is the ``cwd`` Claude Code records on
    the tool_use line (the Bash tool keeps its directory between calls),
    moved by a leading ``cd``/``pushd``/subshell or ``git -C``. Commit ids
    printed by ``git commit`` inside the repository are commit evidence.
    """
    facts = TranscriptFacts()
    uses: dict[str, tuple[str, dict[str, Any], str | None]] = {}
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
                line_cwd = entry.get("cwd")
                uses[str(block.get("id"))] = (name, tool_input, line_cwd if isinstance(line_cwd, str) else None)
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
                name, tool_input, use_cwd = uses[use_id]
                is_error = bool(block.get("is_error"))
                text = _result_text(block.get("content"))
                if name == "Bash":
                    command = str(tool_input.get("command") or "").strip()
                    if not command or tool_input.get("run_in_background"):
                        continue
                    match = _EXIT_RE.match(text)
                    if is_error and not match:
                        continue  # rejected / blocked / interrupted: no real exit code
                    if _outside_root(command, root, use_cwd):
                        continue
                    facts.commit_shas.extend(_COMMIT_LINE_RE.findall(text))
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

    open_todos: list[str] = []
    if todo_snapshot is not None:
        open_todos.extend(str(t.get("content") or "") for t in todo_snapshot if t.get("status") != "completed")
    open_todos.extend(
        t["subject"] for _, t in sorted(tasks.items(), key=lambda kv: kv[0]) if t["status"] not in ("completed", "deleted")
    )

    return _finish(facts, command_log, files, open_todos)


# ---------------------------------------------------------------------------
# Codex rollout transcripts
# ---------------------------------------------------------------------------

CLAUDE_JSONL = "claude-jsonl"
CODEX_ROLLOUT = "codex-rollout-jsonl"
TRANSCRIPT_FORMATS = (CLAUDE_JSONL, CODEX_ROLLOUT)

# Codex's own text when a turn stops on the plan's usage limit. Recorded from
# codex-cli 0.155.0-alpha.16.4 (tests/fixtures/relay/codex/rollout_usage_limit.jsonl):
# "You’ve hit your usage limit. Upgrade to Pro (...), visit https://chatgpt.com/codex/settings/usage ...".
# The structured marker on the same record is codex_error_info = "usage_limit_exceeded".
CODEX_USAGE_LIMIT_INFO = "usage_limit_exceeded"
_CODEX_LIMIT_PREFIXES = ("you've hit your usage limit", "you have hit your usage limit")
_CODEX_SHELL_TOOLS = ("exec_command", "shell", "shell_command", "local_shell")
_CODEX_EXIT_RE = re.compile(r"(?:Process exited with code|Exit code:?)\s+(-?\d+)")


def _codex_limit_text(message: Any) -> bool:
    text = str(message or "").replace("’", "'").strip().lower()
    return text.startswith(_CODEX_LIMIT_PREFIXES) or ": you've hit your usage limit" in text


def _codex_error(payload: dict[str, Any]) -> str | None:
    """The usage-limit message on a ``task_complete`` / ``error`` event, else None."""
    error = payload.get("error") if payload.get("type") == "task_complete" else payload
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if error.get("codex_error_info") == CODEX_USAGE_LIMIT_INFO or _codex_limit_text(message):
        return _clip(str(message or "usage limit reached"), 300)
    return None


def _codex_command_text(command: Any) -> str:
    """``["/bin/zsh", "-lc", "pytest -q"]`` -> ``pytest -q``; a plain string stays as is."""
    if isinstance(command, str):
        return command.strip()
    if isinstance(command, list) and all(isinstance(c, str) for c in command):
        if len(command) >= 3 and command[-2] in ("-lc", "-c") and os.path.basename(command[0]) in ("bash", "zsh", "sh"):
            return str(command[-1]).strip()
        return shlex.join(command).strip()
    return ""


def _codex_cwd(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("file://"):
        return unquote(urlsplit(value).path) or None
    return value


def _codex_output_exit(output: Any) -> tuple[str, int | None]:
    """(text, exit code) from a ``function_call_output`` payload's ``output``.

    Unified exec returns text with "Process exited with code N"; the older
    shell tool returned JSON ``{"output": ..., "metadata": {"exit_code": N}}``.
    """
    if isinstance(output, dict):  # structured output: {"content": ...}
        output = output.get("content") or output.get("output")
    text = output if isinstance(output, str) else ""
    if text.lstrip().startswith("{"):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict) and isinstance(decoded.get("metadata"), dict):
            code = decoded["metadata"].get("exit_code")
            return str(decoded.get("output") or ""), code if isinstance(code, int) else None
    match = _CODEX_EXIT_RE.search(text)
    return text, int(match.group(1)) if match else None


def parse_codex_rollout(path: Path, deadline: Deadline | None = None, root: str | None = None) -> TranscriptFacts:
    """Commands + exit codes, test runs, edited files, open plan steps and a
    usage-limit stop from a Codex rollout (``~/.codex/sessions/.../rollout-*.jsonl``).

    Every line is ``{"timestamp", "type", "payload"}``. Recorded from
    codex-cli 0.155.0-alpha.16.4 (tests/fixtures/relay/codex/):

    - ``session_meta``: ``payload.id`` (the session id the hooks receive) and ``cwd``.
    - ``event_msg`` / ``item_completed`` with ``item.type == "CommandExecution"``:
      ``command`` (argv), ``cwd`` (``file://`` URL), ``exit_code``, ``aggregated_output``.
      This is the authoritative record of a shell command.
    - ``event_msg`` / ``item_completed`` with ``item.type == "FileChange"``:
      ``changes`` maps each edited path to its diff; ``status == "completed"``.
    - ``event_msg`` / ``task_complete`` with ``error.codex_error_info ==
      "usage_limit_exceeded"`` when the turn stopped on the plan's limit.
    - ``response_item`` / ``function_call`` + ``function_call_output`` (same
      ``call_id``): used for a command only when no ``CommandExecution`` event
      exists for it (older rollouts); ``exec_command_end`` events from older
      versions are read the same way.

    Codex has no hook for its usage limit (openai/codex#45977), so the stop is
    read here from the transcript tail. A later turn that starts clears it.
    """
    facts = TranscriptFacts()
    order: list[str] = []
    entries: dict[str, dict[str, Any]] = {}
    from_event: set[str] = set()
    calls: dict[str, tuple[str, str | None]] = {}  # call_id -> (command, workdir)
    files: list[str] = []
    plan: list[dict[str, Any]] | None = None
    session_cwd: str | None = None
    limit: str | None = None

    def record(key: str, command: str, exit_code: int, output: str, cwd: str | None, event: bool) -> None:
        if key in from_event and not event:
            return
        if key not in entries:
            order.append(key)
        entries[key] = {"cmd": command, "exit_code": exit_code, "output": output, "cwd": cwd}
        if event:
            from_event.add(key)

    for n, line in enumerate(_iter_lines(path)):
        if deadline is not None and deadline.expired:
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
            continue
        kind, payload = entry.get("type"), entry["payload"]
        if kind == "session_meta":
            sid = payload.get("id") or payload.get("session_id")
            if isinstance(sid, str) and facts.session_id is None:
                facts.session_id = sid
            ts = payload.get("timestamp") or entry.get("timestamp")
            if isinstance(ts, str) and facts.started_at is None:
                try:
                    facts.started_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    pass
            session_cwd = _codex_cwd(payload.get("cwd")) or session_cwd
            continue
        if kind == "turn_context":
            session_cwd = _codex_cwd(payload.get("cwd")) or session_cwd
            continue
        ptype = payload.get("type")
        if kind == "event_msg":
            if ptype == "task_started":
                limit = None
            elif ptype == "task_complete" and isinstance(payload.get("error"), dict):
                limit = _codex_error(payload)  # this turn's outcome (a different error clears it)
            elif ptype == "error":
                limit = _codex_error(payload) or limit  # older versions: an error event, then task_complete
            elif ptype == "item_completed" and isinstance(payload.get("item"), dict):
                item = payload["item"]
                if item.get("type") == "CommandExecution" and isinstance(item.get("exit_code"), int):
                    output = item.get("aggregated_output") or item.get("formatted_output") or ""
                    if not output:
                        output = "\n".join(str(item.get(k) or "") for k in ("stdout", "stderr")).strip()
                    key = str(item.get("id") or f"line-{n}")
                    record(
                        key,
                        _codex_command_text(item.get("command")),
                        item["exit_code"],
                        str(output),
                        _codex_cwd(item.get("cwd")),
                        True,
                    )
                elif item.get("type") == "FileChange" and item.get("status") == "completed":
                    changes = item.get("changes")
                    if isinstance(changes, dict):
                        files.extend(p for p in changes if isinstance(p, str) and p)
            elif ptype == "exec_command_end" and isinstance(payload.get("exit_code"), int):
                output = payload.get("aggregated_output") or payload.get("formatted_output") or ""
                key = str(payload.get("call_id") or f"line-{n}")
                record(
                    key,
                    _codex_command_text(payload.get("command")),
                    payload["exit_code"],
                    str(output),
                    _codex_cwd(payload.get("cwd")),
                    True,
                )
            continue
        if kind != "response_item":
            continue
        if ptype == "function_call":
            name = str(payload.get("name") or "")
            try:
                args = json.loads(payload.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            if not isinstance(args, dict):
                continue
            call_id = str(payload.get("call_id") or "")
            if name in _CODEX_SHELL_TOOLS and call_id:
                command = _codex_command_text(args.get("cmd") or args.get("command"))
                workdir = args.get("workdir")
                calls[call_id] = (command, workdir if isinstance(workdir, str) else None)
            elif name == "update_plan" and isinstance(args.get("plan"), list):
                plan = [p for p in args["plan"] if isinstance(p, dict)]
        elif ptype == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            if call_id in calls and call_id not in from_event:
                command, workdir = calls[call_id]
                text, code = _codex_output_exit(payload.get("output"))
                if command and code is not None:
                    record(call_id, command, code, text, workdir, False)

    command_log: list[dict[str, Any]] = []
    for key in order:
        item = entries[key]
        command = item["cmd"]
        if not command:
            continue
        cwd = item["cwd"] or session_cwd
        if _outside_root(command, root, cwd):
            continue
        facts.commit_shas.extend(_COMMIT_LINE_RE.findall(item["output"]))
        command_log.append(item)
    open_todos = [str(p.get("step") or "") for p in plan or [] if p.get("status") != "completed"]
    facts.usage_limit = _scrub(limit) if limit else None
    return _finish(facts, command_log, files, open_todos)


def detect_transcript_format(path: Path) -> str | None:
    """``codex-rollout-jsonl`` when the first record is a Codex ``session_meta``,
    ``claude-jsonl`` for any other JSONL, None when unreadable or empty."""
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    first = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    return None
                if isinstance(first, dict) and first.get("type") == "session_meta" and isinstance(first.get("payload"), dict):
                    return CODEX_ROLLOUT
                return CLAUDE_JSONL
    except OSError:
        return None
    return None


def parse_transcript(path: Path, fmt: str, deadline: Deadline | None = None, root: str | None = None) -> TranscriptFacts:
    if fmt == CODEX_ROLLOUT:
        return parse_codex_rollout(path, deadline, root=root)
    if fmt == CLAUDE_JSONL:
        return parse_claude_transcript(path, deadline, root=root)
    raise ValueError(f"unknown transcript format {fmt!r}")


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
    how = facts.pop("commit_range", None)
    if how:
        facts["commit_evidence"] = how
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
