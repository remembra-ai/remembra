"""Which commands are test runs, and whether they passed.

Shared by the relay client (the transcript fact builder decides each run's
verdict) and the server (which re-checks every verdict it is sent, so a
handoff from a client that still reports the shell's exit code is corrected).

Two rules, both mechanical:

* A command is a test run only when a runner is *run* by it: the runner is the
  program of one of its segments (after ``VAR=value`` and wrappers such as
  ``uv run`` or ``timeout 60``), or the program a carrier runs (``ssh host
  "pytest"``, ``docker compose exec app pytest``, ``bash -lc '...'``). A runner
  name inside an argument, a quoted string or a heredoc body is just text
  (``pkill -f pytest``, ``pip install pytest``, ``echo "run pytest"``).
* The shell's exit code is the runner's only when nothing follows the runner
  that replaces it (``pytest | tail -3``, ``pytest; git stash pop``, ``pytest
  || true``). When it is masked, the runner's own summary line decides; with no
  summary the result was not captured and the run is not reported at all.

Pure functions; nothing here executes a command.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import re
from dataclasses import dataclass, replace
from typing import Any

# ---------------------------------------------------------------------------
# Command structure
# ---------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")
_MAX_NESTING = 3
# Separators after which a failed run still fails the whole command.
_PASS_THROUGH = frozenset({"&&", ")", "("})


def _strip_heredocs(command: str) -> str:
    """``command`` without heredoc bodies (the text between ``<<WORD`` and its closing ``WORD`` line)."""
    if "<<" not in command:
        return command
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in _HEREDOC_RE.finditer(line):
            word = match.group(3)
            end = next((j for j in range(i, len(lines)) if lines[j].strip() == word), None)
            if end is not None:  # an unterminated "heredoc" is not one: keep the text
                i = end + 1
    return "\n".join(out)


@dataclass(frozen=True)
class _Segment:
    words: tuple[str, ...]
    quoted: tuple[bool, ...]  # per word: it came from a quoted string
    sep: str  # the operator that ended it ("" at the end of the command)


def _segments(command: str) -> list[_Segment]:
    """Split a shell command on ``; && || | & ( )`` and newlines, outside quotes."""
    text = _strip_heredocs(command)
    out: list[_Segment] = []
    words: list[str] = []
    flags: list[bool] = []
    buf: list[str] = []
    in_word = False
    was_quoted = False

    def end_word() -> None:
        nonlocal in_word, was_quoted
        if in_word:
            words.append("".join(buf))
            flags.append(was_quoted)
        buf.clear()
        in_word = was_quoted = False

    def end_segment(sep: str) -> None:
        end_word()
        out.append(_Segment(tuple(words), tuple(flags), sep))
        words.clear()
        flags.clear()

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            if text[i + 1] != "\n":  # a line continuation is nothing
                buf.append(text[i + 1])
                in_word = True
            i += 2
        elif ch == "'":
            close = text.find("'", i + 1)
            close = n if close < 0 else close
            buf.append(text[i + 1 : close])
            in_word = was_quoted = True
            i = close + 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            in_word = was_quoted = True
            i = j + 1
        elif ch in "$<>" and text.startswith("(", i + 1):
            # $(...) and <(...) are part of the word they stand in, not commands of their own.
            depth, j = 0, i + 1
            while j < n:
                depth += (text[j] == "(") - (text[j] == ")")
                j += 1
                if depth == 0:
                    break
            buf.append(text[i:j])
            in_word = True
            i = j
        elif ch == "`":
            close = text.find("`", i + 1)
            close = n - 1 if close < 0 else close
            buf.append(text[i : close + 1])
            in_word = True
            i = close + 1
        elif ch in " \t":
            end_word()
            i += 1
        elif ch == "#" and not in_word:
            while i < n and text[i] != "\n":  # a comment runs to the end of its line
                i += 1
        elif ch == "\n" or ch == ";":
            end_segment(";")
            i += 1
        elif ch == "|":
            double = text.startswith("||", i)
            end_segment("||" if double else "|")
            i += 2 if double or text.startswith("|&", i) else 1
        elif ch == "&":
            if text.startswith("&&", i):
                end_segment("&&")
                i += 2
            elif text.startswith("&>", i) or (buf and buf[-1] in "<>"):  # a redirection: &>file, 2>&1
                buf.append(ch)
                in_word = True
                i += 1
            else:
                end_segment("&")
                i += 1
        elif ch in "()":
            end_segment(ch)
            i += 1
        else:
            buf.append(ch)
            in_word = True
            i += 1
    end_segment("")
    return out


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# Words in front of a command that do not change which program runs.
_PREFIX_WORDS = frozenset(
    {"sudo", "doas", "env", "time", "nice", "exec", "command", "nohup", "stdbuf", "caffeinate", "unbuffer", "arch"}
    | {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
)
# Programs that run the command given in their later words (or in a quoted argument).
_CARRIERS = frozenset(
    {"timeout", "gtimeout", "npx", "bunx", "pnpx", "xvfb-run", "xargs", "ssh", "bash", "sh", "zsh", "dash", "fish"}
    | {"su", "nix-shell", "script", "watch", "parallel", "flock", "chronic"}
)
# The same, but only with this subcommand (``uv run`` runs something; ``uv pip install pytest`` does not).
_CARRIER_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "uv": frozenset({"run"}),
    "poetry": frozenset({"run"}),
    "pipenv": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "hatch": frozenset({"run"}),
    "rye": frozenset({"run"}),
    "pixi": frozenset({"run"}),
    "conda": frozenset({"run"}),
    "mamba": frozenset({"run"}),
    "pnpm": frozenset({"exec", "dlx"}),
    "yarn": frozenset({"exec", "dlx"}),
    "npm": frozenset({"exec"}),
    "bundle": frozenset({"exec"}),
    "coverage": frozenset({"run"}),
    "docker": frozenset({"exec", "run", "compose"}),
    "docker-compose": frozenset({"exec", "run"}),
    "podman": frozenset({"exec", "run"}),
    "kubectl": frozenset({"exec"}),
    "nix": frozenset({"develop", "shell", "run"}),
    "devbox": frozenset({"run"}),
    "direnv": frozenset({"exec"}),
    "dotenv": frozenset({"run"}),
    "tmux": frozenset(),
}
# Programs a carrier may run that are never a test run, whatever their arguments name
# (``uv run pip install pytest``, ``timeout 5 grep -c "pytest -q" log``).
_NOT_RUNNERS = frozenset(
    {"pip", "pip3", "pipx", "apt", "apt-get", "brew", "grep", "egrep", "rg", "echo", "printf", "cat", "ls", "which"}
    | {"kill", "pkill", "pgrep", "ps", "git", "find", "sed", "awk", "head", "tail", "rm", "cp", "mv", "touch", "mkdir"}
)
_SINGLE_RUNNERS = frozenset({"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "ava", "rspec", "phpunit", "ctest"})
_TWO_WORD_RUNNERS: dict[str, frozenset[str]] = {
    "go": frozenset({"test"}),
    "cargo": frozenset({"test", "nextest"}),
    "swift": frozenset({"test"}),
    "deno": frozenset({"test"}),
    "dotnet": frozenset({"test"}),
    "mix": frozenset({"test"}),
    "flutter": frozenset({"test"}),
    "dart": frozenset({"test"}),
    "make": frozenset({"test", "check"}),
    "gmake": frozenset({"test", "check"}),
}
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
# Package-manager options that take a value (``pnpm --filter web test``, ``npm --prefix web test``).
_PM_VALUE_FLAGS = frozenset({"--filter", "-F", "-C", "--dir", "--prefix", "--cwd", "--workspace"})
# Arguments with which a runner starts but runs no test: it lists, collects, compiles or prints help.
_NO_RUN_FLAGS = frozenset({"--version", "--help", "-h", "--collect-only", "--co", "--list-tests", "--listTests", "--dry-run"})
_NO_RUN_FLAGS_BY_RUNNER: dict[str, frozenset[str]] = {
    "pytest": frozenset({"-V", "--fixtures", "--fixtures-per-test", "--markers", "--setup-only", "--setup-plan"}),
    "py.test": frozenset({"-V", "--fixtures", "--fixtures-per-test", "--markers", "--setup-only", "--setup-plan"}),
    "cargo test": frozenset({"--no-run"}),
    "cargo nextest": frozenset({"--no-run", "list"}),
    "go test": frozenset({"-c", "-list"}),
    "tox": frozenset({"-l", "-a", "--list", "--listenvs", "--listenvs-all", "--showconfig", "--notest"}),
    "nox": frozenset({"-l", "--list"}),
    "jest": frozenset({"--showConfig"}),
    "vitest": frozenset({"list"}),
    "ctest": frozenset({"-N", "--show-only"}),
    "swift test": frozenset({"list"}),
    "make test": frozenset({"-n", "--just-print", "--recon"}),
    "make check": frozenset({"-n", "--just-print", "--recon"}),
    "gmake test": frozenset({"-n", "--just-print", "--recon"}),
    "gmake check": frozenset({"-n", "--just-print", "--recon"}),
}
_BUILD_TOOLS = frozenset({"xcodebuild", "mvn", "mvnw", "gradle", "gradlew"})
_PYTHON_RE = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|py)$")
_REDIRECT_RE = re.compile(r"^(?:\d*[<>]|&>)")
# pytest options that take a value (dropped from a run's targets) and the ones that narrow what runs.
_PYTEST_VALUE_FLAGS = frozenset(
    {"-p", "-n", "-c", "-o", "-W", "--tb", "--timeout", "--maxfail", "--durations", "--basetemp", "--rootdir", "--junitxml"}
    | {"--cov", "--cov-report", "--cov-config", "--color", "--numprocesses", "--dist", "--log-level", "--import-mode"}
)
_PYTEST_SELECTORS_WITH_VALUE = frozenset({"-k", "-m", "--deselect", "--ignore", "--ignore-glob"})
_PYTEST_SELECTORS = _PYTEST_SELECTORS_WITH_VALUE | {"--lf", "--last-failed", "--sw", "--stepwise"}


@dataclass(frozen=True)
class Invocation:
    """One test runner a command runs."""

    runner: str  # "pytest", "go test", "npm test", ...
    args: tuple[str, ...]  # the runner's own arguments (redirections removed)
    decisive: bool  # the command's exit code is this runner's (nothing after it replaces it)
    cwd: tuple[str, ...] = ()  # ``cd`` targets earlier in the command

    @property
    def text(self) -> str:
        return " ".join((self.runner, *self.args))

    @property
    def executes(self) -> bool:
        """False when the arguments keep the runner from running a test (``pytest --collect-only``,
        ``pytest --version``, ``cargo test --no-run``, ``tox -l``): such a command tested nothing."""
        mine = _NO_RUN_FLAGS_BY_RUNNER.get(self.runner, frozenset())
        return not any(arg.split("=", 1)[0] in _NO_RUN_FLAGS or arg.split("=", 1)[0] in mine for arg in self.args)

    @property
    def key(self) -> tuple[Any, ...]:
        """What was tested: runner, directory, targets and selectors (flags and their order do not matter)."""
        targets, selectors = _targets(self.runner, self.args)
        return (self.runner, self.cwd, tuple(sorted(targets)), tuple(sorted(selectors)))

    def covers(self, other: Invocation) -> bool:
        """True when this run tests at least everything ``other`` did (same runner and directory)."""
        if self.runner != other.runner or self.cwd != other.cwd:
            return False
        mine, my_selectors = _targets(self.runner, self.args)
        theirs, their_selectors = _targets(other.runner, other.args)
        if my_selectors:
            return sorted(mine) == sorted(theirs) and sorted(my_selectors) == sorted(their_selectors)
        if not mine:
            return True  # the whole suite
        if not theirs:
            return False
        return all(any(_under(t, m) for m in mine) for t in theirs)


def _under(target: str, scope: str) -> bool:
    """True when the test target ``target`` (a path, or ``path::test``) is inside ``scope`` (a file, a
    directory, a ``path::test`` or a glob such as ``tests/test_inbox*.py``)."""
    path = target.split("::", 1)[0].rstrip("/")
    scope_path = scope.rstrip("/")
    if "::" in scope:
        return target == scope or target.startswith(scope + "::")
    if any(ch in scope_path for ch in "*?["):
        return fnmatch.fnmatchcase(path, scope_path)
    return path == scope_path or path.startswith(scope_path + "/")


def _targets(runner: str, args: tuple[str, ...]) -> tuple[list[str], list[str]]:
    targets: list[str] = []
    selectors: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name = arg.split("=", 1)[0]
        if runner in ("pytest", "py.test") and name in _PYTEST_SELECTORS:
            if name in _PYTEST_SELECTORS_WITH_VALUE:
                value = arg.split("=", 1)[1] if "=" in arg else (args[i + 1] if i + 1 < len(args) else "")
                selectors.append(f"{name} {value}".strip())
                i += 1 if "=" in arg else 2
            else:
                selectors.append(name)
                i += 1
        elif runner in ("pytest", "py.test") and name in _PYTEST_VALUE_FLAGS:
            i += 1 if "=" in arg else 2
        elif arg.startswith("-"):
            i += 1
        else:
            targets.append(arg)
            i += 1
    return targets, selectors


def _clean_args(words: tuple[str, ...]) -> tuple[str, ...]:
    """A runner's arguments without redirections (``2>&1``, ``> out.txt``)."""
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif _REDIRECT_RE.match(word):
            skip = word.endswith((">", "<"))  # the target is the next word
        else:
            out.append(word)
    return tuple(out)


