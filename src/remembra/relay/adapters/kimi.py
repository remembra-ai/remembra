"""Kimi Code CLI (verified): ``[[hooks]]`` tables in ``~/.kimi-code/config.toml``.

Kimi Code (npm @moonshot-ai/kimi-code 2.x, binary ``kimi``) replaced the
archived Python kimi-cli, whose last release (1.52.0) only prints a
deprecation notice, so hooks in its ``~/.kimi/config.toml`` never run.
Docs: docs/en/customization/hooks.md in github.com/MoonshotAI/kimi-code
(accessed 2026-09-26). Verified with Kimi Code 2.1.1, its TUI in a
pseudo-terminal and ``kimi -p``, with a temp HOME and a local stand-in for the
model (an ``openai`` provider); no credentials were used. See
tests/test_relay_kimi_live.py; the payloads it recorded are in
tests/fixtures/relay/kimi/. Only that version has been run.

What the run showed (and the docs and source say):

- The config is ``$KIMI_CODE_HOME/config.toml``, by default
  ``~/.kimi-code/config.toml``. A ``[[hooks]]`` table takes only ``event``,
  ``matcher``, ``command`` and ``timeout`` (seconds, 1-600); any other key, or
  a file that is not valid TOML, fails the whole config load, so the result is
  parsed before it is written. There is no trust step.
- SessionStart only notifies: its stdout is thrown away. The exit-0 stdout of
  UserPromptSubmit reaches the model (as ``<hook_result>``), so the brief runs
  there as ``brief --once``: the first prompt of a session gets it, later
  prompts and a resumed session (``kimi -c``) do not fetch it again. A
  SessionStart brief would only mark the brief as delivered, so no start hook
  is installed.
- SessionEnd fires when the TUI exits (reason ``exit``; ``archive`` for an
  archived session), and Kimi waits for it up to its timeout, so ``close``
  runs inline. ``kimi -p`` never ends its session: a prompt-mode run writes no
  handoff. Kimi runs the hooks of one event at the same time, so copies of
  the close hook (a copied Claude Code one, say) arrive together.
- stdin JSON: ``hook_event_name``, ``session_id`` (``session_<uuid>``),
  ``cwd``, ``client_type`` (``kimi_code_cli``), ``session_title``;
  UserPromptSubmit adds ``prompt``, SessionEnd ``reason``. There is no
  transcript path: close-outs use git facts.
- ``kimi migrate`` copies the old ``~/.kimi/config.toml`` hooks into this file
  without their comments, and places them among the user's tables (Kimi Code
  also adds its own tables after them, such as ``[models."<provider>/<model>"]``).
  Relay hooks outside the marked block are therefore found by their command,
  one TOML table at a time, and ``connect`` and ``disconnect`` remove them.
  Every write is checked: parsed, the new file must equal the old one apart
  from the relay's own ``[[hooks]]`` entries, or nothing is written.
- An earlier release wrote its block to ``~/.kimi/config.toml``, the archived
  kimi-cli's file: ``connect`` and ``disconnect`` remove it from there, so
  ``kimi migrate`` has nothing of ours to copy. That kimi-cli's own ``kimi``
  (a Python entry point) does not count as Kimi Code being installed.
"""

from __future__ import annotations

import json
import math
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from remembra.relay.adapters.base import Adapter, AdapterSpec, PayloadMap, RefusedEdit, is_relay_command
from remembra.relay.adapters.crew_hooks import CrewHook, CrewSpec

TESTED_VERSIONS = ("2.1.1",)

BEGIN = "# >>> remembra-relay (managed block) >>>"
END = "# <<< remembra-relay (managed block) <<<"

