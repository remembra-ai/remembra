"""Zone policy: ``.remembra/zones.yml`` parsing, validation, policy diff and suggestions (WP-5).

Spec anchors: D8 (undeclared paths), D9 (zone authority and pending loosening
changes), D10 (enforcement), D26 (the gate never parses YAML: the server and
crewd do), D28 (the built-in ``crew-policy`` zone), D37 (no-zone bootstrap),
D38 (command grammar), §2 (commons, ignore), §11 (size caps).

This module is pure: it knows nothing about the database or who is asking.
:mod:`remembra.crew.zones` stores what it compiles.

``zones.yml`` (version 1)::

    version: 1                 # optional; only 1 is accepted
    enforcement: enforce       # optional: enforce | observe | off (lowering needs a human)
    zones:
      pos:                     # the zone slug
        title: POS section
        description: Point of sale screens
        parent: app            # slug of the parent zone (holding a parent covers children)
        include: [src/app/pos/**]   # alias: paths
        exclude: [src/app/pos/fixtures/**]
        mode: exclusive        # exclusive | shared | watch
        auto_claim: true
        protected: false       # agents are denied unless a human grants a claim
        reserve_for: codex     # key-verified agent id only (§5.1)
        fail_closed: false
        services: [deploy:vercel]
        commands: ["vercel deploy *"]        # argv-prefix patterns (D38), never regexes
        mcp_tools: [{tool: "*vercel*deploy*", service: "deploy:vercel"}]
        color: "#c2410c"
      reports: [src/app/reports/**]          # shorthand: a list of include globs
    commons:
      package.json: plain                    # mapping form, or a list of {glob, kind} / strings
    ignore: [docs/**]

Every object is closed: an unknown key is an error, never silently dropped, so a
typo can never weaken protection. YAML anchors and aliases are refused (no alias
expansion attacks), and the document is capped at 32 KB (§11).

**Loosening** (D9): a change that could let an agent write where it was denied
before. Loosening items land as a pending change until a human approves them;
:func:`interim_policy` builds the policy that applies meanwhile (every
non-loosening change applied, every loosening one held back).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

import yaml  # type: ignore[import-untyped,unused-ignore]

from remembra.crew import schemas as S
from remembra.crew.gatecore import _expand_braces, compile_glob

POLICY_VERSION: Final = 1
BUILTIN_ZONE_SLUG: Final = "crew-policy"
BUILTIN_ZONE_TITLE: Final = "Crew policy"
# D28: .remembra/** (zones.yml itself), git hooks and the local crew runtime. The crew hook entries
# inside Claude settings, Husky and lefthook files are protected surgically by gatecore (row 2).
BUILTIN_ZONE_GLOBS: Final = tuple(S.CREW_POLICY_GLOBS)
MAX_ZONES_IN_FILE: Final = 200
MAX_COMMONS: Final = 200
MAX_IGNORE: Final = 200
MAX_SERVICES_PER_ZONE: Final = 20
MAX_MCP_RULES_PER_ZONE: Final = 20
MAX_ZONE_DEPTH: Final = 8
MAX_TITLE: Final = 120
MAX_DESCRIPTION: Final = 500
ENFORCEMENT_RANK: Final[Mapping[str, int]] = {"off": 0, "observe": 1, "enforce": 2}
MODE_RANK: Final[Mapping[str, int]] = {"watch": 0, "shared": 1, "exclusive": 2}
COMMONS_RANK: Final[Mapping[str, int]] = {"plain": 0, "serialize": 1, "append_only": 2}

# §2: lockfiles are ``serialize`` by default; migration directories default to ``append_only``.
LOCKFILE_NAMES: Final = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "bun.lock",
        "Cargo.lock",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "Podfile.lock",
        "pubspec.lock",
    }
)
DEFAULT_COMMONS: Final[tuple[Mapping[str, str], ...]] = (
    *({"glob": f"**/{name}", "kind": "serialize"} for name in sorted(LOCKFILE_NAMES)),
    {"glob": "**/migrations/**", "kind": "append_only"},
    {"glob": "db/migrate/**", "kind": "append_only"},
    {"glob": "alembic/versions/**", "kind": "append_only"},
)

_ZONE_KEYS: Final = frozenset(
    {
        "title",
        "description",
        "parent",
        "include",
        "paths",
        "exclude",
        "mode",
        "auto_claim",
        "protected",
        "reserve_for",
        "fail_closed",
        "services",
        "commands",
        "mcp_tools",
        "color",
    }
)
_TOP_KEYS: Final = frozenset({"version", "enforcement", "zones", "commons", "ignore"})
_SLUG_RE: Final = re.compile(S.SLUG_PATTERN)
_AGENT_RE: Final = re.compile(S.AGENT_ID_PATTERN)
_RESOURCE_RE: Final = re.compile(S.RESOURCE_PATTERN)
_MCP_TOOL_RE: Final = re.compile(r"[A-Za-z0-9_.*-]{1,128}")
_COLOR_RE: Final = re.compile(r"#[0-9a-fA-F]{6}")
_GLOB_CHARS: Final = frozenset("*?[{")


class PolicyError(ValueError):
    """``zones.yml`` (or a zone definition) is invalid. ``errors`` lists every problem as ``path: reason``."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:8]))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneDef:
    """One declared zone (the part of a ``crew_zones`` row that a policy controls)."""

    slug: str
    title: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    description: str | None = None
    parent: str | None = None
    mode: str = "exclusive"
    auto_claim: bool = True
    protected: bool = False
    reserve_for: str | None = None
    fail_closed: bool = False
    services: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    mcp_tools: tuple[tuple[str, str | None], ...] = ()
    color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "parent": self.parent,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "mode": self.mode,
            "auto_claim": self.auto_claim,
            "protected": self.protected,
            "reserve_for": self.reserve_for,
            "fail_closed": self.fail_closed,
            "services": list(self.services),
            "commands": list(self.commands),
            "mcp_tools": [{"tool": t, "service": s} for t, s in self.mcp_tools],
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ZoneDef:
        return cls(
            slug=str(data["slug"]),
            title=str(data.get("title") or data["slug"]),
            include=tuple(data.get("include") or ()),
            exclude=tuple(data.get("exclude") or ()),
            description=data.get("description"),
            parent=data.get("parent"),
            mode=str(data.get("mode") or "exclusive"),
            auto_claim=bool(data.get("auto_claim", True)),
            protected=bool(data.get("protected", False)),
            reserve_for=data.get("reserve_for"),
            fail_closed=bool(data.get("fail_closed", False)),
            services=tuple(data.get("services") or ()),
            commands=tuple(data.get("commands") or ()),
            mcp_tools=tuple((str(r["tool"]), r.get("service")) for r in data.get("mcp_tools") or ()),
            color=data.get("color"),
        )


