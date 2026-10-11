"""The desk's nine read tools: fixed GETs (and one package lookup), never a write.

Each tool validates the model's arguments (one pydantic model per tool),
makes exactly one GET through :class:`~remembra.marshal.desk.client.DeskClient`
(``docs_lookup`` reads the bundled help pack instead), projects the JSON
(:mod:`.projections`), cleans every string (hidden characters out, then
secrets redacted, always, whatever ``secret_redaction_enabled`` says), writes
the one-line summary and frames the result for the model as untrusted data
(``dump_untrusted``). A failed read, a refused one and one with bad arguments
all count as reads, with ``ok: false`` and a plain summary.

There is no generic endpoint tool, no ``user_id`` argument, no recall, no
brief without ``preview=1`` and no key listing (key evidence reaches the model
through ``diagnose_agent``, whose route reads keys server-side).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from remembra.marshal import knowledge
from remembra.marshal.desk import projections as pj
from remembra.marshal.desk.client import DeskClient, ToolNotAllowed
from remembra.marshal.desk.events import ReadEvent
from remembra.marshal.diagnosis import adapter_id, canonical_agent_id
from remembra.relay.adapters import REGISTRY
from remembra.security.secrets import redact_secrets
from remembra.security.untrusted import dump_untrusted, strip_hidden

log = structlog.get_logger(__name__, component="marshal_desk")  # lazy: see logs.py

AGENTS: tuple[str, ...] = tuple(REGISTRY)
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_TRAIL_LIMIT = 20


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _registry_agent(value: Any) -> str:
    agent = canonical_agent_id(str(value)) if isinstance(value, str) else ""
    if agent not in REGISTRY:
        raise ValueError(f"agent_id must be one of {', '.join(AGENTS)}")
    return agent


class DaysArgs(_Args):
    days: int = Field(7, ge=1, le=30)


class TrailArgs(_Args):
    agent_id: str | None = None
    project_id: str | None = None
    limit: int = Field(10, ge=1)

    @field_validator("agent_id")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return None if value is None else _registry_agent(value)

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value is not None and not _PROJECT_RE.match(value):
            raise ValueError("project_id must be a project id")
        return value

    @field_validator("limit")
    @classmethod
    def _limit(cls, value: int) -> int:
        return min(value, MAX_TRAIL_LIMIT)


class BriefArgs(_Args):
    project_id: str

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        if not _PROJECT_RE.match(value):
            raise ValueError("project_id must be a project id")
        return value


class NoArgs(_Args):
    pass


class DiagnoseArgs(_Args):
    agent_id: str

    @field_validator("agent_id")
    @classmethod
    def _agent(cls, value: str) -> str:
        return _registry_agent(value)


class DocsArgs(_Args):
    question: str = Field(min_length=1, max_length=300)

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question is empty")
        return value


# ---------------------------------------------------------------------------
# The tool map
# ---------------------------------------------------------------------------

Projector = Callable[[Mapping[str, Any], datetime], dict[str, Any]]
Summarizer = Callable[[Mapping[str, Any], Mapping[str, Any]], str]


@dataclass(frozen=True)
class Tool:
    name: str
    label: str
    args: type[_Args]
    project: Projector
    summarize: Summarizer
    path: str | None = None
    params: Callable[[Mapping[str, Any]], dict[str, Any]] = field(default=lambda _a: {})
    anchor: Callable[[Mapping[str, Any], Mapping[str, Any]], str | None] = field(default=lambda _p, _a: None)
    cloud: bool = False


def _brief_anchor(projected: Mapping[str, Any], _args: Mapping[str, Any]) -> str | None:
    handoff = projected.get("handoff")
    return f"entry:{handoff['id']}" if isinstance(handoff, Mapping) and handoff.get("id") else None


def _no_args(summary: Callable[[Mapping[str, Any]], str]) -> Summarizer:
    return lambda projected, _args: summary(projected)


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            "trail_summary",
            "trail/summary",
            DaysArgs,
            pj.project_trail_summary,
            _no_args(pj.summary_trail_summary),
            "/api/v1/trail/summary",
            lambda a: {"days": a["days"], "tz_offset_minutes": 0},
        ),
        Tool(
            "trail",
            "trail",
            TrailArgs,
            pj.project_trail,
            pj.summary_trail,
            "/api/v1/trail",
            lambda a: {
                "limit": a["limit"],
                **({"agent_id": adapter_id(a["agent_id"])} if a.get("agent_id") else {}),
                **({"project_id": a["project_id"]} if a.get("project_id") else {}),
            },
            lambda _p, a: f"agent:{a['agent_id']}" if a.get("agent_id") else None,
        ),
        Tool(
            "brief_preview",
            "session/brief",
            BriefArgs,
            pj.project_brief,
            pj.summary_brief,
            "/api/v1/session/brief",
            # Never an agent id, never a location: preview=1 records nothing and binds nothing.
            lambda a: {"preview": 1, "project_id": a["project_id"], "recent_n": 5, "inbox_limit": 0},
            _brief_anchor,
        ),
        Tool("inbox_summary", "inbox/summary", NoArgs, pj.project_inbox, _no_args(pj.summary_inbox), "/api/v1/inbox/summary"),
        Tool(
            "usage_summary",
            "cloud/usage/summary",
            NoArgs,
            pj.project_usage_summary,
            _no_args(pj.summary_usage_summary),
            "/api/v1/cloud/usage/summary",
            cloud=True,
        ),
        Tool(
            "usage_daily",
            "cloud/usage/daily",
            DaysArgs,
            pj.project_usage_daily,
            _no_args(pj.summary_usage_daily),
            "/api/v1/cloud/usage/daily",
            lambda a: {"days": a["days"]},
            cloud=True,
        ),
        Tool("plan", "cloud/plan", NoArgs, pj.project_plan, _no_args(pj.summary_plan), "/api/v1/cloud/plan", cloud=True),
        Tool(
            "diagnose_agent",
            "trail/diagnosis",
            DiagnoseArgs,
            pj.project_diagnosis,
            _no_args(pj.summary_diagnosis),
            "/api/v1/trail/diagnosis",
            lambda a: {"agent_id": a["agent_id"]},
            lambda _p, a: f"agent:{a['agent_id']}",
        ),
        Tool("docs_lookup", "docs", DocsArgs, pj.project_docs, _no_args(pj.summary_docs)),
    )
}


def _schema(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = list(required)
    parameters["additionalProperties"] = False
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


_DAYS = {"days": {"type": "integer", "minimum": 1, "maximum": 30}}
# The function schemas passed to the model (plan 3.5).
TOOL_SPECS: list[dict[str, Any]] = [
    _schema(
        "trail_summary",
        "Activity per agent and project: handoffs, checkpoints, last active. Read this first for most questions.",
        _DAYS,
    ),
    _schema(
        "trail",
        "The newest handoffs and checkpoints, optionally for one agent or project, with who picked each up.",
        {
            "agent_id": {"type": "string"},
            "project_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    ),
    _schema(
        "brief_preview",
        "What the next agent would see at session start in one project. Reading it records nothing.",
        {"project_id": {"type": "string"}},
        ("project_id",),
    ),
    _schema("inbox_summary", "Unread and open inbox notes per agent (counts only).", {}),
    _schema("usage_summary", "Plan, smart credits, relay events and recalls this period.", {}),
    _schema("usage_daily", "Stores and recalls per day.", _DAYS),
    _schema("plan", "Plan limits and whether a store, recall or new key is allowed now.", {}),
    _schema(
        "diagnose_agent",
        "The rules verdict for one agent: why it is waiting, proven or inferred, with the one fix.",
        {"agent_id": {"type": "string", "enum": list(AGENTS)}},
        ("agent_id",),
    ),
    _schema(
        "docs_lookup",
        "Quoted sections of Remembra's relay guide and plans page, or the page to read for pricing, refunds, security "
        "and privacy.",
        {"question": {"type": "string"}},
        ("question",),
    ),
]


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadResult:
    id: str
    tool: str
    label: str
    args: dict[str, Any]
    ok: bool
    http_status: int | None
    summary: str
    ms: int
    anchor: str | None
    payload: dict[str, Any]  # the cleaned projection ({} for a failed read)
    message: str  # the tool message the model gets

    def as_event(self) -> ReadEvent:
        return {
            "id": self.id,
            "tool": self.tool,
            "label": self.label,
            "args": self.args,
            "ok": self.ok,
            "http_status": self.http_status,
            "summary": self.summary,
            "ms": self.ms,
            "anchor": self.anchor,
        }


@dataclass
class ToolContext:
    client: DeskClient
    now: datetime


def clean_text(value: str) -> str:
    """Hidden characters out, then every key-shaped string redacted (always on for the desk)."""
    return redact_secrets(strip_hidden(value)[0]).text


_FREE_TEXT_FIELDS = frozenset({"headline", "next", "done", "not_done", "failing", "warnings", "missing"})
_SHORT_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{20,31}(?![A-Za-z0-9])")


def _redact_short_token(match: re.Match[str]) -> str:
    token = match.group()
    classes = ["l" if c.islower() else "u" if c.isupper() else "d" for c in token]
    runs = 1 + sum(a != b for a, b in zip(classes, classes[1:], strict=False))
    if len(set(classes)) == 3 and runs >= 10 and not re.search(r"[a-z]{6}", token):
        return "[REDACTED:high_entropy_token]"
    return token


def clean_deep(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        cleaned = clean_text(value)
        return _SHORT_TOKEN.sub(_redact_short_token, cleaned) if key in _FREE_TEXT_FIELDS else cleaned
    if isinstance(value, Mapping):
        return {k: clean_deep(item, str(k)) for k, item in value.items()}
    if isinstance(value, list | tuple):
        return [clean_deep(item, key) for item in value]
    return value


def failed_summary(tool: Tool, http_status: int | None) -> str:
    if http_status == 429:
        return "couldn't read (HTTP 429) · try again in a minute"
    if tool.cloud and http_status in (404, 503):
        return "not available on this server"
    if http_status is None:
        return "couldn't read · try again"
    return f"couldn't read (HTTP {http_status}) · try again"


def _failed(
    tool: Tool, read_id: str, args: dict[str, Any], http_status: int | None, summary: str, ms: int, **extra: Any
) -> ReadResult:
    message = dump_untrusted({"status": "error", "id": read_id, "http_status": http_status, "error": summary, **extra})
    return ReadResult(read_id, tool.name, tool.label, args, False, http_status, summary, ms, None, {}, message)


def _parse_args(raw: Any) -> Any:
    if isinstance(raw, str):
        return json.loads(raw) if raw.strip() else {}
    return raw if raw is not None else {}


async def run_tool(name: str, raw_args: Any, ctx: ToolContext, read_id: str) -> ReadResult:
    """Validate, read, project, clean, summarise and frame one read. Never raises for a bad read."""
    tool = TOOLS[name]
    started = time.monotonic()
    try:
        args = tool.args.model_validate(_parse_args(raw_args)).model_dump(exclude_none=True)
    except (ValueError, ValidationError, TypeError) as e:
        fields = sorted({str(err["loc"][0]) for err in e.errors() if err.get("loc")}) if isinstance(e, ValidationError) else []
        return _failed(tool, read_id, {}, None, "bad arguments · not read", 0, fields=fields)
    status: int | None = 200
    try:
        if tool.path is None:
            data: Any = knowledge.lookup(str(args["question"]))
            ms = int((time.monotonic() - started) * 1000)
        else:
            result = await ctx.client.get(tool.path, tool.params(args))
            status, data, ms = result.status, result.json, result.ms
    except ToolNotAllowed:
        log.error("marshal_tool_refused", tool=name)
        return _failed(tool, read_id, args, None, failed_summary(tool, None), 0)
    except Exception as e:
        log.warning("marshal_tool", tool=name, ok=False, http_status=None, error_type=type(e).__name__)
        return _failed(tool, read_id, args, None, failed_summary(tool, None), int((time.monotonic() - started) * 1000))
    if status != 200 or not isinstance(data, Mapping):
        log.info("marshal_tool", tool=name, ok=False, http_status=status, ms=ms)
        return _failed(tool, read_id, args, status, failed_summary(tool, status), ms)
    cleaned = clean_deep(tool.project(data, ctx.now))
    summary = clean_text(tool.summarize(cleaned, args))
    message = dump_untrusted({"status": "ok", "id": read_id, **cleaned})
    log.info("marshal_tool", tool=name, ok=True, http_status=status, ms=ms)
    anchor = tool.anchor(cleaned, args)
    return ReadResult(read_id, tool.name, tool.label, args, True, status, summary, ms, anchor, cleaned, message)
