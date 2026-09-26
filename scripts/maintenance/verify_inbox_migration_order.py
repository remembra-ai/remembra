#!/usr/bin/env python3
"""Check that the crew branch's main-DB migration v5 still applies after this branch.

Production runs the Phase 0 inbox fix first (no schema change: a row's project
is its ``metadata.project_id`` tag) and the crew branch later (v5 adds
``agent_inbox.project_id`` and backfills it). This replays that order on one
throw-away SQLite file, each step with its own code tree taken from git:

1. ``--prod`` (the deployed commit) creates the schema and writes inbox rows;
2. ``--this`` (this branch) migrates, writes project-tagged rows, reads as a
   project-restricted caller;
3. ``--crew`` migrates (v5 must apply, not be skipped) and reads;
4. ``--this`` runs again on the migrated file (the column path).

    python scripts/maintenance/verify_inbox_migration_order.py \\
        --prod b034314 --this HEAD --crew feat/crew

Exits non-zero if any invariant fails. Writes only to a temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

TAGGED = {"prod tagged alpha": "alpha", "prod tagged beta": "beta", "this tagged alpha": "alpha", "this post-v5": "beta"}


async def _stage(stage: str, db_path: str) -> dict[str, Any]:
    from remembra.inbox.manager import InboxManager
    from remembra.storage.database import Database

    db = Database(db_path)
    await db.connect()
    await db.init_schema()
    inbox = InboxManager(db)
    await inbox.init_schema()
    out: dict[str, Any] = {"stage": stage}
    if stage == "prod":
        await inbox.send("u1", "codex", "claude-code", "prod tagged alpha", "b", {"project_id": "alpha"})
        await inbox.send("u1", "codex", "claude-code", "prod untagged", "b", {})
        await inbox.send("u1", "claude-code", "codex", "prod tagged beta", "b", {"project_id": "beta"})
    elif stage == "this":
        await inbox.send("u1", "claude-code", "codex", "this tagged alpha", "b", {}, project_id="alpha")
        await inbox.send("u1", "claude-code", "codex", "this untagged", "b", {})
        rows = await inbox.get_for_agent("u1", "claude-code", "all", project_ids=["alpha"])
        out["alpha_view"] = sorted(r["subject"] for r in rows)
    elif stage == "crew":
        rows = await inbox.get_for_agent("u1", "claude-code", "all", project_ids=["alpha"])
        out["alpha_view"] = sorted(r["subject"] for r in rows)
    elif stage == "this_after_crew":
        await inbox.send("u1", "claude-code", "codex", "this post-v5", "b", {}, project_id="beta")
        rows = await inbox.get_for_agent("u1", "codex", "all", project_ids=["beta"])
        out["beta_view"] = sorted(r["subject"] for r in rows)
    cursor = await db.conn.execute("SELECT version, name FROM schema_version ORDER BY version")
    out["schema_version"] = [list(r) for r in await cursor.fetchall()]
    cursor = await db.conn.execute("PRAGMA table_info(agent_inbox)")
    cols = [r[1] for r in await cursor.fetchall()]
    out["has_project_column"] = "project_id" in cols
    if "project_id" in cols:
        cursor = await db.conn.execute("SELECT subject, project_id FROM agent_inbox ORDER BY subject")
        out["rows"] = {r[0]: r[1] for r in await cursor.fetchall()}
    await db.close()
    return out


def _export(repo: Path, ref: str, dest: Path) -> str:
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True).stdout.strip()
    archive = dest.with_suffix(".tar")
    subprocess.run(["git", "-C", str(repo), "archive", "-o", str(archive), sha, "src"], check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    return sha


def _run(tree: Path, stage: str, db_path: Path) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
    env["PYTHONPATH"] = str(tree / "src")
    proc = subprocess.run(
        [sys.executable, __file__, "--stage", stage, "--db", str(db_path)], env=env, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"stage {stage} failed:\n{proc.stderr[-4000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prod", default="main")
    parser.add_argument("--this", default="HEAD")
    parser.add_argument("--crew", default="feat/crew")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--stage", help=argparse.SUPPRESS)
    parser.add_argument("--db", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stage:
        print(json.dumps(asyncio.run(_stage(args.stage, args.db))))
        return 0

    repo = Path(args.repo)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        shas = {
            name: _export(repo, ref, base / name) for name, ref in (("prod", args.prod), ("this", args.this), ("crew", args.crew))
        }
        db = base / "copy.db"
        results = [
            _run(base / "prod", "prod", db),
            _run(base / "this", "this", db),
            _run(base / "crew", "crew", db),
            _run(base / "this", "this_after_crew", db),
        ]
    print(json.dumps({"refs": shas, "results": results}, indent=2))

    failures: list[str] = []
    prod, this, crew, again = results
    if this["has_project_column"]:
        failures.append("this branch added agent_inbox.project_id (it must leave the schema to crew v5)")
    if this.get("alpha_view") != ["prod tagged alpha"]:
        failures.append(f"restricted view before v5 is {this.get('alpha_view')}")
    applied = {v: n for v, n in crew["schema_version"]}
    if applied.get(5) != "crew_agent_inbox_scoping" or not crew["has_project_column"]:
        failures.append(f"crew v5 did not apply: {crew['schema_version']}")
    rows = again.get("rows") or {}
    for subject, project in TAGGED.items():
        if rows.get(subject) != project:
            failures.append(f"{subject!r}: project column {rows.get(subject)!r}, expected {project!r}")
    if "this post-v5" not in (again.get("beta_view") or []):
        failures.append("this branch cannot read its own post-v5 row through the column")
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print("OK" if not failures else f"{len(failures)} failure(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