@dataclass(frozen=True)
class Policy:
    """A compiled zone policy: zones, commons, ignore and (optionally) project enforcement."""

    zones: tuple[ZoneDef, ...] = ()
    commons: tuple[tuple[str, str], ...] = ()  # (glob, kind), declared entries only
    ignore: tuple[str, ...] = ()
    enforcement: str | None = None

    def zone(self, slug: str) -> ZoneDef | None:
        return next((z for z in self.zones if z.slug == slug), None)

    @property
    def slugs(self) -> list[str]:
        return [z.slug for z in self.zones]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "enforcement": self.enforcement,
            "zones": [z.to_dict() for z in self.zones],
            "commons": [{"glob": g, "kind": k} for g, k in self.commons],
            "ignore": list(self.ignore),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Policy:
        return cls(
            zones=tuple(ZoneDef.from_dict(z) for z in data.get("zones") or ()),
            commons=tuple((str(c["glob"]), str(c["kind"])) for c in data.get("commons") or ()),
            ignore=tuple(str(g) for g in data.get("ignore") or ()),
            enforcement=data.get("enforcement"),
        )

    def digest(self) -> str:
        """sha256 of the canonical compiled policy (change detection independent of YAML formatting)."""
        return hashlib.sha256(S.canonical_json(self.to_dict())).hexdigest()


def effective_commons(policy: Policy | None) -> list[dict[str, str]]:
    """Declared commons first (they win on a glob match), then the built-in defaults (§2)."""
    declared = [{"glob": g, "kind": k} for g, k in (policy.commons if policy else ())]
    seen = {c["glob"] for c in declared}
    return declared + [dict(c) for c in DEFAULT_COMMONS if c["glob"] not in seen]


def builtin_zone() -> ZoneDef:
    """The always-on protected ``crew-policy`` zone (D28). Never claimable by agents."""
    return ZoneDef(
        slug=BUILTIN_ZONE_SLUG,
        title=BUILTIN_ZONE_TITLE,
        include=BUILTIN_ZONE_GLOBS,
        mode="exclusive",
        auto_claim=False,
        protected=True,
        fail_closed=True,
    )


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


class _NoAliasLoader(yaml.SafeLoader):  # type: ignore[misc,unused-ignore]
    """SafeLoader that refuses anchors and aliases (no billion-laughs expansion)."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.YAMLError(f"aliases are not allowed (line {event.start_mark.line + 1})")
        event = self.peek_event()
        if getattr(event, "anchor", None):
            raise yaml.YAMLError(f"anchors are not allowed (line {event.start_mark.line + 1})")
        return super().compose_node(parent, index)


def parse_zones_yaml(text: str) -> Policy:
    """Parse and validate ``zones.yml`` text; raise :class:`PolicyError` listing every problem."""
    if not isinstance(text, str):
        raise PolicyError(["$: zones.yml must be text"])
    if len(text.encode("utf-8")) > S.MAX_ZONES_YML_BYTES:
        raise PolicyError([f"$: zones.yml is larger than {S.MAX_ZONES_YML_BYTES} bytes"])
    try:
        data = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as e:
        raise PolicyError([f"$: not valid YAML: {str(e).splitlines()[0][:200]}"]) from e
    if data is None:
        data = {}
    return compile_policy(data)


def compile_policy(data: Any) -> Policy:
    """Validate a parsed ``zones.yml`` document (or an equivalent dict) and compile it."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        raise PolicyError(["$: zones.yml must be a mapping with zones, commons and ignore"])
    for key in data:
        if key not in _TOP_KEYS:
            errors.append(f"$.{key}: unknown key")
    version = data.get("version", POLICY_VERSION)
    if version != POLICY_VERSION or isinstance(version, bool):
        errors.append(f"$.version: only version {POLICY_VERSION} is supported")
    enforcement = data.get("enforcement")
    if enforcement is not None and enforcement not in S.ENFORCEMENT_LEVELS:
        errors.append(f"$.enforcement: must be one of {', '.join(S.ENFORCEMENT_LEVELS)}")
    zones = _compile_zones(data.get("zones"), errors)
    commons = _compile_commons(data.get("commons"), errors)
    ignore = tuple(_glob_list(data.get("ignore"), "$.ignore", errors, MAX_IGNORE))
    if errors:
        raise PolicyError(errors)
    return Policy(zones=zones, commons=commons, ignore=ignore, enforcement=enforcement)