SPEC = AdapterSpec(
    name="kimi",
    display="Kimi Code CLI",
    verified=True,
    config_path=lambda home: Path(home) / ".kimi-code" / "config.toml",
    start_event=None,
    end_event="SessionEnd",
    prompt_event="UserPromptSubmit",
    payload=PayloadMap(session_id=("session_id",), cwd=("cwd",), transcript=(), reason=("reason",)),
    output="text",
    detect_bins=("kimi",),
    detect_dirs=(".kimi-code",),
    hook_timeouts={"prompt": 15, "end": 15},
    timeout_unit="s",
    home_env="KIMI_CODE_HOME",
    # `kimi -c` restores the prompt that carried the brief (seen live): a resumed session holds it.
    resume_keeps_brief_from=("UserPromptSubmit",),
    notes=(
        f"Verified with Kimi Code {', '.join(TESTED_VERSIONS)} (the TUI and kimi -p) with a local stand-in for the model;"
        " other versions have not been run. The brief arrives with the first prompt; `kimi -p` runs write no handoff."
    ),
)

# The archived Python kimi-cli's config (relative to home): an earlier release wrote its block there.
LEGACY_CONFIG = Path(".kimi") / "config.toml"


def _split_block(text: str) -> tuple[str, str | None, str]:
    """``(before, our block or None, after)``; the block is the one ending at the first END marker."""
    end = text.find(END)
    start = text.rfind(BEGIN, 0, end) if end >= 0 else -1
    if start < 0:
        return text, None, ""
    return text[:start], text[start + len(BEGIN) : end], text[end + len(END) :]


def _scan(line: str, quote: str | None, depth: int) -> tuple[str | None, int]:
    """The multi-line string (its delimiter) and the bracket depth still open after ``line``.

    ``line`` is a key/value line or the continuation of a value. Strings (with
    their escapes), comments, and the ``[`` / ``{`` of arrays and inline tables
    that span lines are followed, so no ``[`` inside them is taken for a header.
    """
    i, n = 0, len(line)
    while i < n:
        if quote is not None:  # inside a multi-line string
            if quote == '"""' and line[i] == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                run = 3  # up to two more quote marks are the string's last characters
                while run < 5 and i + run < n and line[i + run] == quote[0]:
                    run += 1
                i += run
                quote = None
                continue
            i += 1
            continue
        ch = line[i]
        if ch == "#":
            break  # a comment runs to the end of the line
        if line.startswith('"""', i) or line.startswith("'''", i):
            quote, i = line[i : i + 3], i + 3
        elif ch == '"':  # a one-line basic string
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
        elif ch == "'":  # a one-line literal string: no escapes
            end = line.find("'", i + 1)
            i = n if end < 0 else end + 1
        else:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth = max(0, depth - 1)
            i += 1
    return quote, depth


def _table_starts(text: str) -> list[int]:
    """Offsets of the lines that open a TOML table: ``[t]`` or ``[[t]]``, whatever characters its quoted keys hold.

    A line inside a multi-line string, or inside an array or inline table that
    spans lines, is never a header. A key/value line cannot start with ``[``.
    """
    starts: list[int] = []
    offset = 0
    quote: str | None = None
    depth = 0
    for line in text.splitlines(keepends=True):
        if quote is None and depth == 0 and line.lstrip(" \t").startswith("["):
            starts.append(offset)
        else:
            quote, depth = _scan(line, quote, depth)
        offset += len(line)
    return starts


def _trailing_comments(piece: str) -> str:
    """The comment lines at the end of a table's text: they belong to the table after it."""
    lines = piece.splitlines(keepends=True)
    last = max((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), default=-1)
    trailing = lines[last + 1 :]
    return "".join(trailing) if any(line.lstrip().startswith("#") for line in trailing) else ""


def _is_relay_hook(entry: Any) -> bool:
    return isinstance(entry, dict) and is_relay_command(entry.get("command"))


def _is_relay_hook_table(piece: str) -> bool:
    """True when ``piece`` (one table: its header up to the next one) is a single ``[[hooks]]`` entry running the relay."""
    try:
        data = tomllib.loads(piece)
    except tomllib.TOMLDecodeError:
        return False
    entries = data.get("hooks")
    return list(data) == ["hooks"] and isinstance(entries, list) and len(entries) == 1 and _is_relay_hook(entries[0])


