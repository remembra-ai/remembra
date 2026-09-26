"""Kimi Code CLI (UNVERIFIED): ``[[hooks]]`` tables in ``~/.kimi/config.toml``.

Reported: ``[[hooks]] event = "SessionStart" command = "..."``; stdout on
exit 0 is added to the context. Written as a marked block, so re-running
replaces only our block.
"""

from __future__ import annotations

import json
from pathlib import Path

from remembra.relay.adapters.base import Adapter, AdapterSpec, PayloadMap

BEGIN = "# >>> remembra-relay (managed block) >>>"
END = "# <<< remembra-relay (managed block) <<<"

SPEC = AdapterSpec(
    name="kimi",
    display="Kimi Code CLI",
    verified=False,
    config_path=lambda home: Path(home) / ".kimi" / "config.toml",
    start_event="SessionStart",
    end_event="SessionEnd",
    payload=PayloadMap(session_id=("session_id",), cwd=("cwd",), transcript=(), reason=("reason",)),
    output="text",
    detect_bins=("kimi",),
    detect_dirs=(".kimi",),
    notes="Unverified: kimi is not installed here.",
)


class KimiTomlAdapter(Adapter):
    def render(self, before: str | None, relay: str) -> tuple[str, list[str]]:
        commands = self.commands(relay)
        block_lines = [BEGIN]
        for key, event in (("start", self.spec.start_event), ("end", self.spec.end_event)):
            if event:
                block_lines += ["[[hooks]]", f"event = {json.dumps(event)}", f"command = {json.dumps(commands[key])}", ""]
        block = "\n".join(block_lines).rstrip() + "\n" + END + "\n"
        text = before or ""
        if BEGIN in text and END in text:
            head, rest = text.split(BEGIN, 1)
            _, tail = rest.split(END, 1)
            new = head + block + tail.lstrip("\n")
        else:
            new = (text.rstrip() + "\n\n" if text.strip() else "") + block
        summary = [] if new == text else [f"write managed [[hooks]] block ({self.spec.start_event}, {self.spec.end_event})"]
        return new, summary

    def render_removal(self, before: str) -> tuple[str, list[str], bool]:
        if BEGIN not in before or END not in before:
            return before, [], False
        head, rest = before.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        new = head.rstrip() + ("\n\n" if head.strip() and tail.strip() else "") + tail.lstrip("\n")
        new = new if not new.strip() else new.rstrip() + "\n"
        return new, ["remove the managed remembra-relay [[hooks]] block"], not new.strip()


ADAPTER = KimiTomlAdapter(SPEC)