def _compile_zones(raw: Any, errors: list[str]) -> tuple[ZoneDef, ...]:
    if raw is None:
        return ()
    items: list[tuple[str, Any]] = []
    if isinstance(raw, Mapping):
        items = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, list):
        for idx, entry in enumerate(raw):
            if not isinstance(entry, Mapping) or "slug" not in entry:
                errors.append(f"$.zones[{idx}]: a zone in list form needs a slug")
                continue
            body = {k: v for k, v in entry.items() if k != "slug"}
            items.append((str(entry["slug"]), body))
    else:
        errors.append("$.zones: must be a mapping of slug to zone")
        return ()
    if len(items) > MAX_ZONES_IN_FILE:
        errors.append(f"$.zones: at most {MAX_ZONES_IN_FILE} zones")
        return ()
    out: list[ZoneDef] = []
    seen: set[str] = set()
    for slug, body in items:
        path = f"$.zones.{slug}"
        if slug in seen:
            errors.append(f"{path}: duplicate slug")
            continue
        seen.add(slug)
        zone = validate_zone_body(slug, body, path, errors)
        if zone is not None:
            out.append(zone)
    _check_tree(out, errors)
    return tuple(out)


def validate_zone_body(slug: str, body: Any, path: str, errors: list[str], *, allow_builtin: bool = False) -> ZoneDef | None:
    """Validate one zone (``zones.yml`` entry or a dashboard/API zone body). Appends to ``errors``."""
    n = len(errors)
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        errors.append(f"{path}: slug must match {S.SLUG_PATTERN}")
    elif slug == BUILTIN_ZONE_SLUG and not allow_builtin:
        errors.append(f"{path}: {BUILTIN_ZONE_SLUG} is the built-in policy zone and cannot be declared")
    if isinstance(body, list):
        body = {"include": body}
    if isinstance(body, str):
        body = {"include": [body]}
    if not isinstance(body, Mapping):
        errors.append(f"{path}: must be a mapping or a list of globs")
        return None
    for key in body:
        if key not in _ZONE_KEYS:
            errors.append(f"{path}.{key}: unknown key")
    if "include" in body and "paths" in body:
        errors.append(f"{path}: use include or paths, not both")
    include = _glob_list(body.get("include", body.get("paths")), f"{path}.include", errors, S.MAX_GLOBS_PER_ZONE)
    exclude = _glob_list(body.get("exclude"), f"{path}.exclude", errors, S.MAX_GLOBS_PER_ZONE)
    title = body.get("title", slug)
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
        errors.append(f"{path}.title: text of 1..{MAX_TITLE} characters")
        title = str(slug)
    description = body.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > MAX_DESCRIPTION):
        errors.append(f"{path}.description: text of at most {MAX_DESCRIPTION} characters")
        description = None
    parent = body.get("parent")
    if parent is not None and (not isinstance(parent, str) or not _SLUG_RE.fullmatch(parent)):
        errors.append(f"{path}.parent: must be a zone slug")
        parent = None
    mode = body.get("mode", "exclusive")
    if mode not in S.CLAIM_MODES:
        errors.append(f"{path}.mode: must be one of {', '.join(S.CLAIM_MODES)}")
        mode = "exclusive"
    flags: dict[str, bool] = {}
    for key, default in (("auto_claim", True), ("protected", False), ("fail_closed", False)):
        value = body.get(key, default)
        if not isinstance(value, bool):
            errors.append(f"{path}.{key}: must be true or false")
            value = default
        flags[key] = value
    reserve_for = body.get("reserve_for")
    if reserve_for is not None and (not isinstance(reserve_for, str) or not _AGENT_RE.fullmatch(reserve_for)):
        errors.append(f"{path}.reserve_for: must be an agent id")
        reserve_for = None
    services = _str_list(body.get("services"), f"{path}.services", errors, MAX_SERVICES_PER_ZONE)
    for svc in services:
        if not _RESOURCE_RE.fullmatch(svc):
            errors.append(f"{path}.services: {svc!r} must look like schema:<db>, deploy:<target> or service:<name>")
    commands = _str_list(body.get("commands"), f"{path}.commands", errors, S.MAX_COMMAND_PATTERNS_PER_ZONE + 1)
    errors.extend(f"{path}.commands: {e}" for e in S.validate_command_patterns(commands))
    mcp_tools = _mcp_rules(body.get("mcp_tools"), f"{path}.mcp_tools", errors)
    color = body.get("color")
    if color is not None and (not isinstance(color, str) or not _COLOR_RE.fullmatch(color)):
        errors.append(f"{path}.color: must be #rrggbb")
        color = None
    if len(errors) > n:
        return None
    return ZoneDef(
        slug=slug,
        title=title.strip(),
        include=tuple(include),
        exclude=tuple(exclude),
        description=description,
        parent=parent,
        mode=str(mode),
        auto_claim=flags["auto_claim"],
        protected=flags["protected"],
        reserve_for=reserve_for,
        fail_closed=flags["fail_closed"],
        services=tuple(services),
        commands=tuple(commands),
        mcp_tools=mcp_tools,
        color=color,
    )


