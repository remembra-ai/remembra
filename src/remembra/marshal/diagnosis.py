"""The "why?" verdict for one agent, on the server: a line-for-line port of ``diagnoseAgent``.

``dashboard/src/lib/marshal.ts`` picks one verdict for a waiting agent from
data the dashboard already reads: your keys, the newest 100 trail entries (with
who picked each handoff up) and the agent's own newest 5. This module does the
same from the same data, in the same words, so the Marshal desk and
``GET /api/v1/trail/diagnosis`` say exactly what the slip says. Both run
``tests/fixtures/marshal/diagnosis_cases.json`` (vitest and
``tests/test_trail_diagnosis.py``); a difference fails one of them.

Rules only: no model, no I/O, standard library only (it lives in the
client-safe package; ``tests/test_marshal_imports.py`` imports it on a base
install). Words come from :mod:`remembra.marshal.words` (the doctor's own
sentences) and command lines from :mod:`remembra.marshal.commands`.

Porting details that change results if missed: JavaScript's ``Math.round``
rounds halves up (:func:`js_round`, never Python's ``round``); a server time
with no zone is UTC; own entries are de-duplicated keeping the first
occurrence in ``[*agent_trail, *trail]`` order; sorts are stable.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from remembra.marshal import commands, words
from remembra.relay.adapters import REGISTRY

VerdictCode = Literal[
    "KEY_MISSING",
    "KEY_NEVER_USED",
    "PICKS_UP_NEVER_CLOSES",
    "CODEX_TRUST_MISSING",
    "NOTHING_WAITING",
    "HOOKS_NOT_FIRING",
    "STALE_CHECKPOINT",
    "HANDED_OFF",
]

# How many trail entries the slip reads for pickups (the API's page maximum), and how many of the agent's own.
SLIP_TRAIL_LIMIT = 100
SLIP_AGENT_LIMIT = 5
# A checkpoint newer than this (with no later handoff) counts as "working now" (agents.ts WORKING_WINDOW_MINUTES).
WORKING_WINDOW_MINUTES = 60

RELAY_GUIDE = commands.DOCS_RELAY
DETACHED_CLOSE_LOG = "~/.remembra/relay/last-detached-close.log"
KEY_CAVEAT = "One key can serve every agent on a machine, so Remembra can't tell which agent used it. Doctor on that machine can."
SLIP_FOOTER = "Built by rules from your keys and trail. No model wrote this."
TICKS = "Its handoff ticks this row."
CODEX_TRUST_LINE = words.codex_trust_call()
CODEX_TRUST_FIX = words.codex_trust_fix()


# ---------------------------------------------------------------------------
# Agents (agents.ts: KNOWN, ALIASES, canonicalAgentId, agentMeta)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentMeta:
    id: str
    name: str
    adapter: str | None = None
    verified: bool = False
    detach_close: bool = False


def _known() -> dict[str, AgentMeta]:
    known = {
        agent: AgentMeta(
            id=agent,
            name=words.AGENT_NAMES[agent],
            adapter=agent,
            verified=bool(REGISTRY[agent].spec.verified),
            detach_close=bool(REGISTRY[agent].spec.detach_close),
        )
        for agent in words.AGENT_NAMES
        if agent in REGISTRY
    }
    known["dashboard"] = AgentMeta(id="dashboard", name="You (dashboard)")
    return known


KNOWN: dict[str, AgentMeta] = _known()
_TITLE_SPLIT = re.compile(r"[-_./:@+\s]+")


def canonical_agent_id(agent_id: str | None) -> str:
    """``canonicalAgentId``: a known id or alias, lower-cased and mapped; any other id unchanged."""
    raw = (agent_id or "").strip()
    key = raw.lower()
    if key in KNOWN:
        return key
    return words.AGENT_ALIASES.get(key, raw)


def _title_case(agent_id: str) -> str:
    return " ".join(part[0].upper() + part[1:] for part in _TITLE_SPLIT.split(agent_id) if part)


def agent_meta(agent_id: str | None) -> AgentMeta:
    """``agentMeta``: the display name, adapter and flags of an agent id (title-cased for an unknown one)."""
    raw = (agent_id or "").strip()
    if not raw:
        return AgentMeta(id="", name="Unattributed")
    known = KNOWN.get(canonical_agent_id(raw))
    if known is not None:
        return AgentMeta(id=raw, name=known.name, adapter=known.adapter, verified=known.verified, detach_close=known.detach_close)
    return AgentMeta(id=raw, name=_title_case(raw) or raw)


def adapter_id(agent_id: str | None) -> str:
    """The ``--agent`` name of an agent: its adapter, else its canonical id (``meta.adapter ?? canonicalAgentId``)."""
    meta = agent_meta(agent_id)
    return meta.adapter if meta.adapter is not None else canonical_agent_id(agent_id)


def _is_agent(agent: str, other: str | None) -> bool:
    return canonical_agent_id(other) == agent


# ---------------------------------------------------------------------------
# Time (time.ts: parseServerTime, relativeTime, minutesSince)
# ---------------------------------------------------------------------------

_ZONED = re.compile(r"(Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def js_round(value: float) -> int:
    """JavaScript's ``Math.round``: halves go up (2.5 -> 3, -2.5 -> -2), never to even."""
    return math.floor(value + 0.5)