def _drop_unmarked_relay_hooks(text: str) -> tuple[str, int]:
    """``text`` without the ``[[hooks]]`` tables that run the relay (copies ``kimi migrate`` made), and how many."""
    starts = _table_starts(text)
    if not starts:
        return text, 0
    pieces = [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]
    kept, dropped = [text[: starts[0]]], 0
    for piece in pieces:
        if _is_relay_hook_table(piece):
            dropped += 1
            kept.append(_trailing_comments(piece))
            continue
        kept.append(piece)
    return "".join(kept), dropped


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{what} is not valid TOML ({e})") from e


def _comparable(value: Any) -> Any:
    """``value`` with every NaN replaced by one marker, so that ``x = nan`` compares equal to itself."""
    if isinstance(value, dict):
        return {k: _comparable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_comparable(v) for v in value]
    if isinstance(value, float) and math.isnan(value):
        return "\0nan"
    return value


def _without_relay_hooks(data: dict[str, Any]) -> Any:
    """``data`` (a parsed config) without the relay's ``[[hooks]]`` entries (``hooks`` goes when none is left)."""
    hooks = data.get("hooks")
    if not isinstance(hooks, list):
        return _comparable(data)
    out = {k: v for k, v in data.items() if k != "hooks"}
    kept = [entry for entry in hooks if not _is_relay_hook(entry)]
    if kept:
        out["hooks"] = kept
    return _comparable(out)


def _relay_hooks(data: dict[str, Any]) -> list[Any]:
    hooks = data.get("hooks")
    return [entry for entry in hooks if _is_relay_hook(entry)] if isinstance(hooks, list) else []


def _check_only_relay_hooks_changed(before: str, after: str, relay_hooks: list[Any]) -> None:
    """Raise :class:`RefusedEdit` unless ``after`` is ``before`` with only the relay's ``[[hooks]]`` changed.

    Parsed, everything but the relay's entries must be equal, and the relay's
    entries in ``after`` must be exactly ``relay_hooks``: what a write promises
    the user. A table the edit would take along by mistake stops it.
    """
    old = _parse(before, "the Kimi Code config")
    new = _parse(after, "the Kimi Code config after the edit")
    if _without_relay_hooks(new) != _without_relay_hooks(old) or _relay_hooks(new) != relay_hooks:
        raise RefusedEdit(
            "the edit would change more of the Kimi Code config than the relay's own [[hooks]] tables, so nothing was"
            " written. Remove the remembra-relay [[hooks]] tables from it by hand, then run the command again"
        )


