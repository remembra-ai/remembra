"""docs/DEPLOYING.md gives operators commands that work inside the production image.

The image has no sqlite3 CLI and does not contain the maintenance .sql files,
so the database snippets are Python heredocs. These tests pull the snippets out
of the document and run them, exactly as written, against a database built by
the current code, and check the documented pre-migration backup facts against
the code that takes the backup.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from remembra.auth.keys import APIKeyManager
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier
from remembra.config import Settings
from remembra.storage.backup import BACKUP_PREFIX, pre_migration_backup
from remembra.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "DEPLOYING.md").read_text()
ROLLBACK_SQL = ROOT / "scripts" / "maintenance" / "rollback_plans_v2.sql"
B034314_PLANS = {"free", "pro", "team", "enterprise"}


def _bash_blocks() -> list[str]:
    return re.findall(r"```bash\n(.*?)```", DOC, flags=re.S)


def _heredoc(marker: str) -> tuple[list[str], str]:
    """The ``python - <args> <<'PY' … PY`` snippet whose first line is ``# <marker>``: (args, code)."""
    for block in _bash_blocks():
        for match in re.finditer(r"python - (.*?) <<'PY'\n(.*?)\nPY(?:\n|$)", block, flags=re.S):
            code = match.group(2)
            if code.splitlines()[0].strip() == f"# {marker}":
                return match.group(1).split(), code
    raise AssertionError(f"no heredoc snippet marked {marker!r} in docs/DEPLOYING.md")


def _without_heredocs(block: str) -> str:
    return re.sub(r"<<'PY'\n.*?\nPY(?:\n|$)", "\n", block, flags=re.S)


def test_no_command_needs_the_sqlite3_cli_or_a_sql_file_inside_the_image() -> None:
    for block in _bash_blocks():
        shell = _without_heredocs(block)
        assert not re.search(r"(^|[\s;&|(])sqlite3(\s|$)", shell, flags=re.M), shell
        # A .sql file is never read from inside the container unless it was copied in first.
        for line in shell.splitlines():
            if "docker exec" in line and ".sql" in line:
                assert "/tmp/rollback_plans_v2.sql" in line, line
    # What the doc says about the image is still true of Dockerfile.cloud.
    dockerfile = (ROOT / "Dockerfile.cloud").read_text()
    production = dockerfile.split("AS production", 1)[1]
    installs = re.search(r"apt-get install -y --no-install-recommends(.*?)&&", production, flags=re.S)
    assert installs is not None and "sqlite3" not in installs.group(1)
    assert "COPY scripts/maintenance/*.py ./scripts/maintenance/" in production
    assert "*.sql" not in production


async def _production_shaped_db(path: Path) -> tuple[str, str]:
    """A database built by the current code, after the relay-launch boot; returns (agent key id, plain key id)."""
    db = Database(str(path))
    await db.connect()
    await db.init_schema()
    try:
        now = "2026-09-20T00:00:00+00:00"
        await db.save_memory_metadata(
            memory_id="m1",
            user_id="u_pro",
            project_id="default",
            content="a fact",
            extracted_facts=["a fact"],
            metadata={},
            created_at=datetime(2026, 9, 20),
        )
        meter = UsageMeter(db)
        await meter.init_schema()
        await db.conn.execute(
            "INSERT INTO cloud_tenants (user_id, plan, stripe_subscription_id, created_at, updated_at)"
            " VALUES ('u_pro', 'legacy_pro_49', 'sub_49', ?, ?), ('u_team', 'legacy_team_199', 'sub_199', ?, ?)",
            (now, now, now, now),
        )
        await db.conn.commit()
        await meter.apply_subscription("u_solo", PlanTier.SOLO, subscription_id="sub_solo")
        keys = APIKeyManager(db)
        agent_key = await keys.create_key(user_id="u_pro", name="codex", agent_id="codex")
        plain_key = await keys.create_key(user_id="u_pro", name="laptop")
        return agent_key.id, plain_key.id
    finally:
        await db.close()


def _run(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-", *args], input=code, capture_output=True, text=True, timeout=60, check=False)


async def test_documented_consistent_copy_runs_and_checks_the_copy(tmp_path) -> None:
    live = tmp_path / "remembra.db"
    await _production_shaped_db(live)
    args, code = _heredoc("consistent copy")
    assert args == ["/data/remembra.db", '"/data/remembra-manual-$STAMP.db"']
    copy = tmp_path / "remembra-manual-20260926T000000Z.db"

    done = _run(code, str(live), str(copy))
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [str(copy), "ok", "memories:", "1"]
    with sqlite3.connect(copy) as check:
        assert check.execute("SELECT content FROM memories").fetchall() == [("a fact",)]


async def test_documented_rollback_snippet_applies_the_script(tmp_path) -> None:
    live = tmp_path / "remembra.db"
    agent_key_id, plain_key_id = await _production_shaped_db(live)
    args, code = _heredoc("apply rollback")
    assert args == ["/data/remembra.db", "/tmp/rollback_plans_v2.sql"]

    done = _run(code, str(live), str(ROLLBACK_SQL))
    assert done.returncode == 0, done.stderr
    printed = dict(ast.literal_eval(done.stdout.strip()))  # a list of (plan, count) tuples
    assert set(printed) <= B034314_PLANS
    with sqlite3.connect(live) as check:
        plans = dict(check.execute("SELECT user_id, plan FROM cloud_tenants").fetchall())
        assert plans["u_pro"] == "pro" and plans["u_team"] == "team" and plans["u_solo"] == "pro"
        active = dict(check.execute("SELECT id, active FROM api_keys").fetchall())
        assert active[agent_key_id] == 0 and active[plain_key_id] == 1


def test_documented_pre_migration_backup_facts_match_the_code(tmp_path) -> None:
    for text in (
        "`REMEMBRA_PRE_MIGRATION_BACKUP`",
        "`REMEMBRA_PRE_MIGRATION_BACKUP_KEEP`",
        "`REMEMBRA_PRE_MIGRATION_BACKUP_DIR`",
        "`REMEMBRA_PRE_MIGRATION_BACKUP=false`",
        "/data/backups/remembra-predeploy-<build>-<UTC time>.db",
        "the newest 3 are kept",
        "once per build",
    ):
        assert text in DOC, text
    fields = Settings.model_fields
    assert fields["pre_migration_backup"].default is True
    assert fields["pre_migration_backup_keep"].default == 3
    assert fields["pre_migration_backup_dir"].default is None
    assert BACKUP_PREFIX == "remembra-predeploy-"

    # /data/remembra.db -> /data/backups, once per build, newest 3 kept.
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "remembra.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE memories (id TEXT)")
        conn.execute("INSERT INTO memories VALUES ('m1')")
    written = []
    for build in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"):
        path = pre_migration_backup(str(db_path), label=build, keep=fields["pre_migration_backup_keep"].default)
        assert path is not None and path.parent == data / "backups"
        assert path.name.startswith(f"remembra-predeploy-{build}-")
        written.append(path.name)
        # A restart of the same build does not copy again.
        assert pre_migration_backup(str(db_path), label=build, keep=3) is None
    kept = sorted(p.name for p in (data / "backups").glob("*.db"))
    assert len(kept) == 3 and written[0] not in kept
    assert oct((data / "backups").stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in (data / "backups").glob("*.db"))