def _runner_at(words: tuple[str, ...], i: int) -> tuple[str, tuple[str, ...]] | None:
    """``(runner, args)`` when the program at ``words[i]`` is a test runner."""
    base = words[i].rsplit("/", 1)[-1]
    rest = words[i + 1 :]
    if base in _SINGLE_RUNNERS:
        return base, rest
    if _PYTHON_RE.match(base):
        for j, word in enumerate(rest[:8]):
            if word == "-m":
                module = rest[j + 1] if j + 1 < len(rest) else ""
                return (module, rest[j + 2 :]) if module in ("pytest", "unittest") else None
            if word.endswith(".py") or word in ("-c", "-"):
                return None
        return None
    if base in _TWO_WORD_RUNNERS:
        plain = [(j, w) for j, w in enumerate(rest) if not w.startswith(("-", "+"))]
        if plain and plain[0][1] in _TWO_WORD_RUNNERS[base]:
            return f"{base} {plain[0][1]}", rest[plain[0][0] + 1 :]
        return None
    if base in _PACKAGE_MANAGERS:
        # The script or program the package manager runs: its first plain word, after its own
        # options (``pnpm -r``, ``pnpm --filter web``), ``yarn workspace <name>`` and ``run``.
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] in _PM_VALUE_FLAGS or (base == "npm" and rest[j] == "-w") else 1
        if base == "yarn" and rest[j : j + 1] == ("workspace",):
            j += 2
        if rest[j : j + 1] == ("run",):
            j += 1
        script = rest[j] if j < len(rest) else ""
        if script == "test" or script.startswith("test:"):
            return f"{base} {script}", rest[j + 1 :]
        if script.rsplit("/", 1)[-1] in _SINGLE_RUNNERS:  # ``yarn jest``, ``pnpm vitest run``, ``bun run vitest``
            return script.rsplit("/", 1)[-1], rest[j + 1 :]
        return None
    if base in _BUILD_TOOLS and any(w in ("test", "test-without-building") or w.endswith(":test") for w in rest):
        return base, tuple(w for w in rest if not w.startswith("-"))
    return None


