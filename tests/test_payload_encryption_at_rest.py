"""Encryption at rest of the Qdrant payload (truth audit P-065 / P-100 / P-071).

With ``REMEMBRA_ENCRYPTION_KEY`` set, every string that carries memory text in
a vector-store record is AES-256-GCM encrypted: ``content``, the strings of
``metadata`` at any depth (inside lists too), every ``extracted_facts`` entry
and the strings of each ``entities`` entry. Filter fields (ids, dates,
memory_type, scope) stay plain because Qdrant filters on them.

Before this fix ``extracted_facts`` (which holds the memory's own text) and
every list in metadata (a handoff's done / not-done lists, commit subjects)
were written in plaintext.

Everything here is real except the embedder: ``MemoryService``, SQLite, and
``QdrantStore`` over ``qdrant_client``'s in-process engine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from qdrant_client import AsyncQdrantClient

from remembra.config import Settings
from remembra.core.time import utcnow
from remembra.extraction import background
from remembra.models.memory import EntityRef, Memory, RecallRequest
from remembra.security.encryption import FieldEncryptor
from remembra.security.secret_scan import _update_qdrant
from remembra.storage.payload_reencrypt import reencrypt_payloads
from remembra.storage.qdrant import QdrantStore
from tests._ret_harness import DIM, USER, make_stack, unit, with_cosine

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[1]
KEY = "unit-test-passphrase-not-a-secret"
PREFIX = "enc:v1:"

# Payload keys that must stay plain (Qdrant filters and sorts on them).
FILTER_FIELDS = {
    "user_id",
    "project_id",
    "created_at",
    "expires_at",
    "memory_type",
    "scope",
    "scope_prefixes",
    "valid_from",
    "valid_to",
}

HANDOFF_METADATA: dict[str, Any] = {
    "agent_id": "claude-code",
    "tags": ["finance", "bank pin hint"],
    "relay": {
        "done": ["rotated the staging password"],
        "not_done": ["tell Alice about the audit"],
        "commits": [{"sha": "c" * 40, "subject": "fix: hide the PIN hint"}],
        "counts": {"files": 3},
        "pushed": True,
    },
    "score": 0.5,
    "empty": None,
}


def _plain_strings(value: Any, path: str = "") -> list[str]:
    """Paths of every string in ``value`` that is not ciphertext."""
    if isinstance(value, str):
        return [] if value.startswith(PREFIX) else [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _plain_strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _plain_strings(v, f"{path}[{i}]")]
    return []


def _text_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in FILTER_FIELDS}


@pytest.fixture()
async def enc_stack(tmp_path):
    s = await make_stack(tmp_path, encryption_key=KEY)
    yield s
    await background.drain(timeout=5.0)
    await s.close()


async def _raw(stack, memory_id: str) -> dict[str, Any]:
    return (await stack.qdrant.get_raw_payloads([memory_id]))[memory_id]


async def _recall(stack, query: str | None, **kw):
    return await stack.service.recall(RecallRequest(query=query, user_id=USER, project_id="p", retrieval_mode="balanced", **kw))


# ---------------------------------------------------------------------------
# Regression: no memory text is written to the vector store in plaintext
# ---------------------------------------------------------------------------


async def test_regression_build_payload_encrypts_facts_entities_and_metadata_lists() -> None:
    store = QdrantStore(Settings(openai_api_key="test"), encryptor=FieldEncryptor(KEY))
    memory = Memory(
        id="00000000-0000-0000-0000-000000000001",
        user_id=USER,
        project_id="p",
        content="My bank PIN hint is blue-heron",
        extracted_facts=["My bank PIN hint is blue-heron", "Alice banks at First National"],
        entities=[EntityRef(id="ent-1", canonical_name="Alice", type="person", confidence=0.9)],
        metadata=HANDOFF_METADATA,
        memory_type="handoff",
        scope="work:acme",
    )

    payload = store.build_payload(memory)

    assert _plain_strings(_text_fields(payload)) == []
    for needle in ("blue-heron", "First National", "Alice", "bank pin hint", "staging password", "hide the PIN"):
        assert needle not in json.dumps(_text_fields(payload))
    # Filter fields stay plain; numbers, booleans and None keep their type.
    assert payload["user_id"] == USER and payload["memory_type"] == "handoff" and payload["scope"] == "work:acme"
    assert payload["scope_prefixes"] == ["work", "work:acme"]
    assert payload["metadata"]["relay"]["counts"] == {"files": 3}
    assert payload["metadata"]["relay"]["pushed"] is True
    assert payload["metadata"]["score"] == 0.5 and payload["metadata"]["empty"] is None
    assert payload["entities"][0]["confidence"] == 0.9

    # And it all reads back exactly.
    decrypted = store._decrypt_payload(payload)
    assert decrypted["content"] == memory.content
    assert decrypted["extracted_facts"] == memory.extracted_facts
    assert decrypted["metadata"] == HANDOFF_METADATA
    assert decrypted["entities"] == [e.model_dump() for e in memory.entities]


async def test_regression_stored_memory_payload_has_no_plaintext_text(enc_stack) -> None:
    mid = await enc_stack.seed("Alice banks at First National", metadata=HANDOFF_METADATA)

    raw = await _raw(enc_stack, mid)

    assert raw["extracted_facts"] and _plain_strings(_text_fields(raw)) == []
    assert "First National" not in json.dumps(raw)
    assert "tell Alice about the audit" not in json.dumps(raw)

    got = await enc_stack.qdrant.get_by_id(mid)
    assert got is not None
    assert got["content"] == "Alice banks at First National"
    assert got["extracted_facts"] == ["Alice banks at First National"]
    assert got["metadata"]["relay"] == HANDOFF_METADATA["relay"]
    assert got["metadata"]["tags"] == HANDOFF_METADATA["tags"]


async def test_regression_updated_and_metadata_rewritten_points_stay_encrypted(enc_stack) -> None:
    mid = await enc_stack.seed("Old deploy note")
    await enc_stack.service.update(mid, USER, "Deploy key lives in the ops vault", {"owners": ["ops", "Mani"]}, enrich=False)
    raw = await _raw(enc_stack, mid)
    assert _plain_strings(_text_fields(raw)) == []
    assert "ops vault" not in json.dumps(raw) and "Mani" not in json.dumps(raw)

    await enc_stack.qdrant.set_metadata(mid, {"reviewers": ["Alice"], "note": "second pass"})
    raw = await _raw(enc_stack, mid)
    assert _plain_strings(raw["metadata"]) == []
    assert (await enc_stack.qdrant.get_by_id(mid))["metadata"] == {"reviewers": ["Alice"], "note": "second pass"}  # type: ignore[index]


async def test_regression_relay_close_payload_has_no_plaintext_strings(tmp_path) -> None:
    """The audit's reproduction: a real relay close left 21 plaintext strings in the handoff's Qdrant metadata."""
    from tests.agent_api_harness import build_api
    from tests.test_relay_api import FACTS

    captured: list[Memory] = []

    class CapturingQdrant:
        async def upsert(self, memory: Memory) -> None:
            captured.append(memory)

        async def search(self, **kwargs: Any) -> list[Any]:
            return []

    gen = build_api(tmp_path)
    api = next(gen)
    try:
        api["app"].state.memory_service.qdrant = CapturingQdrant()
        res = api["http"].post(
            "/api/v1/session/close",
            json={"agent_id": "claude-code", "session_id": "s1", "project_id": "widget", "facts": FACTS},
        )
        assert res.status_code == 200, res.text
    finally:
        for _ in gen:
            pass

    handoff = next(m for m in captured if m.memory_type == "handoff")
    assert handoff.metadata.get("relay"), "the close should carry relay metadata"
    store = QdrantStore(Settings(openai_api_key="test"), encryptor=FieldEncryptor(KEY))
    payload = store.build_payload(handoff)
    assert _plain_strings(_text_fields(payload)) == []
    for subject in ("feat: widget api", "fix: widget edge case"):
        assert subject not in json.dumps(payload)
    assert store._decrypt_payload(payload)["metadata"] == handoff.metadata


async def test_secret_scan_rewrite_encrypts_facts_and_metadata_lists() -> None:
    client = SimpleNamespace(set_payload=AsyncMock())
    qdrant = SimpleNamespace(_get_client=AsyncMock(return_value=client), _encryptor=FieldEncryptor(KEY), collection_name="m")

    facts = json.dumps(["key is [REDACTED:x]"])
    await _update_qdrant(qdrant, "id-1", "key is [REDACTED:x]", facts, {"failing": ["cmd [REDACTED:x]"]})

    payload = client.set_payload.await_args.kwargs["payload"]
    assert _plain_strings(payload) == []
    enc = qdrant._encryptor
    assert [enc.decrypt(f) for f in payload["extracted_facts"]] == ["key is [REDACTED:x]"]
    assert enc.decrypt_dict(payload["metadata"]) == {"failing": ["cmd [REDACTED:x]"]}


# ---------------------------------------------------------------------------
# Backward compatibility: payloads written before this fix still read and search
# ---------------------------------------------------------------------------


async def _make_legacy(stack, memory_id: str, *, plain_content: bool = False) -> dict[str, Any]:
    """Rewrite a point the way the old code stored it: facts and metadata lists in plaintext."""
    row = await stack.row(memory_id)
    enc = stack.qdrant._encryptor
    meta = json.loads(row["metadata"] or "{}")
    legacy_meta = {k: (enc.encrypt(v) if isinstance(v, str) else v) for k, v in meta.items()}
    fields = {
        "content": row["content"] if plain_content else enc.encrypt(row["content"]),
        "metadata": legacy_meta,
        "extracted_facts": json.loads(row["extracted_facts"] or "[]"),
        "entities": [{"id": "ent-1", "canonical_name": "Alice", "type": "person", "confidence": 0.8}],
    }
    await stack.qdrant.set_payload_fields(memory_id, fields)
    return fields


async def test_legacy_plaintext_payload_reads_back_unchanged(enc_stack) -> None:
    mid = await enc_stack.seed("Alice prefers morning standups", metadata={"tags": ["team", "Alice"], "note": "from 1:1"})
    await _make_legacy(enc_stack, mid, plain_content=True)
    raw = await _raw(enc_stack, mid)
    assert raw["extracted_facts"] == ["Alice prefers morning standups"] and raw["metadata"]["tags"] == ["team", "Alice"]

    got = await enc_stack.qdrant.get_by_id(mid)
    assert got is not None
    assert got["content"] == "Alice prefers morning standups"
    assert got["extracted_facts"] == ["Alice prefers morning standups"]
    assert got["metadata"] == {"tags": ["team", "Alice"], "note": "from 1:1"}
    assert got["entities"][0]["canonical_name"] == "Alice"


async def test_search_and_filters_work_over_encrypted_and_legacy_points(enc_stack) -> None:
    enc_stack.emb.vectors["morning standup"] = unit(1)
    new = await enc_stack.seed("Standups move to 9am", vector=with_cosine(0.9), metadata={"kind": "ritual", "tags": ["team"]})
    old = await enc_stack.seed("Standups were at 10am", vector=with_cosine(0.8), metadata={"kind": "ritual", "tags": ["team"]})
    other = await enc_stack.seed("Lunch is at noon", vector=with_cosine(0.85), metadata={"kind": "food"})
    await _make_legacy(enc_stack, old)

    hits = await enc_stack.qdrant.search(unit(1), USER, "p", limit=10, score_threshold=0.5)
    assert {h[0] for h in hits} == {new, old, other}
    by_id = {h[0]: h[2] for h in hits}
    assert by_id[new]["metadata"]["tags"] == ["team"] and by_id[old]["metadata"]["tags"] == ["team"]
    assert by_id[new]["extracted_facts"] == ["Standups move to 9am"]

    resp = await _recall(enc_stack, "morning standup", limit=5, filters={"kind": "ritual"})
    assert {m.id for m in resp.memories} == {new, old}
    assert all(m.metadata["tags"] == ["team"] for m in resp.memories)

    only_filters = await _recall(enc_stack, None, filters={"kind": "food"})
    assert [m.id for m in only_filters.memories] == [other]


async def test_plaintext_mode_payload_is_unchanged(tmp_path) -> None:
    """Without a key nothing is encrypted and metadata filters still run inside Qdrant."""
    s = await make_stack(tmp_path)
    try:
        mid = await s.seed("Plain note", metadata={"tags": ["a", "b"], "kind": "k"})
        raw = await _raw(s, mid)
        assert raw["content"] == "Plain note" and raw["extracted_facts"] == ["Plain note"]
        assert raw["metadata"] == {"tags": ["a", "b"], "kind": "k"}
        assert s.qdrant.metadata_filterable is True
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Maintenance: re-encrypt payloads written before this fix
# ---------------------------------------------------------------------------


async def test_reencrypt_dry_run_then_apply_then_clean(enc_stack) -> None:
    enc_stack.emb.vectors["standup time"] = unit(1)
    legacy = await enc_stack.seed("Standups are at 10am", vector=with_cosine(0.9), metadata={"tags": ["team", "Alice"]})
    fresh = await enc_stack.seed("Retro is on Friday", vector=with_cosine(0.8), metadata={"tags": ["team"]})
    plain = await enc_stack.seed("Plain legacy content", vector=with_cosine(0.7))
    await _make_legacy(enc_stack, legacy)
    await _make_legacy(enc_stack, plain, plain_content=True)
    before = {mid: await enc_stack.qdrant.get_by_id(mid) for mid in (legacy, fresh, plain)}

    dry = await reencrypt_payloads(enc_stack.qdrant)
    assert dry.scanned == 3 and dry.needs_update == 2 and dry.up_to_date == 1 and dry.updated == 0
    assert dry.fields == {"content": 1, "extracted_facts": 2, "entities": 2, "metadata": 1}
    assert {s["id"] for s in dry.samples} == {legacy, plain}
    assert "Alice" not in json.dumps(dry.to_dict()) and "10am" not in json.dumps(dry.to_dict())
    assert (await _raw(enc_stack, legacy))["extracted_facts"] == ["Standups are at 10am"]  # dry run wrote nothing

    applied = await reencrypt_payloads(enc_stack.qdrant, apply=True)
    assert applied.needs_update == 2 and applied.updated == 2 and applied.errors == 0

    for mid in (legacy, fresh, plain):
        raw = await _raw(enc_stack, mid)
        assert _plain_strings(_text_fields(raw)) == [], mid
        assert raw["user_id"] == USER and raw["project_id"] == "p"  # filter fields untouched
        assert await enc_stack.qdrant.get_by_id(mid) == before[mid]  # same values after decrypting

    again = await reencrypt_payloads(enc_stack.qdrant, apply=True)
    assert again.needs_update == 0 and again.up_to_date == 3

    resp = await _recall(enc_stack, "standup time", limit=5)
    assert legacy in {m.id for m in resp.memories}


async def test_reencrypt_limits_to_one_user_and_refuses_without_a_key(enc_stack, tmp_path) -> None:
    mine = await enc_stack.seed("Mine")
    theirs = await enc_stack.seed("Theirs", user_id="u2")
    await _make_legacy(enc_stack, mine)
    await _make_legacy(enc_stack, theirs)

    report = await reencrypt_payloads(enc_stack.qdrant, apply=True, user_id="u2")
    assert report.scanned == 1 and report.updated == 1
    assert (await _raw(enc_stack, mine))["extracted_facts"] == ["Mine"]  # other tenant untouched
    assert _plain_strings((await _raw(enc_stack, theirs))["extracted_facts"]) == []

    plain_store = QdrantStore(Settings(openai_api_key="test"), encryptor=FieldEncryptor(None))
    with pytest.raises(ValueError, match="REMEMBRA_ENCRYPTION_KEY"):
        await reencrypt_payloads(plain_store)


async def test_reencrypt_skips_a_point_rewritten_during_the_scan(enc_stack) -> None:
    """A point the app rewrote between the scan and the write keeps the app's version."""
    mid = await enc_stack.seed("Original text")
    await _make_legacy(enc_stack, mid)
    real_get = enc_stack.qdrant.get_raw_payloads

    async def racing_get(ids: list[str]) -> dict[str, dict[str, Any]]:
        # The app updates the memory right before the script re-reads it.
        await enc_stack.service.update(mid, USER, "Rewritten by the app", None, enrich=False)
        return await real_get(ids)

    enc_stack.qdrant.get_raw_payloads = racing_get  # type: ignore[method-assign]
    report = await reencrypt_payloads(enc_stack.qdrant, apply=True)
    enc_stack.qdrant.get_raw_payloads = real_get  # type: ignore[method-assign]

    assert report.updated == 0 and report.changed_during_scan == 1
    got = await enc_stack.qdrant.get_by_id(mid)
    assert got is not None and got["content"] == "Rewritten by the app"


