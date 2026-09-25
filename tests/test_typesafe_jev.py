"""TypeSafe / Jev decisions (UPG-5): client, decision rule, shadow + enforce modes.

HTTP is mocked with httpx.MockTransport; the store pipeline, SQLite and
decision_log are real. The live smoke test at the bottom only runs with
RUN_TYPESAFE_LIVE=1 and a TYPESAFE_API_KEY in the environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from remembra.config import Settings
from remembra.extraction import background
from remembra.extraction.typesafe import (
    ChoiceAnswer,
    FactAssessment,
    JevDecider,
    TypeSafeClient,
    TypeSafeError,
    build_fact_request,
    decide,
)
from remembra.models.memory import StoreRequest
from tests._ingest_fakes import all_rows, close_ingest_dbs, make_service, row, seed  # noqa: F401

KEY = "ts-test-key-never-logged"


def _choice(dup: float = 0.0, sup: float = 0.0, add: float = 0.0, unrel: float = 0.0) -> dict[str, Any]:
    probs = {"duplicate": dup, "supersedes": sup, "add": add, "unrelated": unrel}
    return {"type": "choice", "choice": max(probs, key=probs.__getitem__), "probabilities": probs, "confidence": 0.5}


class JevServer:
    """Scripted TypeSafe endpoint. ``answer(question_id, question) -> answer``."""

    def __init__(self, answer: Any = None, status: int = 200, delay: float = 0.0) -> None:
        self.answer = answer or (lambda qid, q: {"type": "noul", "noul": 0.9} if q["type"] == "noul" else _choice(add=1.0))
        self.status = status
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        answers = {qid: self.answer(qid, q) for qid, q in body["questions"].items()}
        return httpx.Response(200, json={"model": "jev-test", "answers": answers, "usage": {}})

    def client(self, timeout: float = 2.0) -> TypeSafeClient:
        return TypeSafeClient(api_key=KEY, timeout=timeout, transport=httpx.MockTransport(self.handler))


async def _jev_service(tmp_path, server: JevServer, mode: str, timeout: float = 2.0, **kw):  # type: ignore[no-untyped-def]
    service, db, qdrant, cons_llm, ext_llm = await make_service(tmp_path, typesafe_mode=mode, typesafe_api_key=KEY, **kw)
    service.jev = JevDecider(service.settings, client=server.client(timeout))
    return service, db, qdrant, cons_llm, ext_llm


async def _decisions(db: Any, decision_type: str) -> list[dict[str, Any]]:
    await background.drain()
    cursor = await db.conn.execute("SELECT * FROM decision_log WHERE decision_type = ? ORDER BY created_at", (decision_type,))
    return [dict(r) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_mode_defaults_to_shadow_with_key_and_off_without(monkeypatch) -> None:
    monkeypatch.delenv("REMEMBRA_TYPESAFE_MODE", raising=False)
    monkeypatch.delenv("REMEMBRA_TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    s = Settings(openai_api_key="t")
    assert s.typesafe_api_key == "from-env"
    assert s.typesafe_effective_mode == "shadow"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert Settings(openai_api_key="t").typesafe_effective_mode == "off"
    assert Settings(openai_api_key="t", typesafe_api_key="k", typesafe_mode="ENFORCE").typesafe_effective_mode == "enforce"
    assert Settings(openai_api_key="t", typesafe_mode="enforce").typesafe_effective_mode == "off"  # no key -> off
    with pytest.raises(ValidationError):
        Settings(openai_api_key="t", typesafe_mode="yolo")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_sends_documented_request_shape() -> None:
    server = JevServer()
    client = server.client()
    data = await client.system_one({"new_fact": "x"}, {"g": {"type": "noul", "instructions": "?"}})
    assert data["answers"]["g"]["noul"] == 0.9
    body = server.requests[0]
    assert body["model"] == "jev-latest" and body["state"] == {"new_fact": "x"} and "g" in body["questions"]
    assert server.headers[0]["authorization"] == f"Bearer {KEY}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server",
    [
        JevServer(status=500),
        JevServer(status=429),
        JevServer(answer=lambda qid, q: {"type": "noul", "noul": 7}),
    ],
)
async def test_client_errors_are_typed_and_never_leak_the_key(server: JevServer) -> None:
    client = server.client()
    state, questions, qmap = build_fact_request("fact", "source", [])
    with pytest.raises(TypeSafeError) as exc:
        from remembra.extraction.typesafe import parse_fact_response

        parse_fact_response(await client.system_one(state, questions), questions, qmap)
    assert KEY not in str(exc.value)


@pytest.mark.asyncio
async def test_client_timeout_raises_typed_error_quickly() -> None:
    server = JevServer(delay=1.0)
    client = server.client(timeout=0.1)
    started = time.perf_counter()
    with pytest.raises(TypeSafeError, match="timeout"):
        await client.system_one({"s": 1}, {"q": {"type": "noul", "instructions": "?"}})
    assert time.perf_counter() - started < 0.9


@pytest.mark.asyncio
async def test_missing_answer_is_an_error() -> None:
    server = JevServer(answer=lambda qid, q: None)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {}})

    client = TypeSafeClient(api_key=KEY, transport=httpx.MockTransport(handler))
    with pytest.raises(TypeSafeError, match="missing"):
        await client.system_one({}, {"q": {"type": "noul", "instructions": "?"}})
    del server


def test_fact_request_is_pairwise_and_keyed_by_index() -> None:
    state, questions, qmap = build_fact_request("new", "src", [("mem-a", "A"), ("mem-b", "B")])
    assert state == {"new_fact": "new", "source_text": "src"}
    assert set(questions) == {"grounded", "rel_0", "rel_1"}
    assert qmap == {"rel_0": "mem-a", "rel_1": "mem-b"}
    assert questions["rel_1"]["instructions"]["existing_memory"] == "B"
    assert set(questions["rel_0"]["criteria"]) == {"duplicate", "supersedes", "add", "unrelated"}
    _, q2, _ = build_fact_request("new", None, [])
    assert q2 == {}


def test_decide_rule() -> None:
    def a(**rel: dict[str, float]) -> FactAssessment:
        return FactAssessment(
            grounding=None,
            relations={
                k: ChoiceAnswer("x", {"duplicate": 0, "supersedes": 0, "add": 0, "unrelated": 0, **v}, 0.5)
                for k, v in rel.items()
            },
        )

    assert decide(a(m1={"supersedes": 0.9}), set(), 0.85, 0.85).action == "SUPERSEDE"
    low = decide(a(m1={"supersedes": 0.6}), set(), 0.85, 0.85)
    assert low.action == "ADD" and "possible conflict" in low.reason
    assert decide(a(m1={"supersedes": 0.95}), {"m1"}, 0.85, 0.85).action == "ADD"  # pinned
    dup = decide(a(m1={"duplicate": 0.9}, m2={"supersedes": 0.86}), set(), 0.85, 0.85)
    assert (dup.action, dup.target_id) == ("NOOP", "m1")
    sup = decide(a(m1={"duplicate": 0.5}, m2={"supersedes": 0.9}), set(), 0.85, 0.85)
    assert (sup.action, sup.target_id) == ("SUPERSEDE", "m2")


# ---------------------------------------------------------------------------
# Shadow mode: LLM authoritative, Jev logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_logs_agreement_and_llm_stays_authoritative(tmp_path) -> None:
    server = JevServer(
        answer=lambda qid, q: _choice(dup=0.9, add=0.1) if q["type"] == "choice" else {"type": "noul", "noul": 0.9}
    )
    service, db, _, cons_llm, _ = await _jev_service(tmp_path, server, "shadow")
    old = await seed(service, "Sarah works at Microsoft")
    cons_llm.replies = [{"action": "SUPERSEDE", "target_id": old, "reason": "job", "confidence": 0.9}]

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    # The LLM decided (supersede) even though Jev says duplicate.
    assert resp.consolidation[0].action == "supersede" and resp.consolidation[0].decided_by == "llm"
    assert (await row(db, old))["superseded_by"] == resp.id
    logs = await _decisions(db, "consolidation")
    assert len(logs) == 1
    entry = logs[0]
    assert entry["mode"] == "shadow" and entry["agreed"] == 0
    assert entry["llm_decision"] == f"SUPERSEDE:{old}" and entry["jev_decision"] == f"NOOP:{old}"
    assert entry["memory_id"] == resp.id and entry["latency_ms"] is not None
    assert json.loads(entry["jev_probs"])["relations"][old]["duplicate"] == 0.9


@pytest.mark.asyncio
async def test_shadow_batches_all_questions_for_a_fact_into_one_request(tmp_path) -> None:
    server = JevServer()
    content = "Clawbot deploys to Vercel. Mani owns the Clawbot repo."
    facts = ["Clawbot deploys to Vercel", "Mani owns the Clawbot repo"]
    service, db, _, cons_llm, _ = await _jev_service(tmp_path, server, "shadow", extractor_replies=[{"facts": facts}])
    await seed(service, "Clawbot deploys to Vercel preview")
    await seed(service, "Clawbot repo is private")
    cons_llm.default = {"action": "ADD", "target_id": None, "reason": "new", "confidence": 0.9}

    await service.store(StoreRequest(content=content, user_id="u1"))
    await background.drain()

    assert len(server.requests) == 2, "one Jev request per fact"
    for body in server.requests:
        assert "grounded" in body["questions"]
        assert all(q.startswith(("grounded", "rel_")) for q in body["questions"])
    assert any(len(b["questions"]) >= 2 for b in server.requests)
    grounding = await _decisions(db, "grounding")
    assert len(grounding) == 2 and all(g["agreed"] == 1 for g in grounding)


@pytest.mark.asyncio
async def test_shadow_jev_failure_never_affects_store(tmp_path) -> None:
    server = JevServer(status=500)
    service, db, _, cons_llm, _ = await _jev_service(tmp_path, server, "shadow")
    old = await seed(service, "Sarah works at Microsoft")
    cons_llm.replies = [{"action": "ADD", "target_id": None, "reason": "new", "confidence": 0.9}]

    resp = await service.store(StoreRequest(content="Sarah works at Microsoft Research", user_id="u1"))

    assert resp.status == "stored"
    assert await _decisions(db, "consolidation") == []
    assert (await row(db, old))["superseded_by"] is None


# ---------------------------------------------------------------------------
# Enforce mode: Jev decides, LLM fallback on any Jev failure
# ---------------------------------------------------------------------------


class _Boom:
    async def consolidate(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("LLM consolidator must not run when Jev answered in enforce mode")


class _Counting:
    def __init__(self) -> None:
        self.calls = 0

    async def consolidate(self, fact, existing):  # type: ignore[no-untyped-def]
        from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult

        self.calls += 1
        return ConsolidationResult(ConsolidationAction.ADD, None, reason="llm fallback", confidence=0.9)


@pytest.mark.asyncio
async def test_enforce_jev_supersedes_above_threshold(tmp_path) -> None:
    server = JevServer(
        answer=lambda qid, q: _choice(sup=0.93, add=0.07) if q["type"] == "choice" else {"type": "noul", "noul": 0.9}
    )
    service, db, _, _, _ = await _jev_service(tmp_path, server, "enforce")
    old = await seed(service, "Sarah works at Microsoft")
    service.consolidator = _Boom()  # type: ignore[assignment]

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    entry = resp.consolidation[0]
    assert (entry.action, entry.target_id, entry.decided_by) == ("supersede", old, "jev")
    assert entry.confidence == pytest.approx(0.93)
    assert (await row(db, old))["superseded_by"] == resp.id
    logs = await _decisions(db, "consolidation")
    assert logs[0]["mode"] == "enforce" and logs[0]["jev_decision"] == f"SUPERSEDE:{old}"


@pytest.mark.asyncio
async def test_enforce_below_threshold_adds_and_flags(tmp_path) -> None:
    server = JevServer(
        answer=lambda qid, q: _choice(sup=0.6, add=0.4) if q["type"] == "choice" else {"type": "noul", "noul": 0.9}
    )
    service, db, _, _, _ = await _jev_service(tmp_path, server, "enforce")
    old = await seed(service, "Sarah works at Microsoft")
    service.consolidator = _Boom()  # type: ignore[assignment]

    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    assert resp.consolidation[0].action == "add"
    assert "possible conflict" in (resp.consolidation[0].reason or "")
    assert (await row(db, old))["superseded_by"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("server", [JevServer(status=500), JevServer(delay=1.0)])
async def test_enforce_falls_back_to_llm_on_error_or_timeout(tmp_path, server: JevServer) -> None:
    service, db, _, _, _ = await _jev_service(tmp_path, server, "enforce", timeout=0.1)
    await seed(service, "Sarah works at Microsoft")
    llm = _Counting()
    service.consolidator = llm  # type: ignore[assignment]

    started = time.perf_counter()
    resp = await service.store(StoreRequest(content="Sarah works at Google", user_id="u1"))

    assert resp.status == "stored" and resp.consolidation[0].decided_by == "llm"
    assert llm.calls == 1
    assert time.perf_counter() - started < 1.5


@pytest.mark.asyncio
async def test_enforce_grounding_drops_fact_jev_rejects(tmp_path) -> None:
    content = "Mani prefers TradingView Pine v6 strategies with margin_long=100 on futures."
    facts = ["Mani prefers Pine v6 strategies with margin_long 100", "Mani prefers TradingView strategies on futures"]
    server = JevServer(
        answer=lambda qid, q: (
            _choice(add=1.0)
            if q["type"] == "choice"
            else {"type": "noul", "noul": 0.1 if "TradingView strategies on futures" in json.dumps(q) else 0.9}
        )
    )

    # Grounding is asked against the fact in `state`, so answer per request.
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        server.requests.append(body)
        g = 0.1 if body["state"]["new_fact"] == facts[1] else 0.95
        answers = {
            qid: ({"type": "noul", "noul": g} if q["type"] == "noul" else _choice(add=1.0))
            for qid, q in body["questions"].items()
        }
        return httpx.Response(200, json={"answers": answers})

    service, db, _, _, _ = await make_service(
        tmp_path, typesafe_mode="enforce", typesafe_api_key=KEY, extractor_replies=[{"facts": facts}]
    )
    service.jev = JevDecider(service.settings, client=TypeSafeClient(api_key=KEY, transport=httpx.MockTransport(handler)))

    resp = await service.store(StoreRequest(content=content, user_id="u1"))

    assert resp.extracted_facts == [facts[0]]
    assert [(d.fact, d.decided_by) for d in resp.dropped_facts] == [(facts[1], "jev")]
    stored = [r for r in await all_rows(db) if r["memory_type"] != "source"]
    assert json.loads(stored[0]["metadata"])["grounding_score"] == 0.95


@pytest.mark.asyncio
async def test_enforce_never_supersedes_pinned(tmp_path) -> None:
    server = JevServer(answer=lambda qid, q: _choice(sup=0.99) if q["type"] == "choice" else {"type": "noul", "noul": 0.9})
    service, db, _, _, _ = await _jev_service(tmp_path, server, "enforce")
    pinned = await seed(service, "Mani's timezone is EST")
    await db.set_memory_pin(pinned, "u1", True)
    service.consolidator = _Boom()  # type: ignore[assignment]

    resp = await service.store(StoreRequest(content="Mani's timezone is PST", user_id="u1"))

    assert resp.consolidation[0].action == "add"
    assert (await row(db, pinned))["superseded_by"] is None


# ---------------------------------------------------------------------------
# Live smoke test (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_TYPESAFE_LIVE") != "1" or not os.environ.get("TYPESAFE_API_KEY"),
    reason="live TypeSafe smoke test: set RUN_TYPESAFE_LIVE=1 and TYPESAFE_API_KEY",
)
@pytest.mark.asyncio
async def test_live_typesafe_shadow_store_end_to_end(tmp_path, capsys) -> None:
    key = os.environ["TYPESAFE_API_KEY"]
    service, db, _, cons_llm, _ = await make_service(tmp_path, typesafe_mode="shadow", typesafe_api_key=key)
    service.jev = JevDecider(service.settings, client=TypeSafeClient(api_key=key, timeout=5.0))
    existing = await seed(service, "X repo is at /a; next: check fixes-master.md")
    cons_llm.replies = [{"action": "SUPERSEDE", "target_id": existing, "reason": "next step changed", "confidence": 0.9}]

    # 1) Direct assessment: one batched request, real answers.
    started = time.perf_counter()
    assessment = await service.jev.assess_fact(
        "Next: compile fix list", "Next: compile fix list", [(existing, "X repo is at /a; next: check fixes-master.md")]
    )
    wall_ms = round((time.perf_counter() - started) * 1000, 1)
    assert assessment is not None, "live Jev call failed (see logs)"
    assert assessment.grounding is not None and 0.0 <= assessment.grounding <= 1.0
    probs = assessment.relations[existing].probabilities
    assert set(probs) == {"duplicate", "supersedes", "add", "unrelated"}
    assert abs(sum(probs.values()) - 1.0) < 0.05

    # 2) Full shadow store: LLM decides, Jev logged to decision_log.
    resp = await service.store(StoreRequest(content="Next: compile fix list", user_id="u1"))
    assert resp.extracted_facts == ["Next: compile fix list"]
    logs = await _decisions(db, "consolidation")
    assert len(logs) == 1 and logs[0]["jev_decision"] is not None

    with capsys.disabled():
        rel = assessment.relations[existing]
        print(
            f"\n[live jev] model={assessment.model} latency_ms={assessment.latency_ms} wall_ms={wall_ms} "
            f"grounding={assessment.grounding:.2f} relation={json.dumps(probs)} choice={rel.choice} "
            f"shadow_log={logs[0]['jev_decision']} agreed={logs[0]['agreed']}"
        )
    await service.jev.close()


@pytest.mark.asyncio
async def test_shadow_does_not_compare_rule_decisions(tmp_path) -> None:
    """Exact duplicates / no-candidate ADDs are rule decisions: logging them would inflate agreement."""
    server = JevServer()
    service, db, _, cons_llm, _ = await _jev_service(tmp_path, server, "shadow")
    await seed(service, "Mani prefers dark mode")

    dup = await service.store(StoreRequest(content="Mani prefers dark mode", user_id="u1"))
    new = await service.store(StoreRequest(content="Totally unrelated astronomy fact about Jupiter moons", user_id="u1"))

    assert dup.consolidation[0].decided_by == "rule" and new.consolidation[0].decided_by == "rule"
    assert cons_llm.calls == []
    assert await _decisions(db, "consolidation") == []
