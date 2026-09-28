"""The published role/permission tables are the code's, and every permission guards something.

Truth audit 2026-09-26: SECURITY.md (P-277) and docs/guides/rbac.md (P-279)
listed permissions that do not exist (memory:create, entity:merge,
audit:read ...) and role grants the code does not make; SECURITY.md also
claimed a configurable 7-day session and audit events nothing writes (P-278).
The docs now carry ``permission_table()`` verbatim, and these tests fail when
the code's role map, the routes or the docs move apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from remembra.api.router import api_router
from remembra.api.v1 import keys
from remembra.auth.rbac import PERMISSION_ATTR, PERMISSION_SUMMARIES, ROLE_PERMISSIONS, Permission, permission_table
from remembra.auth.users import JWT_EXPIRATION_HOURS
from remembra.security.audit import AuditAction
from tests.security_harness import MASTER_KEY, secure_app

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "remembra"
DOCS_WITH_TABLE = (ROOT / "docs" / "guides" / "rbac.md", ROOT / "SECURITY.md")
START, END = "<!-- permission-table:start -->", "<!-- permission-table:end -->"

# Defined but not checked by any route yet; its table row says so.
UNCHECKED = {Permission.ADMIN_USERS}


def _table_in(path: Path) -> str:
    text = path.read_text()
    assert START in text and END in text, f"{path.name} has no generated permission table"
    return text[text.index(START) + len(START) : text.index(END)].strip()


def test_docs_carry_the_role_table_the_code_enforces():
    expected = permission_table()
    for path in DOCS_WITH_TABLE:
        assert _table_in(path) == expected, (
            f"{path.relative_to(ROOT)} is out of date. Paste the output of\n"
            "  python -c 'from remembra.auth.rbac import permission_table; print(permission_table())'\n"
            f"between the permission-table markers:\n\n{expected}"
        )


def test_generated_table_matches_the_role_map_row_by_row():
    rows = permission_table().splitlines()[2:]
    assert len(rows) == len(Permission) == len(PERMISSION_SUMMARIES)
    for perm, row in zip(Permission, rows, strict=True):
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[0] == f"`{perm.value}`"
        held = {role.value for role, perms in ROLE_PERMISSIONS.items() if perm in perms}
        assert {r for r, mark in zip(("admin", "editor", "viewer"), cells[1:4], strict=True) if mark == "yes"} == held


def _route_permissions() -> dict[Permission, set[str]]:
    """Permissions each route declares as a dependency (walked from the production router)."""
    app = FastAPI()
    app.include_router(api_router)
    found: dict[Permission, set[str]] = {}

    def walk(dependant, route: str) -> None:
        for dep in dependant.dependencies:
            perm = getattr(dep.call, PERMISSION_ATTR, None)
            if perm is not None:
                found.setdefault(perm, set()).add(route)
            walk(dep, route)

    for route in iter_route_contexts(app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            walk(dependant, f"{','.join(sorted(route.methods or ['WS']))} {route.path}")
    return found


def _in_handler_permissions() -> set[str]:
    """Permission names passed as literals to in-handler checks (``has_permission(user, "x:y")`` etc.)."""
    names: set[str] = set()
    for path in [*(SRC / "api").rglob("*.py"), *(SRC / "auth").rglob("*.py")]:
        text = path.read_text()
        names |= set(re.findall(r'(?:has_permission|_require)\(\s*\w+\s*,\s*"([a-z_]+:[a-z_]+)"', text))
        names |= set(re.findall(r'require_permission\(\s*"([a-z_]+:[a-z_]+)"', text))
        for a, b in re.findall(r'"([a-z_]+:[a-z_]+)" if [\w.]+ else "([a-z_]+:[a-z_]+)"', text):
            names |= {a, b}
    return names


def test_every_permission_guards_a_route_except_the_documented_unchecked_one():
    declared = _route_permissions()
    assert set(declared) | UNCHECKED == set(Permission), set(Permission) - set(declared) - UNCHECKED
    assert not UNCHECKED & set(declared), "a route now checks it: update its summary in auth/rbac.py and the docs"
    assert PERMISSION_SUMMARIES[Permission.ADMIN_USERS] == "Nothing yet: no route checks it"


def test_every_permission_name_the_code_checks_exists():
    names = _in_handler_permissions()
    assert {"memory:store", "memory:recall", "memory:delete"} <= names  # the scan sees the in-handler checks
    assert names <= {p.value for p in Permission}, names - {p.value for p in Permission}


# ---------------------------------------------------------------------------
# SECURITY.md: sessions and the audit log (P-278)
# ---------------------------------------------------------------------------


def test_security_md_states_the_fixed_session_length():
    md = (ROOT / "SECURITY.md").read_text()
    jwt = md[md.index("### JWT Tokens") :].split("\n### ")[0].split("\n## ")[0]
    assert f"Expiration: {JWT_EXPIRATION_HOURS} hours" in jwt
    assert "7 days" not in jwt and "(configurable)" not in jwt


def _operator_files() -> list[Path]:
    """Files that tell an operator what to set: docs (not the changelog), SECURITY.md, README, env and compose files."""
    files = [p for p in (ROOT / "docs").rglob("*.md") if p.name != "changelog.md"]
    files += [ROOT / name for name in ("SECURITY.md", "README.md", "ARCHITECTURE.md")]
    files += sorted(ROOT.glob(".env*.example")) + sorted(ROOT.glob("docker-compose*.yml")) + sorted(ROOT.glob("Dockerfile*"))
    files += sorted((ROOT / "landing").glob("*.html")) + sorted((ROOT / "landing").glob("*.txt"))
    return [p for p in files if p.name != "changelog.html"]


def test_session_length_is_fixed_and_has_no_setting(monkeypatch):
    """Owner decision 5 (2026-09-27): the session-length setting was never read, so it is gone.

    ``jwt_expiration_hours`` / ``REMEMBRA_JWT_EXPIRATION_HOURS`` did nothing:
    dashboard sessions last a fixed ``JWT_EXPIRATION_HOURS`` (24). A server
    whose environment still sets the variable starts as before and ignores it.
    """
    import jwt as pyjwt

    from remembra.auth.users import JWT_ALGORITHM, UserManager
    from remembra.config import Settings

    assert "jwt_expiration_hours" not in Settings.model_fields
    monkeypatch.setenv("REMEMBRA_JWT_EXPIRATION_HOURS", "720")
    settings = Settings(openai_api_key="test")
    assert not hasattr(settings, "jwt_expiration_hours")

    secret = "s" * 40
    claims = pyjwt.decode(
        UserManager(db=None, jwt_secret=secret).create_jwt_token("user_1", "a@example.com"),  # type: ignore[arg-type]
        secret,
        algorithms=[JWT_ALGORITHM],
    )
    assert claims["exp"] - claims["iat"] == JWT_EXPIRATION_HOURS * 3600 == 24 * 3600

    offenders = [str(p.relative_to(ROOT)) for p in _operator_files() if "JWT_EXPIRATION_HOURS" in p.read_text()]
    assert not offenders, f"these still name the removed setting: {offenders}"


def _written_audit_actions() -> set[str]:
    """Audit actions some code path outside security/audit.py actually writes."""
    written: set[str] = set()
    helpers = {
        "log_memory_store": AuditAction.MEMORY_STORE,
        "log_memory_recall": AuditAction.MEMORY_RECALL,
        "log_memory_forget": AuditAction.MEMORY_FORGET,
        "log_key_created": AuditAction.KEY_CREATED,
    }
    for path in SRC.rglob("*.py"):
        if path == SRC / "security" / "audit.py":
            continue
        text = path.read_text()
        written |= {AuditAction[name].value for name in re.findall(r"AuditAction\.([A-Z_]+)", text)}
        written |= {action.value for helper, action in helpers.items() if f".{helper}(" in text}
        if ".log_event(" in text:  # free-form action strings (api/v1/keys.py)
            written |= set(re.findall(r'action\s*=\s*"([a-z_]+)"', text))
    return written


def test_security_md_lists_only_audit_events_the_code_writes():
    md = (ROOT / "SECURITY.md").read_text()
    table = md[md.index("### Tracked Events") :].split("\n### ")[0]
    documented = set(re.findall(r"^\| ([^|]+)\|", table, re.M)) - {"Event ", "---"}
    events = {e.lower() for cell in documented for e in re.findall(r"`([A-Za-z_]+)`", cell)}
    assert events, "no events parsed from SECURITY.md"
    written = _written_audit_actions()
    assert events <= written, f"documented but never written: {sorted(events - written)}"
    assert written <= events, f"written but not documented: {sorted(written - events)}"
    for never in ("auth_success", "auth_failed", "auth_rate_limited", "memory_get", "key_listed"):
        assert never not in events


# ---------------------------------------------------------------------------
# Key-creation examples
# ---------------------------------------------------------------------------


async def test_docs_send_the_master_key_the_way_the_server_reads_it(tmp_path):
    """POST /keys reads the master key from X-API-Key only; the docs said Authorization: Bearer."""
    async with secure_app(tmp_path, [keys.router]) as h:
        body = {"user_id": "user_123", "name": "Production", "role": "editor"}
        r = await h.client.post("/api/v1/keys", json=body, headers={"Authorization": f"Bearer {MASTER_KEY}"})
        assert r.status_code == 401, r.text
        r = await h.client.post("/api/v1/keys", json=body, headers={"X-API-Key": MASTER_KEY})
        assert r.status_code == 201, r.text
        assert {"id", "key", "user_id", "name", "rate_limit_tier", "role", "project_ids", "agent_id"} <= set(r.json())

    bearer_master = re.compile(r"Bearer\s*(?:\$?\{?MASTER_KEY|master_key|\$REMEMBRA_AUTH_MASTER_KEY)", re.I)
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "docs").rglob("*.md") if bearer_master.search(p.read_text())]
    assert not offenders, offenders
