"""One reading of the facts an agent declares, whichever vocabulary it used.

Two tools take the same evidence in two shapes. ``close_session`` (the relay
close, ``/api/v1/session/close``) wants ``tests [{cmd, passed: bool,
summary}]``, ``commits [{sha, subject}]`` and ``unpushed_commits`` as a count.
``crew_checkpoint`` / ``crew_report`` want ``tests [{command, passed: int,
failed: int}]`` and ``commits [sha]``. Agents mix them, and a handoff must not
fail (or record a failing run as passing) because of the spelling.

:func:`normalize_close_facts` rewrites a facts object into the close shape and
:func:`normalize_crew_evidence` rewrites commits and tests into the crew shape.
Both are pure and total: they never raise, they never invent a verdict (a test
entry with no pass/fail goes to the notes instead of being counted), and they
say what they rewrote so nothing changes silently. Both are idempotent.

The module imports nothing from the server, so the MCP client uses it before a
request is sent (which also covers servers that predate it) and the API uses it
before validation (which covers clients that predate it).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

# The same rule as ``remembra.api.v1.relay.valid_branch_name`` (git check-ref-format --branch),
# kept in step by tests/test_fs_declared_facts.py: the API module cannot be imported by a client.
_BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f-\x9f~^:?*\[\\]|\.\.|@\{|//|/\.")
_SHA_RE = re.compile(r"[0-9a-fA-F]{4,64}")
_INT_RE = re.compile(r"[0-9]{1,9}")

MAX_CHANGES = 20
CREW_COMMAND_MAX = 256

_TRUE_WORDS = frozenset({"true", "t", "yes", "y", "on", "pass", "passed", "passing", "ok", "green", "success"})
_FALSE_WORDS = frozenset({"false", "f", "no", "n", "off", "fail", "failed", "failing", "red", "error", "failure"})
# What an agent writes to say "nothing here" where a list of items is expected.
_EMPTY_WORDS = frozenset({"", "none", "n/a", "na", "nil", "null", "nothing", "-"})

_STRING_LISTS = ("files_changed", "uncommitted_files", "errors", "todos_open")
_LISTS = ("tests", "commits", "commands", *_STRING_LISTS, "incomplete")
_STRINGS = ("head_commit", "upstream", "diff_stat", "notes", "next_step")


def valid_branch_name(value: str) -> bool:
    """Whether ``value`` is a branch name git would accept."""
    return not (
        value.startswith(("-", "/", "."))
        or value.endswith(("/", ".", ".lock"))
        or value == "@"
        or _BRANCH_FORBIDDEN_RE.search(value)
    )


def _count(value: Any) -> int | None:
    """A non-negative count from an int, an integral float or a digit string; None for anything else (bools too)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value == int(value) else None
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _text(value: Any) -> str | None:
    """A scalar as stripped text; None for empty text, containers and None."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _first_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = _text(item.get(key))
        if text:
            return text
    return None


def _as_list(value: Any) -> list[Any]:
    """A list from a list, a tuple, or one item given bare (a string or an object)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Changes:
    """What was rewritten, bounded (the list is shown to the agent)."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.more = 0

    def add(self, text: str) -> None:
        if len(self.items) < MAX_CHANGES:
            self.items.append(text)
        else:
            self.more += 1

    def result(self) -> list[str]:
        return [*self.items, f"(+{self.more} more)"] if self.more else list(self.items)


def _verdict(item: Mapping[str, Any]) -> tuple[bool | None, int | None, int | None]:
    """``(passed, passed_count, failed_count)`` of one test entry; ``passed`` None when it does not say.

    A failure count above zero or a non-zero exit code always means failed, whatever ``passed`` says:
    ``{passed: 1, failed: 2}`` is one test passing and two failing, not a passing run.
    """
    raw = item.get("passed")
    failed = _count(item.get("failed"))
    exit_code = item.get("exit_code")
    exit_known = isinstance(exit_code, int) and not isinstance(exit_code, bool)
    passed: bool | None = None
    count: int | None = None
    if isinstance(raw, bool):
        passed = raw
    elif isinstance(raw, str) and raw.strip().lower() in _TRUE_WORDS | _FALSE_WORDS:
        passed = raw.strip().lower() in _TRUE_WORDS
    else:
        count = _count(raw)
        if count is not None:
            passed = count > 0
        elif failed is not None:
            passed = failed == 0
        elif exit_known:
            passed = exit_code == 0
        else:
            word = (_first_text(item, ("status", "result", "outcome")) or "").lower()
            if word in _TRUE_WORDS | _FALSE_WORDS:
                passed = word in _TRUE_WORDS
    if passed is not None and ((failed is not None and failed > 0) or (exit_known and exit_code != 0)):
        passed = False
    return passed, count, failed


def _command_of(item: Mapping[str, Any]) -> str | None:
    return _first_text(item, ("cmd", "command", "fingerprint", "name"))


def _close_tests(value: Any, changes: _Changes, undecided: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(_as_list(value)):
        where = f"tests[{idx}]"
        if not isinstance(item, Mapping):
            text = _text(item)
            if text:
                undecided.append(text)
                changes.add(f"{where}: text without a pass/fail result, kept in notes")
            else:
                changes.add(f"{where}: dropped (not a test run)")
            continue
        passed, count, failed = _verdict(item)
        summary = _text(item.get("summary"))
        cmd = _command_of(item) or summary
        if cmd is None or passed is None:
            kept = _short({k: v for k, v in item.items() if v is not None}, 200)
            undecided.append(kept)
            changes.add(f"{where}: no command or no pass/fail result, kept in notes")
            continue
        if summary is None and (count is not None or failed is not None):
            parts = [f"{count} passed"] if count is not None else []
            summary = ", ".join([*parts, f"{failed or 0} failed"])
        entry: dict[str, Any] = {"cmd": cmd, "passed": passed}
        if summary is not None and summary != cmd:
            entry["summary"] = summary
        if dict(item) != entry:
            if "cmd" not in item:
                changes.add(f"{where}: command read from another key as cmd")
            if not isinstance(item.get("passed"), bool):
                changes.add(f"{where}.passed: {_short(item.get('passed'), 20)} read as {str(passed).lower()}")
            elif item.get("passed") is not passed:
                changes.add(f"{where}.passed: true with failures recorded, read as false")
        out.append(entry)
    return out


def _commit_of(item: Any) -> dict[str, Any] | None:
    """``{sha, subject?}`` from a commit object, a bare sha, or ``"<sha> <subject>"``; None when there is no sha."""
    if isinstance(item, Mapping):
        sha = _first_text(item, ("sha", "hash", "commit", "id"))
        if sha is None:
            return None
        entry: dict[str, Any] = {"sha": sha[:64]}
        subject = _first_text(item, ("subject", "message", "title", "summary"))
        if subject:
            entry["subject"] = subject
        return entry
    text = _text(item) if isinstance(item, str) else None
    if not text:
        return None
    head, _, rest = text.partition(" ")
    if not _SHA_RE.fullmatch(head):
        return None
    entry = {"sha": head}
    if rest.strip():
        entry["subject"] = rest.strip().lstrip(":-– ").strip() or rest.strip()
    return entry


def _close_commits(value: Any, changes: _Changes, undecided: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(_as_list(value)):
        entry = _commit_of(item)
        if entry is None:
            if item not in (None, "", {}):
                undecided.append(_short(item, 200))
                changes.add(f"commits[{idx}]: no commit id, kept in notes")
            continue
        if not isinstance(item, Mapping):
            changes.add(f"commits[{idx}]: text read as {{sha{', subject' if 'subject' in entry else ''}}}")
        elif "sha" not in item:
            changes.add(f"commits[{idx}]: commit id read from another key as sha")
        out.append(entry)
    return out


def _close_commands(value: Any, changes: _Changes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(_as_list(value)):
        if isinstance(item, Mapping):
            cmd = _first_text(item, ("cmd", "command"))
            code = item.get("exit_code")
        else:
            cmd, code = _text(item), None
        if cmd is None:
            changes.add(f"commands[{idx}]: dropped (no command)")
            continue
        entry: dict[str, Any] = {"cmd": cmd}
        if isinstance(code, int) and not isinstance(code, bool):
            entry["exit_code"] = code
        elif isinstance(code, str) and re.fullmatch(r"-?[0-9]{1,6}", code.strip()):
            entry["exit_code"] = int(code.strip())
        if not isinstance(item, Mapping) or dict(item) != entry:
            if not isinstance(item, Mapping) or "cmd" not in item:
                changes.add(f"commands[{idx}]: read as {{cmd}}")
        out.append(entry)
    return out


def _string_list(name: str, value: Any, changes: _Changes) -> list[str]:
    items = _as_list(value)
    out: list[str] = []
    for item in items:
        # A string is kept exactly as written (only a placeholder for "nothing" is dropped).
        text = item if isinstance(item, str) else _text(item)
        if text is None and isinstance(item, (Mapping, list, tuple)) and item:
            text = _short(item, 300)
        if text is not None and text.strip().lower() not in _EMPTY_WORDS:
            out.append(text)
    if not isinstance(value, list):
        changes.add(f"{name}: {type(value).__name__} read as a list of {len(out)} item(s)")
    elif out != value:
        changes.add(f"{name}: empty or non-text items rewritten ({len(value)} given, {len(out)} kept)")
    return out


def _string(name: str, value: Any, changes: _Changes) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [
            p for p in (_text(v) or (_short(v, 300) if isinstance(v, (Mapping, list, tuple)) and v else None) for v in value) if p
        ]
        text: str | None = "; ".join(parts) or None
    elif isinstance(value, Mapping):
        text = json.dumps(value, default=str, sort_keys=True) if value else None
    else:
        text = _text(value)
    changes.add(f"{name}: {type(value).__name__} read as text" if text else f"{name}: dropped (not text)")
    return text


def _unpushed(value: Any, changes: _Changes) -> int | None:
    if isinstance(value, bool):
        changes.add("unpushed_commits: true/false is not a count, push state left unrecorded")
        return None
    count = _count(value)
    if count is not None:
        if not isinstance(value, int):
            changes.add(f"unpushed_commits: {_short(value, 20)} read as {count}")
        return count
    if isinstance(value, (list, tuple)):
        changes.add(f"unpushed_commits: a list of {len(value)} read as the count {len(value)}")
        return len(value)
    changes.add(f"unpushed_commits: {_short(value, 30)} is not a count, push state left unrecorded")
    return None


def normalize_close_facts(facts: Any, *, lenient_branch: bool = False) -> tuple[dict[str, Any], list[str]]:
    """``facts`` in the shape ``/api/v1/session/close`` validates, and what was rewritten.

    Accepts the crew vocabulary and the common near-misses: ``tests`` entries with ``command`` and
    pass/fail counts, commits as bare shas, ``unpushed_commits`` as a list of shas, one string where a
    list is expected, a list where ``notes`` is expected. Entries that cannot be read as what the key
    means (a test with no result, a commit with no id) are appended to ``notes`` as the agent wrote
    them, so they still reach the next agent.

    ``lenient_branch`` (the MCP tool) moves a ``branch`` git would refuse into the notes. The API
    keeps refusing it with 422 (the name is printed into ``git switch`` lines), so it is not the default.

    Keys this function does not know are passed through untouched. Anything that is not a mapping
    gives ``({}, [...])``.
    """
    changes = _Changes()
    if facts is None:
        return {}, []
    if not isinstance(facts, Mapping):
        return {}, [f"facts: expected an object, got {type(facts).__name__}; ignored"]
    out: dict[str, Any] = {str(k): v for k, v in facts.items()}
    for name in _LISTS:
        if name in out and out[name] is None:  # an explicit null is "not given", which the API has no value for
            del out[name]
    kept: list[tuple[str, list[str]]] = []

    if out.get("tests") is not None:
        undecided: list[str] = []
        out["tests"] = _close_tests(out["tests"], changes, undecided)
        if undecided:
            kept.append(("Tests as declared (no pass/fail result recorded)", undecided))
    if out.get("commits") is not None:
        undecided = []
        out["commits"] = _close_commits(out["commits"], changes, undecided)
        if undecided:
            kept.append(("Commits as declared (no commit id)", undecided))
    if out.get("commands") is not None:
        out["commands"] = _close_commands(out["commands"], changes)
    for name in _STRING_LISTS:
        if out.get(name) is not None:
            out[name] = _string_list(name, out[name], changes)
    for name in _STRINGS:
        if name in out and out[name] is not None and not isinstance(out[name], str):
            out[name] = _string(name, out[name], changes)
    if "unpushed_commits" in out and out["unpushed_commits"] is not None:
        value = out["unpushed_commits"]
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            out["unpushed_commits"] = _unpushed(value, changes)
    if "branch" in out and out["branch"] is not None:
        branch = out["branch"]
        if not isinstance(branch, str):
            text = _text(branch)
            out["branch"] = None
            if text:
                kept.append(("Branch as declared (not a branch name)", [text]))
            changes.add("branch: dropped (not text)")
        elif not branch.strip():
            out["branch"] = None
        elif lenient_branch and not valid_branch_name(branch):
            out["branch"] = None
            kept.append(("Branch as declared (not a valid git branch name)", [_short(branch, 200)]))
            changes.add("branch: not a valid git branch name, kept in notes")

    if kept:
        lines = [f"{label}: {'; '.join(items)}" for label, items in kept]
        notes = out.get("notes")
        out["notes"] = "\n".join([*([notes] if isinstance(notes, str) and notes.strip() else []), *lines])
    return out, changes.result()


def normalize_crew_evidence(commits: Any, tests: Any) -> tuple[list[str] | None, list[dict[str, Any]] | None, list[str]]:
    """``(commits, tests, notes)`` in the shape ``crew_checkpoint`` / ``crew_report`` validate.

    ``commits`` become sha strings (from ``{sha, subject}`` objects too) and ``tests`` become
    ``{command (at most 256 chars), passed: int, failed: int}`` (from ``cmd``, a boolean ``passed``,
    a missing ``failed``). ``notes`` carries what the closed shape has no place for: a test's
    ``summary`` text, and entries with no result or no commit id, as the agent wrote them. None in
    gives None out, so "not given" stays "not given".
    """
    notes: list[str] = []
    out_commits: list[str] | None = None
    if commits is not None:
        out_commits = []
        for item in _as_list(commits):
            entry = _commit_of(item)
            if entry is not None:
                out_commits.append(entry["sha"])
            elif item not in (None, "", {}):
                notes.append(f"commit as declared (no commit id): {_short(item, 160)}")
    out_tests: list[dict[str, Any]] | None = None
    if tests is not None:
        out_tests = []
        for item in _as_list(tests):
            if not isinstance(item, Mapping):
                text = _text(item)
                if text:
                    notes.append(f"test as declared (no result): {_short(text, 160)}")
                continue
            passed, count, failed = _verdict(item)
            summary = _text(item.get("summary"))
            command = _command_of(item) or summary
            if command is None or passed is None:
                notes.append(f"test as declared (no command or no result): {_short(dict(item), 160)}")
                continue
            if count is None:
                count = 1 if passed else 0
            if failed is None:
                failed = 0 if passed else 1
            out_tests.append({"command": command[:CREW_COMMAND_MAX], "passed": count, "failed": failed})
            if summary and summary != command:
                notes.append(f"test {_short(command, 60)}: {_short(summary, 120)}")
    return out_commits, out_tests, notes