def parse_server_time(value: str | datetime | None) -> datetime | None:
    """A server timestamp as an aware UTC time. A time with no zone is UTC, never local time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value).strip().replace(" ", "T", 1)
    if not text:
        return None
    if not _ZONED.search(text):
        text += "Z"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ms(value: datetime) -> int:
    """Milliseconds since the epoch (what a JavaScript ``Date`` holds: microseconds are dropped)."""
    return (value - _EPOCH) // timedelta(milliseconds=1)


def relative_time(value: str | datetime | None, now: datetime) -> str:
    """ "just now", "4m ago", "2h ago", "yesterday", "3d ago", then "Sep 12" (", 2025" when the year differs)."""
    date = parse_server_time(value)
    if date is None:
        return "unknown time"
    seconds = js_round((_ms(now) - _ms(date)) / 1000)
    if seconds < 45:
        return "just now"
    minutes = js_round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = js_round(minutes / 60)
    if hours < 24:
        return f"{hours}h ago"
    days = math.floor(hours / 24)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    # The browser formats this in the viewer's locale; the server says it the en-US way, in UTC.
    label = f"{_MONTHS[date.month - 1]} {date.day}"
    return label if date.year == now.astimezone(UTC).year else f"{label}, {date.year}"


def minutes_since(value: str | datetime | None, now: datetime) -> float | None:
    date = parse_server_time(value)
    return (_ms(now) - _ms(date)) / 60000 if date is not None else None


# ---------------------------------------------------------------------------
# Input and verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyEvidence:
    """The fields of a key the check reads (never the key itself)."""

    name: str | None
    created_at: str | None
    last_used_at: str | None
    active: bool | None = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> KeyEvidence:
        return cls(
            name=data.get("name"),
            created_at=_text(data.get("created_at")),
            last_used_at=_text(data.get("last_used_at")),
            active=data.get("active", True),
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


@dataclass(frozen=True)
class DiagnosisInput:
    agent_id: str
    keys: Sequence[KeyEvidence]
    # Trail items as the trail route returns them: id, memory_type, agent_id, created_at, picked_up_by.
    trail: Sequence[Mapping[str, Any]]
    agent_trail: Sequence[Mapping[str, Any]]
    # The agent's row of the trail summary (handoffs, checkpoints, last_active), when it has one.
    summary_agent: Mapping[str, Any] | None
    now: datetime
    server_url: str | None = None


@dataclass(frozen=True)
class ReadLine:
    """One read, as a ``›`` line: what it read and what it found."""

    label: Literal["keys", "entries", "pickups", "trail"]
    text: str
    failed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "text": self.text, "failed": self.failed}


@dataclass(frozen=True)
class SlipCommand:
    """A line to copy: ``$`` runs in a terminal, ``>`` is typed into an agent."""

    prompt: Literal["$", ">"]
    text: str
    label: str
    caption: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"prompt": self.prompt, "text": self.text, "label": self.label, "caption": self.caption}


@dataclass(frozen=True)
class Fix:
    text: str
    commands: tuple[SlipCommand, ...] = ()


@dataclass(frozen=True)
class Check:
    lead: str
    commands: tuple[SlipCommand, ...] = ()


@dataclass(frozen=True)
class Verdict:
    code: VerdictCode
    proven: bool  # True: the data shows it ([!!]); False: inferred from it ([??])
    lines: tuple[ReadLine, ...]
    verdict: str
    detail: str | None
    causes: tuple[str, ...]
    unverified: str | None
    fix: Fix | None
    then: str | None
    check: Check | None
    commands: tuple[str, ...]  # every copyable line, in the order the slip shows them
    caveat: str | None
    doc: str

    def as_dict(self) -> dict[str, Any]:
        """The TypeScript ``Verdict`` field names, with absent optional fields as null."""
        return {
            "code": self.code,
            "proven": self.proven,
            "lines": [line.as_dict() for line in self.lines],
            "verdict": self.verdict,
            "detail": self.detail,
            "causes": list(self.causes),
            "unverified": self.unverified,
            "fix": None if self.fix is None else {"text": self.fix.text, "commands": [c.as_dict() for c in self.fix.commands]},
            "then": self.then,
            "check": None
            if self.check is None
            else {"lead": self.check.lead, "commands": [c.as_dict() for c in self.check.commands]},
            "commands": list(self.commands),
            "caveat": self.caveat,
            "doc": self.doc,
        }


# ---------------------------------------------------------------------------
# Command lines (agents.ts: doctorCommand, askAgentDoctor, pipxRunDoctorCommand,
# oneLineInstall, agentConnectCommand)
# ---------------------------------------------------------------------------


def _doctor(agent: str) -> str:
    return f"remembra-relay doctor --agent {adapter_id(agent)}"


def _pipx_run_doctor(agent: str) -> str:
    return f"pipx run --spec 'remembra>={commands.DOCTOR_MIN_VERSION}' {_doctor(agent)}"


def _one_line_install(server_url: str | None) -> str:
    save_key = f"remembra-install --all --url {server_url}" if server_url else commands.INSTALL_KEEP_SERVER
    return f"{commands.PIPX_INSTALL} && {save_key} && remembra-relay connect --apply"


def _agent_connect(agent: str) -> str:
    meta = agent_meta(agent)
    base = f"remembra-relay connect --apply --agent {adapter_id(agent)}"
    return base if meta.verified else f"{base} --include-unverified"


# ---------------------------------------------------------------------------
# Helpers (marshal.ts)
# ---------------------------------------------------------------------------


def _key_label(name: str | None) -> str:
    """Key names are the user's own labels: at most 40 characters of one are shown."""
    clean = re.sub(r"\s+", " ", name or "").strip()
    if not clean:
        return ""
    return f"{clean[:39]}…" if len(clean) > 40 else clean


