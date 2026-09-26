"""The doctor's rule table: signals in, findings out. Deterministic; no model, no network, no writes.

Each rule reads :class:`remembra.marshal.signals.Signals` and returns
findings in a fixed order. A finding says what is wrong in one short
sentence, shows the evidence it read, marks whether the verdict is proven
from what was read or inferred from it, and carries at most one fix. A fix's
command always comes from :mod:`remembra.marshal.commands` (checked by
:func:`remembra.marshal.commands.is_allowed`), and says where it runs: in the
user's agent (``agent_ok``), only in the user's own terminal because it
involves the key (``user_terminal``), in Codex's own UI, or in the
dashboard. Wording follows the relay's own messages (``relay/cli.py``) and
the plan limits in :mod:`remembra.cloud.plans`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from remembra.marshal import codex_hooks
from remembra.marshal import commands as cmd
from remembra.marshal.signals import (
    TRAIL_WINDOW,
    AgentSignals,
    KeyCheck,
    OutboxEntry,
    Signals,
    clean_text,
    error_class,
    slot_ts,
)
from remembra.relay.adapters import REGISTRY
from remembra.relay.config import DEFAULT_URL

BLOCKER = "blocker"
WARN = "warn"
INFO = "info"

DOC = "docs.remembra.dev/guides/relay/"
DOC_SETUP = DOC + "#setup"
DOC_TRUST = DOC + "#codex-trust"
DOC_OUTBOX = DOC + "#if-the-server-cannot-be-reached"
DOC_PROJECT = DOC + "#which-project-a-repository-uses"
DOC_DOCTOR = DOC + "#doctor"
DOC_MCP = DOC + "#mcp-by-hand"
DOC_PLANS = "docs.remembra.dev/reference/plans-and-credits/"


@dataclass(frozen=True)
class Fix:
    kind: str  # command | codex_ui | dashboard | none
    text: str
    command: str | None = None
    runs_where: str = "agent_ok"  # agent_ok | user_terminal | codex_ui | dashboard | none
    writes: tuple[str, ...] = ()
    backup: bool = False

    def __post_init__(self) -> None:
        if self.command is not None and not cmd.is_allowed(self.command):
            raise ValueError(f"not a template command: {self.command!r}")


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    agent: str | None
    what: str
    evidence: tuple[str, ...] = ()
    inferred: bool = False
    fix: Fix | None = None
    then: str | None = None
    doc: str | None = None
    caveat: str | None = None

    @property
    def actionable(self) -> bool:
        """Needs attention (blocker or warn); ``[!!]`` unless inferred."""
        return self.severity in (BLOCKER, WARN)

    @property
    def to_do(self) -> bool:
        """Needs attention and has a step to take (not only "wait for the next retry")."""
        return self.actionable and self.fix is not None and self.fix.kind != "none"

    @property
    def marker(self) -> str:
        if self.inferred:
            return "[??]"
        return "[!!]" if self.actionable else ""

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["marker"] = self.marker
        out["proven"] = not self.inferred
        return out


@dataclass(frozen=True)
class Rule:
    id: str
    applies: Callable[[Signals], list[Finding]] = field(repr=False)


def age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def _ago(sig: Signals, ts: Any) -> str:
    try:
        return age(sig.now - float(ts))
    except (TypeError, ValueError):
        return "at an unknown time"


def scope(sig: Signals) -> list[str]:
    return [name for name in REGISTRY if not sig.wanted or name in sig.wanted]


def _in_play(agent: AgentSignals, sig: Signals) -> bool:
    """Checked for hooks: on this machine, or already wired (hooks in its config file)."""
    return agent.detected or agent.any_hooks


def _host(url: str) -> str:
    return url.split("://", 1)[-1]


def _key_fix(url: str | None) -> Fix:
    """Save a (new) key where the hooks read it; the key is typed at a hidden prompt, in the user's terminal."""
    try:
        command = cmd.save_key_command(url) if url and url.rstrip("/") != DEFAULT_URL else cmd.INSTALL_KEEP_SERVER
    except ValueError:
        command = cmd.INSTALL_KEEP_SERVER
    return Fix(
        kind="command",
        text="Create a key at app.remembra.dev (API keys), then run this in your own terminal;"
        " it asks for the key at a hidden prompt:",
        command=command,
        runs_where="user_terminal",
        writes=("~/.remembra/credentials", "the remembra MCP entry of each agent it finds"),
        backup=True,
    )