def _segment_runs(segment: _Segment, depth: int) -> list[tuple[str, tuple[str, ...], bool]]:
    """``(runner, args, nested_decisive)`` for every runner this one segment runs."""
    words, quoted = segment.words, segment.quoted
    i = 0
    while i < len(words) and not quoted[i] and (_ASSIGN_RE.match(words[i]) or words[i] in _PREFIX_WORDS):
        i += 1
    if i >= len(words) or quoted[i]:
        return []
    found = _runner_at(words, i)
    if found is not None:
        return [(found[0], _clean_args(found[1]), True)]
    program = words[i].rsplit("/", 1)[-1]
    subcommands = _CARRIER_SUBCOMMANDS.get(program)
    if program not in _CARRIERS and subcommands is None:
        return []
    if subcommands and not any(w in subcommands for w in words[i + 1 : i + 4]):
        return []
    runs: list[tuple[str, tuple[str, ...], bool]] = []
    for j in range(i + 1, len(words)):
        if quoted[j]:
            if depth < _MAX_NESTING and (" " in words[j] or j == len(words) - 1):
                runs.extend((inv.runner, inv.args, inv.decisive) for inv in _invocations(words[j], depth + 1))
        elif not words[j].startswith("-"):
            found = _runner_at(words, j)
            if found is not None:
                runs.append((found[0], _clean_args(found[1]), True))
                break
            if words[j].rsplit("/", 1)[-1] in _NOT_RUNNERS:
                break  # the carried program: the rest are its arguments
    return runs