def _newest_first(items: Iterable[Any], at: Callable[[Any], str | datetime | None]) -> list[Any]:
    def key(item: Any) -> int:
        parsed = parse_server_time(at(item))
        return -(_ms(parsed) if parsed is not None else 0)

    return sorted(items, key=key)  # stable, as Array.prototype.sort is


def _count(summary: Mapping[str, Any] | None, key: str) -> int:
    value = (summary or {}).get(key)
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def pickups_by(agent: str, trail: Sequence[Mapping[str, Any]]) -> int:
    """Handoffs in ``trail`` that ``agent`` picked up (one per handoff, whatever the reader session)."""
    return sum(1 for item in trail if any(_is_agent(agent, p.get("agent_id")) for p in item.get("picked_up_by") or []))


def keys_line(keys: Sequence[KeyEvidence], now: datetime) -> ReadLine:
    active = [k for k in keys if k.active is not False]
    if not active:
        return ReadLine("keys", "none active")
    used = _newest_first([k for k in active if k.last_used_at], lambda k: k.last_used_at)
    if not used:
        return ReadLine("keys", f"{len(active)} active · never used")
    label = _key_label(used[0].name)
    named = f' ("{label}")' if label else ""
    return ReadLine("keys", f"{len(active)} active · newest used {relative_time(used[0].last_used_at, now)}{named}")


