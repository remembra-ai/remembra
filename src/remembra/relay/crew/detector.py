"""Transcript-tail limit detector for every adapter (spec D14, §8.1 "Limit detection for live processes").

Codex and Gemini stay open after running out of credits, so a live pid says nothing. crewd tails
each session's transcript and applies one rule:

    an assistant (or error) message matching the adapter's limit patterns,
    followed by 5 minutes without tool activity, while the process is alive
    → ``quota_blocked`` with source ``detected``.

It runs for Claude Code too until StopFailure is V-live for the subscription usage limit (S0
result: the same prefixes the Claude client uses are the Claude patterns here). Patterns for
the other adapters are research-grade (labelled U in §8.3) and live in one table.

The tail is incremental (a byte offset per transcript) and bounded (at most 256 KB per pass).
Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

QUIET_AFTER_LIMIT_S: Final = 300.0
MAX_READ_BYTES: Final = 256 * 1024

# (S0 / D14) Claude Code's own usage-limit wording, and the credit-balance message.
CLAUDE_PATTERNS: Final = (
    r"^You've hit your",
    r"^You're out of usage credits",
    r"^Your org is out of usage",
    r"^You've used \d+%",
    r"Credit balance is too low",
)
LIMIT_PATTERNS: Final[Mapping[str, tuple[str, ...]]] = {
    "claude-code": CLAUDE_PATTERNS,
    "codex": (
        r"You've hit your usage limit",
        r"Rate limit reached",
        r"insufficient_quota",
        r"exceeded your current quota",
        r"usage limit (?:has been )?reached",
    ),
    "gemini": (r"Quota exceeded", r"RESOURCE_EXHAUSTED", r"You have exhausted your", r"daily quota", r"usage limit"),
    "cursor": (r"You've hit your usage limit", r"usage limit", r"out of (?:fast )?requests"),
    "qwen": (r"Quota exceeded", r"insufficient_quota", r"Free allocated quota exceeded", r"usage limit"),
    "kimi": (r"exceeded_current_quota", r"insufficient balance", r"rate_limit_reached", r"usage limit"),
}
_TOOL_MARKERS: Final = ('"tool_use"', '"function_call"', '"tool_call"', '"functionCall"', '"exec_command', '"tool_result"')


def patterns_for(adapter: str) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in LIMIT_PATTERNS.get(adapter, LIMIT_PATTERNS["codex"])]


@dataclass
class TailState:
    """Per-transcript detector state (kept by crewd per session)."""

    offset: int = 0
    inode: int | None = None
    limit_at: float | None = None
    limit_text: str | None = None
    last_tool_at: float | None = None
    reported: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "inode": self.inode,
            "limit_at": self.limit_at,
            "limit_text": self.limit_text,
            "last_tool_at": self.last_tool_at,
            "reported": self.reported,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> TailState:
        d = dict(data or {})
        return cls(
            offset=int(d.get("offset") or 0),
            inode=d.get("inode"),
            limit_at=d.get("limit_at"),
            limit_text=d.get("limit_text"),
            last_tool_at=d.get("last_tool_at"),
            reported=bool(d.get("reported")),
        )


def _ts(value: Any, fallback: float) -> float:
    if isinstance(value, int | float):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return fallback
    return fallback


def _texts(obj: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for item in obj[:50]:
            _texts(item, out, depth + 1)
    elif isinstance(obj, dict):
        kind = obj.get("type")
        if kind in ("tool_use", "tool_result", "function_call", "function_call_output"):
            return
        for key in ("text", "message", "content", "error", "msg"):
            if key in obj:
                _texts(obj[key], out, depth + 1)


@dataclass
class LineInfo:
    at: float
    assistant_text: str | None = None
    tool: bool = False


def classify_line(line: str, fallback_ts: float) -> LineInfo | None:
    """What one transcript line says: assistant/error text, tool activity, both, or nothing."""
    raw = line.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return LineInfo(fallback_ts, assistant_text=raw[:2000])
    if not isinstance(obj, dict):
        return None
    at = _ts(obj.get("timestamp") or obj.get("ts") or obj.get("time"), fallback_ts)
    info = LineInfo(at, tool=any(m in raw for m in _TOOL_MARKERS))
    role = obj.get("type") or obj.get("role")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None
    message = obj.get("message") if isinstance(obj.get("message"), dict) else None
    texts: list[str] = []
    if role == "assistant" and message is not None:  # Claude Code JSONL
        _texts(message.get("content"), texts)
    elif role == "event_msg" and payload is not None:  # Codex rollout
        if payload.get("type") in ("agent_message", "error", "stream_error", "task_complete"):
            _texts(payload.get("message") or payload.get("last_agent_message"), texts)
        if str(payload.get("type") or "").startswith("exec_command") or payload.get("type") in (
            "patch_apply_begin",
            "mcp_tool_call_begin",
        ):
            info.tool = True
    elif role == "response_item" and payload is not None:
        if payload.get("type") in ("function_call", "local_shell_call", "custom_tool_call"):
            info.tool = True
        elif payload.get("role") == "assistant":
            _texts(payload.get("content"), texts)
    elif role in ("assistant", "model", "gemini", "error") or obj.get("role") in ("assistant", "model"):
        _texts(obj.get("content") or obj.get("parts") or obj.get("text") or obj.get("message") or obj.get("error"), texts)
    elif "error" in obj:
        _texts(obj.get("error"), texts)
    if texts:
        info.assistant_text = "\n".join(t for t in texts if t)[:4000]
    return info if (info.assistant_text or info.tool) else None


def scan(path: Path, adapter: str, state: TailState, *, now: float) -> TailState:
    """Read what was appended since the last pass and update the state (limit message, tool activity)."""
    try:
        st = path.stat()
    except OSError:
        return state
    if state.inode is not None and st.st_ino != state.inode:
        state = TailState()
    state.inode = st.st_ino
    if st.st_size < state.offset:
        state.offset = 0  # truncated or rotated
    start = max(state.offset, st.st_size - MAX_READ_BYTES)
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read(st.st_size - start)
    except OSError:
        return state
    skip = 0
    if start > state.offset:
        first = chunk.find(b"\n")
        if first < 0:
            return state
        skip = first + 1  # skipped ahead: drop the partial first line
    end = chunk.rfind(b"\n")
    if end < skip:
        return state  # no complete line yet
    complete = chunk[skip : end + 1]
    state.offset = start + end + 1
    rx = patterns_for(adapter)
    fallback = st.st_mtime
    for line in complete.decode("utf-8", "replace").splitlines():
        info = classify_line(line, fallback)
        if info is None:
            continue
        if info.tool:
            state.last_tool_at = max(state.last_tool_at or 0.0, info.at)
        if info.assistant_text and any(r.search(info.assistant_text) for r in rx):
            state.limit_at = info.at
            state.limit_text = info.assistant_text.strip().splitlines()[0][:200]
            state.reported = False
    return state


@dataclass
class Detection:
    detected: bool
    reason: str
    limit_text: str | None = None
    quiet_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def evaluate(
    state: TailState,
    *,
    now: float,
    process_alive: bool,
    last_hook_activity_at: float | None = None,
    quiet_s: float = QUIET_AFTER_LIMIT_S,
) -> Detection:
    """Apply the D14 rule to one session's state."""
    if state.limit_at is None:
        return Detection(False, "no_limit_message")
    if state.reported:
        return Detection(False, "already_reported", state.limit_text)
    if not process_alive:
        return Detection(False, "process_dead", state.limit_text)  # a dead process is the orphan path, not quota
    last_tool = max(state.last_tool_at or 0.0, last_hook_activity_at or 0.0)
    if last_tool > state.limit_at:
        return Detection(False, "activity_after_limit", state.limit_text)
    quiet = now - state.limit_at
    if quiet < quiet_s:
        return Detection(False, "waiting", state.limit_text, quiet)
    return Detection(True, "limit_message_then_quiet", state.limit_text, quiet)


def default_transcript_dirs(home: Path, adapter: str) -> list[Path]:
    """Where an adapter keeps transcripts when the hook payload does not say (research-grade)."""
    return {
        "codex": [home / ".codex" / "sessions"],
        "gemini": [home / ".gemini" / "tmp"],
        "qwen": [home / ".qwen" / "tmp"],
        "claude-code": [home / ".claude" / "projects"],
    }.get(adapter, [])


def newest_file(dirs: Iterable[Path], *, suffixes: tuple[str, ...] = (".jsonl", ".json"), since: float = 0.0) -> Path | None:
    best: tuple[float, Path] | None = None
    for d in dirs:
        if not d.is_dir():
            continue
        for root, _dirs, files in os.walk(d):
            for name in files:
                if not name.endswith(suffixes):
                    continue
                p = Path(root) / name
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m >= since and (best is None or m > best[0]):
                    best = (m, p)
    return best[1] if best else None