def _invocations(command: str, depth: int) -> list[Invocation]:
    segments = _segments(command)
    out: list[Invocation] = []
    cwd: list[str] = []
    for k, segment in enumerate(segments):
        if segment.words[:1] == ("cd",) and len(segment.words) > 1:
            cwd.append(segment.words[1])
            continue
        runs = _segment_runs(segment, depth)
        if not runs:
            continue
        # Decisive: every later, non-empty segment is reached only through "&&" (or a subshell's parenthesis).
        decisive = True
        for m in range(k, len(segments) - 1):
            if any(s.words for s in segments[m + 1 :]) and segments[m].sep not in _PASS_THROUGH:
                decisive = False
                break
        if segments[k].sep == "&" or any(s.sep == "&" for s in segments[k:]):
            decisive = False  # a background job's exit code is never the command's
        out.extend(Invocation(runner, args, decisive and nested, tuple(cwd)) for runner, args, nested in runs)
    return out


@functools.lru_cache(maxsize=2048)
def _cached_invocations(command: str) -> tuple[Invocation, ...]:
    return tuple(_invocations(command, 0))


def runner_invocations(command: Any) -> list[Invocation]:
    """The test runners ``command`` runs, in order. Empty when it runs none."""
    text = str(command or "")
    return list(_cached_invocations(text)) if text.strip() else []