def _check_tree(zones: Sequence[ZoneDef], errors: list[str]) -> None:
    """Parents exist, no cycles, bounded depth, and every childless zone has include globs."""
    by_slug = {z.slug: z for z in zones}
    has_child = {z.parent for z in zones if z.parent}
    for z in zones:
        if z.parent is not None and z.parent not in by_slug:
            errors.append(f"$.zones.{z.slug}.parent: unknown zone {z.parent!r}")
            continue
        depth, cur, seen = 0, z, {z.slug}
        while cur.parent is not None and cur.parent in by_slug:
            depth += 1
            if cur.parent in seen or depth > MAX_ZONE_DEPTH:
                errors.append(f"$.zones.{z.slug}.parent: cycle or nesting deeper than {MAX_ZONE_DEPTH}")
                break
            seen.add(cur.parent)
            cur = by_slug[cur.parent]
        if not z.include and z.slug not in has_child:
            errors.append(f"$.zones.{z.slug}.include: a zone without child zones needs at least one glob")


def check_glob(glob: Any) -> str | None:
    """Why ``glob`` is not an acceptable repo-relative zone/commons/ignore glob (None when it is)."""
    if not isinstance(glob, str) or not glob.strip():
        return "must be a non-empty string"
    if len(glob) > 256:
        return "longer than 256 characters"
    if glob != glob.strip() or any(ord(ch) < 32 for ch in glob) or "\\" in glob:
        return "no surrounding spaces, control characters or backslashes"
    if glob.startswith(("/", "~")):
        return "must be repo-relative (no leading / or ~)"
    if ".." in glob.split("/"):
        return "must not contain '..'"
    if glob.count("{") != glob.count("}") or glob.count("[") != glob.count("]"):
        return "unbalanced brackets"
    return None


def _glob_list(raw: Any, path: str, errors: list[str], limit: int) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        errors.append(f"{path}: must be a list of globs")
        return []
    if len(raw) > limit:
        errors.append(f"{path}: at most {limit} globs")
        return []
    out: list[str] = []
    for idx, g in enumerate(raw):
        reason = check_glob(g)
        if reason:
            errors.append(f"{path}[{idx}]: {reason}")
        elif g not in out:
            out.append(g)
    return out


def _str_list(raw: Any, path: str, errors: list[str], limit: int) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        errors.append(f"{path}: must be a list of strings")
        return []
    if len(raw) > limit:
        errors.append(f"{path}: at most {limit} entries")
        return []
    return list(dict.fromkeys(raw))


