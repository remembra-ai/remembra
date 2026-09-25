"""SEC-23: secrets never reach storage on any write path, never leave on read, and can be backfilled."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from remembra.api.v1 import memories
from remembra.models.memory import (
    ConversationIngestRequest,
    Memory,
    RecallResponse,
    RecallResult,
    StoreRequest,
    StoreResponse,
    SupersedeRequest,
    UpdateRequest,
)
from remembra.security.encryption import FieldEncryptor
from remembra.security.secret_scan import scan_and_redact
from remembra.storage.database import Database
from tests.security_harness import secure_app
from tests.test_sec23_redaction import FAKE

RESEND = FAKE["resend_key"]
REMEMBRA = FAKE["remembra_key"]
REPO = Path(__file__).resolve().parents[1]


def test_every_write_model_redacts():
    text = f"Resend key {RESEND} and Remembra key {REMEMBRA}"
    produced = [
        StoreRequest(content=text).content,
        Memory(user_id="u", content=text).content,
        Memory(user_id="u", content="x", extracted_facts=[text]).extracted_facts[0],
        UpdateRequest(content=text).content,
        SupersedeRequest(new_content=text, reason="r").new_content,
        ConversationIngestRequest(messages=[{"role": "user", "content": text}]).messages[0].content,
    ]
    for value in produced:
        assert RESEND not in value and REMEMBRA not in value
        assert "[REDACTED:resend_key]" in value and "[REDACTED:remembra_key]" in value


def test_every_read_model_redacts():
    text = f"key {RESEND}"
    result = RecallResult(id="m", relevance=0.9, content=text, created_at=datetime.now(UTC))
    response = RecallResponse(context=text, memories=[result], entities=[])
    dumped = response.model_dump_json()
    assert RESEND not in dumped
    assert RESEND not in StoreResponse(id="m", extracted_facts=[text], entities=[]).model_dump_json()


async def test_store_endpoint_never_passes_secret_to_service(tmp_path):
    captured: list[StoreRequest] = []

    async def store(body, **_):
        captured.append(body)
        return StoreResponse(id="new", extracted_facts=[body.content], entities=[])

    async with secure_app(tmp_path, [memories.router]) as h:
        h.app.state.memory_service.store = store
        h.app.state.sanitizer = SimpleNamespace(
            analyze=lambda c, source: SimpleNamespace(content=c, trust_score=1.0, checksum="x")
        )
        key, _ = await h.api_key("tenant-a", "editor")
        r = await h.client.post(
            "/api/v1/memories", json={"content": f"our resend key is {RESEND}"}, headers={"X-API-Key": key}
        )
        assert r.status_code == 201, r.text
        assert RESEND not in captured[0].content
        assert RESEND not in r.text


async def test_get_memory_scrubs_rows_stored_before_redaction(tmp_path):
    async with secure_app(tmp_path, [memories.router]) as h:
        memory_id = "3f2b8c1e-9a4d-4e2b-8f1a-2c3d4e5f6a7b"
        now = datetime.now(UTC).isoformat()
        await h.db.conn.execute(
            "INSERT INTO memories (id, user_id, project_id, content, extracted_facts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, "tenant-a", "default", f"legacy {RESEND}", json.dumps([f"fact {REMEMBRA}"]), now, now),
        )
        await h.db.conn.commit()
        key, _ = await h.api_key("tenant-a", "viewer")
        r = await h.client.get(f"/api/v1/memories/{memory_id}", headers={"X-API-Key": key})
        assert r.status_code == 200, r.text
        assert RESEND not in r.text and REMEMBRA not in r.text
        assert "[REDACTED:resend_key]" in r.text


async def _seed(db: Database) -> None:
    now = datetime.now(UTC).isoformat()
    rows = [
        ("m-secret", f"deploy with {RESEND}", json.dumps([f"key is {REMEMBRA}", "plain fact"])),
        ("m-clean", "nothing sensitive here", json.dumps(["plain"])),
    ]
    for memory_id, content, facts in rows:
        await db.conn.execute(
            "INSERT INTO memories (id, user_id, project_id, content, extracted_facts, created_at, updated_at)"
            " VALUES (?, 'tenant-a', 'default', ?, ?, ?, ?)",
            (memory_id, content, facts, now, now),
        )
        await db.index_memory_fts(memory_id, "tenant-a", "default", content)
    await db.conn.execute(
        "INSERT INTO archived_memories (id, user_id, project_id, content, extracted_facts, created_at, updated_at,"
        " archived_at) VALUES ('a-secret', 'tenant-a', 'default', ?, NULL, ?, ?, ?)",
        (f"old {FAKE['stripe_key']}", now, now, now),
    )
    await db.conn.commit()


async def test_scan_dry_run_then_apply(tmp_path):
    db = Database(str(tmp_path / "scan.db"))
    await db.connect()
    await db.init_schema()
    await _seed(db)

    client = SimpleNamespace(set_payload=AsyncMock())
    qdrant = SimpleNamespace(
        _get_client=AsyncMock(return_value=client), _encryptor=FieldEncryptor(None), collection_name="memories"
    )
    try:
        dry = await scan_and_redact(db, qdrant, apply=False)
        assert dry.rows_with_secrets == 2 and dry.rows_redacted == 0
        assert dry.counts == {"resend_key": 1, "remembra_key": 1, "stripe_key": 1}
        row = await (await db.conn.execute("SELECT content FROM memories WHERE id='m-secret'")).fetchone()
        assert RESEND in row[0]  # dry run changed nothing
        client.set_payload.assert_not_awaited()

        applied = await scan_and_redact(db, qdrant, apply=True)
        assert applied.rows_redacted == 2 and applied.qdrant_payloads_updated == 1
        content, facts = await (
            await db.conn.execute("SELECT content, extracted_facts FROM memories WHERE id='m-secret'")
        ).fetchone()
        assert content == "deploy with [REDACTED:resend_key]"
        assert json.loads(facts) == ["key is [REDACTED:remembra_key]", "plain fact"]
        fts = await (await db.conn.execute("SELECT content FROM memories_fts WHERE id='m-secret'")).fetchone()
        assert RESEND not in fts[0]
        archived = await (await db.conn.execute("SELECT content FROM archived_memories WHERE id='a-secret'")).fetchone()
        assert archived[0] == "old [REDACTED:stripe_key]"
        payload = client.set_payload.await_args.kwargs["payload"]
        assert RESEND not in json.dumps(payload) and payload["content"] == "deploy with [REDACTED:resend_key]"
        # Nothing deleted, and a re-scan is clean.
        count = await (await db.conn.execute("SELECT COUNT(*) FROM memories")).fetchone()
        assert count[0] == 2
        assert (await scan_and_redact(db, None, apply=False)).rows_with_secrets == 0
    finally:
        await db.close()


async def test_cli_dry_run_reports_counts_without_values(tmp_path):
    db_path = tmp_path / "cli.db"
    db = Database(str(db_path))
    await db.connect()
    await db.init_schema()
    await _seed(db)
    await db.close()

    env = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "REMEMBRA_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
    }
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts/maintenance/redact_stored_secrets.py"), "--no-qdrant"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["applied"] is False and report["rows_with_secrets"] == 2
    assert report["counts_by_type"] == {"remembra_key": 1, "resend_key": 1, "stripe_key": 1}
    assert RESEND not in proc.stdout + proc.stderr
