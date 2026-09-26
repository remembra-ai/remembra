"""The exchange slip: the doctor's findings as a monospace printout of the user's own relay.

Every agent is a station on a dashed rail; the baton marks the last handoff;
each read shows its result; each finding shows its evidence and one fix.
The same text is the MCP tool's ``rendered`` field. Lines are hard-wrapped at
:data:`WIDTH` columns. Colour is only ever added on top of the plain text:
signal orange marks the baton and the fix (the one thing to act on), the
rail is trail grey, reads and evidence are dim.

Line grammar: ``›`` a read · ``●`` / ``○`` / ``·`` a station (activity seen /
waiting / not on this machine) · ``◆`` the baton · ``=`` a verdict · ``seen``
evidence · ``fix →`` the one action · ``then`` the re-check · ``doc`` the
page · ``[ok]`` / ``[!!]`` / ``[??]`` ok / needs you / inferred, not proven.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Any

from remembra.marshal import RULESET_VERSION, codex_hooks
from remembra.marshal.rules import Finding, age, scope
from remembra.marshal.signals import AgentSignals, Signals, clean_text, slot_ts

WIDTH = 76
FOOTER_END = "Nothing was changed."

UNICODE = {"read": "›", "active": "●", "waiting": "○", "absent": "·", "baton": "◆", "rail": "┊", "arrow": "→", "dot": "·"}
ASCII = {"read": ">", "active": "*", "waiting": "o", "absent": ".", "baton": "<>", "rail": ":", "arrow": "->", "dot": "-"}

# Characters the slip's own wording uses, for a terminal that can't show them.
_ASCII_TEXT = str.maketrans({"·": "-", "…": "...", "→": "->", "›": ">", "◆": "<>", "┊": ":", "●": "*", "○": "o"})

RUNS_WHERE = {
    "agent_ok": "your agent may run it after you say yes",
    "user_terminal": "run it yourself in your own terminal: it involves your key",
    "codex_ui": "done in Codex; Marshal can't do it",
    "dashboard": "done in the dashboard; Marshal can't do it",
    "none": "",
}


@dataclass(frozen=True)
class Style:
    color: bool = False
    truecolor: bool = False
    ascii: bool = False

    @property
    def g(self) -> dict[str, str]:
        return ASCII if self.ascii else UNICODE

    def paint(self, text: str, tone: str | None) -> str:
        if not self.color or not tone or not text:
            return text
        codes = {
            "dim": "2",
            "ok": "32",
            "fail": "31",
            "signal": "38;2;255;107;43" if self.truecolor else "38;5;208",
            "rail": "38;2;74;78;83" if self.truecolor else "38;5;240",
            "bold": "1",
        }
        return f"\033[{codes[tone]}m{text}\033[0m"


Segment = tuple[str, str | None]


class _Slip:
    def __init__(self, style: Style) -> None:
        self.style = style
        self.lines: list[list[Segment]] = []

    def add(self, segments: list[Segment], marker: str = "") -> None:
        """One line; ``marker`` is right-aligned at the last column when it fits."""
        text_len = sum(len(t) for t, _ in segments)
        if marker:
            pad = WIDTH - text_len - len(marker)
            segments = [*segments, (" " * max(1, pad), None), (marker, _marker_tone(marker))]
        self.lines.append(segments)

    def wrapped(self, first: list[Segment], rest: list[Segment], text: str, tone: str | None = None, marker: str = "") -> None:
        """``text`` wrapped under a lead (``first``) and a hanging indent (``rest``)."""
        lead = sum(len(t) for t, _ in first)
        hang = sum(len(t) for t, _ in rest)
        room_first = WIDTH - lead - (len(marker) + 1 if marker else 0)
        chunks = textwrap.wrap(text, width=max(20, room_first), break_long_words=True, break_on_hyphens=False) or [""]
        head, tail = chunks[0], " ".join(chunks[1:])
        self.add([*first, (head, tone)], marker)
        for chunk in textwrap.wrap(tail, width=max(20, WIDTH - hang), break_long_words=True, break_on_hyphens=False):
            self.add([*rest, (chunk, tone)])

    def rail(self, indent: str = "  ") -> Segment:
        return (f"{indent}{self.style.g['rail']}", "rail")

    def text(self) -> str:
        out = "\n".join("".join(self.style.paint(t, tone) for t, tone in line).rstrip() for line in self.lines)
        return out.translate(_ASCII_TEXT) if self.style.ascii else out


def _marker_tone(marker: str) -> str | None:
    return {"[ok]": "ok", "[!!]": "fail", "[??]": "dim"}.get(marker)


def _worst(findings: list[Finding]) -> str:
    markers = [f.marker for f in findings]
    if "[!!]" in markers:
        return "[!!]"
    if "[??]" in markers:
        return "[??]"
    return "[ok]"


def _trust_word(sig: Signals) -> str | None:
    trust = sig.codex.trust
    if trust is None or not trust.per_hook:
        return None
    statuses = trust.statuses()
    if trust.config_state == "unreadable":
        return "trust unchecked"
    if codex_hooks.UNTRUSTED in statuses:
        return "trust NOT recorded"
    if codex_hooks.DISABLED in statuses:
        return "a hook turned off"
    if codex_hooks.MODIFIED in statuses:
        return "trust out of date"
    if codex_hooks.UNCHECKED in statuses:
        return "trust unchecked"
    return "trusted"


def hooks_word(agent: AgentSignals) -> str:
    if not agent.config_readable:
        return "config unreadable"
    if not agent.any_hooks:
        # connect --apply leaves an unverified adapter out unless asked: left out, not missing.
        return "hooks NOT written" if agent.verified else "hooks left out"
    if not agent.core_written:
        return "hooks partly written"
    if agent.missing_binary:
        return "hooks call a missing command"
    if agent.missing_events or agent.outdated:
        return "hooks from an older connect"
    return "hooks written"


def _active(sig: Signals, agent: AgentSignals) -> bool:
    if sig.server is not None and sig.server.entries_7d.get(agent.name, 0) > 0:
        return True
    at = slot_ts(agent.last_success)
    return at is not None and sig.now - at < 7 * 86400


def _local_line(sig: Signals, agent: AgentSignals) -> str | None:
    parts = []
    for slot, word in ((agent.last_success, "ok"), (agent.last_failure, "failed")):
        if not isinstance(slot, dict):
            continue
        at = slot_ts(slot)
        when = age(sig.now - at) if at is not None else "at an unknown time"
        command = clean_text(str(slot.get("command") or "?"), 20)
        extra = f" (HTTP {slot['http_status']})" if word == "failed" and slot.get("http_status") else ""
        parts.append(f"{command} {word} {when}{extra}")
    return " · ".join(parts) if parts else None


def _trail_line(sig: Signals, agent: AgentSignals) -> str | None:
    server = sig.server
    if server is None:
        return None
    name = agent.name
    parts = [f"{server.entries_7d.get(name, 0)} entries in 7d ({server.handoffs_7d.get(name, 0)} handoffs)"]
    if server.trail_read:
        picked = server.pickups_by.get(name, 0)
        parts.append(f"picked up {picked}" + (f", last {age(sig.now - server.last_pickup[name])}" if picked else ""))
    last = server.last_active.get(name)
    if last:
        parts.append(f"last entry {age(sig.now - last)}")
    return " · ".join(parts)


def _baton_line(
    sig: Signals, baton: tuple[str, float, tuple[tuple[str, float], ...]], g: dict[str, str], with_author: bool = False
) -> str:
    author, at, pickups = baton
    picks = [(r, t) for r, t in pickups if r != author]
    said = (
        "picked up by " + ", ".join(f"{r} {age(sig.now - t)}" for r, t in picks[:3])
        if picks
        else "not picked up by another agent yet"
    )
    who = f" from {author}" if with_author else ""
    return f"last handoff{who} {age(sig.now - at)} {g['dot']} {said}"


def _finding_block(slip: _Slip, finding: Finding, indent: list[Segment]) -> None:
    g = slip.style.g
    hang = [*indent, ("  ", None)]
    slip.wrapped([*indent, ("= ", None)], hang, finding.what, marker=finding.marker)
    for line in finding.evidence:
        slip.wrapped([*hang, ("seen  ", "dim")], [*hang, ("      ", None)], line, "dim")
    if finding.caveat:
        slip.wrapped([*hang, ("note  ", "dim")], [*hang, ("      ", None)], finding.caveat, "dim")
    fix = finding.fix
    if fix is not None:
        label = f"fix {g['arrow']} "
        pad = " " * len(label)
        slip.wrapped([*hang, (label, "signal")], [*hang, (pad, None)], fix.text, "signal" if not fix.command else None)
        if fix.command:
            # A command is never wrapped (a broken line would not paste): aligned under the fix text when it
            # fits, else two columns in; past the width only when it is longer than the line itself.
            lead = sum(len(t) for t, _ in hang)
            command_indent = pad if lead + len(pad) + len(fix.command) <= WIDTH else "  "
            slip.add([*hang, (command_indent, None), (fix.command, "signal")])
            where = RUNS_WHERE.get(fix.runs_where, "")
            if fix.writes:
                where += ("; " if where else "") + "writes " + ", ".join(fix.writes)
            if where:
                slip.wrapped([*hang, (pad, None)], [*hang, (pad, None)], where, "dim")
    if finding.then:
        slip.wrapped([*hang, ("then  ", None)], [*hang, ("      ", None)], finding.then)
    if finding.doc:
        slip.wrapped([*hang, ("doc   ", "dim")], [*hang, ("      ", None)], finding.doc, "dim")


def render_text(sig: Signals, findings: list[Finding], style: Style | None = None) -> str:
    style = style or Style()
    g = style.g
    slip = _Slip(style)

    where = ""
    if sig.repo:
        name, branch = sig.repo
        where = f" {g['dot']} {clean_text(name, 24)}" + (f" ({branch})" if branch else "")
    title = f"remembra-relay doctor{where} {g['dot']} remembra {sig.version}"
    if len(title) + len(" marshal") > WIDTH:  # a long repository or branch name: the title keeps the width
        title = title[: WIDTH - len(" marshal") - 1] + "…"
    slip.add([(title, "bold")], "marshal")

    for read in sig.reads:
        timing = f"{read.ms}ms" if read.ms is not None else ""
        lead: list[Segment] = [(f"  {g['read']} ", "dim"), (f"{read.what:<7} ", "dim")]
        slip.wrapped(lead, [("            ", None)], read.result, "dim" if read.ok else None, marker=timing)

    baton = sig.server.baton if sig.server is not None else None
    stations = [n for n in scope(sig) if sig.agents[n].detected or sig.agents[n].any_hooks]
    if baton is not None and baton[0] not in stations:  # left by an agent with no station here
        slip.wrapped([(f"  {g['baton']} ", "signal")], [("     ", None)], _baton_line(sig, baton, g, with_author=True))

    machine = [f for f in findings if f.agent is None]
    for finding in machine:
        slip.add([slip.rail()])
        _finding_block(slip, finding, [("  ", None)])

    for name in scope(sig):
        agent = sig.agents[name]
        own = [f for f in findings if f.agent == name]
        in_play = agent.detected or agent.any_hooks
        slip.add([slip.rail()])
        if not in_play:
            slip.add([(f"  {g['absent']} ", "dim"), (f"{name:<12} ", "dim"), ("not detected on this machine · skipped", "dim")])
            for finding in own:
                _finding_block(slip, finding, [slip.rail(), ("   ", None)])
            continue
        glyph = g["active"] if _active(sig, agent) else g["waiting"]
        words = [hooks_word(agent), "verified" if agent.verified else "unverified"]
        if name == "codex" and agent.core_written:
            trust = _trust_word(sig)
            if trust:
                words.append(trust)
        if not agent.detected:
            words.append("not on PATH")
        slip.wrapped(
            [(f"  {glyph} ", None), (f"{name:<12} ", "bold")],
            [slip.rail(), ("              ", None)],
            " · ".join(words),
            marker=_worst(own),
        )
        rail_indent = [slip.rail(), ("   ", None)]
        local = _local_line(sig, agent)
        if local:
            slip.wrapped([*rail_indent, ("here  ", "dim")], [*rail_indent, ("      ", None)], local, "dim")
        trail = _trail_line(sig, agent)
        if trail:
            slip.wrapped([*rail_indent, ("trail ", "dim")], [*rail_indent, ("      ", None)], trail, "dim")
        if baton is not None and baton[0] == name:
            slip.wrapped(
                [slip.rail(), (" ", None), (g["baton"], "signal"), (" ", None)],
                [*rail_indent, ("  ", None)],
                _baton_line(sig, baton, g),
            )
        for finding in own:
            _finding_block(slip, finding, rail_indent)

    slip.add([slip.rail()])
    todo = sum(1 for f in findings if f.to_do)
    watch = sum(1 for f in findings if f.actionable and not f.to_do)
    count = "Nothing to do." if not todo else f"{todo} thing{'s' if todo != 1 else ''} to do."
    if watch:
        count += f" {watch} to watch."
    slip.add([("  ", None), (count, "bold")])
    if sig.unchecked:
        slip.wrapped([("  ", None), ("not checked: ", "dim")], [("  ", None)], "; ".join(sig.unchecked) + ".", "dim")
    sources = "this machine's files and your trail" if sig.server is not None else "this machine's files"
    footer = f"Built by rules in remembra {sig.version} from {sources}. No model wrote this. {FOOTER_END}"
    slip.wrapped([("  ", None)], [("  ", None)], footer, "dim")
    return slip.text() + "\n"


def _slot(slot: Any) -> dict[str, Any] | None:
    if not isinstance(slot, dict):
        return None
    out: dict[str, Any] = {"command": clean_text(str(slot.get("command") or ""), 40), "at": slot.get("at")}
    if slot.get("http_status"):
        out["http_status"] = slot.get("http_status")
    if slot.get("error"):
        out["error"] = clean_text(str(slot.get("error")))
    return out


def render_json(sig: Signals, findings: list[Finding], exit_code: int) -> dict[str, Any]:
    agents = []
    for name in scope(sig):
        agent = sig.agents[name]
        item: dict[str, Any] = {
            "name": name,
            "display": agent.display,
            "verified": agent.verified,
            "detected": agent.detected,
            "hooks": {
                "state": hooks_word(agent),
                "config_path": agent.config_path,
                "present": list(agent.present_events),
                "missing": list(agent.missing_events),
                "backup_from_connect": agent.backup,
                "missing_command": agent.missing_binary,
            },
            "key_source": agent.key_source,
            "mcp": {"state": agent.mcp, "config_path": agent.mcp_path, "project": agent.mcp_project},
            "last_success": _slot(agent.last_success),
            "last_failure": _slot(agent.last_failure),
            "findings": [f.id for f in findings if f.agent == name],
        }
        if name == "codex" and sig.codex.trust is not None:
            item["trust"] = {
                "config_state": sig.codex.trust.config_state,
                "hooks": [{"event": t.hook.event, "status": t.status, "proven": t.proven} for t in sig.codex.trust.per_hook],
            }
        if sig.server is not None:
            server = sig.server
            last = server.last_entry.get(name)
            item["trail"] = {
                "entries_7d": server.entries_7d.get(name, 0),
                "handoffs_7d": server.handoffs_7d.get(name, 0),
                "pickups": server.pickups_by.get(name, 0),
                "last_entry": {"type": last[0], "age": age(sig.now - last[1])} if last else None,
            }
        agents.append(item)
    return {
        "version": sig.version,
        "ruleset": RULESET_VERSION,
        "generated_at": sig.generated_at,
        "repo": {"name": sig.repo[0], "branch": sig.repo[1]} if sig.repo else None,
        "config": dict(sig.config),
        "reads": [{"what": r.what, "result": r.result, "ms": r.ms, "ok": r.ok} for r in sig.reads],
        "agents": agents,
        "findings": [f.as_dict() for f in findings],
        "unchecked": list(sig.unchecked),
        "things_to_do": sum(1 for f in findings if f.to_do),
        "to_watch": sum(1 for f in findings if f.actionable and not f.to_do),
        "exit_code": exit_code,
        "changed_nothing": True,
    }