def _mcp_rules(raw: Any, path: str, errors: list[str]) -> tuple[tuple[str, str | None], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_MCP_RULES_PER_ZONE:
        errors.append(f"{path}: a list of at most {MAX_MCP_RULES_PER_ZONE} rules")
        return ()
    out: list[tuple[str, str | None]] = []
    for idx, rule in enumerate(raw):
        if isinstance(rule, str):
            rule = {"tool": rule}
        if not isinstance(rule, Mapping) or set(rule) - {"tool", "service"} or "tool" not in rule:
            errors.append(f"{path}[{idx}]: must be a tool pattern or {{tool, service}}")
            continue
        tool, service = rule.get("tool"), rule.get("service")
        if not isinstance(tool, str) or not _MCP_TOOL_RE.fullmatch(tool):
            errors.append(f"{path}[{idx}].tool: letters, digits, _ . - and * only")
            continue
        if service is not None and (not isinstance(service, str) or not _RESOURCE_RE.fullmatch(service)):
            errors.append(f"{path}[{idx}].service: must look like schema:<db> or deploy:<target>")
            continue
        out.append((tool, service))
    return tuple(out)


def default_commons_kind(glob: str) -> str:
    """Kind for a commons glob declared without one: lockfiles serialize, migrations append-only (§2)."""
    last = glob.rstrip("/").rsplit("/", 1)[-1]
    if last in LOCKFILE_NAMES:
        return "serialize"
    if "migrations" in glob.split("/") or glob.startswith(("db/migrate", "alembic/versions")):
        return "append_only"
    return "plain"


def _compile_commons(raw: Any, errors: list[str]) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    entries: list[tuple[Any, Any]] = []
    if isinstance(raw, Mapping):
        entries = list(raw.items())
    elif isinstance(raw, list):
        for idx, item in enumerate(raw):
            if isinstance(item, str):
                entries.append((item, None))
            elif isinstance(item, Mapping) and set(item) <= {"glob", "kind"} and "glob" in item:
                entries.append((item["glob"], item.get("kind")))
            else:
                errors.append(f"$.commons[{idx}]: must be a glob or {{glob, kind}}")
    else:
        errors.append("$.commons: must be a list or a mapping of glob to kind")
        return ()
    if len(entries) > MAX_COMMONS:
        errors.append(f"$.commons: at most {MAX_COMMONS} entries")
        return ()
    out: dict[str, str] = {}
    for glob, kind in entries:
        reason = check_glob(glob)
        if reason:
            errors.append(f"$.commons.{glob}: {reason}")
            continue
        kind = kind if kind is not None else default_commons_kind(str(glob))
        if kind not in S.COMMONS_KINDS:
            errors.append(f"$.commons.{glob}: kind must be one of {', '.join(S.COMMONS_KINDS)}")
            continue
        out[str(glob)] = str(kind)
    return tuple(out.items())


# ---------------------------------------------------------------------------
# Diff (D9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffItem:
    op: str  # add | remove | change
    target: str  # zone:<slug> | commons:<glob> | ignore:<glob> | enforcement
    field: str | None = None
    loosening: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "target": self.target, "field": self.field, "loosening": self.loosening, "reason": self.reason}


@dataclass(frozen=True)
class PolicyDiff:
    items: tuple[DiffItem, ...] = ()

    @property
    def loosening(self) -> bool:
        return any(i.loosening for i in self.items)

    @property
    def changed(self) -> bool:
        return bool(self.items)

    @property
    def loosening_items(self) -> list[DiffItem]:
        return [i for i in self.items if i.loosening]

    def summary(self, limit: int = 500) -> str:
        """Server template (slugs, globs clipped, counts): what ``zone.synced`` / ``zone.change_pending`` carry."""
        if not self.items:
            return "no policy change"
        zones = [i for i in self.items if i.target.startswith("zone:")]
        added = sorted({i.target[5:] for i in zones if i.op == "add"})
        removed = sorted({i.target[5:] for i in zones if i.op == "remove"})
        changed = sorted({i.target[5:] for i in zones if i.op == "change"})
        parts: list[str] = []
        if added:
            parts.append("added " + ", ".join(added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        if changed:
            parts.append("changed " + ", ".join(changed))
        commons = [i for i in self.items if i.target.startswith("commons:")]
        if commons:
            parts.append(f"commons {len(commons)} change(s)")
        ignore = [i for i in self.items if i.target.startswith("ignore:")]
        if ignore:
            parts.append(f"ignore {len(ignore)} change(s)")
        enf = next((i for i in self.items if i.target == "enforcement"), None)
        if enf is not None:
            parts.append(f"enforcement {enf.reason}")
        loose = self.loosening_items
        if loose:
            parts.append("loosening: " + ", ".join(sorted({_short(i) for i in loose})))
        text = "; ".join(parts)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def to_dict(self) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in self.items], "loosening": self.loosening, "summary": self.summary()}


def _short(item: DiffItem) -> str:
    if item.target == "enforcement":
        return "enforcement"
    kind, _, name = item.target.partition(":")
    name = re.sub(r"[^\w./*@+,=%-]", "?", name)[:60]
    if kind == "zone":
        return f"{item.op} {name}" if item.field is None else f"{name}.{item.field}"
    return f"{kind} {name}"


def _narrowed(old: ZoneDef, new: ZoneDef) -> bool:
    """New include globs drop an old one, or new exclude globs add one (conservative: any such edit)."""
    return bool(set(old.include) - set(new.include)) or bool(set(new.exclude) - set(old.exclude))


def diff_zone(old: ZoneDef | None, new: ZoneDef | None, *, live_claim: bool = False) -> list[DiffItem]:
    """Diff one zone. ``live_claim``: the zone has a live claim (narrowing it is then loosening)."""
    if old is None and new is None:
        return []
    if old is None:
        assert new is not None
        return [DiffItem("add", f"zone:{new.slug}")]
    target = f"zone:{old.slug}"
    if new is None:
        return [DiffItem("remove", target, loosening=True, reason="zone removed")]
    items: list[DiffItem] = []
    sensitive = live_claim or old.protected or old.fail_closed or old.reserve_for is not None
    if (old.include, old.exclude) != (new.include, new.exclude):
        loose = sensitive and _narrowed(old, new)
        items.append(DiffItem("change", target, "globs", loose, "narrowed" if loose else None))
    if old.protected and not new.protected:
        items.append(DiffItem("change", target, "protected", True, "protection removed"))
    elif old.protected != new.protected:
        items.append(DiffItem("change", target, "protected"))
    if old.fail_closed and not new.fail_closed:
        items.append(DiffItem("change", target, "fail_closed", True, "fail_closed removed"))
    elif old.fail_closed != new.fail_closed:
        items.append(DiffItem("change", target, "fail_closed"))
    if old.reserve_for != new.reserve_for:
        items.append(DiffItem("change", target, "reserve_for", old.reserve_for is not None, "reserve_for removed"))
    if old.mode != new.mode:
        loose = MODE_RANK[new.mode] < MODE_RANK[old.mode]
        items.append(DiffItem("change", target, "mode", loose, "mode lowered" if loose else None))
    for fld in ("services", "commands", "mcp_tools"):
        before, after = getattr(old, fld), getattr(new, fld)
        if before != after:
            loose = bool(set(before) - set(after))
            items.append(DiffItem("change", target, fld, loose, f"{fld} removed" if loose else None))
    for fld in ("title", "description", "parent", "auto_claim", "color"):
        if getattr(old, fld) != getattr(new, fld):
            items.append(DiffItem("change", target, fld))
    return items


