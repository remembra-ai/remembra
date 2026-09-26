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
  handoff.
- stdin JSON: ``hook_event_name``, ``session_id`` (``session_<uuid>``),
  ``cwd``, ``client_type`` (``kimi_code_cli``), ``session_title``;
  UserPromptSubmit adds ``prompt``, SessionEnd ``reason``. There is no
  transcript path: close-outs use git facts.
- ``kimi migrate`` copies the old ``~/.kimi/config.toml`` hooks into this file
  without their comments. Relay hooks outside the marked block are therefore
  found by their command, and ``connect`` and ``disconnect`` remove them.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from remembra.relay.adapters.base import Adapter, AdapterSpec, PayloadMap, is_relay_command

TESTED_VERSIONS = ("2.1.1",)

BEGIN = "# >>> remembra-relay (managed block) >>>"
END = "# <<< remembra-relay (managed block) <<<"
# A TOML table header: [table], [[array.of.tables]], [a."quoted key"].
_HEADER = re.compile(r"^[ \t]*\[\[?[ \t]*[A-Za-z0-9_\-.\"' ]+[ \t]*\]\]?[ \t]*(?:#.*)?$", re.M)

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
    notes=(
        f"Verified with Kimi Code {', '.join(TESTED_VERSIONS)} (the TUI and kimi -p) with a local stand-in for the model;"
        " other versions have not been run. The brief arrives with the first prompt; `kimi -p` runs write no handoff."
    ),
)


def _split_block(text: str) -> tuple[str, str | None, str]:
    """``(before, our block or None, after)``; the block is the one ending at the first END marker."""
    end = text.find(END)
    start = text.rfind(BEGIN, 0, end) if end >= 0 else -1
    if start < 0:
        return text, None, ""
    return text[:start], text[start + len(BEGIN) : end], text[end + len(END) :]


def _trailing_comments(piece: str) -> str:
    """The comment lines at the end of a table's text: they belong to the table after it."""
    lines = piece.splitlines(keepends=True)
    last = max((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), default=-1)
    trailing = lines[last + 1 :]
    return "".join(trailing) if any(line.lstrip().startswith("#") for line in trailing) else ""


def _drop_unmarked_relay_hooks(text: str) -> tuple[str, int]:
    """``text`` without the ``[[hooks]]`` tables that run the relay (copies ``kimi migrate`` made), and how many."""
    starts = [m.start() for m in _HEADER.finditer(text)]
    if not starts:
        return text, 0
    pieces = [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]
    kept, dropped = [text[: starts[0]]], 0
    for piece in pieces:
        if piece.lstrip().startswith("[[hooks]]"):
            try:
                entries = tomllib.loads(piece).get("hooks")
            except tomllib.TOMLDecodeError:
                entries = None
            entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
            if is_relay_command(entry.get("command")):
                dropped += 1
                kept.append(_trailing_comments(piece))
                continue
        kept.append(piece)
    return "".join(kept), dropped


def _parse(text: str, what: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{what} is not valid TOML ({e})") from e


class KimiTomlAdapter(Adapter):
    """Our ``[[hooks]]`` tables as one marked block, rewritten in place; the rest of the file is kept as written."""

    def _block(self, relay: str) -> str:
        commands = self.commands(relay)
        lines = [BEGIN]
        for key, event, matcher in self.hook_events():
            lines += ["[[hooks]]", f"event = {json.dumps(event)}"]
            if matcher:
                lines.append(f"matcher = {json.dumps(matcher)}")
            lines.append(f"command = {json.dumps(commands[key])}")
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
        summary = ["remove the managed remembra-relay [[hooks]] block"] if block is not None else []
        dropped = dropped_head + dropped_tail
        if dropped:
            summary.append(f"remove {dropped} relay [[hooks]] table{'s' if dropped != 1 else ''} outside the block")
        return new, summary, not new.strip()


ADAPTER = KimiTomlAdapter(SPEC)
