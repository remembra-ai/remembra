#!/usr/bin/env python3
"""Boot the real app on a production-shaped database and prove the main-DB migrations are safe.

Each step boots ``remembra.main.create_app()`` through its lifespan, in its own
process, from a code tree exported from git (Qdrant in memory, no API keys, no
network: the embedding provider points at a closed local port). Every database
lives in a temporary directory.

Production (``--prod``, ce067fd) is at schema 9: versions 1-4 and 6-9. Crew
mode's version 5 (``crew_agent_inbox_scoping``) was merged after them, so the
first boot of ``--this`` must apply 5 and nothing else.

1. Fresh: ``--this`` boots an empty database. Versions 1-9 apply, no backup is
   written, and the schema (tables, columns, indexes, versions) equals the
   upgraded one from step 2.
2. Upgrade: ``--prod`` creates its schema and the script fills every table
   with rows (realistic memories, FTS rows, tenants and inbox rows with and
   without a project tag written through the deployed code's own managers,
   plus a row with malformed metadata, then generic rows in every other table).
   ``--this`` then boots it: exactly version 5 applies; a pre-migration backup
   is written first (it holds exactly the pre-upgrade rows and versions 1-4,
   6-9); every old row keeps every old value (nothing deleted, no column
   dropped, nothing rewritten); v5's backfill gives each inbox row the
   expected project; a second boot changes nothing and writes no new backup.
3. Crew mode on: ``--this`` boots with ``REMEMBRA_CREW_MODE=true``. ``crew.db``
   is created next to the main database at the latest crew schema version and
   the main database is unchanged.
4. Rollback: ``--prod`` boots the upgraded database (the rollback image). Its
   schema is accepted as is, every row is kept, and its inbox reads and writes
   through the ``project_id`` column; ``--this`` then boots it again without a
   schema change.

    python scripts/maintenance/verify_release_migrations.py --prod ce067fd --this HEAD

Exits non-zero if any invariant fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

USERS = ("u_alice", "u_bob")
CREW_VERSION = (5, "crew_agent_inbox_scoping")
PROD_VERSIONS = [1, 2, 3, 4, 6, 7, 8, 9]
ALL_VERSIONS = list(range(1, 10))
# Inbox subject -> the project v5 must give the row (None: stays unscoped, visible to unrestricted keys only).
EXPECTED_INBOX_PROJECTS: dict[str, str | None] = {
    "review PR 12": "alpha",  # metadata.project_id tag (older clients)
    "tagged via project_id": "beta",  # the deployed project_id argument (metadata tag + no column yet)
    "bob only": "beta",  # other account, tagged
    "untagged note": "alpha",  # untagged; both agents' only known project for this owner is alpha
    "unknown agents": None,  # untagged; neither agent is known to work in any project
    "malformed metadata": None,  # metadata is not JSON: read as untagged, never an error
}
# Columns a boot may legitimately change on rows it did not write.
VOLATILE: dict[str, set[str]] = {
    "vector_store_state": {"updated_at"},
}


# ---------------------------------------------------------------------------
# Inside a booted app (run in a subprocess with the tree's src on PYTHONPATH)
# ---------------------------------------------------------------------------


def _value(col: dict[str, Any], table: str, n: int, user: str) -> Any:
    name, ctype = col["name"], (col["type"] or "").upper()
    lname = name.lower()
    if lname in ("user_id", "owner_id", "owner_user_id", "link_user_id") or lname.endswith("_user_id"):
        return user
    if lname in ("created_at", "updated_at", "applied_at", "last_accessed", "valid_from", "timestamp", "picked_up_at"):
        return f"2026-09-{10 + n:02d}T12:00:00+00:00"
    if lname.endswith("_at") or lname in ("until", "expires"):
        return None if col["notnull"] == 0 else f"2027-01-{10 + n:02d}T00:00:00+00:00"
    if lname in ("metadata", "details", "payload", "data", "config", "settings", "top_entities") or lname.endswith("_json"):
        return json.dumps({"seed": table, "n": n})
    if "INT" in ctype or lname in ("pinned", "is_active", "founding", "attempts", "count", "seats"):
        return n
    if any(t in ctype for t in ("REAL", "FLOA", "DOUB")):
        return 0.5 + n
    if lname == "email":
        return f"{user}.{table}.{n}@example.com"
    if lname == "status":
        return "active"
    return f"{table}:{name}:{user}:{n}"


async def _seed(app: Any) -> dict[str, Any]:
    """Rows in every table of the deployed schema: realistic ones through the app, the rest generic."""
    db = app.state.db
    conn = db.conn
    seeded: dict[str, int] = {}
    skipped: dict[str, str] = {}

    # Realistic rows first, through the deployed code's own writers where they exist.
    for i, uid in enumerate(USERS):
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (uid, f"{uid}@example.com", "$2b$12$" + "x" * 53, f"2026-08-0{i + 1}T00:00:00+00:00"),
        )
    for i in range(6):
        uid = USERS[i % 2]
        mid = f"mem_{i}"
        meta = {"agent_id": "claude-code", "project_id": "alpha"} if i % 3 == 0 else {"source": "note"}
        await conn.execute(
            """INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at, metadata, memory_type,
                                     pinned, access_count, valid_from)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mid,
                uid,
                "alpha" if i % 2 == 0 else "default",
                f"Mani met client number {i} in Lisbon; the invoice total was {100 * i} JMD.",
                f"2026-09-0{i + 1}T10:00:00+00:00",
                f"2026-09-0{i + 1}T10:00:00+00:00",
                json.dumps(meta),
                "handoff" if i == 3 else "fact",
                1 if i == 1 else 0,
                i,
                f"2026-09-0{i + 1}T10:00:00+00:00",
            ),
        )
        await conn.execute(
            "INSERT INTO memories_fts (id, user_id, project_id, content) VALUES (?, ?, ?, ?)",
            (mid, uid, "alpha" if i % 2 == 0 else "default", f"Mani met client number {i} in Lisbon"),
        )
    await conn.execute(
        "UPDATE memories SET superseded_by = 'mem_2', superseded_at = ? WHERE id = 'mem_0'", ("2026-09-03T10:00:00+00:00",)
    )
    await conn.commit()
    inbox = getattr(app.state, "inbox_manager", None)
    if inbox is not None:
        await inbox.send(USERS[0], "codex", "claude-code", "review PR 12", "tests pass on main", {"project_id": "alpha"})
        await inbox.send(USERS[0], "codex", "claude-code", "tagged via project_id", "deployed argument", {}, project_id="beta")
        await inbox.send(USERS[0], "claude-code", "codex", "untagged note", "no project", {})
        await inbox.send(USERS[0], "gemini", "qwen", "unknown agents", "no project and no known agent", {})
        await inbox.send(USERS[1], "codex", "claude-code", "bob only", "other account", {"project_id": "beta"})
        await conn.execute(
            """INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata, status,
                                        created_at)
               VALUES ('inbox_malformed', ?, 'gemini', 'qwen', 'malformed metadata', 'legacy row', '{not json', 'unread', ?)""",
            (USERS[0], "2026-09-12T12:00:00+00:00"),
        )
        await conn.commit()
    meter = getattr(app.state, "usage_meter", None)
    if meter is not None:  # the deployed metering API (b034314: register_tenant / record_*)
        from remembra.cloud.plans import PlanTier

        await meter.register_tenant(
            USERS[0],
            PlanTier.PRO,
            stripe_customer_id="ctm_alice",
            stripe_subscription_id="sub_alice",
            email="u_alice@example.com",
        )
        await meter.register_tenant(USERS[1], PlanTier.FREE, email="u_bob@example.com")
        for _ in range(3):
            await meter.record_store(USERS[0])
        await meter.record_recall(USERS[1])

    # Then at least two rows in every table still empty, parents before children.
    cursor = await conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
    tables = [(r[0], r[1] or "") for r in await cursor.fetchall()]
    skip = {"schema_version", "cloud_migrations", "sqlite_sequence", "sqlite_stat1"}
    tables = [(t, sql) for t, sql in tables if t not in skip and not t.startswith(("sqlite_", "memories_fts"))]
    fks: dict[str, list[tuple[str, str, str]]] = {}
    for t, _ in tables:
        cur = await conn.execute(f'PRAGMA foreign_key_list("{t}")')
        fks[t] = [(r[3], r[2], r[4]) for r in await cur.fetchall()]  # (from col, parent table, parent col)
    ordered: list[str] = []
    pending = [t for t, _ in tables]
    while pending:
        progressed = False
        for t in list(pending):
            parents = {p for _, p, _ in fks[t] if p != t}
            if parents <= set(ordered) | ({p for p in parents if p not in pending}):
                ordered.append(t)
                pending.remove(t)
                progressed = True
        if not progressed:
            ordered += pending
            break
    for t in ordered:
        cur = await conn.execute(f'SELECT COUNT(*) FROM "{t}"')
        have = (await cur.fetchone())[0]
        cur = await conn.execute(f'PRAGMA table_info("{t}")')
        cols = [{"name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]} for r in await cur.fetchall()]
        for n in range(have, 2):
            row: dict[str, Any] = {}
            for col in cols:
                fk = next((f for f in fks[t] if f[0] == col["name"]), None)
                if fk is not None:
                    pcol = fk[2] or "rowid"
                    cur = await conn.execute(f'SELECT "{pcol}" FROM "{fk[1]}" ORDER BY rowid LIMIT 1 OFFSET ?', (n,))
                    prow = await cur.fetchone()
                    if prow is None:
                        cur = await conn.execute(f'SELECT "{pcol}" FROM "{fk[1]}" ORDER BY rowid LIMIT 1')
                        prow = await cur.fetchone()
                    row[col["name"]] = prow[0] if prow else None
                elif col["pk"] and "INT" in (col["type"] or "").upper() and len([c for c in cols if c["pk"]]) == 1:
                    continue  # INTEGER PRIMARY KEY: let SQLite number it
                else:
                    row[col["name"]] = _value(col, t, n, USERS[n % 2])
            names = ", ".join(f'"{k}"' for k in row)
            marks = ", ".join("?" for _ in row)
            try:
                await conn.execute(f'INSERT INTO "{t}" ({names}) VALUES ({marks})', list(row.values()))
                seeded[t] = seeded.get(t, 0) + 1
            except Exception as e:  # a CHECK or UNIQUE rule the generic values miss: reported, not hidden
                skipped[t] = f"{type(e).__name__}: {e}"
        await conn.commit()
    return {"seeded_generic": seeded, "skipped": skipped}


async def _rollback_inbox(app: Any) -> dict[str, Any]:
    """The rollback image's inbox on the v5 database: a restricted read and a tagged write."""
    inbox = app.state.inbox_manager
    alpha = await inbox.get_for_agent(USERS[0], "claude-code", "all", project_ids=["alpha"])
    row = await inbox.send(USERS[0], "codex", "claude-code", "sent while rolled back", "b", {}, project_id="alpha")
    cursor = await app.state.db.conn.execute(
        "SELECT project_id, sender_kind, sender_verified, crew_id FROM agent_inbox WHERE inbox_id = ?", (row["inbox_id"],)
    )
    stored = await cursor.fetchone()
    return {"alpha_view": sorted(r["subject"] for r in alpha), "rollback_row": list(stored) if stored else None}


async def _boot(stage: str, db_path: str, seed: bool) -> dict[str, Any]:
    from qdrant_client import AsyncQdrantClient

    import remembra.config
    from remembra.storage.qdrant import QdrantStore

    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self: Any) -> Any:
        return local

    QdrantStore._get_client = local_client  # type: ignore[method-assign]
    remembra.config._settings = None
    import remembra.main as main

    app = main.create_app()
    out: dict[str, Any] = {"stage": stage}
    async with app.router.lifespan_context(app):
        if seed:
            out.update(await _seed(app))
        if stage == "rollback":
            out.update(await _rollback_inbox(app))
        out["schema_version"] = await app.state.db.get_schema_version()
        crew_db = getattr(app.state, "crew_db", None)
        if crew_db is not None:
            out["crew_db_path"] = getattr(crew_db, "db_path", None)
            out["crew_schema_version"] = await crew_db.get_schema_version()
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _export(repo: Path, ref: str, dest: Path) -> str:
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True).stdout.strip()
    archive = dest.with_suffix(".tar")
    subprocess.run(["git", "-C", str(repo), "archive", "-o", str(archive), sha, "src"], check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    return sha


def _run(
    base: Path, tree: str, stage: str, db: Path, build: str, seed: bool = False, extra_env: dict[str, str] | None = None
) -> dict[str, Any]:
    home = base / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONPATH": str(base / tree / "src"),
        "REMEMBRA_DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "REMEMBRA_CLOUD_ENABLED": "true",
        "REMEMBRA_DEBUG": "true",
        "REMEMBRA_OPENAI_API_KEY": "sk-not-used",
        "REMEMBRA_EMBEDDING_PROVIDER": "ollama",
        "REMEMBRA_OLLAMA_URL": "http://127.0.0.1:9",
        "REMEMBRA_QDRANT_URL": "http://127.0.0.1:9",
        "REMEMBRA_QDRANT_COLLECTION": f"verify_{stage}",
        "REMEMBRA_SLEEP_TIME_ENABLED": "false",
        # Schema only: no background job may touch rows between the snapshots.
        "REMEMBRA_TEMPORAL_CLEANUP_ENABLED": "false",
        "REMEMBRA_RECONCILE_INTERVAL_HOURS": "0",
        "REMEMBRA_PENDING_EMBEDDINGS_WORKER_ENABLED": "false",
        "REMEMBRA_WEBHOOKS_ENABLED": "true",
        "REMEMBRA_TYPESAFE_MODE": "off",
        "REMEMBRA_BUILD_SHA": build,
        "REMEMBRA_CREW_MODE": "false",  # production ships with Crew mode off
        **(extra_env or {}),
    }
    cmd = [sys.executable, __file__, "--stage", stage, "--db", str(db)] + (["--seed"] if seed else [])
    proc = subprocess.run(cmd, env=env, cwd=str(home), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"stage {stage} ({tree}) failed:\n{proc.stderr[-6000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _snapshot(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if not r[0].startswith(("sqlite_", "memories_fts_"))
        ]
        out: dict[str, Any] = {"tables": {}, "indexes": {}, "versions": []}
        for t in tables:
            info = list(conn.execute(f'PRAGMA table_info("{t}")'))
            cols = [r[1] for r in info]
            pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            key_cols = pk or ["rowid"]
            select = ", ".join(f'"{c}"' for c in key_cols + cols) if pk else "rowid, " + ", ".join(f'"{c}"' for c in cols)
            rows = {}
            for r in conn.execute(f'SELECT {select} FROM "{t}"'):
                k = tuple(r[: len(key_cols)])
                rows[repr(k)] = dict(zip(cols, r[len(key_cols) :], strict=True))
            out["tables"][t] = {"columns": cols, "rows": rows}
        out["indexes"] = sorted(
            (r[0], r[1])
            for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
        )
        if "schema_version" in out["tables"]:
            out["versions"] = sorted((r[0], r[1]) for r in conn.execute("SELECT version, name FROM schema_version"))
        return out
    finally:
        conn.close()