def diff_policy(
    old: Policy,
    new: Policy,
    *,
    live_claim_slugs: Iterable[str] = (),
    current_enforcement: str | None = None,
) -> PolicyDiff:
    """Everything that changes from ``old`` to ``new``, each item marked loosening or not (D9).

    ``current_enforcement`` is the crew's effective enforcement (settings); a
    ``zones.yml`` that lowers it is loosening, one that omits it changes nothing.
    """
    live = set(live_claim_slugs)
    items: list[DiffItem] = []
    old_by, new_by = {z.slug: z for z in old.zones}, {z.slug: z for z in new.zones}
    for slug in sorted(set(old_by) | set(new_by)):
        items.extend(diff_zone(old_by.get(slug), new_by.get(slug), live_claim=slug in live))
    zone_globs = sorted({g for z in (*old.zones, *new.zones) for g in z.include})
    old_c, new_c = dict(old.commons), dict(new.commons)
    for glob in sorted(set(old_c) | set(new_c)):
        before, after = old_c.get(glob), new_c.get(glob)
        if before == after:
            continue
        target = f"commons:{glob}"
        if before is None:
            # a new commons entry skips auto-claim and task rules (rows 13-19) for the zone paths it covers
            loose = any_overlap([glob], zone_globs)
            items.append(DiffItem("add", target, loosening=loose, reason="commons over a zone" if loose else None))
        elif after is None:
            items.append(DiffItem("remove", target))
        else:
            loose = COMMONS_RANK[after] < COMMONS_RANK[before]
            items.append(DiffItem("change", target, "kind", loose, "commons kind lowered" if loose else None))
    for glob in sorted(set(old.ignore) ^ set(new.ignore)):
        added = glob in new.ignore
        # ignored paths skip every rule after row 5; that only loosens where a zone applies
        loose = added and any_overlap([glob], zone_globs)
        reason = "zone path ignored" if loose else None
        items.append(DiffItem("add" if added else "remove", f"ignore:{glob}", loosening=loose, reason=reason))
    base = current_enforcement or old.enforcement or "enforce"
    if new.enforcement is not None and new.enforcement != base:
        loose = ENFORCEMENT_RANK[new.enforcement] < ENFORCEMENT_RANK[base]
        items.append(DiffItem("change", "enforcement", None, loose, f"{base}->{new.enforcement}"))
    return PolicyDiff(tuple(items))


def interim_policy(old: Policy, new: Policy, diff: PolicyDiff, *, current_enforcement: str | None = None) -> Policy:
    """``new`` with every loosening item held back at its ``old`` value (applies while approval is pending)."""
    zones = {z.slug: z for z in new.zones}
    old_by = {z.slug: z for z in old.zones}
    order = [z.slug for z in new.zones]
    commons = dict(new.commons)
    old_commons = dict(old.commons)
    ignore = list(new.ignore)
    enforcement = new.enforcement
    for item in diff.loosening_items:
        kind, _, name = item.target.partition(":")
        if kind == "zone":
            prev = old_by[name]
            if item.op == "remove":
                zones[name] = prev
                order.append(name)
                continue
            cur = zones[name]
            if item.field == "globs":
                cur = replace(cur, include=prev.include, exclude=prev.exclude)
            elif item.field in ("protected", "fail_closed", "reserve_for", "mode", "services", "commands", "mcp_tools"):
                cur = replace(cur, **{item.field: getattr(prev, item.field)})
            zones[name] = cur
        elif kind == "commons":
            if item.op == "add":
                commons.pop(name, None)
            else:
                commons[name] = old_commons[name]
        elif kind == "ignore":
            ignore = [g for g in ignore if g != name]
        elif item.target == "enforcement":
            enforcement = old.enforcement if old.enforcement is not None else current_enforcement
    # A re-added parent keeps its children valid; a child whose parent vanished stays top-level.
    final = [zones[s] for s in dict.fromkeys(order)]
    slugs = {z.slug for z in final}
    final = [z if z.parent is None or z.parent in slugs else replace(z, parent=None) for z in final]
    return Policy(tuple(final), tuple(commons.items()), tuple(ignore), enforcement)