def entries_line(
    agent_id: str, agent_trail: Sequence[Mapping[str, Any]], summary_agent: Mapping[str, Any] | None, now: datetime
) -> ReadLine:
    agent = canonical_agent_id(agent_id)
    adapter = adapter_id(agent)
    if summary_agent and _count(summary_agent, "handoffs") + _count(summary_agent, "checkpoints") > 0:
        return ReadLine(
            "entries",
            f"{adapter}: {words.plural(_count(summary_agent, 'handoffs'), 'handoff')} · "
            f"{words.plural(_count(summary_agent, 'checkpoints'), 'checkpoint')} · "
            f"newest {relative_time(summary_agent.get('last_active'), now)}",
        )
    own = [item for item in agent_trail if _is_agent(agent, item.get("agent_id"))]
    if not own:
        return ReadLine("entries", f"{adapter}: no handoffs or checkpoints yet")
    handoffs = sum(1 for item in own if item.get("memory_type") == "handoff")
    newest = _newest_first(own, lambda item: item.get("created_at"))[0]
    return ReadLine(
        "entries",
        f"{adapter}: {words.plural(handoffs, 'handoff')} · {words.plural(len(own) - handoffs, 'checkpoint')} "
        f"in its last {len(own)} · newest {relative_time(newest.get('created_at'), now)}",
    )


def pickups_line(agent_id: str, trail: Sequence[Mapping[str, Any]]) -> ReadLine:
    agent = canonical_agent_id(agent_id)
    adapter = adapter_id(agent)
    others = sum(1 for item in trail if item.get("memory_type") == "handoff" and not _is_agent(agent, item.get("agent_id")))
    briefs = pickups_by(agent, trail)
    return ReadLine(
        "pickups",
        f"{adapter} read {words.plural(briefs, 'brief')} · {words.plural(others, 'handoff')} from other agents "
        f"(last {words.plural(len(trail), 'entry', 'entries')})",
    )


def _check_for(agent: str) -> Check:
    name = agent_meta(agent).name
    return Check(
        lead=f"on the machine where you run {name}:",
        commands=(
            SlipCommand("$", _doctor(agent), f"Doctor for {name}"),
            SlipCommand(
                ">",
                commands.ask_agent_doctor(adapter_id(agent)),
                f"Ask your agent to run remembra_doctor for {name}",
                "ask your agent:",
            ),
            SlipCommand("$", _pipx_run_doctor(agent), f"Doctor for {name} without upgrading", "no upgrade yet?"),
        ),
    )


def _unverified_line(agent: str) -> str | None:
    meta = agent_meta(agent)
    if meta.verified or not meta.adapter:
        return None
    return words.UNVERIFIED.format(name=meta.name)


def _end_one_session(name: str) -> str:
    return f"{words.say('NOTHING_WAITING', 'fix', name=name)} {TICKS}"


def _doc_for(rule: str) -> str:
    return f"{RELAY_GUIDE}{words.SHARED_RULES[rule]['doc']}"


# ---------------------------------------------------------------------------
# The verdict table: first match wins (spec section 7, M1 dashboard)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """What the verdict table decides on (also the evidence the trail/diagnosis route returns)."""

    agent: str
    active_keys: int
    used_keys: int
    own: tuple[Mapping[str, Any], ...]  # the agent's own entries, newest first, de-duplicated
    entry_count: int
    own_handoffs: int
    briefs: int
    others_handoffs: int
    trail_entries: int