def executing_invocations(command: Any) -> list[Invocation]:
    """The runners ``command`` runs tests with: :func:`runner_invocations` without the ones whose
    arguments run no test (``pytest --version``, ``pytest --collect-only``, ``cargo test --no-run``)."""
    return [run for run in runner_invocations(command) if run.executes]


def mentions_runner_only(command: Any) -> bool:
    """True when ``command`` is known not to run tests: every program it runs is one that never
    does (``pip install pytest``, ``pkill -f pytest``, ``grep -rn 'cargo test' docs``). False for a
    program this module does not know (``./scripts/run-tests.sh``, ``python manage.py test``)."""
    segments = [seg for seg in _segments(str(command or "")) if seg.words]
    if not segments or runner_invocations(command):
        return False
    for seg in segments:
        words = [w for w, q in zip(seg.words, seg.quoted, strict=True) if not q and not w.startswith("-")]
        while words and (_ASSIGN_RE.match(words[0]) or words[0] in _PREFIX_WORDS):
            words.pop(0)
        if not words:
            return False
        program = words[0].rsplit("/", 1)[-1]
        if program in _CARRIERS or program in _CARRIER_SUBCOMMANDS:
            # ``uv pip install pytest``, ``timeout 5 grep pytest log``: the program it carries.
            carried = [w.rsplit("/", 1)[-1] for w in words[1:4]]
            if not any(w in _NOT_RUNNERS for w in carried):
                return False
        elif program not in _NOT_RUNNERS and program != "cd":
            return False
    return True


def moved_directory(command: Any, directory: str) -> str:
    """The shell's directory after ``command``, when the command only changes directory
    (``cd ../other``, ``cd a && cd b``); ``directory`` unchanged for anything else."""
    segments = [seg for seg in _segments(str(command or "")) if seg.words]
    if not segments or any(seg.words[0] != "cd" for seg in segments):
        return directory
    for seg in segments:
        target = seg.words[1] if len(seg.words) > 1 else "~"
        # ``cd -`` goes somewhere this log does not name: kept as a directory of its own ("-").
        directory = _join_dir(directory, (target,))
    return directory


def _join_dir(base: str, targets: tuple[str, ...]) -> str:
    return os.path.normpath(os.path.join(base, *(os.path.expanduser(t) for t in targets)))


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

