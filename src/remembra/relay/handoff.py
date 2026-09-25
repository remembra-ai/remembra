"""Deterministic handoff construction, summary grounding and brief rendering.

A close-out turns *facts* (git state, commands, tests, errors, open todos —
gathered mechanically by the client) into ONE handoff with fixed sections::

    [HANDOFF] <agent> · <when> · <branch>@<sha> · session <id> · ended: <reason>
    Done: ...
    Not done / open: ...
    Failing / errors: ...
    Next step: ...

The optional free-text ``summary`` an agent writes is never trusted as fact:
:func:`check_summary_grounding` compares its checkable claims (commit ids,
file paths, "tests pass", "no errors", "pushed") with the facts and the
summary is rendered with that verdict attached.

Pure functions; everything that ends up stored first goes through
:func:`redact` (``redact_secrets`` on every string).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from remembra.security.secrets import redact_secrets

HANDOFF_FORMAT_VERSION = 1
MAX_BRIEF_CHARS = 6000  # ~1500 tokens
_SHA_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}|[\w-]+\.(?:py|ts|tsx|js|jsx|go|rs|md|toml|json|yml|yaml|sql|sh|swift|kt|java|rb|php|css|html))(?![\w/])"
)
_TESTS_PASS_RE = re.compile(
    r"\b(all\s+)?tests?\s+(are\s+|now\s+)?(pass(es|ing|ed)?|green)\b|\bgreen\s+(test\s+)?suite\b|\bsuite\s+(is\s+)?green\b",
    re.IGNORECASE,
)
_NO_ERRORS_RE = re.compile(r"\bno\s+(remaining\s+)?(errors?|failures?)\b|\bnothing\s+(is\s+)?failing\b", re.IGNORECASE)
_PUSHED_RE = re.compile(r"\b(pushed|deployed|merged|shipped|live\s+on)\b", re.IGNORECASE)
_NEGATED_PUSH_RE = re.compile(r"\b(not|n't|never|un)\s*(yet\s+)?(pushed|deployed|merged|shipped)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(value: Any, counts: dict[str, int] | None = None, extra: Callable[[str], str] | None = None) -> Any:
    """Return ``value`` with ``redact_secrets`` (then ``extra``, e.g. a PII
    scrubber) applied to every string inside it."""
    if counts is None:
        counts = {}
    if isinstance(value, str):
        result = redact_secrets(value)
        for kind, n in result.counts.items():
            counts[kind] = counts.get(kind, 0) + n
        text = result.text
        if extra is not None and text:
            scrubbed = extra(text)
            if scrubbed != text:
                counts["pii"] = counts.get("pii", 0) + 1
            text = scrubbed
        return text
    if isinstance(value, dict):
        return {redact(k, counts, extra) if isinstance(k, str) else k: redact(v, counts, extra) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, counts, extra) for v in value]
    return value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clip(text: Any, limit: int) -> str:
    """Single-line, whitespace-collapsed, clipped to ``limit`` chars."""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def short_sha(sha: str | None) -> str:
    return (sha or "")[:7]


def _list_more(items: list[str], shown: int) -> str:
    head = ", ".join(items[:shown])
    return head + (f" (+{len(items) - shown} more)" if len(items) > shown else "")


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def relative_time(value: Any, now: datetime | None = None) -> str:
    """``"just now"``, ``"5m ago"``, ``"3h ago"``, ``"2d ago"``; ``"unknown time"`` if unparseable."""
    dt = _parse_ts(value)
    if dt is None:
        return "unknown time"
    now = now or datetime.now(UTC)
    seconds = int((now - dt).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _where(branch: str | None, head: str | None) -> str:
    if branch and head:
        return f"{branch}@{short_sha(head)}"
    if branch:
        return branch
    if head:
        return f"@{short_sha(head)}"
    return "(no git info)"


# Commands whose non-zero exit is normal control flow (no match, false test).
_PROBE_COMMANDS = {
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "find",
    "test",
    "[",
    "[[",
    "diff",
    "cmp",
    "ls",
    "which",
    "type",
    "command",
    "pgrep",
    "stat",
    "head",
    "tail",
    "cat",
    "file",
    "readlink",
    "timeout",
}


def _first_program(command: str) -> str:
    for token in re.split(r"\s+", command.strip()):
        if not token or "=" in token.split("/")[0] and not token.startswith(("./", "/")):
            continue  # leading VAR=value assignments
        if token in ("sudo", "env", "time", "nice", "exec"):
            continue
        return token.rsplit("/", 1)[-1]
    return ""


def is_probe_command(command: str) -> bool:
    """True for commands whose non-zero exit is normal control flow (grep no-match, test false)."""
    segments = [s for s in re.split(r"\|\||&&|;|\|", command) if s.strip()]
    last = segments[-1] if segments else command
    return _first_program(last) in _PROBE_COMMANDS or _first_program(command) in _PROBE_COMMANDS


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def check_summary_grounding(summary: str | None, facts: dict[str, Any]) -> dict[str, Any]:
    """Check an agent-written summary's verifiable claims against the facts.

    Returns ``{"status": "none" | "consistent" | "contradicted", "issues": [...],
    "checked": [...]}``. "consistent" means no checkable claim was contradicted —
    the narrative itself stays unverified.
    """
    text = (summary or "").strip()
    if not text:
        return {"status": "none", "issues": [], "checked": []}

    issues: list[str] = []
    checked: list[str] = []
    commits = facts.get("commits") or []
    known_shas = [str(c.get("sha") or "").lower() for c in commits]
    head = str(facts.get("head_commit") or "").lower()
    if head:
        known_shas.append(head)

    for sha in dict.fromkeys(_SHA_TOKEN_RE.findall(text)):
        if not any(ch in "abcdef" for ch in sha) and len(sha) < 12:
            continue  # all-digit tokens are numbers, not commit ids
        checked.append(f"commit {sha}")
        if not any(k.startswith(sha) for k in known_shas if k):
            issues.append(f"mentions commit {sha}, which is not among this session's commits")

    changed = [str(p) for p in (facts.get("files_changed") or []) + (facts.get("uncommitted_files") or [])]
    for path in dict.fromkeys(m for m in _PATH_TOKEN_RE.findall(text)):
        checked.append(f"file {path}")
        if not any(c == path or c.endswith("/" + path) or path.endswith("/" + c) for c in changed):
            issues.append(f"mentions {path}, which is not in the changed files")

    tests = facts.get("tests") or []
    failing = [t for t in tests if t.get("passed") is False]
    if _TESTS_PASS_RE.search(text):
        checked.append("tests pass")
        if failing:
            issues.append(f"claims tests pass, but {len(failing)} test run(s) failed: {clip(failing[0].get('cmd'), 80)}")
        elif not tests:
            issues.append("claims tests pass, but no test run was recorded")

    if _NO_ERRORS_RE.search(text):
        checked.append("no errors")
        if failing or facts.get("errors"):
            issues.append("claims no errors/failures, but errors or failing tests were recorded")

    if _PUSHED_RE.search(text) and not _NEGATED_PUSH_RE.search(text):
        checked.append("pushed/deployed")
        unpushed = facts.get("unpushed_commits")
        if isinstance(unpushed, int) and unpushed > 0:
            issues.append(
                f"claims pushed/deployed, but {unpushed} commit(s) are not pushed to {facts.get('upstream') or 'upstream'}"
            )
        elif unpushed is None:
            issues.append("claims pushed/deployed; push state was not recorded (unverifiable)")

    contradicted = [i for i in issues if "(unverifiable)" not in i]
    status = "contradicted" if contradicted else "consistent"
    return {"status": status, "issues": issues, "checked": checked}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def latest_tests(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last result per distinct command, in first-seen order."""
    by_cmd: dict[str, dict[str, Any]] = {}
    for test in tests:
        by_cmd[str(test.get("cmd") or "")] = test
    return list(by_cmd.values())