def facts_of(inp: DiagnosisInput) -> Facts:
    agent = canonical_agent_id(inp.agent_id)
    active = [k for k in inp.keys if k.active is not False]
    combined = [*inp.agent_trail, *inp.trail]
    first_index: dict[Any, int] = {}
    for index, item in enumerate(combined):
        first_index.setdefault(item.get("id"), index)
    own = _newest_first(
        [
            item
            for index, item in enumerate(combined)
            if _is_agent(agent, item.get("agent_id")) and first_index.get(item.get("id")) == index
        ],
        lambda item: item.get("created_at"),
    )
    summary = inp.summary_agent
    summary_count = _count(summary, "handoffs") + _count(summary, "checkpoints") if summary else 0
    return Facts(
        agent=agent,
        active_keys=len(active),
        used_keys=sum(1 for k in active if k.last_used_at),
        own=tuple(own),
        entry_count=max(summary_count, len(own)),
        own_handoffs=max(_count(summary, "handoffs"), sum(1 for item in own if item.get("memory_type") == "handoff")),
        briefs=pickups_by(agent, inp.trail),
        others_handoffs=sum(
            1 for item in inp.trail if item.get("memory_type") == "handoff" and not _is_agent(agent, item.get("agent_id"))
        ),
        trail_entries=len(inp.trail),
    )