def _dashboard_fix() -> Fix:
    return Fix(
        kind="dashboard",
        text="In the dashboard (API keys), check which projects and agent this key may use.",
        runs_where="dashboard",
    )


# ---------------------------------------------------------------------------
# Key and server
# ---------------------------------------------------------------------------


def _who(key: KeyCheck) -> str:
    return ", ".join(key.agents) if key.agents else "the hooks"


def rule_keys(sig: Signals) -> list[Finding]:
    out: list[Finding] = []
    for key in sig.keys:
        seen = [f"key from {key.source}" + (f" · used by {_who(key)} hooks" if key.agents else "")]
        recorded = key.recorded or {}
        if key.state == "missing":
            if not key.primary:
                continue
            out.append(
                Finding(
                    "KEY_MISSING",
                    BLOCKER,
                    None,
                    "No API key on this machine, so the hooks can't load or save handoffs.",
                    ("checked REMEMBRA_API_KEY, ~/.claude.json, ~/.codex/config.toml, ~/.remembra/credentials",),
                    fix=_key_fix(sig.server_url if sig.config.get("source") == "none" else key.url),
                    then=cmd.doctor(),
                    doc=DOC_SETUP,
                )
            )
        elif key.state == "rejected" or (key.state == "skipped" and recorded.get("state") == "rejected"):
            live = key.state == "rejected"
            if live and key.error:
                seen.append(f"{_host(key.url)} said: {key.error}")
            if not live:
                seen.append(f"the hooks last got HTTP 401 with this key {_ago(sig, recorded.get('ts'))}")
            out.append(
                Finding(
                    "KEY_REJECTED",
                    BLOCKER,
                    None,
                    f"Key rejected by {_host(key.url)} (HTTP 401): handoffs from {_who(key)} are not being saved.",
                    tuple(seen),
                    inferred=not live,
                    fix=_key_fix(key.url),
                    then=cmd.doctor(),
                    doc=DOC_SETUP,
                )
            )
        elif key.state == "refused" or (key.state == "skipped" and recorded.get("state") == "refused"):
            live = key.state == "refused"
            if live and key.error:
                seen.append(f"{_host(key.url)} said: {key.error}")
            if not live:
                seen.append(f"the hooks last got HTTP 403 with this key {_ago(sig, recorded.get('ts'))}")
            out.append(
                Finding(
                    "KEY_REFUSED",
                    BLOCKER,
                    None,
                    f"{_host(key.url)} refused the key (HTTP 403): it may not write for {_who(key)} or these projects.",
                    tuple(seen),
                    inferred=not live,
                    fix=_dashboard_fix(),
                    then=cmd.doctor(),
                    doc=DOC_SETUP,
                )
            )
        elif key.state == "firewall":
            if key.ray_id:
                seen.append(f"Cloudflare Ray ID {key.ray_id}")
            seen.append("the answer was an HTML page, not Remembra's JSON: the key was never checked")
            out.append(
                Finding(
                    "KEY_FIREWALL",
                    WARN,
                    None,
                    f"Blocked by the server's firewall: {_host(key.url)} answered HTTP 403 before Remembra saw the request.",
                    tuple(seen),
                    fix=Fix(
                        kind="none",
                        text=f"Nothing to change on this machine. If it lasts, send the Ray ID to {cmd.CONTACT_URL}.",
                        runs_where="none",
                    ),
                    then=cmd.doctor(),
                )
            )
        elif key.state == "unchecked" and key.error and "read budget" not in key.error:
            seen.append(key.error)
            out.append(
                Finding(
                    "SERVER_UNREACHABLE",
                    WARN,
                    None,
                    f"Can't get an answer from {_host(key.url)}. Handoffs queue on this machine and retry.",
                    tuple(seen),
                    fix=Fix(
                        kind="none",
                        text=f"Check the network, or the server URL {key.url} (from {key.source}).",
                        runs_where="none",
                    ),
                    then=cmd.doctor(),
                    doc=DOC_OUTBOX,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Outbox and closes
# ---------------------------------------------------------------------------


def _limits_line() -> str:
    from remembra.marshal.knowledge import plans_module

    plans = plans_module()
    free = plans.PLANS[plans.PlanTier.FREE]
    return (
        f"{free.display_name} allows {free.relay_burst_per_min} relay events a minute"
        f" and {free.max_unenriched_writes_per_day} unenriched writes a day"
    )


def _entry_line(sig: Signals, entry: OutboxEntry) -> str:
    what = f"HTTP {entry.http_status}" if entry.http_status else entry.error_class.replace("_", " ")
    tries = f"{entry.attempts} attempt{'s' if entry.attempts != 1 else ''}"
    return f"{entry.agent} · {tries} · {what} · queued {_ago(sig, entry.queued_ts)} · {entry.file}"


_QUEUE_CLASSES: dict[str, tuple[str, str]] = {
    "rejected": (BLOCKER, "the server rejected the key (HTTP 401)"),
    "refused": (BLOCKER, "the server refused them (HTTP 403)"),
    "firewall": (WARN, "blocked by the server's firewall (an HTML 403, not Remembra)"),
    "rate_limited": (WARN, "rate-limited (HTTP 429)"),
    "network": (WARN, "the server couldn't be reached"),
    "server": (WARN, "the server had an error (HTTP 5xx)"),
    "no_key": (WARN, "no key was set up when they were queued"),
    "other": (WARN, "the last try failed"),
}


def rule_outbox(sig: Signals) -> list[Finding]:
    out: list[Finding] = []
    queued = [e for e in sig.outbox if not e.held]
    accepted = {k.source for k in sig.keys if k.state == "accepted"}
    for klass, (severity, why) in _QUEUE_CLASSES.items():
        group = [e for e in queued if e.error_class == klass]
        if not group:
            continue
        n = len(group)
        what = f"{n} handoff{'s' if n != 1 else ''} queued on this machine: {why}."
        evidence = [_entry_line(sig, e) for e in group[:5]]
        if klass == "firewall":
            evidence.append("the answer was an HTML page from the firewall, not Remembra's JSON")
        elif group[-1].last_error:
            evidence.append(f"last error: {group[-1].last_error}")
        if group[-1].ray_id:
            evidence.append(f"Cloudflare Ray ID {group[-1].ray_id}")
        fix: Fix | None
        then = cmd.doctor()
        if klass == "rejected" and all(e.config_source in accepted for e in group):
            severity = WARN
            what = f"{n} handoff{'s' if n != 1 else ''} queued after an HTTP 401; the key is accepted now."
            fix = Fix(kind="none", text="Nothing to do: the next brief or close sends them.", runs_where="none")
        elif klass == "rejected":
            fix = _key_fix(group[-1].url)
        elif klass == "refused":
            fix = _dashboard_fix()
        elif klass == "rate_limited":
            evidence.append(_limits_line())
            fix = Fix(kind="none", text="Nothing to do: the next brief or close retries them.", runs_where="none")
        elif klass == "no_key" and any(k.state != "missing" for k in sig.keys if k.primary):
            severity = INFO
            what = f"{n} handoff{'s' if n != 1 else ''} queued before a key was set up; the next brief or close sends them."
            fix, then = None, ""
        else:
            fix = Fix(kind="none", text="Nothing to change here: the next brief or close retries them.", runs_where="none")
        out.append(Finding("OUTBOX_QUEUED", severity, None, what, tuple(evidence), fix=fix, then=then or None, doc=DOC_OUTBOX))
    for entry in sig.outbox:
        if not entry.held:
            continue
        out.append(
            Finding(
                "OUTBOX_HELD",
                WARN,
                None,
                f"A queued handoff from {entry.agent} is held and will never be sent from here.",
                (_entry_line(sig, entry), f"held: {entry.held}"),
                fix=Fix(
                    kind="command",
                    text="Drop that one file (or point REMEMBRA_URL back at the server it was queued for):",
                    command=cmd.remove_outbox_file(entry.file),
                    runs_where="agent_ok",
                    writes=(f"~/.remembra/relay/outbox/{entry.file}",),
                ),
                then=cmd.doctor(),
                doc=DOC_OUTBOX,
            )
        )
    return out


# Failures the next brief or close retries on its own (the handoff waits in the queue).
_RETRIED = frozenset({"network", "server", "rate_limited", "firewall"})


def _close_ts(slot: Any) -> float | None:
    if isinstance(slot, dict) and str(slot.get("command") or "").startswith("close"):
        return slot_ts(slot)
    return None


def rule_close_failing(sig: Signals) -> list[Finding]:
    out: list[Finding] = []
    for name in scope(sig):
        agent = sig.agents[name]
        failure, success = agent.last_failure, agent.last_success
        failed_at = _close_ts(failure)
        if failed_at is None or not isinstance(failure, dict):
            continue
        success_at = slot_ts(success)
        close_ok_at = _close_ts(success)
        if close_ok_at is not None and close_ok_at > failed_at:
            continue
        # A brief that succeeded after the failed close hides whether a close succeeded in between.
        inferred = success_at is not None and close_ok_at is None and success_at > failed_at
        error = str(failure.get("error") or "")
        status = failure.get("http_status") if isinstance(failure.get("http_status"), int) else None
        klass, ray = error_class(error, status)
        waiting = [e for e in sig.outbox if e.agent == name and not e.held]
        if klass in _RETRIED and waiting:
            continue  # queued and retried: OUTBOX_QUEUED already says so
        evidence = [f"~/.remembra/relay/status.json: {failure.get('command')} failed {_ago(sig, failed_at)}"]
        if error:
            evidence.append(f"error: {clean_text(error)}")
        if ray:
            evidence.append(f"Cloudflare Ray ID {ray}")
        key = sig.key_for(name)
        fix: Fix
        if klass in ("rejected", "no_key"):
            fix = _key_fix(key.url if key else None)
        elif klass == "refused":
            fix = _dashboard_fix()
        elif klass in _RETRIED:
            # Retried failures with nothing queued: a later run sent it, or the queue dropped it (14 days, 50 entries).
            fix = Fix(
                kind="none",
                text="Nothing from it is queued now; ~/.remembra/relay/relay.log says whether it was sent or dropped.",
                runs_where="none",
            )
        else:
            fix = Fix(
                kind="command",
                text=f"In a repository {name} worked in, close by hand to see the error:",
                command=cmd.close(name),
                runs_where="agent_ok",
            )
        label = {"rejected": "HTTP 401", "refused": "HTTP 403", "firewall": "the server's firewall", "rate_limited": "HTTP 429"}
        why = label.get(klass, klass.replace("_", " "))
        out.append(
            Finding(
                "CLOSE_FAILING",
                WARN if klass in _RETRIED else BLOCKER,
                name,
                f"{name}'s last close failed ({why}) {_ago(sig, failed_at)}; no close has worked since.",
                tuple(evidence),
                inferred=inferred,
                fix=fix,
                then=cmd.doctor(name),
                doc=DOC_OUTBOX,
            )
        )
    log = sig.close_log
    if log is not None and log.has_error and log.mtime is not None:
        detached = [sig.agents[n] for n in scope(sig) if sig.agents[n].detach_close]
        detached = [a for a in detached if a.detected or a.any_hooks] or detached
        newest_ok = max((t for a in detached if (t := _close_ts(a.last_success)) is not None), default=None)
        if detached and (newest_ok is None or newest_ok < log.mtime):
            out.append(
                Finding(
                    "CLOSE_FAILING",
                    BLOCKER,
                    None,
                    f"The last background close failed {_ago(sig, log.mtime)}; no close has worked since.",
                    (f"{log.path} (secrets redacted):", *log.tail),
                    fix=Fix(
                        kind="command",
                        text="In a repository the agent worked in, close by hand to see the error:",
                        command=cmd.close(detached[0].name),
                        runs_where="agent_ok",
                    ),
                    then=cmd.doctor(),
                    doc=DOC,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _format_of(path: str) -> str:
    return "TOML" if path.endswith(".toml") else "JSON"


def _connect_fix(agent: AgentSignals) -> Fix:
    return Fix(
        kind="command",
        text="Write the hooks (a backup of the file is kept):",
        command=cmd.connect([agent.name], unverified=not agent.verified),
        runs_where="agent_ok",
        writes=(agent.config_path,),
        backup=True,
    )


def rule_hooks(sig: Signals) -> list[Finding]:
    out: list[Finding] = []
    for name in scope(sig):
        agent = sig.agents[name]
        if not _in_play(agent, sig):
            if name in sig.wanted:
                spec = REGISTRY[name].spec
                places = [f"no {b} on PATH" for b in spec.detect_bins] + [f"no ~/{d}" for d in spec.detect_dirs]
                out.append(
                    Finding(
                        "NOT_DETECTED",
                        INFO,
                        name,
                        f"{agent.display} isn't on this machine.",
                        (", ".join(places),),
                        fix=Fix(kind="none", text=f"Install {agent.display} here, or skip it.", runs_where="none"),
                    )
                )
            continue
        if not agent.config_readable:
            out.append(
                Finding(
                    "CONFIG_UNREADABLE",
                    BLOCKER,
                    name,
                    f"{agent.config_path} can't be read as its format; connect can't check or write the hooks.",
                    (f"{agent.config_path}: not valid {_format_of(agent.config_path)}, or not readable",),
                    fix=Fix(
                        kind="none",
                        text="Repair the file (or restore a *.bak-relay-* backup next to it), then run connect again.",
                        runs_where="none",
                    ),
                    then=cmd.doctor(name),
                    doc=DOC_SETUP,
                )
            )
            continue
        if not agent.core_written:
            evidence = [
                f"{agent.config_path}: "
                + ("no remembra-relay hooks" if not agent.any_hooks else "missing " + ", ".join(agent.missing_events))
            ]
            if agent.mcp == "configured":
                evidence.append(f"{agent.mcp_path}: remembra MCP server set up (remembra-install ran)")
            if agent.backup:
                evidence.append(f"{agent.backup}: connect --apply wrote here once; the hooks were removed since")
            elif not agent.any_hooks:
                evidence.append(
                    "no *.bak-relay-* backup here: connect --apply never wrote this file (dry run only, or never run)"
                )
            if agent.verified:
                out.append(
                    Finding(
                        "HOOKS_NOT_WRITTEN",
                        BLOCKER,
                        name,
                        f"{agent.display}: hooks not written, so it gets no brief and leaves no handoff.",
                        tuple(evidence),
                        fix=_connect_fix(agent),
                        then=cmd.doctor(name),
                        doc=DOC_SETUP,
                    )
                )
            else:
                out.append(
                    Finding(
                        "UNVERIFIED_NOT_WRITTEN",
                        WARN,
                        name,
                        f"{agent.display}: hooks not written. Its adapter is built from its hook docs and has never run"
                        " against the real tool.",
                        (*evidence, f"adapter: {agent.notes}"),
                        fix=_connect_fix(agent),
                        then=cmd.doctor(name),
                        doc=DOC_SETUP,
                        caveat="Without hooks: add the AGENTS.md section (connect --agents-md PATH), or use the MCP tools"
                        " session_brief and close_session.",
                    )
                )
            continue
        if agent.missing_binary:
            out.append(
                Finding(
                    "HOOKS_STALE_COMMAND",
                    BLOCKER,
                    name,
                    f"{agent.display}: its hooks call {agent.missing_binary}, which no longer exists; every hook fails.",
                    (f"{agent.config_path}: hooks run `{agent.relay_prefix}`",),
                    fix=_connect_fix(agent),
                    then=cmd.doctor(name),
                    doc=DOC_SETUP,
                    caveat="Codex asks for trust again after its hooks change." if name == "codex" else None,
                )
            )
        elif agent.missing_events or agent.outdated:
            missing = agent.missing_events
            what = (
                f"{agent.display}: hooks from an older connect; {', '.join(missing)} missing."
                if missing
                else f"{agent.display}: hooks from an older connect; connect would update them."
            )
            out.append(
                Finding(
                    "HOOKS_INCOMPLETE",
                    WARN,
                    name,
                    what,
                    (f"{agent.config_path}: remembra-relay hooks on {', '.join(agent.present_events)}",),
                    fix=_connect_fix(agent),
                    then=cmd.doctor(name),
                    doc=DOC_SETUP,
                    caveat="Codex asks for trust again after its hooks change." if name == "codex" else None,
                )
            )
    return out


def _codex_ui_fix(events: list[str]) -> Fix:
    listed = ", ".join(dict.fromkeys(events))
    return Fix(
        kind="codex_ui",
        text=f"Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust {listed}.",
        runs_where="codex_ui",
        writes=("~/.codex/config.toml (Codex writes it)",),
    )


def rule_codex_trust(sig: Signals) -> list[Finding]:
    if "codex" not in scope(sig) or sig.codex.trust is None:
        return []
    trust = sig.codex.trust
    agent = sig.agents["codex"]
    if not agent.core_written or agent.missing_binary:
        return []
    hooks_line = f"{agent.config_path}: {len(trust.relay_hooks)} remembra-relay hooks"
    config = agent.config_path.replace("hooks.json", "config.toml")
    if trust.config_state == "unreadable":
        return [
            Finding(
                "CODEX_TRUST_UNCHECKED",
                INFO,
                "codex",
                "Codex hook trust unchecked: config.toml couldn't be read. Not counted as trusted.",
                (hooks_line, f"{config}: {trust.config_error or 'unreadable'}"),
                inferred=True,
                fix=_codex_ui_fix([t.hook.event for t in trust.per_hook]),
                then=cmd.doctor("codex"),
                doc=DOC_TRUST,
            )
        ]
    out: list[Finding] = []
    by_status: dict[str, list[str]] = {}
    for item in trust.per_hook:
        by_status.setdefault(item.status, []).append(item.hook.event)
    if by_status.get(codex_hooks.UNTRUSTED):
        events = by_status[codex_hooks.UNTRUSTED]
        state_line = (
            f"{config}: no [hooks.state] entries"
            if trust.state_entries == 0
            else f"{config}: no trust record for {', '.join(dict.fromkeys(events))}"
        )
        out.append(
            Finding(
                "CODEX_TRUST_MISSING",
                BLOCKER,
                "codex",
                "Codex: hooks written, trust not recorded. Codex skips untrusted hooks without a message.",
                (hooks_line, state_line),
                fix=_codex_ui_fix(events),
                then=cmd.doctor("codex"),
                doc=DOC_TRUST,
            )
        )
    if by_status.get(codex_hooks.MODIFIED):
        events = by_status[codex_hooks.MODIFIED]
        out.append(
            Finding(
                "CODEX_TRUST_STALE",
                BLOCKER,
                "codex",
                f"Codex: trust was recorded for an older version of {', '.join(dict.fromkeys(events))}; Codex skips a"
                " changed hook until it is trusted again.",
                (
                    hooks_line,
                    f"{config}: trusted_hash differs from the current hook",
                    "hash computed the way codex-cli " + " and ".join(codex_hooks.HASH_VERIFIED_WITH) + " compute it",
                ),
                inferred=True,
                fix=_codex_ui_fix(events),
                then=cmd.doctor("codex"),
                doc=DOC_TRUST,
            )
        )
    if by_status.get(codex_hooks.DISABLED):
        events = by_status[codex_hooks.DISABLED]
        out.append(
            Finding(
                "CODEX_HOOK_DISABLED",
                BLOCKER,
                "codex",
                f"Codex: {', '.join(dict.fromkeys(events))} is turned off in Codex.",
                (hooks_line, f"{config}: enabled = false for {', '.join(dict.fromkeys(events))}"),
                fix=Fix(
                    kind="codex_ui",
                    text="Turn it back on in Codex Settings > Hooks, or with /hooks in the Codex CLI.",
                    runs_where="codex_ui",
                    writes=("~/.codex/config.toml (Codex writes it)",),
                ),
                then=cmd.doctor("codex"),
                doc=DOC_TRUST,
            )
        )
    if by_status.get(codex_hooks.UNCHECKED):
        events = by_status[codex_hooks.UNCHECKED]
        out.append(
            Finding(
                "CODEX_TRUST_UNCHECKED",
                INFO,
                "codex",
                f"Codex: trust of {', '.join(dict.fromkeys(events))} unchecked; the hook has fields this check doesn't hash.",
                (hooks_line,),
                inferred=True,
                fix=_codex_ui_fix(events),
                then=cmd.doctor("codex"),
                doc=DOC_TRUST,
            )
        )
    return out


def rule_codex_automations(sig: Signals) -> list[Finding]:
    codex = sig.codex
    if "codex" not in scope(sig) or codex.automations_7d == 0:
        return []
    agent = sig.agents["codex"]
    trust = codex.trust
    hooks_run = (
        agent.core_written
        and trust is not None
        and bool(trust.per_hook)
        and all(t.status in (codex_hooks.TRUSTED, codex_hooks.UNCHECKED) for t in trust.per_hook)
    )
    if not hooks_run:
        return []
    n = codex.automations_7d
    runs = f"{n} Codex automation run{'s' if n != 1 else ''} in 7 days"
    evidence = [f"~/.codex/sessions: {runs} (thread_source automation, {codex.rollouts_scanned} rollouts read)"]
    if codex.skip_supported and not codex.include_automations:
        if codex.skipped_logged:
            evidence.append(f"~/.remembra/relay/relay.log: {codex.skipped_logged} skipped automation sessions")
        return [
            Finding(
                "CODEX_AUTOMATIONS",
                INFO,
                "codex",
                f"{runs}; the relay skips them (no brief, no handoff).",
                tuple(evidence),
            )
        ]
    if codex.skip_supported:
        return [
            Finding(
                "CODEX_AUTOMATIONS",
                INFO,
                "codex",
                f"{runs}; each leaves a handoff because REMEMBRA_RELAY_INCLUDE_AUTOMATIONS is set here.",
                tuple(evidence),
            )
        ]
    return [
        Finding(
            "CODEX_AUTOMATIONS",
            WARN,
            "codex",
            f"{runs}, and this install runs the relay hooks for each: every run gets a brief and leaves a handoff"
            " that buries your own sessions.",
            (*evidence, f"remembra {sig.version} has no automation skip"),
            fix=Fix(
                kind="command",
                text="Upgrade remembra; doctor then says whether the new install skips automation runs:",
                command=cmd.PIPX_INSTALL,
                runs_where="agent_ok",
                writes=("the pipx environment of remembra",),
            ),
            then=cmd.doctor("codex"),
            doc=DOC,
        )
    ]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def rule_namespace(sig: Signals) -> list[Finding]:
    out: list[Finding] = []
    if sig.namespace is not None:
        project, where = sig.namespace
        out.append(
            Finding(
                "LEGACY_NAMESPACE",
                INFO,
                None,
                f"One namespace: every repository this machine hasn't bound yet joins project {project}.",
                (f"project {project} configured in {where}",),
                fix=Fix(
                    kind="none",
                    text="Keep it to share one project. To give a repository its own, run in it:"
                    " remembra-relay resolve --project <name> --bind.",
                    runs_where="none",
                ),
                doc=DOC_PROJECT,
            )
        )
    distinct = sorted(set(sig.mcp_projects.values()))
    if len(distinct) > 1:
        pairs = [f"{agent} reads {project}" for agent, project in sorted(sig.mcp_projects.items())]
        out.append(
            Finding(
                "MCP_PROJECT_SPLIT",
                WARN,
                None,
                "Your agents' MCP servers use different projects, so their memory tools don't see each other's memories.",
                (" · ".join(pairs),),
                fix=Fix(
                    kind="none",
                    text="Use one REMEMBRA_PROJECT in every agent's remembra MCP env, or remove it from all of them.",
                    runs_where="none",
                ),
                then=cmd.doctor(),
                doc=DOC_PROJECT,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Trail
# ---------------------------------------------------------------------------


def _hooks_should_run(sig: Signals, agent: AgentSignals) -> bool:
    if not agent.core_written or agent.missing_binary:
        return False
    if agent.name == "codex":
        trust = sig.codex.trust
        return trust is not None and bool(trust.per_hook) and all(t.status == codex_hooks.TRUSTED for t in trust.per_hook)
    return True


def rule_server_entries(sig: Signals) -> list[Finding]:
    server = sig.server
    if server is None:
        return []
    out: list[Finding] = []
    for name in scope(sig):
        agent = sig.agents[name]
        if not _in_play(agent, sig) or not _hooks_should_run(sig, agent):
            continue
        if server.entries_7d.get(name, 0) > 0:
            continue
        pickups = server.pickups_by.get(name, 0)
        evidence = [f"trail 7d: 0 handoffs or checkpoints from {name}"]
        last = server.last_active.get(name)
        evidence.append(f"last entry from {name}: {_ago(sig, last)}" if last else f"no entry from {name} ever")
        if pickups:
            evidence.append(f"{name} picked up {pickups} handoff{'s' if pickups != 1 else ''} in the last {TRAIL_WINDOW} entries")
            what = f"{name} reads briefs but no handoff from it reached Remembra in 7 days: its close isn't arriving."
            inferred = False
        else:
            waiting = server.waiting_for(name) if server.trail_read else 0
            if waiting:
                evidence.append(f"{waiting} handoff{'s' if waiting != 1 else ''} from other agents waited for it")
            what = f"{name}: hooks written, but nothing from {name} reached Remembra in 7 days."
            inferred = True
        out.append(
            Finding(
                "SERVER_NO_ENTRIES",
                WARN,
                name,
                what,
                tuple(evidence),
                inferred=inferred,
                fix=Fix(
                    kind="command",
                    text=f"End one {agent.display} session in a repository, then run:",
                    command=cmd.doctor(name),
                    runs_where="agent_ok",
                ),
                doc=DOC_DOCTOR,
                caveat=None if pickups else f"Also possible: {name} hasn't ended a session since the hooks were written.",
            )
        )
    return out


def rule_stale_checkpoint(sig: Signals) -> list[Finding]:
    server = sig.server
    if server is None:
        return []
    out: list[Finding] = []
    for name in scope(sig):
        last = server.last_entry.get(name)
        if not last or last[0] != "checkpoint":
            continue
        minutes = (sig.now - last[1]) / 60
        if minutes <= 60:
            continue
        out.append(
            Finding(
                "STALE_CHECKPOINT",
                WARN,
                name,
                f"{name}'s last session stopped without a handoff (its last entry is a checkpoint, {_ago(sig, last[1])}).",
                (f"trail: newest {name} entry is a checkpoint from {_ago(sig, last[1])}, with no handoff after it",),
                fix=Fix(
                    kind="command",
                    text="In the repository it worked in, write the handoff now:",
                    command=cmd.close(name),
                    runs_where="agent_ok",
                ),
                then=cmd.doctor(name),
                doc=DOC,
            )
        )
    return out


RULES: tuple[Rule, ...] = (
    Rule("KEY", rule_keys),
    Rule("OUTBOX", rule_outbox),
    Rule("CLOSE_FAILING", rule_close_failing),
    Rule("PROJECTS", rule_namespace),
    Rule("HOOKS", rule_hooks),
    Rule("CODEX_TRUST", rule_codex_trust),
    Rule("CODEX_AUTOMATIONS", rule_codex_automations),
    Rule("SERVER_NO_ENTRIES", rule_server_entries),
    Rule("STALE_CHECKPOINT", rule_stale_checkpoint),
)

RULE_IDS: tuple[str, ...] = (
    "KEY_MISSING",
    "KEY_REJECTED",
    "KEY_REFUSED",
    "KEY_FIREWALL",
    "SERVER_UNREACHABLE",
    "OUTBOX_QUEUED",
    "OUTBOX_HELD",
    "CLOSE_FAILING",
    "LEGACY_NAMESPACE",
    "MCP_PROJECT_SPLIT",
    "NOT_DETECTED",
    "CONFIG_UNREADABLE",
    "HOOKS_NOT_WRITTEN",
    "UNVERIFIED_NOT_WRITTEN",
    "HOOKS_STALE_COMMAND",
    "HOOKS_INCOMPLETE",
    "CODEX_TRUST_MISSING",
    "CODEX_TRUST_STALE",
    "CODEX_HOOK_DISABLED",
    "CODEX_TRUST_UNCHECKED",
    "CODEX_AUTOMATIONS",
    "SERVER_NO_ENTRIES",
    "STALE_CHECKPOINT",
)


def evaluate(sig: Signals) -> list[Finding]:
    """All findings, machine-wide ones first, then per agent in registry order."""
    findings = [finding for rule in RULES for finding in rule.applies(sig)]
    order = {name: i for i, name in enumerate(REGISTRY)}
    return sorted(findings, key=lambda f: (f.agent is not None, order.get(f.agent or "", -1)))
