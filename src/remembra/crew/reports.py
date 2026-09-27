"""Crew reports: the completion gate with per-item source labels, review, waivers and the report invariant (WP-6, §5.6).

Reports are built by ``remembra-crew report T-n`` (hook/CLI session) or ``crew_report``
(MCP session) and submitted to ``POST /tasks/{id}/reports``. The server gate:

* collects evidence **since the task started**: checkpoints and client
  ``activity.*`` events of every session that owned the task (current owner and
  earlier baton holders), footprints, then the report body;
* labels every item by where it came from — ``relay-cli`` (observed by a hook,
  crewd or the CLI: checkpoints and events of hook/CLI sessions, and the body of a
  report a hook/CLI session submits), ``agent-declared`` (anything an MCP session
  says) or ``server-verified`` (a server live check);
* evaluates each required criterion: ``met`` when the evidence resolves (a matching
  observed test/command whose latest run passed **after the last change to the
  task's zone files**, a commit sha, a file, a 2xx live check), ``waived`` when a
  human waived it, else ``unmet`` or ``unknown``;
* ``verdict = complete`` when every required criterion is met or waived, the push
  gate holds (``deploy_gate.require_pushed`` ⇒ pushed; ``require_live_check`` ⇒ a
  server-verified live check passed) and no observed test is failing; else ``partial``;
* ``complete ∧ no reviewer ∧ (¬strict_reports ∨ every met item is relay-cli or
  server-verified)`` → ``task.done`` + ``report.accepted``; otherwise → ``review`` and a
  Needs-you ``review_report`` item.

The receipt seal reads e.g. ``tests ✓ (observed) · pushed ✓ (observed) · live ✓
(server-verified)``; agent-declared items read "self-reported".

Nothing ever executes a criterion's ``match``; ``deploy`` URLs are fetched only by
:class:`remembra.crew.livecheck.LiveChecker` (outside the database transaction, one
check per criterion per report).

The invariant (§5.6): every task in ``done``, ``stalled`` or ``cancelled`` after it
reached ``in_progress`` has exactly one current report (the partial unique index
guarantees at most one everywhere). :func:`check_report_invariant` asserts it;
:func:`enforce_report_invariant` opens a Needs-you ``report_invariant`` item per
violation; :func:`report_invariant_loop` runs it nightly (startup hook ``crew.report_invariant``).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog

from remembra.crew import schemas
from remembra.crew.events import Actor, CrewEventLog
from remembra.crew.livecheck import LiveChecker, LiveCheckResult, normalise_host
from remembra.crew.redact import outbound
from remembra.crew.store import dumps, loads, new_id, now_iso, parse_iso
from remembra.crew.tasks import (
    Caller,
    CrewServiceError,
    TaskService,
    crew_settings,
    fetchall,
    fetchone,
    insert_report,
    load_task,
    open_inbox_item,
    report_view,
    resolve_inbox_items,
    session_channel_source,
    task_ref,
    waivers_of,
)

log = structlog.get_logger(__name__)

REPORTABLE_STATUSES: Final = ("in_progress", "blocked", "review")
STRONG_SOURCES: Final = frozenset({"relay-cli", "server-verified"})
SOURCE_LABEL: Final[Mapping[str, str]] = {
    "relay-cli": "observed",
    "server-verified": "server-verified",
    "agent-declared": "self-reported",
    "server-inferred": "inferred",
}
SEAL_GROUPS: Final[Mapping[str, str]] = {
    "test": "tests",
    "command": "commands",
    "commit": "commits",
    "file": "files",
    "deploy": "live",
    "manual": "manual",
}
PUSHED_EVIDENCE_ID: Final = "@pushed"
INVARIANT_RUN_AT_UTC: Final = (3, 47)

LiveCheckerFactory = Callable[[Sequence[str]], LiveChecker]
Scrubber = Callable[[str], str]


def _err(status: int, error: str, message: str) -> CrewServiceError:
    return CrewServiceError(status, error, message)


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    kind: str  # test | command | commit | push | file
    value: str  # fingerprint, sha, upstream or repo-relative path
    passed: bool | None
    at: datetime
    source: str  # relay-cli | agent-declared
    order: int = 0


@dataclass
class Evidence:
    observations: list[Observation] = field(default_factory=list)
    declared: dict[str, dict[str, Any]] = field(default_factory=dict)  # criterion id -> {met, note}
    last_zone_change: datetime | None = None
    files: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    def add(self, obs: Observation) -> None:
        self.observations.append(Observation(obs.kind, obs.value, obs.passed, obs.at, obs.source, len(self.observations)))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _test_passed(item: Mapping[str, Any]) -> bool:
    passed, failed, exit_code = item.get("passed"), _int(item.get("failed")), item.get("exit_code")
    if passed is False or (failed is not None and failed > 0) or exit_code not in (None, 0):
        return False
    if passed is True:
        return True
    count = _int(passed)
    return count is not None and count > 0 or (count == 0 and failed == 0 and exit_code == 0)


def _fingerprint(item: Mapping[str, Any]) -> str | None:
    for key in ("fingerprint", "command", "cmd"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _shas(items: Any) -> list[str]:
    out: list[str] = []
    for c in items or []:
        sha = c.get("sha") if isinstance(c, Mapping) else c
        if isinstance(sha, str) and sha.strip():
            out.append(sha.strip().lower())
    return out


def _paths(facts: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("files_changed", "uncommitted_files", "dirty", "files"):
        for p in facts.get(key) or []:
            if isinstance(p, str) and schemas.is_path_rel(p):
                out.append(p.removeprefix("./"))
    return out


def facts_observations(evidence: Evidence, facts: Mapping[str, Any], at: datetime, source: str) -> None:
    """Add what one checkpoint's facts (or a report body) show."""
    for t in facts.get("tests") or []:
        if isinstance(t, Mapping) and (fp := _fingerprint(t)):
            evidence.add(Observation("test", fp, _test_passed(t), at, source))
    for c in facts.get("commands") or []:
        if isinstance(c, Mapping) and isinstance(c.get("fingerprint"), str):
            code = c.get("exit_code")
            evidence.add(Observation("command", str(c["fingerprint"]), code in (0, None), at, source))
    for sha in _shas(facts.get("commits")):
        evidence.add(Observation("commit", sha, True, at, source))
    for path in _paths(facts):
        evidence.add(Observation("file", path, True, at, source))
    unpushed = _int(facts.get("unpushed_commits"))
    if facts.get("pushed") is True or (unpushed == 0 and (facts.get("commits") or facts.get("head") or facts.get("head_commit"))):
        evidence.add(Observation("push", str(facts.get("upstream") or "upstream"), True, at, source))