def diagnose_agent(inp: DiagnosisInput) -> Verdict:
    facts = facts_of(inp)
    agent = facts.agent
    meta = agent_meta(agent)
    name = meta.name
    adapter = meta.adapter if meta.adapter is not None else agent
    now = inp.now
    lines = (
        keys_line(inp.keys, now),
        entries_line(agent, inp.agent_trail, inp.summary_agent, now),
        pickups_line(agent, inp.trail),
    )
    own = facts.own
    summary = inp.summary_agent
    entry_count, own_handoffs, briefs = facts.entry_count, facts.own_handoffs, facts.briefs
    others_handoffs = facts.others_handoffs
    codex_waiting = agent == "codex" and briefs == 0 and entry_count == 0

    def build(
        code: VerdictCode,
        *,
        proven: bool,
        verdict: str,
        doc: str,
        detail: str | None = None,
        causes: Sequence[str] = (),
        unverified: str | None = None,
        fix: Fix | None = None,
        then: str | None = None,
        check: Check | None = None,
        caveat: str | None = None,
    ) -> Verdict:
        copyable = [*(fix.commands if fix else ()), *(check.commands if check else ())]
        return Verdict(
            code=code,
            proven=proven,
            lines=lines,
            verdict=verdict,
            detail=detail,
            causes=tuple(causes),
            unverified=unverified,
            fix=fix,
            then=then,
            check=check,
            commands=tuple(c.text for c in copyable),
            caveat=caveat,
            doc=doc,
        )

    if facts.active_keys == 0:
        return build(
            "KEY_MISSING",
            proven=True,
            verdict=words.say("KEY_MISSING", "what", where="active on your account"),
            fix=Fix(f"Create a relay key in step 1. Then run the one-line install where you run {name}."),
            then=CODEX_TRUST_FIX if codex_waiting else _end_one_session(name),
            check=_check_for(agent),
            doc=_doc_for("KEY_MISSING"),
        )

    if facts.used_keys == 0:
        return build(
            "KEY_NEVER_USED",
            proven=True,
            verdict=(
                "Your keys have never been used: nothing sent with them has reached Remembra. "
                "The install never saved one, no hook ran, or its request was blocked or went to another server."
            ),
            fix=Fix(
                f"Run the one-line install on the machine where you run {name}. It asks for the key at a hidden prompt.",
                (SlipCommand("$", _one_line_install(inp.server_url), "One-line install and connect"),),
            ),
            then=CODEX_TRUST_FIX if codex_waiting else _end_one_session(name),
            check=_check_for(agent),
            caveat=KEY_CAVEAT,
            doc=f"{RELAY_GUIDE}#setup",
        )

    if briefs > 0 and own_handoffs == 0:
        return build(
            "PICKS_UP_NEVER_CLOSES",
            proven=True,
            verdict=words.say("PICKS_UP_NEVER_CLOSES", "what", name=name, briefs=words.plural(briefs, "brief")),
            detail=f"{name} closes in the background and logs to {DETACHED_CLOSE_LOG} on that machine."
            if meta.detach_close
            else None,
            causes=(f"a {name} session is still open: the handoff is written when it ends", "the close failed on that machine"),
            fix=Fix(words.say("PICKS_UP_NEVER_CLOSES", "fix", name=name)),
            check=_check_for(agent),
            doc=_doc_for("PICKS_UP_NEVER_CLOSES"),
        )

    if codex_waiting:
        return build(
            "CODEX_TRUST_MISSING",
            proven=False,
            verdict=CODEX_TRUST_LINE,
            detail=f"{words.SHARED_RULES['CODEX_TRUST_MISSING']['detail']} No brief or handoff from Codex has reached Remembra.",
            causes=(
                "Codex hooks not trusted yet",
                "connect ran as a dry run (the old homepage lines did this)",
                "Codex runs on a machine without the install",
            ),
            fix=Fix(
                CODEX_TRUST_FIX,
                (SlipCommand(">", commands.CODEX_HOOKS, "The Codex CLI command that lists hooks to trust", "in the Codex CLI:"),),
            ),
            then=_end_one_session("Codex"),
            check=_check_for(agent),
            doc=_doc_for("CODEX_TRUST_MISSING"),
        )

    if entry_count == 0 and others_handoffs == 0:
        honest = _unverified_line(agent)
        return build(
            "NOTHING_WAITING",
            proven=True,
            verdict=words.say("NOTHING_WAITING", "what", name=name),
            unverified=honest,
            fix=Fix(
                f"connect --apply leaves {name}'s hooks out unless you add --include-unverified:",
                (SlipCommand("$", _agent_connect(agent), f"Connect command for {name}"),),
            )
            if honest
            else Fix(words.say("NOTHING_WAITING", "fix", name=name)),
            then=_end_one_session(name) if honest else TICKS,
            check=_check_for(agent),
            doc=_doc_for("NOTHING_WAITING"),
        )

    if entry_count == 0 and briefs == 0:
        honest = _unverified_line(agent)
        causes = (
            (
                f"connect --apply left {name}'s hooks out (they need --include-unverified)",
                "connect ran as a dry run (the old homepage lines did this)",
                f"{name} runs on a machine without the install",
            )
            if honest
            else (
                "connect ran as a dry run (the old homepage lines did this)",
                f"{name} runs on a machine without the install",
                f"no {name} session has ended since connect",
            )
        )
        check = _check_for(agent)
        return build(
            "HOOKS_NOT_FIRING",
            proven=False,
            verdict=words.say("HOOKS_NOT_FIRING", "what", name=name, since=""),
            causes=causes,
            unverified=honest,
            fix=Fix(
                f"Write {name}'s hooks with --include-unverified, on the machine where you run it:",
                (SlipCommand("$", _agent_connect(agent), f"Connect command for {name}"),),
            )
            if honest
            else Fix(f"{check.lead[0].upper()}{check.lead[1:-1]}, doctor names the cause:", check.commands),
            then=_end_one_session(name) if honest else None,
            check=check if honest else None,
            caveat=KEY_CAVEAT,
            doc=_doc_for("HOOKS_NOT_FIRING"),
        )

    newest = own[0] if own else None
    newest_minutes = minutes_since(newest.get("created_at"), now) if newest else None
    if (
        newest is not None
        and newest.get("memory_type") == "checkpoint"
        and newest_minutes is not None
        and newest_minutes > WORKING_WINDOW_MINUTES
    ):
        return build(
            "STALE_CHECKPOINT",
            proven=True,
            verdict=words.say("STALE_CHECKPOINT", "what", name=name),
            detail=words.say("STALE_CHECKPOINT", "detail", ago=relative_time(newest.get("created_at"), now)),
            fix=Fix(
                words.SHARED_RULES["STALE_CHECKPOINT"]["fix"],
                (SlipCommand("$", f"remembra-relay close --agent {adapter}", f"Close command for {name}"),),
            ),
            check=_check_for(agent),
            doc=_doc_for("STALE_CHECKPOINT"),
        )

    at = newest.get("created_at") if newest is not None else None
    if at is None and summary:
        at = summary.get("last_active")
    return build(
        "HANDED_OFF",
        proven=True,
        verdict=f"{name} is connected: its newest entry arrived {relative_time(at, now)}.",
        doc=RELAY_GUIDE,
    )