def build_sections(facts: dict[str, Any], next_step_hint: str | None = None) -> dict[str, Any]:
    """Deterministic Done / Not done / Failing / Next sections from facts."""
    commits = facts.get("commits") or []
    tests = latest_tests(facts.get("tests") or [])
    files = [str(f) for f in facts.get("files_changed") or []]
    uncommitted = [str(f) for f in facts.get("uncommitted_files") or []]
    todos = [str(t) for t in facts.get("todos_open") or [] if str(t).strip()]
    errors = [str(e) for e in facts.get("errors") or [] if str(e).strip()]
    commands = facts.get("commands") or []
    unpushed = facts.get("unpushed_commits")

    done: list[str] = [f"{short_sha(c.get('sha'))} {clip(c.get('subject'), 100)}".strip() for c in commits[:10]]
    if len(commits) > 10:
        done.append(f"(+{len(commits) - 10} more commits)")
    for test in tests:
        if test.get("passed") is True:
            summary = f" ({clip(test.get('summary'), 80)})" if test.get("summary") else ""
            done.append(f"tests passing: {clip(test.get('cmd'), 100)}{summary}")
    if files:
        done.append(f"changed {len(files)} file(s): {_list_more([clip(f, 80) for f in files], 8)}")
    if facts.get("diff_stat"):
        done.append(f"diff: {clip(facts.get('diff_stat'), 120)}")

    not_done: list[str] = [f"TODO: {clip(t, 160)}" for t in todos[:10]]
    if len(todos) > 10:
        not_done.append(f"(+{len(todos) - 10} more open todos)")
    if uncommitted:
        not_done.append(f"uncommitted changes in {len(uncommitted)} file(s): {_list_more([clip(f, 80) for f in uncommitted], 6)}")
    if isinstance(unpushed, int) and unpushed > 0:
        not_done.append(f"{unpushed} commit(s) not pushed to {facts.get('upstream') or 'upstream'}")
    elif facts.get("no_upstream") and commits:
        not_done.append(f"branch {facts.get('branch') or '(detached)'} has no upstream: commits are not pushed")

    failing: list[str] = []
    for test in tests:
        if test.get("passed") is False:
            summary = f" ({clip(test.get('summary'), 100)})" if test.get("summary") else ""
            failing.append(f"FAILING: {clip(test.get('cmd'), 120)}{summary}")
    # Failed commands: last result per exact command (a later success clears
    # it), minus test runs, probes (grep no-match) and anything an error
    # entry already describes.
    test_cmds = {str(t.get("cmd") or "") for t in tests}
    last_exit: dict[str, Any] = {}
    for cmd in commands:
        last_exit[str(cmd.get("cmd") or "")] = cmd.get("exit_code")
    for text, code in last_exit.items():
        if not isinstance(code, int) or code == 0 or not text or text in test_cmds or is_probe_command(text):
            continue
        if any(clip(text, 100).rstrip("…") in e for e in errors):
            continue
        failing.append(f"`{clip(text, 100)}` exited {code}")
    failing.extend(f"error: {clip(e, 200)}" for e in errors)
    failing = list(dict.fromkeys(failing))[:12]

    agent_next = clip(next_step_hint, 240) if next_step_hint and next_step_hint.strip() else None
    first_failing_test = next((t for t in tests if t.get("passed") is False), None)
    if first_failing_test is not None:
        derived_next = f"fix the failing run: {clip(first_failing_test.get('cmd'), 120)}"
    elif failing:
        derived_next = f"resolve: {failing[0]}"
    elif todos:
        derived_next = clip(todos[0], 200)
    elif uncommitted:
        derived_next = f"review and commit {len(uncommitted)} uncommitted file(s)"
    elif isinstance(unpushed, int) and unpushed > 0:
        derived_next = f"push {unpushed} commit(s) to {facts.get('upstream') or 'upstream'}"
    else:
        derived_next = None
    next_step = agent_next or derived_next

    if commits:
        done_part = f"{len(commits)} commit(s), last: {clip(commits[-1].get('subject'), 70)}"
    elif files or uncommitted:
        done_part = f"{len(set(files) | set(uncommitted))} file(s) changed, nothing committed"
    else:
        done_part = "no commits or file changes"
    open_count = len(todos) + sum(1 for item in not_done if not item.startswith(("TODO: ", "(+")))
    tail = []
    if open_count:
        tail.append(f"{open_count} open")
    if failing:
        tail.append(f"{len(failing)} failing")
    headline = done_part + (f" [{', '.join(tail)}]" if tail else "")

    return {
        "done": done,
        "not_done": not_done,
        "failing": failing,
        "next": next_step,
        "next_source": "agent" if agent_next else ("derived" if derived_next else None),
        "headline": headline,
    }