# The line a runner prints to say how the run went (one per runner invocation).
_SUMMARY_RES = (
    re.compile(r"^(?:\d+ [a-z]+(?:, | and )?)+ in \d[\d.:]*\s*(?:s|sec|seconds|m|ms)\b", re.IGNORECASE),  # pytest
    re.compile(r"^no tests ran\b", re.IGNORECASE),
    re.compile(r"^Tests:?\s+.*\d+\s+(?:passed|failed|total|skipped|todo)\b", re.IGNORECASE),  # jest, vitest
    re.compile(r"\*\* TEST (?:SUCCEEDED|FAILED) \*\*"),  # xcodebuild
    re.compile(r"\btest result: \w+", re.IGNORECASE),  # cargo
    re.compile(r"^(?:ok|FAIL)\s+\S+\s+[\d.]+s\b|^(?:FAIL|PASS)$"),  # go
    re.compile(r"^(?:OK|FAILED)(?:\s+\(.*\))?$"),  # unittest, phpunit
    re.compile(r"^\d+ (?:passing|failing)\b"),  # mocha
    re.compile(r"^Tests: \d+, Assertions: \d+", re.IGNORECASE),  # phpunit
    re.compile(r"^(?:Passed|Failed)!\s+-\s+Failed:\s+\d+", re.IGNORECASE),  # dotnet
    re.compile(r"^# (?:pass|fail) \d+$"),  # node:test, TAP
    re.compile(r"^\d+ examples?, \d+ failures?\b"),  # rspec
    re.compile(r"\bExecuted \d+ tests?, with \d+ failures?\b"),  # xctest, swift
    re.compile(r"^(?:All tests passed!|Some tests failed\.)"),  # flutter, dart
)
# Any line that reads like a result, when no runner summary is found (kept for display).
_LOOSE_SUMMARY_RE = re.compile(
    r"\b\d+\s+(?:passed|failed|errors?|failures?)\b|^Tests?:\s|\bTEST (?:SUCCEEDED|FAILED)\b|^(?:ok|FAIL)\s|\btest result:",
    re.IGNORECASE,
)
_FAILURE_RES = (
    re.compile(r"(?<![\w.])[1-9]\d*\s+(?:failed|failing|failures?|errors?)\b", re.IGNORECASE),
    re.compile(r"\bTEST FAILED\b"),
    re.compile(r"\btest result: FAILED\b", re.IGNORECASE),
    re.compile(r"^FAIL(?:ED)?\b"),
    re.compile(r"\b(?:failures|errors|failed)\s*[:=]\s*[1-9]", re.IGNORECASE),
    re.compile(r"^# fail [1-9]"),
    re.compile(r"^not ok\b"),
    re.compile(r"^Some tests failed\b"),
    re.compile(r"^FAILURES!"),
)


# A summary an agent typed as counts only ("1 failed, 214 passed", "2 errors"): a runner's line without its timing.
_COUNTS_ONLY_RE = re.compile(
    r"^(?:\d+ (?:passed|failed|errors?|failures?|skipped|xfailed|xpassed|warnings?|deselected|rerun)(?:, | and |; | \| )?)+"
    r"(?: in \d[\d.:]*\s*[a-z]*)?\.?$",
    re.IGNORECASE,
)
# A summary that says tests ran and passed ...
_RAN_RES = (
    re.compile(r"(?<![\w.])[1-9]\d*\s+(?:tests?\s+)?(?:passed|passing|pass|succeeded|examples?)\b", re.IGNORECASE),
    re.compile(r"\b(?:passed|pass)\s*[:=]\s*[1-9]", re.IGNORECASE),
    re.compile(r"^ok\s+\S+\s+[\d.]+s\b|^PASS$|^OK(?:\s+\(.*\))?$|^All tests passed!"),
    re.compile(r"\*\* TEST SUCCEEDED \*\*|\btest result: ok\b|\bExecuted [1-9]\d* tests?, with 0 failures\b"),
    re.compile(r"^# pass [1-9]|^Tests: [1-9]\d*, Assertions: \d+", re.IGNORECASE),
)
# ... and one that says the runner ran none.
_NOTHING_RAN_RE = re.compile(
    r"\bno tests? (?:ran|to run|files|found|collected|were (?:run|found))\b|\bcollected 0 items\b|\b0 passed; 0 failed\b"
    r"|\bran 0 tests\b|\bExecuted 0 tests\b|^0 examples\b",
    re.IGNORECASE,
)


def _reads_failed(text: str) -> bool:
    return any(rx.search(text) for rx in _FAILURE_RES)