async def test_reencrypt_cli_on_a_local_qdrant_folder(tmp_path) -> None:
    """The maintenance script end to end, on an embedded Qdrant folder (never a server)."""
    qpath = tmp_path / "qdrant"
    settings = Settings(openai_api_key="test", embedding_dimensions=DIM, qdrant_collection="cli_test", encryption_key=KEY)
    store = QdrantStore(settings)
    store._client = AsyncQdrantClient(path=str(qpath))
    await store.init_collection()
    memory = Memory(
        id="00000000-0000-0000-0000-00000000000a",
        user_id=USER,
        project_id="p",
        content="CLI legacy text",
        extracted_facts=["CLI legacy text"],
        metadata={"tags": ["cli", "legacy"]},
        embedding=unit(1),
        created_at=utcnow(),
    )
    await store.upsert(memory)
    legacy = {"extracted_facts": ["CLI legacy text"], "metadata": {"tags": ["cli", "legacy"]}}
    await store.set_payload_fields(memory.id, legacy)
    await store.close()

    dropped = ("OPENAI_API_KEY", "TYPESAFE_API_KEY")
    env = {k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_") and k not in dropped}
    env.update(
        {
            "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}",
            "REMEMBRA_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
            "REMEMBRA_QDRANT_COLLECTION": "cli_test",
            "REMEMBRA_ENCRYPTION_KEY": KEY,
        }
    )
    script = str(REPO / "scripts/maintenance/reencrypt_payloads.py")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, script, "--qdrant-path", str(qpath), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            timeout=120,
        )

    dry = run()
    assert dry.returncode == 0, dry.stderr
    report = json.loads(dry.stdout)
    assert report["mode"] == "dry_run" and report["collection"] == "cli_test" and report["needs_update"] == 1
    assert "CLI legacy text" not in dry.stdout + dry.stderr

    applied = run("--apply")
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["updated"] == 1
    assert json.loads(run().stdout)["needs_update"] == 0

    no_key = subprocess.run(
        [sys.executable, script, "--qdrant-path", str(qpath)],
        capture_output=True,
        text=True,
        env={k: v for k, v in env.items() if k != "REMEMBRA_ENCRYPTION_KEY"},
        cwd=tmp_path,
        timeout=120,
    )
    assert no_key.returncode == 1 and "REMEMBRA_ENCRYPTION_KEY" in no_key.stderr

    store = QdrantStore(settings)
    store._client = AsyncQdrantClient(path=str(qpath))
    try:
        raw = (await store.get_raw_payloads([memory.id]))[memory.id]
        assert _plain_strings(_text_fields(raw)) == []
        got = await store.get_by_id(memory.id)
        assert got is not None and got["extracted_facts"] == ["CLI legacy text"]
        assert got["metadata"] == {"tags": ["cli", "legacy"]}
    finally:
        await store.close()