def argvs_of(fingerprint: str) -> list[list[str]]:
    """Normalised argv per segment of a (redacted) command fingerprint; never executed."""
    from remembra.crew.gatecore import parse_bash_full

    out = [list(argv) for _cwd, argv in parse_bash_full(fingerprint).argvs if argv]
    plain = fingerprint.split()
    if plain and plain not in out:
        out.append(plain)
    return out


def fingerprint_matches(pattern: str, fingerprint: str) -> bool:
    return any(schemas.command_pattern_matches(pattern, argv) for argv in argvs_of(fingerprint))


async def _owner_sessions(conn: Any, crew_id: str, task: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    ids: set[str] = set()
    if task.get("owner_session_id"):
        ids.add(str(task["owner_session_id"]))
    for b in await fetchall(
        conn, "SELECT from_session, to_session FROM crew_batons WHERE crew_id = ? AND task_id = ?", (crew_id, task["id"])
    ):
        ids.update(str(x) for x in (b["from_session"], b["to_session"]) if x)
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = await fetchall(conn, f"SELECT * FROM crew_sessions WHERE crew_id = ? AND id IN ({marks})", (crew_id, *sorted(ids)))  # noqa: S608
    return {str(r["id"]): r for r in rows}


async def _zone_ids_with_descendants(conn: Any, crew_id: str, zone_ids: Sequence[str]) -> list[dict[str, Any]]:
    zones = await fetchall(conn, "SELECT * FROM crew_zones WHERE crew_id = ? AND archived_at IS NULL", (crew_id,))
    by_parent: dict[str, list[dict[str, Any]]] = {}
    by_id = {str(z["id"]): z for z in zones}
    for z in zones:
        if z.get("parent_id"):
            by_parent.setdefault(str(z["parent_id"]), []).append(z)
    out: dict[str, dict[str, Any]] = {}
    stack = [by_id[z] for z in zone_ids if z in by_id]
    while stack:
        z = stack.pop()
        if z["id"] in out:
            continue
        out[str(z["id"])] = z
        stack.extend(by_parent.get(str(z["id"]), []))
    return list(out.values())


def in_zones(path: str, zones: Sequence[Mapping[str, Any]]) -> bool:
    from remembra.crew.gatecore import glob_match

    for z in zones:
        includes = loads(z.get("include_globs"), []) or []
        excludes = loads(z.get("exclude_globs"), []) or []
        if any(glob_match(g, path) for g in includes) and not any(glob_match(g, path) for g in excludes):
            return True
    return False


async def collect_evidence(
    conn: Any,
    crew_id: str,
    task: Mapping[str, Any],
    *,
    body: Mapping[str, Any],
    body_source: str,
    now: datetime,
) -> tuple[Evidence, list[dict[str, Any]]]:
    """Every observation relevant to ``task`` since it started, plus the report body; returns (evidence, task zones)."""
    evidence = Evidence()
    since = task.get("started_at") or task.get("created_at")
    sessions = await _owner_sessions(conn, crew_id, task)
    session_ids = sorted(sessions)
    marks = ", ".join("?" for _ in session_ids) or "NULL"
    rows = await fetchall(
        conn,
        f"SELECT session_id, facts, facts_source, created_at FROM crew_checkpoints WHERE crew_id = ?"  # noqa: S608
        f" AND (task_id = ? OR session_id IN ({marks})) AND created_at >= ?"
        " AND facts_source IN ('relay-cli','agent-declared') ORDER BY created_at, rowid",
        (crew_id, task["id"], *session_ids, since),
    )
    for r in rows:
        facts = loads(r["facts"], {}) or {}
        at = _ts(r["created_at"]) or now
        if isinstance(facts, Mapping):
            facts_observations(evidence, facts, at, str(r["facts_source"]))
    if session_ids:
        events = await fetchall(
            conn,
            f"SELECT session_id, type, payload, ts FROM crew_events WHERE crew_id = ? AND origin = 'client'"  # noqa: S608
            f" AND type IN ('activity.commit','activity.push','activity.test_verdict_changed')"
            f" AND session_id IN ({marks}) AND ts >= ? ORDER BY seq",
            (crew_id, *session_ids, since),
        )
        for e in events:
            payload = loads(e["payload"], {}) or {}
            at = _ts(e["ts"]) or now
            source = session_channel_source(sessions.get(str(e["session_id"])))
            if e["type"] == "activity.commit" and isinstance(payload.get("sha"), str):
                evidence.add(Observation("commit", payload["sha"].lower(), True, at, source))
                for p in payload.get("files") or []:
                    if isinstance(p, str):
                        evidence.add(Observation("file", p, True, at, source))
            elif e["type"] == "activity.push":
                evidence.add(Observation("push", str(payload.get("upstream") or "upstream"), True, at, source))
            elif e["type"] == "activity.test_verdict_changed" and isinstance(payload.get("fingerprint"), str):
                evidence.add(Observation("test", payload["fingerprint"], payload.get("to") == "pass", at, source))
        prints = await fetchall(
            conn,
            f"SELECT session_id, path, last_at FROM crew_footprints WHERE crew_id = ? AND session_id IN ({marks})",  # noqa: S608
            (crew_id, *session_ids),
        )
        for f in prints:
            at = _ts(f["last_at"]) or now
            if at >= (_ts(since) or at):
                evidence.add(Observation("file", str(f["path"]), True, at, "relay-cli"))
    zones = await _zone_ids_with_descendants(conn, crew_id, loads(task.get("zone_ids"), []) or [])
    zone_id_set = {str(z["id"]) for z in zones}
    if zone_id_set:
        changes = await fetchall(conn, "SELECT zone_ids, last_at FROM crew_footprints WHERE crew_id = ?", (crew_id,))
        latest: datetime | None = None
        for c in changes:
            if zone_id_set & set(loads(c["zone_ids"], []) or []):
                changed_at = _ts(c["last_at"])
                if changed_at is not None and (latest is None or changed_at > latest):
                    latest = changed_at
        evidence.last_zone_change = latest
    # The body: tests and commits count at submission time, labelled by the submitting channel.
    facts_observations(evidence, {"tests": body.get("tests") or [], "commits": body.get("commits") or []}, now, body_source)
    for item in body.get("criteria_evidence") or []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            evidence.declared[item["id"]] = {"met": item.get("met") is True, "note": item.get("note")}
    evidence.commits = list(dict.fromkeys(o.value for o in evidence.observations if o.kind == "commit"))
    evidence.files = list(dict.fromkeys(o.value for o in evidence.observations if o.kind == "file"))
    return evidence, zones


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    verdict: str  # complete | partial
    criteria: list[dict[str, Any]]
    pushed: bool
    pushed_source: str | None
    failing_tests: list[dict[str, Any]]
    tests: list[dict[str, Any]]
    live: list[dict[str, Any]]
    auto_accept: bool
    reasons: list[str]
    seal: str


def _latest(observations: Iterable[Observation]) -> Observation | None:
    best: Observation | None = None
    for o in observations:
        if best is None or (o.at, o.order) >= (best.at, best.order):
            best = o
    return best


def _prefer_strong(observations: list[Observation]) -> list[Observation]:
    strong = [o for o in observations if o.source in STRONG_SOURCES]
    return strong or observations


def evaluate_criterion(
    criterion: Mapping[str, Any],
    evidence: Evidence,
    live: Mapping[str, LiveCheckResult],
    waivers: Mapping[str, Any],
) -> dict[str, Any]:
    cid, kind, match = str(criterion["id"]), str(criterion["kind"]), criterion.get("match")
    out: dict[str, Any] = {"id": cid, "kind": kind, "required": bool(criterion.get("required", True))}
    if cid in waivers:
        return {**out, "status": "waived", "source": None, "detail": "waived by a human"}
    declared = evidence.declared.get(cid)

    def fallback(detail: str) -> dict[str, Any]:
        if declared is not None:
            return {**out, "status": "met" if declared["met"] else "unmet", "source": "agent-declared", "detail": "declared"}
        return {**out, "status": "unknown", "source": None, "detail": detail}

    if kind in ("test", "command"):
        kinds = ("test",) if kind == "test" else ("test", "command")
        matching = [o for o in evidence.observations if o.kind in kinds and match and fingerprint_matches(str(match), o.value)]
        if not matching:
            return fallback("no matching run observed")
        last = _latest(_prefer_strong(matching))
        assert last is not None
        if not last.passed:
            return {**out, "status": "unmet", "source": last.source, "detail": "latest matching run failed"}
        if evidence.last_zone_change is not None and last.source == "relay-cli" and last.at < evidence.last_zone_change:
            return {**out, "status": "unknown", "source": last.source, "detail": "passed before the last change to zone files"}
        return {**out, "status": "met", "source": last.source, "detail": "latest matching run passed"}
    if kind == "commit":
        commits = [
            o for o in evidence.observations if o.kind == "commit" and (not match or o.value.startswith(str(match).lower()))
        ]
        if not commits:
            return fallback("no matching commit")
        best = _prefer_strong(commits)[0]
        return {**out, "status": "met", "source": best.source, "detail": "commit present"}
    if kind == "file":
        want = str(match or "").removeprefix("./")
        files = [o for o in evidence.observations if o.kind == "file" and o.value == want]
        if not files:
            return fallback("file not observed")
        best = _prefer_strong(files)[0]
        return {**out, "status": "met", "source": best.source, "detail": "file present"}
    if kind == "deploy":
        res = live.get(cid)
        if res is None:
            return fallback("host not in live_check_domains")
        if res.error in ("host_not_allowed", "scheme_not_https", "invalid_url", "port_not_allowed", "userinfo_not_allowed"):
            return fallback(f"live check refused: {res.error}")
        if res.ok:
            return {**out, "status": "met", "source": "server-verified", "detail": f"live check {res.status}"}
        return {**out, "status": "unmet", "source": "server-verified", "detail": f"live check {res.status or res.error}"}
    return fallback("manual criterion")


def receipt_seal(criteria: Sequence[Mapping[str, Any]], pushed: bool, pushed_source: str | None, require_pushed: bool) -> str:
    """``tests ✓ (observed) · pushed ✓ (observed) · live ✓ (server-verified)`` (self-reported for agent-declared)."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for c in criteria:
        groups.setdefault(SEAL_GROUPS.get(str(c.get("kind")), "other"), []).append(c)
    parts: list[str] = []
    for name in ("tests", "commands", "commits", "files", "manual"):
        if name in groups:
            parts.append(_seal_part(name, groups[name]))
    if pushed or require_pushed:
        parts.append(f"pushed ✓ ({SOURCE_LABEL.get(pushed_source or '', 'observed')})" if pushed else "pushed ✗")
    if "live" in groups:
        parts.append(_seal_part("live", groups["live"]))
    return " · ".join(parts)


def _seal_part(name: str, items: Sequence[Mapping[str, Any]]) -> str:
    statuses = [str(i["status"]) for i in items]
    if any(s == "unmet" for s in statuses):
        return f"{name} ✗"
    if all(s == "waived" for s in statuses):
        return f"{name} waived"
    if all(s in ("met", "waived") for s in statuses):
        sources = [str(i.get("source")) for i in items if i["status"] == "met"]
        weakest = (
            "agent-declared" if "agent-declared" in sources else ("relay-cli" if "relay-cli" in sources else "server-verified")
        )
        return f"{name} ✓ ({SOURCE_LABEL[weakest]})"
    return f"{name} ?"


def run_gate(
    task: Mapping[str, Any],
    settings: Mapping[str, Any],
    evidence: Evidence,
    live: Mapping[str, LiveCheckResult],
) -> GateResult:
    acceptance = [c for c in loads(task.get("acceptance"), []) or [] if isinstance(c, Mapping)]
    waivers = waivers_of(acceptance)
    results = [evaluate_criterion(c, evidence, live, waivers) for c in acceptance]
    reasons: list[str] = []
    for r in results:
        if r["required"] and r["status"] not in ("met", "waived"):
            reasons.append(f"criterion {r['id']} {r['status']}")
    pushes = [o for o in evidence.observations if o.kind == "push"]
    declared_push = evidence.declared.get(PUSHED_EVIDENCE_ID)
    pushed_source: str | None = None
    if pushes:
        pushed_source = _prefer_strong(pushes)[0].source
    elif declared_push and declared_push["met"]:
        pushed_source = "agent-declared"
    pushed = pushed_source is not None
    gate = settings.get("deploy_gate") or {}
    if gate.get("require_pushed") and not pushed:
        reasons.append("not pushed")
    live_ok = any(r["kind"] == "deploy" and r["status"] == "met" and r["source"] == "server-verified" for r in results)
    if gate.get("require_live_check") and not live_ok:
        reasons.append("no passing server live check")
    latest: dict[str, Observation] = {}
    for o in evidence.observations:
        if o.kind == "test":
            prev = latest.get(o.value)
            if (
                prev is None
                or (o.source in STRONG_SOURCES) > (prev.source in STRONG_SOURCES)
                or ((o.source in STRONG_SOURCES) == (prev.source in STRONG_SOURCES) and (o.at, o.order) >= (prev.at, prev.order))
            ):
                latest[o.value] = o
    tests = [
        {"fingerprint": fp[:256], "passed": bool(o.passed), "source": o.source, "at": now_iso(o.at)} for fp, o in latest.items()
    ][:50]
    failing = [t for t in tests if not t["passed"]]
    if failing:
        reasons.append(f"{len(failing)} failing test run(s)")
    verdict = "partial" if reasons else "complete"
    strict = bool(settings.get("strict_reports", True))
    met_sources = [r["source"] for r in results if r["status"] == "met"]
    if gate.get("require_pushed") and pushed:
        met_sources.append(pushed_source)
    strong = all(s in STRONG_SOURCES for s in met_sources)
    auto = verdict == "complete" and not task.get("reviewer") and (not strict or strong)
    live_list = [
        {"criterion_id": cid, "url": r.url, "ok": r.ok, "status": r.status, "error": r.error, "ip": r.ip}
        for cid, r in live.items()
    ]
    seal = receipt_seal(results, pushed, pushed_source, bool(gate.get("require_pushed")))
    return GateResult(verdict, results, pushed, pushed_source, failing, tests, live_list, auto, reasons, seal)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def default_live_checker(hosts: Sequence[str]) -> LiveChecker:
    return LiveChecker(hosts)


@dataclass
class ReportResult:
    report: dict[str, Any]
    task: dict[str, Any] | None
    seq: int | None
    outcome: str  # accepted | review | replay | waived | approved | rejected
    gate: GateResult | None = None


def report_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    view = report_view(row)
    criteria = loads(row.get("criteria"), []) or []
    deploy = loads(row.get("deploy"), {}) or {}
    view.update(
        {
            "crew_id": row["crew_id"],
            "criteria_detail": criteria,
            "commits": loads(row.get("commits"), []) or [],
            "files": loads(row.get("files"), []) or [],
            "out_of_zone_files": loads(row.get("out_of_zone_files"), []) or [],
            "tests": loads(row.get("tests"), []) or [],
            "deploy": deploy,
            "sections": loads(row.get("sections"), {}) or {},
            "summary": row.get("summary"),
            "grounding": loads(row.get("grounding"), None),
            "facts_hash": row.get("facts_hash"),
            "reviewed_by": row.get("reviewed_by"),
            "review_note": row.get("review_note"),
            "seal": deploy.get("seal") if isinstance(deploy, Mapping) else None,
            "created_at": row["created_at"],
        }
    )
    return view


class ReportService:
    """Report submission, review, waivers and the invariant (see the module docstring)."""

    def __init__(
        self,
        events: CrewEventLog,
        tasks: TaskService,
        *,
        live_checker: LiveCheckerFactory = default_live_checker,
        pii: Scrubber | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.events = events
        self.tasks = tasks
        self.live_checker = live_checker
        self.pii = pii
        self.clock = clock or (lambda: datetime.now(UTC))

    async def list_reports(self, crew_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = await fetchall(
            self.events.db.conn,
            "SELECT * FROM crew_reports WHERE crew_id = ? AND task_id = ? ORDER BY created_at, rowid",
            (crew_id, task_id),
        )
        return [report_detail(r) for r in rows]

    async def _live_checks(self, task: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, LiveCheckResult]:
        """One server live check per deploy criterion (hosts in ``live_check_domains`` only), outside any transaction."""
        allowed = [h for h in settings.get("live_check_domains") or []]
        acceptance = [c for c in loads(task.get("acceptance"), []) or [] if isinstance(c, Mapping)]
        waivers = waivers_of(acceptance)
        targets: dict[str, str] = {}
        for c in acceptance:
            if c.get("kind") != "deploy" or not c.get("url") or c["id"] in waivers:
                continue
            host = normalise_host(str(c["url"]).split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].split("@")[-1])
            if host and host in allowed:
                targets[str(c["id"])] = str(c["url"])
        if not targets:
            return {}
        checker = self.live_checker(allowed)
        results = await asyncio.gather(*(checker.check(url) for url in targets.values()))
        return dict(zip(targets, results, strict=True))

    async def submit(self, crew_id: str, task_id: str, caller: Caller, body: Mapping[str, Any]) -> ReportResult:
        """``POST /tasks/{id}/reports``: run the gate and finish the task or send it to review."""
        session = caller.session
        if session is None:
            raise _err(403, "session_required", "Reports are submitted by the session that owns the task.")
        full = {"sections": {}, "criteria_evidence": [], "commits": [], "tests": [], "release": True, **dict(body)}
        errors = schemas.validate(full, schemas.REQUEST_SHAPES["Report"], "$")
        if errors:
            raise _err(422, "invalid_report", "; ".join(errors[:3]))
        if schemas.json_size(full) > schemas.MAX_REPORT_BYTES:
            raise _err(413, "report_too_large", f"reports are limited to {schemas.MAX_REPORT_BYTES} bytes")
        if full.get("session_id") not in (None, session["id"]):
            raise _err(403, "session_mismatch", "The session token does not belong to session_id.")
        clean = outbound("report", full, pii=self.pii)
        assert isinstance(clean, dict)
        conn = self.events.db.conn
        task = await load_task(conn, crew_id, task_id)
        settings = await crew_settings(conn, crew_id)
        live = await self._live_checks(task, settings)
        digest = hashlib.sha256(schemas.canonical_json(clean)).hexdigest()
        body_source = session_channel_source(session)
        async with self.events.transaction() as tx:
            existing = await fetchone(
                tx.conn,
                "SELECT * FROM crew_reports WHERE task_id = ? AND session_id = ? AND facts_hash = ?",
                (task_id, session["id"], digest),
            )
            if existing is not None:
                return ReportResult(report_detail(existing), None, None, "replay")
            task = await load_task(tx.conn, crew_id, task_id)
            if task.get("owner_session_id") != session["id"]:
                raise _err(403, "not_task_owner", f"Only the session that owns {task_ref(task)} reports on it.")
            if task["status"] not in REPORTABLE_STATUSES:
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}; start it before reporting.")
            now = self.clock()
            evidence, zones = await collect_evidence(tx.conn, crew_id, task, body=clean, body_source=body_source, now=now)
            gate = run_gate(task, settings, evidence, live)
            out_of_zone = [p for p in evidence.files if zones and not in_zones(p, zones)][:200]
            grounding = _grounding(clean.get("summary"), evidence, gate)
            deploy = {
                "pushed": gate.pushed,
                "pushed_source": gate.pushed_source,
                "live": gate.live,
                "seal": gate.seal,
                "reasons": gate.reasons,
            }
            row = await insert_report(
                tx,
                crew_id,
                task,
                kind="completion" if gate.verdict == "complete" else "partial",
                session_id=str(session["id"]),
                facts_source=body_source,
                actor=caller.actor,
                verdict=gate.verdict,
                review_state="accepted" if gate.auto_accept else "review",
                criteria=gate.criteria,
                commits=evidence.commits[:200],
                files=evidence.files[:500],
                out_of_zone_files=out_of_zone,
                tests=gate.tests,
                deploy=deploy,
                sections=clean.get("sections") or {},
                summary=clean.get("summary"),
                grounding=grounding,
                facts_hash=digest,
            )
            task = await load_task(tx.conn, crew_id, task_id)
            if gate.auto_accept:
                await tx.emit(
                    crew_id=crew_id,
                    type="report.accepted",
                    actor=Actor.system(),
                    payload={"report": report_view(row)},
                    summary=f"report {row['id']} for {task_ref(task)} accepted by the gate",
                    refs={"task_id": task_id, "report_id": row["id"]},
                )
                detail, seq = await self.tasks.finish_done(tx, crew_id, task, str(row["id"]), caller)
                outcome = "accepted"
            else:
                detail, seq = await self.tasks.to_review(tx, crew_id, task, str(row["id"]), caller)
                outcome = "review"
            row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (row["id"],)) or row
        return ReportResult(report_detail(row), detail, seq, outcome, gate)

    async def review(self, crew_id: str, task_id: str, caller: Caller, decision: str, note: str | None) -> ReportResult:
        """(H) Approve (→ done) or reject (→ in_progress) the current report of a task in review."""
        if not caller.is_privileged:
            raise _err(403, "human_only", "Only a human reviews reports.")
        if decision not in ("approve", "reject"):
            raise _err(422, "invalid_review", "decision must be approve or reject")
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise _err(422, "invalid_review", "note must be a string of at most 2000 characters")
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] != "review":
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}, not in review.")
            row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE task_id = ? AND is_current = 1", (task_id,))
            if row is None:
                raise _err(409, "report_required", f"{task_ref(task)} has no current report to review.")
            if decision == "approve":
                await tx.conn.execute(
                    "UPDATE crew_reports SET review_state = 'accepted', reviewed_by = ?, review_note = ? WHERE id = ?",
                    (caller.user_id, note, row["id"]),
                )
                row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (row["id"],)) or row
                await tx.emit(
                    crew_id=crew_id,
                    type="report.accepted",
                    actor=caller.actor,
                    payload={"report": report_view(row)},
                    summary=f"report {row['id']} for {task_ref(task)} approved by human",
                    refs={"task_id": task_id, "report_id": row["id"]},
                )
                await self.tasks._emit_task(
                    tx,
                    crew_id,
                    task_id,
                    "task.review_decided",
                    caller,
                    f"{task_ref(task)} review approved",
                    report_id=row["id"],
                    decision="approve",
                )
                detail, seq = await self.tasks.finish_done(tx, crew_id, task, str(row["id"]), caller)
                outcome = "approved"
            else:
                await tx.conn.execute(
                    "UPDATE crew_reports SET review_state = 'rejected', reviewed_by = ?, review_note = ?, is_current = 0,"
                    " superseded_reason = 'rejected' WHERE id = ?",
                    (caller.user_id, note, row["id"]),
                )
                await tx.conn.execute("UPDATE crew_tasks SET current_report_id = NULL WHERE id = ?", (task_id,))
                row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (row["id"],)) or row
                await tx.emit(
                    crew_id=crew_id,
                    type="report.rejected",
                    actor=caller.actor,
                    payload={"report": report_view(row)},
                    summary=f"report {row['id']} for {task_ref(task)} rejected by human",
                    refs={"task_id": task_id, "report_id": row["id"]},
                )
                task = await load_task(tx.conn, crew_id, task_id)
                await self.tasks._set_status(tx, crew_id, task, "in_progress", caller)
                detail, seq = await self.tasks._emit_task(
                    tx,
                    crew_id,
                    task_id,
                    "task.review_decided",
                    caller,
                    f"{task_ref(task)} review rejected",
                    report_id=row["id"],
                    decision="reject",
                )
                outcome = "rejected"
            await resolve_inbox_items(tx, crew_id, [f"review_report:{task_id}"], resolved_by=caller.user_id, actor=caller.actor)
        return ReportResult(report_detail(row), detail, seq, outcome)

    async def waive(self, crew_id: str, task_id: str, caller: Caller, criterion_id: Any, reason: Any) -> ReportResult:
        """(H, step-up) Waive one criterion, or ``"all"``: a ``waived`` report and ``done`` without evidence (D17)."""
        if not caller.is_privileged:
            raise _err(403, "human_only", "Only a human can waive acceptance criteria.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 280:
            raise _err(422, "invalid_waiver", "reason is required (at most 280 characters)")
        if not isinstance(criterion_id, str) or not criterion_id:
            raise _err(422, "invalid_waiver", 'criterion_id must be a criterion id or "all"')
        async with self.events.transaction() as tx:
            task = await load_task(tx.conn, crew_id, task_id)
            if task["status"] in ("done", "cancelled"):
                raise _err(409, "invalid_transition", f"{task_ref(task)} is {task['status']}.")
            acceptance = [dict(c) for c in loads(task.get("acceptance"), []) or [] if isinstance(c, Mapping)]
            waiver = {"by": caller.user_id, "reason": reason.strip(), "at": now_iso()}
            if criterion_id != "all":
                target = next((c for c in acceptance if c.get("id") == criterion_id), None)
                if target is None:
                    raise _err(422, "unknown_criterion", f"{task_ref(task)} has no criterion {criterion_id}.")
                target["waiver"] = waiver
                await tx.conn.execute(
                    "UPDATE crew_tasks SET acceptance = ?, version = version + 1, updated_at = ? WHERE id = ?",
                    (dumps(acceptance), now_iso(), task_id),
                )
                await tx.emit(
                    crew_id=crew_id,
                    type="human.override",
                    actor=caller.actor,
                    payload={"action": "waive", "reason": reason.strip()[:280], "target_kind": "task", "target_id": task_id},
                    summary=f"human waived criterion {criterion_id[:32]} of {task_ref(task)}",
                    refs={"task_id": task_id},
                )
                detail, seq = await self.tasks._emit_task(
                    tx, crew_id, task_id, "task.updated", caller, f"{task_ref(task)} criterion waived", changed=["acceptance"]
                )
                current = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE task_id = ? AND is_current = 1", (task_id,))
                return ReportResult(report_detail(current) if current else {}, detail, seq, "criterion_waived")
            for c in acceptance:
                c.setdefault("waiver", waiver)
            await tx.conn.execute(
                "UPDATE crew_tasks SET acceptance = ?, updated_at = ? WHERE id = ?", (dumps(acceptance), now_iso(), task_id)
            )
            task = await load_task(tx.conn, crew_id, task_id)
            await tx.emit(
                crew_id=crew_id,
                type="human.override",
                actor=caller.actor,
                payload={"action": "waive", "reason": reason.strip()[:280], "target_kind": "task", "target_id": task_id},
                summary=f"human waived the report for {task_ref(task)}",
                refs={"task_id": task_id},
            )
            criteria = [{"id": c["id"], "kind": c.get("kind"), "status": "waived", "source": None} for c in acceptance]
            row = await insert_report(
                tx,
                crew_id,
                task,
                kind="waived",
                session_id=task.get("owner_session_id"),
                facts_source="server-inferred",
                actor=caller.actor,
                verdict=None,
                review_state="waived",
                criteria=criteria,
                sections={},
                summary=reason.strip(),
                deploy={"seal": "waived by a human", "waived_by": caller.user_id},
                facts_hash=hashlib.sha256(f"waive:{task_id}:{new_id('report')}".encode()).hexdigest(),
                supersede_reason="waived",
                event_type="report.waived",
            )
            await tx.conn.execute(
                "UPDATE crew_reports SET reviewed_by = ?, review_note = ? WHERE id = ?",
                (caller.user_id, reason.strip(), row["id"]),
            )
            task = await load_task(tx.conn, crew_id, task_id)
            detail, seq = await self.tasks.finish_done(tx, crew_id, task, str(row["id"]), caller)
            row = await fetchone(tx.conn, "SELECT * FROM crew_reports WHERE id = ?", (row["id"],)) or row
        return ReportResult(report_detail(row), detail, seq, "waived")


def _grounding(summary: Any, evidence: Evidence, gate: GateResult) -> dict[str, Any]:
    from remembra.relay.handoff import check_summary_grounding

    facts = {
        "commits": [{"sha": s} for s in evidence.commits],
        "files_changed": evidence.files,
        "tests": [{"cmd": t["fingerprint"], "passed": t["passed"]} for t in gate.tests],
        "unpushed_commits": 0 if gate.pushed else None,
    }
    return check_summary_grounding(summary if isinstance(summary, str) else None, facts)


# ---------------------------------------------------------------------------
# The invariant job
# ---------------------------------------------------------------------------


async def check_report_invariant(conn: Any, crew_id: str | None = None) -> list[dict[str, Any]]:
    """Violations of §5.6: finished-after-start tasks without exactly one current report, and any task with two."""
    sql = (
        "SELECT t.id, t.crew_id, t.number, t.status,"
        " (SELECT COUNT(*) FROM crew_reports r WHERE r.task_id = t.id AND r.is_current = 1) AS current_reports"
        " FROM crew_tasks t WHERE t.started_at IS NOT NULL"
    )
    params: list[Any] = []
    if crew_id is not None:
        sql += " AND t.crew_id = ?"
        params.append(crew_id)
    out: list[dict[str, Any]] = []
    for r in await fetchall(conn, sql, params):
        n = int(r["current_reports"])
        if (r["status"] in ("done", "stalled", "cancelled") and n != 1) or n > 1:
            out.append(
                {"task_id": r["id"], "crew_id": r["crew_id"], "number": r["number"], "status": r["status"], "current_reports": n}
            )
    return out


async def enforce_report_invariant(events: CrewEventLog, crew_id: str | None = None) -> list[dict[str, Any]]:
    """Run :func:`check_report_invariant` and open one Needs-you ``report_invariant`` item per violation."""
    violations = await check_report_invariant(events.db.conn, crew_id)
    for v in violations:
        async with events.transaction() as tx:
            await open_inbox_item(
                tx,
                str(v["crew_id"]),
                audience="project",
                kind="report_invariant",
                title=f"T-{int(v['number'])} is {v['status']} with {v['current_reports']} current reports",
                dedupe_key=f"report_invariant:{v['task_id']}",
                ref_type="task",
                ref_id=str(v["task_id"]),
                priority=1,
            )
        log.warning("crew_report_invariant_violation", **v)
    return violations


def seconds_until(now: datetime, at: tuple[int, int] = INVARIANT_RUN_AT_UTC) -> float:
    target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def report_invariant_loop(
    events: CrewEventLog,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Nightly :func:`enforce_report_invariant` at :data:`INVARIANT_RUN_AT_UTC` until cancelled."""
    while True:
        await sleep(seconds_until(clock()))
        try:
            await enforce_report_invariant(events)
        except Exception as e:  # keep the schedule alive
            log.error("crew_report_invariant_failed", error_type=type(e).__name__, error=str(e))


# ---------------------------------------------------------------------------
# Startup hook (listed in remembra.crew.startup.HOOK_MODULES)
# ---------------------------------------------------------------------------

INVARIANT_HOOK_ORDER: Final = 45


async def _start_invariant(app: Any, rt: Any) -> None:
    from remembra.crew import startup

    events = getattr(app.state, "crew_events", None)
    if events is None:
        raise RuntimeError("crew.report_invariant needs app.state.crew_events (crew.bus hook)")
    startup._spawn(app, rt, report_invariant_loop(events), "crew-report-invariant")


async def _stop_invariant(app: Any, rt: Any) -> None:
    from remembra.crew import startup

    await startup._cancel(rt, "crew-report-invariant")


def register_hooks() -> None:
    from remembra.crew import startup

    startup.add_hook("crew.report_invariant", order=INVARIANT_HOOK_ORDER, start=_start_invariant, stop=_stop_invariant)


register_hooks()