# Lines only a runner prints for a failure ("not ok 3 - parses the header", "FAILED tests/a.py::test_x", "FAILURES!").
_FAILURE_LINE_RE = re.compile(r"^(?:not ok\b|FAIL(?:ED|URES!)?(?=$|[\s:(])|# fail [1-9]|Some tests failed\b)|\bTEST FAILED\b")


def _is_summary_line(text: str) -> bool:
    return any(rx.search(text) for rx in _SUMMARY_RES) or bool(_COUNTS_ONLY_RE.match(text)) or bool(_FAILURE_LINE_RE.search(text))


def summary_shows_failure(summary: Any) -> bool:
    """True when ``summary`` is a runner's summary line that reports failed tests or errors
    (``"1 failed, 214 passed in 3.2s"``, ``"1 failed, 214 passed"``, ``"Tests: 1 failed, 10 total"``).

    Only a runner's line counts. A sentence an agent wrote about its run (``"214 passed after
    fixing the 2 failures from the first run"``) is not one and never overrides the verdict it
    comes with.
    """
    text = str(summary or "").strip(" =\t")
    return bool(text) and _is_summary_line(text) and _reads_failed(text)


def summary_shows_tests_ran(summary: Any) -> bool:
    """True when ``summary`` says tests ran and none failed (``"4 passed in 0.1s"``, ``"12 passed"``,
    ``"ok pkg 0.01s"``): what a green run needs before it stands in for an earlier red one. False
    with no summary, for one that ran nothing (``"no tests ran"``, ``"4 deselected"``, ``"[no tests to
    run]"``) and for one that reports failures."""
    text = str(summary or "").strip(" =\t")
    if not text or _NOTHING_RAN_RE.search(text) or _reads_failed(text):
        return False
    return any(rx.search(text) for rx in _RAN_RES)


def _summary_lines(output: str) -> tuple[list[str], list[str]]:
    lines = [ln.strip(" =\t") for ln in str(output or "").splitlines() if ln.strip(" =\t")]
    strict = [ln for ln in lines if any(rx.search(ln) for rx in _SUMMARY_RES)]
    loose = [ln for ln in lines if _LOOSE_SUMMARY_RE.search(ln)]
    return strict, loose


def run_summary(output: Any, runs: int = 1) -> str | None:
    """The runner's summary line in ``output``: its last one, or, when the command ran
    several different suites, the first that reports failures (so one suite's failure is
    not lost behind a later suite's pass). Falls back to the last line that reads like a
    result; None when nothing does."""
    strict, loose = _summary_lines(str(output or ""))
    if strict:
        if runs > 1:
            failed = next((ln for ln in strict if _reads_failed(ln)), None)
            if failed:
                return failed
        return strict[-1]
    return loose[-1] if loose else None


_FAILED_TEST_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$")
_FAILED_COUNT_RE = re.compile(r"(?<![\w.])(\d+) (?:failed|errors?)\b")


def failed_tests(output: Any, summary: Any) -> list[str]:
    """The tests a failing run names as failed (pytest's ``FAILED path::test`` lines), only when
    the output shows all of them: as many as the summary counts. Empty otherwise."""
    names = list(dict.fromkeys(m.group(1) for ln in str(output or "").splitlines() if (m := _FAILED_TEST_RE.match(ln.strip()))))
    counted = sum(int(n) for n in _FAILED_COUNT_RE.findall(str(summary or "")))
    return names if names and len(names) == counted else []


def run_verdict(command: Any, exit_code: Any, output: Any) -> tuple[bool, str | None] | None:
    """``(passed, summary)`` for a command that ran tests; None when it is not a test run
    or its result was not captured.

    The runner's exit code decides when it is the command's (and a runner's
    summary line that reports failures still fails the run). When something
    after the runner replaces its exit code, the summary decides; with no summary
    there is no verdict. A command whose runners run no test (``pytest
    --version``, ``pytest --collect-only``) is not a test run.
    """
    runs = executing_invocations(command)
    if not runs:
        return None
    summary = run_summary(output, len({run.key for run in runs}))
    if all(run.decisive for run in runs):
        return (exit_code == 0 and not summary_shows_failure(summary)), summary
    if summary is None:
        return None
    return (not _reads_failed(summary.strip(" =\t"))), summary


# ---------------------------------------------------------------------------
# The current result per test target
# ---------------------------------------------------------------------------