# One-time data migrations the release makes on purpose (cloud/metering.py, recorded in
# cloud_migrations): existing $49 Pro / $199 Team subscribers become the grandfathered
# tiers, and their teams follow the owner's plan. Any other changed value is a failure.
LEGACY_TIERS = {"pro": "legacy_pro_49", "team": "legacy_team_199"}


def _expected(table: str, col: str, old: dict[str, Any], new: dict[str, Any], after: dict[str, Any]) -> bool:
    tenants = after["tables"].get("cloud_tenants", {}).get("rows", {})
    if table == "cloud_tenants" and LEGACY_TIERS.get(old.get("plan")) == new.get("plan"):
        return col in ("plan", "updated_at")
    if table == "teams" and col == "plan":
        owner = tenants.get(repr((old.get("owner_id"),)))
        return owner is not None and owner.get("plan") in LEGACY_TIERS.values() and new.get("plan") == owner.get("plan")
    return False


def _lost(before: dict[str, Any], after: dict[str, Any], label: str, expected: list[str] | None = None) -> list[str]:
    """Rows, columns or values of ``before`` that ``after`` no longer has (intended data migrations go to ``expected``)."""
    problems: list[str] = []
    for t, old in before["tables"].items():
        new = after["tables"].get(t)
        if new is None:
            problems.append(f"{label}: table {t} dropped")
            continue
        missing_cols = [c for c in old["columns"] if c not in new["columns"]]
        if missing_cols:
            problems.append(f"{label}: {t} lost columns {missing_cols}")
        for key, row in old["rows"].items():
            now = new["rows"].get(key)
            if now is None:
                problems.append(f"{label}: {t} row {key} deleted")
                continue
            for c, v in row.items():
                if c in new["columns"] and now.get(c) != v and c not in VOLATILE.get(t, set()):
                    change = f"{label}: {t} row {key} column {c} changed {v!r} -> {now.get(c)!r}"
                    if expected is not None and _expected(t, c, row, now, after):
                        expected.append(change)
                    else:
                        problems.append(change)
    return problems


