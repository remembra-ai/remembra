#!/usr/bin/env python3
"""Check the agent inbox across the production -> Crew mode upgrade, and back.

Production (``--prod``, 4d335ce: 0.16.1) is at main-DB schema 10 (versions 1-4 and 6-10):
a row's project is its ``metadata.project_id`` tag, and rows carry the trust
policy's ``trust_score`` (v6). This release (``--this``) adds Crew mode's
version 5: ``agent_inbox.project_id``, ``crew_id``, ``kind``, ``sender_kind``
and ``sender_verified``, with the project backfilled once. This replays the
order on one throw-away SQLite file, each step with its own code tree from git:

1. ``--prod`` creates the schema and writes inbox rows (tagged through
   metadata, tagged through its ``project_id`` argument, untagged);
2. ``--this`` migrates (only v5 applies), reads as a project-restricted caller
   and as an agent-scoped caller, and writes a row with sender provenance;
3. ``--prod`` again (the rollback image) reads and writes through the column;
4. ``--this`` again: nothing to migrate, and the rollback's row is scoped.

    python scripts/maintenance/verify_inbox_migration_order.py --prod 4d335ce --this HEAD

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

TAGGED = {
    "prod tagged alpha": "alpha",
    "prod argument beta": "beta",
    "prod tagged beta": "beta",
    "this post-v5": "beta",
    "prod after rollback": "alpha",
}
PROD_VERSIONS = [1, 2, 3, 4, 6, 7, 8, 9, 10]


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
        await inbox.send("u1", "codex", "claude-code", "prod argument beta", "b", {}, project_id="beta")
        await inbox.send("u1", "codex", "claude-code", "prod untagged", "b", {})
        await inbox.send("u1", "claude-code", "codex", "prod tagged beta", "b", {"project_id": "beta"})
    elif stage == "this":
        rows = await inbox.get_for_agent("u1", "claude-code", "all", project_ids=["alpha"])
        out["alpha_view"] = sorted(r["subject"] for r in rows)
        listed = await inbox.list_messages("u1", status="all", recipient="codex")
        out["codex_only_view"] = sorted(r["subject"] for r in listed["items"])
        row = await inbox.send("u1", "claude-code", "codex", "this post-v5", "b", {}, project_id="beta", sender_verified=True)
        out["post_v5_row"] = {k: row.get(k) for k in ("project_id", "sender_kind", "sender_verified", "kind", "trust_score")}
    elif stage == "prod_rollback":
        rows = await inbox.get_for_agent("u1", "codex", "all", project_ids=["beta"])
        out["beta_view"] = sorted(r["subject"] for r in rows)
        await inbox.send("u1", "codex", "claude-code", "prod after rollback", "b", {}, project_id="alpha")
    elif stage == "this_again":
        rows = await inbox.get_for_agent("u1", "claude-code", "all", project_ids=["alpha"])
        out["alpha_view"] = sorted(r["subject"] for r in rows)
    cursor = await db.conn.execute("SELECT version, name FROM schema_version ORDER BY version")
    out["schema_version"] = [list(r) for r in await cursor.fetchall()]
    cursor = await db.conn.execute("PRAGMA table_info(agent_inbox)")
    cols = [r[1] for r in await cursor.fetchall()]
    out["columns"] = cols
    if "project_id" in cols:
        cursor = await db.conn.execute(
            "SELECT subject, project_id, sender_kind, sender_verified, trust_score FROM agent_inbox ORDER BY subject"
        )
        out["rows"] = {
            r[0]: {"project_id": r[1], "sender_kind": r[2], "sender_verified": r[3], "trust_score": r[4]}
            for r in await cursor.fetchall()
        }
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
    parser.add_argument("--prod", default="4d335ce")
    parser.add_argument("--this", default="HEAD")
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
        shas = {name: _export(repo, ref, base / name) for name, ref in (("prod", args.prod), ("this", args.this))}
        db = base / "copy.db"
        results = [
            _run(base / "prod", "prod", db),
            _run(base / "this", "this", db),
            _run(base / "prod", "prod_rollback", db),
            _run(base / "this", "this_again", db),
        ]
    print(json.dumps({"refs": shas, "results": results}, indent=2))

    failures: list[str] = []
    prod, this, rollback, again = results
    if [v for v, _ in prod["schema_version"]] != PROD_VERSIONS or "project_id" in prod["columns"]:
        failures.append(f"prod is not at production's schema: {prod['schema_version']}")
    applied = {v: n for v, n in this["schema_version"]}
    names = (applied.get(5), applied.get(10))
    if sorted(applied) != list(range(1, 11)) or names != ("crew_agent_inbox_scoping", "account_reviews"):
        failures.append(f"v5 did not apply after 6-10: {this['schema_version']}")
    if this.get("alpha_view") != ["prod tagged alpha"]:
        failures.append(f"restricted view after v5 is {this.get('alpha_view')}")
    if this.get("codex_only_view") != ["prod tagged beta"]:
        failures.append(f"agent-scoped view after v5 is {this.get('codex_only_view')}")
    post = this.get("post_v5_row") or {}
    if post.get("sender_kind") != "agent" or post.get("sender_verified") != 1 or post.get("trust_score") is None:
        failures.append(f"post-v5 row lost provenance or trust score: {post}")
    if rollback["schema_version"] != this["schema_version"]:
        failures.append(f"the rollback image changed the schema: {rollback['schema_version']}")
    if rollback.get("beta_view") != ["prod tagged beta", "this post-v5"]:
        failures.append(f"rollback image's restricted view is {rollback.get('beta_view')}")
    if again["schema_version"] != this["schema_version"]:
        failures.append("this release changed the schema on its second boot")
    if again.get("alpha_view") != ["prod after rollback", "prod tagged alpha"]:
        failures.append(f"restricted view after the rollback is {again.get('alpha_view')}")
    rows = again.get("rows") or {}
    for subject, project in TAGGED.items():
        if (rows.get(subject) or {}).get("project_id") != project:
            failures.append(f"{subject!r}: project column {rows.get(subject)!r}, expected {project!r}")
    if (rows.get("prod untagged") or {}).get("project_id") is not None:
        failures.append(f"'prod untagged' was given a project: {rows.get('prod untagged')}")
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print("OK" if not failures else f"{len(failures)} failure(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