def _covered(run: Invocation, green: list[Invocation], failed: list[str] | None = None) -> bool:
    """True when later passing runs tested everything ``run`` did: one of them covers it, or
    each of its targets is under a target of some later passing, unfiltered run. For a failing
    run whose failed tests are known (``failed``), those are what has to be seen green."""
    same = [g for g in green if g.runner == run.runner and g.cwd == run.cwd]
    if any(g.covers(run) for g in same):
        return True
    targets = failed or _targets(run.runner, run.args)[0]
    scopes = [scope for g in same for scope in ([] if _targets(g.runner, g.args)[1] else _targets(g.runner, g.args)[0])]
    return bool(targets) and all(any(_under(target, scope) for scope in scopes) for target in targets)


def resolve_runs(tests: Any, *, collected: bool = False) -> tuple[list[dict[str, Any]], set[str]]:
    """``(current, test_commands)`` from recorded runs ``[{cmd, passed, summary}]``, oldest first.

    ``current`` holds each run that still stands, in order, with ``passed``
    corrected: a run whose summary is a runner's line that reports failures did
    not pass, whatever verdict was sent. A run is replaced by a later run of
    the same command, by a later run of the same tests, and by later *passing*
    runs that cover them (the whole suite, their directory, or between them
    every file it ran; for a failing run that carries ``failed_tests``, every
    test that failed), so a red run that was fixed and re-run does not stay
    failing. A passing run stands in for another command's run only when its
    summary says tests ran (:func:`summary_shows_tests_ran`), and never when
    its arguments run no test (``pytest --version``, ``--collect-only``): a
    command that tested nothing does not clear a failure.

    An entry may carry ``where``, the directory its command ran in (the client
    knows it; it is not sent): runs in different directories never replace
    each other.

    ``collected`` marks runs a client took from a transcript by itself (not
    typed by an agent): there, a "passed" entry whose command runs no test is
    not a test, and a "passed" with no summary whose exit code was not the
    runner's is unknown. Both are left out. A failing entry is kept unless its
    command is known not to run tests (:func:`mentions_runner_only`).
    ``test_commands`` is every entry that is a real test run (current or replaced).
    """
    runs: list[tuple[dict[str, Any], tuple[Invocation, ...]]] = []
    for item in tests or []:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("cmd") or "")
        # A clipped command ("…") may have lost its runner, a target or a filter: it is kept as
        # recorded and only an identical command replaces it.
        clipped = cmd.endswith("…")
        found = () if clipped else tuple(executing_invocations(cmd))
        where = item.get("where")
        if found and isinstance(where, str) and where:
            found = tuple(replace(run, cwd=(_join_dir(where, run.cwd),)) for run in found)
        passed = item.get("passed")
        summary = item.get("summary")
        if collected and not clipped:
            # A failing entry is dropped only when its command is known not to run tests (an
            # older client listed ``pip install pytest``); one this module cannot read is kept.
            if not found and (passed is not False or mentions_runner_only(cmd)):
                continue
            if passed is True and not summary and not all(run.decisive for run in found):
                continue
        if passed is True and summary_shows_failure(summary):
            passed = False
        runs.append(({**item, "passed": passed}, found))

    def stands_in(other: dict[str, Any]) -> bool:
        """A later run whose result may replace an earlier, different command's: not a bare "passed"."""
        return other.get("passed") is not True or summary_shows_tests_ran(other.get("summary"))

    current: list[dict[str, Any]] = []
    for i, (item, found) in enumerate(runs):
        cmd = str(item.get("cmd") or "")
        later = runs[i + 1 :]
        replaced = any(str(other.get("cmd") or "") == cmd and other.get("where") == item.get("where") for other, _ in later)
        if not replaced and found:
            keys = [run.key for run in found]
            green = [
                run
                for other, other_found in later
                if other.get("passed") is True and summary_shows_tests_ran(other.get("summary"))
                for run in other_found
            ]
            failed = [str(t) for t in item.get("failed_tests") or []] if item.get("passed") is False else None
            replaced = any(
                other_found and [run.key for run in other_found] == keys and stands_in(other) for other, other_found in later
            ) or all(_covered(run, green, failed) for run in found)
        if not replaced:
            current.append(item)
    return current, {str(item.get("cmd") or "") for item, _ in runs}