def _schema(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "tables": {t: sorted(v["columns"]) for t, v in snap["tables"].items()},
        "indexes": snap["indexes"],
        "versions": snap["versions"],
    }


def _versions(snap: dict[str, Any]) -> list[int]:
    return [v for v, _ in snap["versions"]]


def _inbox_projects(snap: dict[str, Any]) -> dict[str, Any]:
    return {r["subject"]: r.get("project_id") for r in snap["tables"]["agent_inbox"]["rows"].values()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prod", default="ce067fd", help="the deployed commit (schema 9: versions 1-4 and 6-9)")
    parser.add_argument("--this", default="HEAD")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--keep", help="copy the databases (and backups) here for inspection")
    parser.add_argument("--stage", help=argparse.SUPPRESS)
    parser.add_argument("--db", help=argparse.SUPPRESS)
    parser.add_argument("--seed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stage:
        try:
            print(json.dumps(asyncio.run(_boot(args.stage, args.db, args.seed))), flush=True)
        except BaseException:
            import traceback

            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)  # a failed lifespan can leave the aiosqlite thread running
        sys.stdout.flush()
        os._exit(0)

    repo = Path(args.repo)
    failures: list[str] = []
    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shas = {name: _export(repo, ref, base / name) for name, ref in (("prod", args.prod), ("this", args.this))}
        report["refs"] = shas
        this_build = shas["this"][:12]
        prod_build = shas["prod"][:12]

        # (1) fresh database: every version, no backup
        fresh_dir = base / "db_fresh"
        fresh_dir.mkdir()
        fresh = fresh_dir / "remembra.db"
        report["fresh_boot"] = _run(base, "this", "fresh", fresh, this_build)
        fresh_snap = _snapshot(fresh)
        if list(fresh_dir.glob("backups/*")):
            failures.append("fresh: a pre-migration backup was written for an empty database")
        if _versions(fresh_snap) != ALL_VERSIONS:
            failures.append(f"fresh: versions {_versions(fresh_snap)}, expected {ALL_VERSIONS}")
        if (fresh_dir / "crew.db").exists():
            failures.append("fresh: crew.db was created with Crew mode off")

        # (2) production-shaped database at the deployed schema 9
        prod_dir = base / "db_prod"
        prod_dir.mkdir()
        db = prod_dir / "data" / "remembra.db"
        db.parent.mkdir()
        report["prod_boot"] = _run(base, "prod", "prod", db, prod_build, seed=True)
        prod_snap = _snapshot(db)
        report["prod_versions"] = prod_snap["versions"]
        if _versions(prod_snap) != PROD_VERSIONS:
            failures.append(f"prod: versions {_versions(prod_snap)}, expected production's {PROD_VERSIONS}")
        report["prod_rows"] = {t: len(v["rows"]) for t, v in prod_snap["tables"].items()}
        report["prod_empty_tables"] = sorted(
            t for t, v in prod_snap["tables"].items() if not v["rows"] and t not in ("schema_version", "cloud_migrations")
        )
        prod_inbox_cols = set(prod_snap["tables"]["agent_inbox"]["columns"])
        if "project_id" in prod_inbox_cols or "trust_score" not in prod_inbox_cols:
            failures.append(f"prod: agent_inbox is not production-shaped: {sorted(prod_inbox_cols)}")
        missing = sorted(set(EXPECTED_INBOX_PROJECTS) - set(_inbox_projects(prod_snap)))
        if missing:
            failures.append(f"prod: inbox rows not seeded: {missing}")

        report["upgrade_boot"] = _run(base, "this", "upgrade", db, this_build)
        up_snap = _snapshot(db)
        applied_now = sorted(set(_versions(up_snap)) - set(_versions(prod_snap)))
        report["upgrade_applied"] = applied_now
        if applied_now != [CREW_VERSION[0]] or _versions(up_snap) != ALL_VERSIONS:
            failures.append(f"upgrade: applied {applied_now} (versions now {_versions(up_snap)}); expected only 5")
        if CREW_VERSION not in [tuple(v) for v in up_snap["versions"]]:
            failures.append(f"upgrade: version 5 is not {CREW_VERSION}: {up_snap['versions']}")
        intended: list[str] = []
        failures += _lost(prod_snap, up_snap, "upgrade", intended)
        report["intended_data_migrations"] = intended
        if intended:
            failures.append("upgrade: rows changed (production already ran the one-time data migrations)")
        backups = sorted((db.parent / "backups").glob("*"))
        report["backups"] = [b.name for b in backups]
        if len(backups) != 1 or this_build not in backups[0].name:
            failures.append(f"upgrade: expected one pre-migration backup for {this_build}, found {[b.name for b in backups]}")
        else:
            backup_copy = base / "backup-check.db"
            shutil.copy(backups[0], backup_copy)
            back_snap = _snapshot(backup_copy)
            if back_snap["tables"] != prod_snap["tables"] or back_snap["versions"] != prod_snap["versions"]:
                failures.append("upgrade: the pre-migration backup does not hold exactly the pre-upgrade database")
            if _versions(back_snap) != PROD_VERSIONS:
                failures.append(f"upgrade: the backup was not taken before migrating (versions {_versions(back_snap)})")
        inbox_cols = set(up_snap["tables"]["agent_inbox"]["columns"])
        if not {"project_id", "crew_id", "kind", "sender_kind", "sender_verified", "trust_score"} <= inbox_cols:
            failures.append(f"upgrade: agent_inbox columns {sorted(inbox_cols)}")
        projects = _inbox_projects(up_snap)
        report["upgrade_inbox_projects"] = projects
        for subject, expected in EXPECTED_INBOX_PROJECTS.items():
            if projects.get(subject) != expected:
                failures.append(f"upgrade: inbox row {subject!r} got project {projects.get(subject)!r}, expected {expected!r}")
        provenance = {
            (r.get("sender_kind"), r.get("sender_verified"), r.get("kind"))
            for r in up_snap["tables"]["agent_inbox"]["rows"].values()
        }
        if provenance != {("agent", 0, "directive")}:
            failures.append(f"upgrade: old inbox rows got provenance {sorted(provenance, key=str)}")
        if _schema(up_snap) != _schema(fresh_snap):
            a, b = _schema(up_snap), _schema(fresh_snap)
            report["schema_diff_upgraded_vs_fresh"] = {
                "tables_only_upgraded": sorted(set(a["tables"]) - set(b["tables"])),
                "tables_only_fresh": sorted(set(b["tables"]) - set(a["tables"])),
                "column_diffs": {
                    t: {
                        "upgraded": sorted(set(a["tables"][t]) - set(b["tables"][t])),
                        "fresh": sorted(set(b["tables"][t]) - set(a["tables"][t])),
                    }
                    for t in set(a["tables"]) & set(b["tables"])
                    if a["tables"][t] != b["tables"][t]
                },
                "indexes_only_upgraded": sorted(set(map(tuple, a["indexes"])) - set(map(tuple, b["indexes"]))),
                "indexes_only_fresh": sorted(set(map(tuple, b["indexes"])) - set(map(tuple, a["indexes"]))),
                "versions": [a["versions"], b["versions"]],
            }
            failures.append("upgrade: the upgraded schema differs from a fresh database's")
        report["upgraded_versions"] = up_snap["versions"]

        report["second_boot"] = _run(base, "this", "second", db, this_build)
        again = _snapshot(db)
        failures += _lost(up_snap, again, "second boot")
        added = {t: len(v["rows"]) - len(up_snap["tables"].get(t, {"rows": {}})["rows"]) for t, v in again["tables"].items()}
        if any(added.values()) or _schema(again) != _schema(up_snap):
            failures.append(f"second boot: not idempotent (rows added {dict((k, v) for k, v in added.items() if v)})")
        if len(sorted((db.parent / "backups").glob("*"))) != 1:
            failures.append("second boot: wrote another pre-migration backup")

        # (3) Crew mode on: crew.db is created beside the main database; the main database is untouched
        report["crew_mode_boot"] = _run(base, "this", "crew_on", db, this_build, extra_env={"REMEMBRA_CREW_MODE": "true"})
        crew_file = db.parent / "crew.db"
        if not crew_file.is_file():
            failures.append("crew mode: crew.db was not created next to the main database")
        else:
            conn = sqlite3.connect(crew_file)
            try:
                crew_versions = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
                crew_tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
            finally:
                conn.close()
            report["crew_db"] = {"versions": crew_versions, "tables": len(crew_tables)}
            if not crew_versions or report["crew_mode_boot"].get("crew_schema_version") != crew_versions[-1]:
                failures.append(f"crew mode: crew.db versions {crew_versions}, boot saw {report['crew_mode_boot']}")
            if "crews" not in crew_tables or "crew_events" not in crew_tables:
                failures.append(f"crew mode: crew.db tables {crew_tables}")
        crew_on = _snapshot(db)
        failures += _lost(again, crew_on, "crew mode boot")
        if _schema(crew_on) != _schema(again):
            failures.append("crew mode: the main database schema changed")

        # (4) rollback: the deployed image on the upgraded database, then this release again
        report["rollback_boot"] = _run(base, "prod", "rollback", db, prod_build)
        rolled = _snapshot(db)
        failures += _lost(crew_on, rolled, "rollback")
        if rolled["versions"] != crew_on["versions"]:
            failures.append(f"rollback: schema_version changed to {rolled['versions']}")
        rb = report["rollback_boot"]
        if rb.get("alpha_view") != ["review PR 12"]:
            failures.append(f"rollback: a key limited to alpha sees {rb.get('alpha_view')}")
        if rb.get("rollback_row") != ["alpha", "agent", 0, None]:
            failures.append(f"rollback: a message sent while rolled back is stored as {rb.get('rollback_row')}")
        report["after_rollback_boot"] = _run(base, "this", "after_rollback", db, this_build)
        after = _snapshot(db)
        failures += _lost(rolled, after, "this after rollback")
        if after["versions"] != rolled["versions"] or _schema(after) != _schema(rolled):
            failures.append("this after rollback: the schema changed")
        if args.keep:
            for name in ("db_fresh", "db_prod"):
                shutil.copytree(base / name, Path(args.keep) / name, dirs_exist_ok=True)

    print(json.dumps(report, indent=2, default=str))
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print("OK" if not failures else f"{len(failures)} failure(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