def render_handoff(
    *,
    agent_id: str,
    session_id: str,
    project_id: str,
    closed_at: datetime,
    facts: dict[str, Any],
    sections: dict[str, Any],
    summary: str | None,
    grounding: dict[str, Any],
    end_reason: str | None,
) -> str:
    """The handoff memory's text: fixed header + sections, deterministic for the same input."""
    when = closed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    header = [f"[HANDOFF] {agent_id}", when, f"project {project_id}", _where(facts.get("branch"), facts.get("head_commit"))]
    header.append(f"session {clip(session_id, 60)}")
    if end_reason:
        header.append(f"ended: {clip(end_reason, 40)}")
    lines = [" · ".join(header)]

    def section(title: str, items: list[str], empty: str) -> None:
        lines.append(f"{title}:")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append(f"- {empty}")

    section("Done", sections["done"], "nothing recorded")
    section("Not done / open", sections["not_done"], "nothing open recorded")
    section("Failing / errors", sections["failing"], "none recorded")
    nxt = sections.get("next")
    if nxt:
        label = "Next step (agent)" if sections.get("next_source") == "agent" else "Next step"
        lines.append(f"{label}: {nxt}")
    else:
        lines.append("Next step: none recorded")
    notes = facts.get("notes")
    if notes and str(notes).strip():
        lines.append(f"Notes (agent): {clip(notes, 1500)}")
    if summary and summary.strip():
        verdict = grounding.get("status")
        if verdict == "contradicted":
            flag = "CONTRADICTED by facts: " + "; ".join(grounding.get("issues") or [])
        else:
            extra = list(grounding.get("issues") or [])
            flag = "unverified narrative; no checkable claim contradicted" + (f" ({'; '.join(extra)})" if extra else "")
        lines.append(f"Agent summary [{clip(flag, 400)}]: {clip(summary, 1500)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Brief rendering
# ---------------------------------------------------------------------------


def _relay_meta(handoff: dict[str, Any] | None) -> dict[str, Any] | None:
    if not handoff:
        return None
    meta = handoff.get("metadata") or {}
    relay = meta.get("relay") if isinstance(meta, dict) else None
    return relay if isinstance(relay, dict) else None


def handoff_headline(memory: dict[str, Any]) -> str:
    """One-line description of a handoff/checkpoint memory (relay or legacy)."""
    relay = _relay_meta(memory)
    if relay and relay.get("headline"):
        return clip(relay["headline"], 160)
    content = str(memory.get("content") or "")
    first = next((ln for ln in content.splitlines() if ln.strip()), "")
    return clip(first, 160)


def render_last_session(handoff: dict[str, Any] | None, now: datetime | None = None) -> str:
    """``Last session: <agent>, <relative time>, on <branch>@<sha>: done… / NOT done… / failing… / next…``."""
    if not handoff:
        return "Last session: none recorded for this project."
    relay = _relay_meta(handoff)
    agent = handoff.get("agent_id") or (relay or {}).get("agent_id") or "unknown agent"
    when = relative_time(handoff.get("created_at"), now)
    if not relay:
        return f"Last session: {agent}, {when} (free-form handoff): {clip(handoff.get('content'), 700)}"
    where = _where(relay.get("branch"), relay.get("head_commit"))
    parts = [
        "done: " + ("; ".join(clip(x, 90) for x in relay.get("done", [])[:4]) or "nothing recorded"),
        "NOT done: " + ("; ".join(clip(x, 90) for x in relay.get("not_done", [])[:4]) or "nothing open"),
        "failing: " + ("; ".join(clip(x, 90) for x in relay.get("failing", [])[:3]) or "none"),
        "next: " + (clip(relay.get("next"), 160) if relay.get("next") else "none recorded"),
    ]
    line = f"Last session: {agent}, {when}, on {where}: " + " / ".join(parts)
    grounding = relay.get("grounding") or {}
    if grounding.get("status") == "contradicted":
        line += " (agent summary contradicted by facts; trust the facts)"
    return line


def render_brief(brief: dict[str, Any], now: datetime | None = None, max_chars: int = MAX_BRIEF_CHARS) -> str:
    """Compact text brief: last session, inbox, status, linked projects, recent memories.

    Capped at ``max_chars`` (~1500 tokens); recent memories are dropped first.
    """
    now = now or datetime.now(UTC)
    head = [
        f"# Remembra brief · project {brief.get('project_id') or '(all)'} · you are {brief.get('agent_id') or '(no agent id)'}"
    ]
    head.append(render_last_session(brief.get("handoff"), now))

    body: list[str] = []
    inbox = brief.get("inbox")
    if inbox and inbox.get("available", True) and inbox.get("unread_count"):
        body.append(f"Inbox: {inbox['unread_count']} unread (get_inbox for bodies, ack_inbox when done)")
        for item in (inbox.get("items") or [])[:5]:
            body.append(
                f"- [{item.get('inbox_id')}] {item.get('from_agent')}, {relative_time(item.get('created_at'), now)}: "
                f"{clip(item.get('subject'), 80)} — {clip(item.get('body_preview'), 120)}"
            )
    status_items = brief.get("status_items") or []
    if status_items:
        body.append("Status:")
        body.extend(f"- {s.get('key')}: {clip(s.get('value'), 140)}" for s in status_items[:12])
    linked = brief.get("linked_projects") or []
    if linked:
        body.append("Linked projects:")
        for link in linked[:8]:
            latest = link.get("latest_handoff")
            if latest:
                who = latest.get("agent_id") or "unknown"
                info = f"{who}, {relative_time(latest.get('created_at'), now)}: {clip(latest.get('headline'), 120)}"
            else:
                info = "no handoff yet"
            body.append(f"- {link.get('project_id')} ({link.get('relation')}): {info}")
    warnings = brief.get("warnings") or []

    recent_lines: list[str] = []
    for mem in brief.get("recent") or []:
        who = f" [{mem.get('agent_id')}]" if mem.get("agent_id") else ""
        kind = f" ({mem.get('memory_type')})" if mem.get("memory_type") else ""
        recent_lines.append(f"- {relative_time(mem.get('created_at'), now)}{who}{kind}: {clip(mem.get('content'), 160)}")

    tail = [f"Note: {clip(w, 200)}" for w in warnings[:4]]
    tail.append("Before you finish: run `remembra-relay close` or call close_session so the next agent can pick up.")

    def assemble(recent: list[str]) -> str:
        parts = head + body
        if recent:
            parts = parts + ["Recent (newest first):"] + recent
        return "\n".join(parts + tail)

    text = assemble(recent_lines)
    while len(text) > max_chars and recent_lines:
        recent_lines.pop()
        text = assemble(recent_lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text