# ---------------------------------------------------------------------------
# Export (repo zones → zones.yml text)
# ---------------------------------------------------------------------------


def _zone_yaml(z: ZoneDef) -> dict[str, Any]:
    out: dict[str, Any] = {"title": z.title}
    if z.description:
        out["description"] = z.description
    if z.parent:
        out["parent"] = z.parent
    if z.include:
        out["include"] = list(z.include)
    if z.exclude:
        out["exclude"] = list(z.exclude)
    if z.mode != "exclusive":
        out["mode"] = z.mode
    if not z.auto_claim:
        out["auto_claim"] = False
    if z.protected:
        out["protected"] = True
    if z.reserve_for:
        out["reserve_for"] = z.reserve_for
    if z.fail_closed:
        out["fail_closed"] = True
    if z.services:
        out["services"] = list(z.services)
    if z.commands:
        out["commands"] = list(z.commands)
    if z.mcp_tools:
        out["mcp_tools"] = [{"tool": t, **({"service": s} if s else {})} for t, s in z.mcp_tools]
    if z.color:
        out["color"] = z.color
    return out


def export_yaml(policy: Policy) -> str:
    """Render a policy as ``zones.yml`` (deterministic; parses back to the same policy)."""
    doc: dict[str, Any] = {"version": POLICY_VERSION}
    if policy.enforcement:
        doc["enforcement"] = policy.enforcement
    doc["zones"] = {z.slug: _zone_yaml(z) for z in policy.zones}
    if policy.commons:
        doc["commons"] = {g: k for g, k in policy.commons}
    if policy.ignore:
        doc["ignore"] = list(policy.ignore)
    header = "# Remembra Crew zones. Loosening changes need a human approval in the dashboard.\n"
    return header + str(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100))


def export_patch(before: Policy, after: Policy, path: str = ".remembra/zones.yml") -> str:
    """Unified diff a human commits to apply a dashboard edit to a repo zone (D9)."""
    a = export_yaml(before).splitlines(keepends=True)
    b = export_yaml(after).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}"))


# ---------------------------------------------------------------------------
# Glob overlap (claims on overlapping zones conflict; crew_zone_overlaps)
# ---------------------------------------------------------------------------


def _glob_alternatives(glob: str) -> list[list[str]]:
    alts: list[list[str]] = []
    for alt in _expand_braces(glob):
        alt = alt.lstrip("/")
        while alt.startswith("./"):
            alt = alt[2:]
        trailing = alt.endswith("/")
        segs = [s for s in alt.split("/") if s not in ("", ".")] or ["**"]
        if trailing:
            segs.append("**")
        alts.append(segs)
        if not any(set(s) & _GLOB_CHARS for s in segs):
            alts.append([*segs, "**"])  # a literal glob also covers everything below it
    return alts


def _seg_wild(seg: str) -> bool:
    return bool(set(seg) & _GLOB_CHARS)


def _seg_compatible(a: str, b: str) -> bool:
    """Could some single path segment match both segment patterns? (Sound: never a false 'no'.)"""
    fa, fb = a.casefold(), b.casefold()
    wa, wb = _seg_wild(fa), _seg_wild(fb)
    if not wa and not wb:
        return fa == fb
    if not wa:
        return compile_glob(b, True).match(a)
    if not wb:
        return compile_glob(a, True).match(b)

    def literal_ends(s: str) -> tuple[str, str]:
        first = min(i for i, ch in enumerate(s) if ch in _GLOB_CHARS)
        last = max(i for i, ch in enumerate(s) if ch in _GLOB_CHARS or ch in "]}")
        return s[:first], s[last + 1 :]

    pa, sa = literal_ends(fa)
    pb, sb = literal_ends(fb)
    prefix_ok = pa.startswith(pb) or pb.startswith(pa)
    suffix_ok = sa.endswith(sb) or sb.endswith(sa)
    return prefix_ok and suffix_ok