def toml_string(value: str) -> str:
    """``value`` as a TOML basic string.

    JSON's escapes are all valid TOML, but ``json.dumps`` writes a character
    outside the Basic Multilingual Plane as a ``\\uXXXX`` surrogate pair, which
    TOML rejects, so non-ASCII text is written as it is. DEL (U+007F), which TOML
    forbids in a string and JSON leaves alone, is escaped. A lone surrogate (a
    path byte that is not UTF-8) cannot be written to a UTF-8 file: ValueError.
    """
    if any("\ud800" <= ch <= "\udfff" for ch in value):
        raise ValueError(f"{value!r} holds bytes that are not valid UTF-8; it cannot be written to a TOML file")
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def archived_kimi_cli(path: str) -> bool:
    """True when ``path`` is the archived Python kimi-cli's ``kimi``: a Python entry point, or a wrapper that runs it."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2048)
    except OSError:
        return False  # cannot tell: counted as Kimi Code
    first = head.split(b"\n", 1)[0]
    return (first.startswith(b"#!") and b"python" in first.lower()) or b"kimi_cli" in head or b"kimi-cli" in head


class KimiTomlAdapter(Adapter):
    """Our ``[[hooks]]`` tables as one marked block, rewritten in place; the rest of the file is kept as written."""

    def detect(self, home: Path, which: Callable[[str], str | None] = shutil.which) -> bool:
        """Kimi Code's directory, or a ``kimi`` that is not the archived kimi-cli (which only prints a deprecation notice)."""

        def kimi_code(name: str) -> str | None:
            found = which(name)
            return None if found is None or archived_kimi_cli(found) else found

        return super().detect(home, which=kimi_code)

    def retired_files(self, home: Path) -> list[Path]:
        return [Path(home) / LEGACY_CONFIG]

    def _block(self, relay: str) -> str:
        commands = self.commands(relay)
        lines = [BEGIN]
        for key, event, matcher in self.hook_events():
            lines += ["[[hooks]]", f"event = {toml_string(event)}"]
            if matcher:
                lines.append(f"matcher = {toml_string(matcher)}")
            lines.append(f"command = {toml_string(commands[key])}")
            timeout = self.spec.timeout_value(key)
            if timeout:
                lines.append(f"timeout = {timeout}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n" + END + "\n"

    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        text = before or ""
        _parse(text, "the Kimi Code config")
        head, old_block, tail = _split_block(text)
        head, dropped_head = _drop_unmarked_relay_hooks(head)
        tail, dropped_tail = _drop_unmarked_relay_hooks(tail)
        block = self._block(relay)
        if old_block is not None:  # in place; what follows the END line is kept as written
            new = head + block.rstrip("\n") + (tail or "\n")
        else:
            new = (head.rstrip() + "\n\n" if head.strip() else "") + block
        # A [[hooks]] table cannot follow an inline `hooks = [...]`: Kimi would then reject the whole file.
        _parse(new, "the Kimi Code config with the relay's [[hooks]] added")
        same_block = old_block is not None and BEGIN + old_block + END + "\n" == block
        dropped = dropped_head + dropped_tail
        if new == text or (same_block and not dropped):
            return text, []  # already current (at most a missing final newline): left as it is
        _check_only_relay_hooks_changed(text, new, _parse(block, "the relay's [[hooks]] block")["hooks"])
        events = ", ".join(event for _, event, _ in self.hook_events())
        summary = [] if same_block else [f"write the managed [[hooks]] block ({events})"]
        if dropped:
            tables = f"{dropped} relay [[hooks]] table{'s' if dropped != 1 else ''}"
            summary.append(f"remove {tables} outside the block (copied by `kimi migrate`)")
        return new, summary

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        _parse(before, "the Kimi Code config")
        head, block, tail = _split_block(before)
        head, dropped_head = _drop_unmarked_relay_hooks(head)
        tail, dropped_tail = _drop_unmarked_relay_hooks(tail)
        if block is None and not (dropped_head + dropped_tail):
            return before, [], False
        new = head.rstrip() + ("\n\n" if head.strip() and tail.strip() else "") + tail.lstrip("\n")
        new = new if not new.strip() else new.rstrip() + "\n"
        _check_only_relay_hooks_changed(before, new, [])
        summary = ["remove the managed remembra-relay [[hooks]] block"] if block is not None else []
        dropped = dropped_head + dropped_tail
        if dropped:
            summary.append(f"remove {dropped} relay [[hooks]] table{'s' if dropped != 1 else ''} outside the block")
        return new, summary, not new.strip()


ADAPTER = KimiTomlAdapter(SPEC)


# ---------------------------------------------------------------------------
# Crew mode (§8.3): UNVERIFIED, observe only until `remembra-crew verify` passes
# ---------------------------------------------------------------------------

# Research-grade: [[hooks]] tables in ~/.kimi/config.toml with Claude-compatible event names, written as a
# marked block that replaces the relay block.
CREW = CrewSpec(
    adapter="kimi",
    verified=False,
    style="kimi-toml",
    hooks=(
        CrewHook("SessionStart", "start", "cli"),
        CrewHook("UserPromptSubmit", "turn", "gate"),
        CrewHook("PreToolUse", "pretool", "gate"),
        CrewHook("PostToolUse", "posttool", "gate"),
        CrewHook("Stop", "stop", "gate"),
        CrewHook("SessionEnd", "end", "cli"),
    ),
    output="text",
    pretool_events=("PreToolUse",),
    file_gate=True,
    notes="Unverified: Kimi CLI hook events are from research; kimi is not installed here.",
)