def _segs_overlap(a: list[str], b: list[str]) -> bool:
    memo: dict[tuple[int, int], bool] = {}

    def go(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(a) and j == len(b):
            res = True
        elif i < len(a) and a[i] == "**":
            res = go(i + 1, j) or (j < len(b) and go(i, j + 1))
        elif j < len(b) and b[j] == "**":
            res = go(i, j + 1) or (i < len(a) and go(i + 1, j))
        elif i == len(a) or j == len(b):
            res = False
        else:
            res = _seg_compatible(a[i], b[j]) and go(i + 1, j + 1)
        memo[key] = res
        return res

    return go(0, 0)


def globs_overlap(a: str, b: str) -> bool:
    """True when some repo path could match both globs (conservative: case-folded, braces expanded)."""
    for alt_a in _glob_alternatives(a):
        for alt_b in _glob_alternatives(b):
            if _segs_overlap(alt_a, alt_b):
                return True
    return False


def any_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    bl = list(b)
    return any(globs_overlap(x, y) for x in a for y in bl)


# ---------------------------------------------------------------------------
# Suggestions (deterministic, from the tree snapshot) and no-zone bootstrap (D37)
# ---------------------------------------------------------------------------

CONTAINER_DIRS: Final = frozenset(
    {"src", "app", "apps", "packages", "lib", "libs", "services", "modules", "features", "pages", "components", "internal", "pkg"}
)
SKIP_DIRS: Final = frozenset(
    {
        "node_modules",
        ".git",
        ".github",
        ".remembra",
        ".husky",
        ".vscode",
        ".idea",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        "vendor",
        "public",
        "static",
        "assets",
        "docs",
        "doc",
        "test",
        "tests",
        "__tests__",
        "spec",
        "fixtures",
        "scripts",
        "bin",
        "tmp",
        ".next",
        ".venv",
        "venv",
        "migrations",
    }
)
MAX_SUGGESTIONS: Final = 20


@dataclass
class _Node:
    name: str
    files: int
    children: list[_Node] = field(default_factory=list)

    def total(self) -> int:
        return self.files + sum(c.total() for c in self.children)


def _to_node(raw: Mapping[str, Any]) -> _Node:
    return _Node(str(raw.get("name", "")), int(raw.get("files") or 0), [_to_node(c) for c in raw.get("children") or ()])


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return (s or "zone")[:48]


def suggest_zones(
    tree: Mapping[str, Any], churn: Mapping[str, int] | None = None, *, limit: int = MAX_SUGGESTIONS
) -> list[ZoneDef]:
    """Deterministic zone suggestions: the feature directories of the tree snapshot.

    Walks down through container directories (``src``, ``app``, ``packages`` …)
    and suggests each non-container, non-tooling child that holds files, as an
    exclusive leaf zone ``<path>/**``. Ranked by churn (if given), then file
    count, then path; at most ``limit``. Tree errors raise :class:`PolicyError`.
    """
    errors = S.validate_tree(tree)
    if errors:
        raise PolicyError(errors)
    root = _to_node(tree)
    churn = churn or {}
    found: list[tuple[str, _Node]] = []

    def walk(node: _Node, prefix: str, depth: int) -> None:
        for child in sorted(node.children, key=lambda c: c.name):
            name = child.name
            if name in SKIP_DIRS or name.startswith("."):
                continue
            path = f"{prefix}{name}"
            if name.lower() in CONTAINER_DIRS and child.children and depth < S.TREE_MAX_DEPTH:
                walk(child, path + "/", depth + 1)
            elif child.total() > 0:
                found.append((path, child))

    walk(root, "", 0)
    ranked = sorted(found, key=lambda pn: (-int(churn.get(pn[0], 0)), -pn[1].total(), pn[0]))[:limit]
    out: list[ZoneDef] = []
    used: set[str] = set()
    for path, _node in sorted(ranked, key=lambda pn: pn[0]):
        parts = path.split("/")
        slug = _slugify(parts[-1])
        if slug in used or slug == BUILTIN_ZONE_SLUG:
            slug = _slugify("-".join(parts[-2:]))
        base, n = slug, 2
        while slug in used or slug == BUILTIN_ZONE_SLUG:
            slug = f"{base[:44]}-{n}"
            n += 1
        used.add(slug)
        out.append(ZoneDef(slug=slug, title=parts[-1][:MAX_TITLE] or slug, include=(f"{path}/**",)))
    return out


def policy_from_rows(rows: Iterable[Mapping[str, Any]]) -> Policy:
    """Rebuild the declared part of a policy from ``crew_zones`` rows (archived and builtin rows skipped)."""
    by_id = {str(r["id"]): r for r in rows}
    zones: list[ZoneDef] = []
    for r in by_id.values():
        if r.get("archived_at") or r.get("builtin"):
            continue
        parent_row = by_id.get(str(r.get("parent_id") or ""))
        zones.append(zone_from_row(r, parent_slug=str(parent_row["slug"]) if parent_row else None))
    zones.sort(key=lambda z: z.slug)
    return Policy(tuple(zones))


def zone_from_row(row: Mapping[str, Any], *, parent_slug: str | None = None) -> ZoneDef:
    def arr(key: str) -> list[Any]:
        value = row.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return list(value or [])

    return ZoneDef(
        slug=str(row["slug"]),
        title=str(row.get("title") or row["slug"]),
        include=tuple(arr("include_globs")),
        exclude=tuple(arr("exclude_globs")),
        description=row.get("description"),
        parent=parent_slug,
        mode=str(row.get("mode") or "exclusive"),
        auto_claim=bool(row.get("auto_claim", 1)),
        protected=bool(row.get("protected", 0)),
        reserve_for=row.get("reserve_for"),
        fail_closed=bool(row.get("fail_closed", 0)),
        services=tuple(arr("services")),
        commands=tuple(arr("command_patterns")),
        mcp_tools=tuple((str(r.get("tool")), r.get("service")) for r in arr("mcp_tools") if isinstance(r, Mapping)),
        color=row.get("color"),
    )


def clone(policy: Policy) -> Policy:
    return Policy.from_dict(copy.deepcopy(policy.to_dict()))


def policy_json(policy: Policy) -> str:
    return json.dumps(policy.to_dict(), sort_keys=True, separators=(",", ":"))
