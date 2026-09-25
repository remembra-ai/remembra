"""TypeSafe / Jev decisions for the ingest pipeline (UPG-5).

Jev answers typed questions with calibrated probabilities instead of free
text, which is exactly what consolidation needs: a *decision* over a closed
set of options, never a rewrite. Three decisions are asked:

* **Consolidation** — one ``choice`` question per (new fact, candidate) pair
  with options ``duplicate`` / ``supersedes`` / ``add`` / ``unrelated``.
  Questions are pairwise and keyed by *our* candidate index, so Jev can never
  invent a memory id.
* **Grounding** — one ``noul`` question: is the fact supported by its source
  text?
* **Entity coreference** — one ``noul`` per (mention, existing entity) pair.

All questions for one fact go out in ONE request (the API evaluates them in
parallel), so a fact with five candidates is one call, not six.

Modes (``Settings.typesafe_effective_mode``):

* ``off``     — never called.
* ``shadow``  — the LLM path stays authoritative; Jev is evaluated in the
  background and both decisions are written to ``decision_log``.
* ``enforce`` — Jev decides; any error or timeout falls back to the LLM path.
  A store never fails because of Jev.

API reference: https://docs.typesafe.ai/api.md
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from remembra.core import ai_spend
from remembra.extraction import metrics

log = structlog.get_logger(__name__)

MAX_SOURCE_CHARS = 12000
MAX_CANDIDATE_CHARS = 2000

# Pairwise consolidation options. "add" and "unrelated" both mean ADD for the
# pipeline; they are kept apart because the distinction helps the model (and
# the shadow log) separate "same topic, new detail" from "different topic".
RELATION_CRITERIA: dict[str, str] = {
    "duplicate": ("`new_fact` states the same information as `existing_memory`; storing it would add nothing new"),
    "supersedes": (
        "`new_fact` is about the same subject as `existing_memory` and updates, corrects or contradicts it, "
        "so `existing_memory` is now outdated"
    ),
    "add": ("`new_fact` is about the same subject as `existing_memory` but adds different information; both remain true"),
    "unrelated": "`new_fact` and `existing_memory` are about different subjects",
}

RELATION_QUESTION = "How does `new_fact` relate to `existing_memory`?"
GROUNDING_QUESTION = "Is every claim in `new_fact` stated in, or directly implied by, `source_text`?"
GROUNDING_CRITERIA = {
    "true": "`source_text` states or directly implies everything `new_fact` says",
    "false": "`new_fact` contains a claim, name, number or detail that `source_text` does not support",
}
COREF_QUESTION = "Do `new_entity` and `existing_entity` refer to the same real-world entity?"


class TypeSafeError(Exception):
    """Any failure talking to TypeSafe (HTTP, timeout, malformed answer)."""


@dataclass
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass
class FactAssessment:
    """Jev's answers for one fact: grounding + one relation per candidate."""

    grounding: float | None
    relations: dict[str, ChoiceAnswer] = field(default_factory=dict)  # candidate_id -> answer
    latency_ms: float = 0.0
    model: str | None = None

    def probs_json(self) -> dict[str, Any]:
        return {
            "grounding": self.grounding,
            "relations": {cid: a.probabilities for cid, a in self.relations.items()},
            "model": self.model,
        }


@dataclass
class JevDecision:
    """What Jev would decide for a fact (pure function of an assessment)."""

    action: str  # "ADD" | "NOOP" | "SUPERSEDE"
    target_id: str | None
    confidence: float
    reason: str


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


class TypeSafeClient:
    """Minimal async client for ``POST /v1/systemone``."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.typesafe.ai",
        model: str = "jev-latest",
        timeout: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
        usd_per_request: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("TypeSafe api_key is required")
        self._api_key = api_key
        # Flat price of one request: metered against the write's AI budget
        # (smart credits) or, outside a write, recorded per user (free breaker).
        self.usd_per_request = max(0.0, float(usd_per_request))
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self.model = model
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), transport=self._transport)
        return self._client

    async def system_one(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        """Evaluate ``questions`` against ``state``; returns the raw JSON body.

        Raises TypeSafeError on any failure. The API key never appears in
        errors or logs.
        """
        body = {"model": self.model, "state": state, "questions": questions}
        usd = self.usd_per_request
        try:
            ai_spend.hold_flat(usd)
        except ai_spend.SpendBudgetExceeded as e:
            raise TypeSafeError("AI budget of this write is used up") from e
        try:
            resp = await asyncio.wait_for(
                self._get_client().post(
                    self._url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                ),
                timeout=self.timeout + 0.5,
            )
        except (TimeoutError, httpx.TimeoutException) as e:
            # The request may have reached TypeSafe: bill it (errs on the safe side).
            await ai_spend.charge_flat(usd)
            raise TypeSafeError(f"timeout after {self.timeout}s") from e
        except httpx.HTTPError as e:
            ai_spend.release_flat(usd)
            raise TypeSafeError(f"transport error: {type(e).__name__}") from e
        except BaseException:
            ai_spend.release_flat(usd)
            raise
        await ai_spend.charge_flat(usd)
        if resp.status_code != 200:
            raise TypeSafeError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise TypeSafeError("non-JSON response") from e
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise TypeSafeError("response has no answers map")
        missing = set(questions) - set(data["answers"])
        if missing:
            raise TypeSafeError(f"answers missing for {sorted(missing)}")
        return data

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse_noul(answer: Any) -> float:
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise TypeSafeError("expected a noul answer")
    value = answer.get("noul")
    if not isinstance(value, int | float) or not 0.0 <= float(value) <= 1.0:
        raise TypeSafeError("noul value out of range")
    return float(value)


def _parse_choice(answer: Any, options: Sequence[str]) -> ChoiceAnswer:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise TypeSafeError("expected a choice answer")
    choice = answer.get("choice")
    probs = answer.get("probabilities")
    if choice not in options or not isinstance(probs, dict):
        raise TypeSafeError("choice answer outside the offered options")
    clean = {opt: float(probs.get(opt, 0.0)) for opt in options}
    confidence = answer.get("confidence")
    return ChoiceAnswer(
        choice=str(choice),
        probabilities=clean,
        confidence=float(confidence) if isinstance(confidence, int | float) else max(clean.values()),
    )


def build_fact_request(
    fact: str,
    source_text: str | None,
    candidates: Sequence[tuple[str, str]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Build (state, questions, question_id -> candidate_id) for one fact.

    ``candidates`` are (memory_id, content) pairs. Question ids are
    ``rel_<index>``; ids map back through the returned dict only.
    """
    state: dict[str, Any] = {"new_fact": fact}
    questions: dict[str, Any] = {}
    qmap: dict[str, str] = {}
    if source_text:
        state["source_text"] = _truncate(source_text, MAX_SOURCE_CHARS)
        questions["grounded"] = {"type": "noul", "instructions": GROUNDING_QUESTION, "criteria": GROUNDING_CRITERIA}
    for i, (cid, content) in enumerate(candidates):
        qid = f"rel_{i}"
        qmap[qid] = cid
        questions[qid] = {
            "type": "choice",
            "instructions": {
                "existing_memory": _truncate(content, MAX_CANDIDATE_CHARS),
                "question": RELATION_QUESTION,
            },
            "criteria": RELATION_CRITERIA,
        }
    return state, questions, qmap


def parse_fact_response(data: dict[str, Any], questions: dict[str, Any], qmap: dict[str, str]) -> FactAssessment:
    answers = data["answers"]
    grounding = _parse_noul(answers["grounded"]) if "grounded" in questions else None
    relations = {cid: _parse_choice(answers[qid], list(RELATION_CRITERIA)) for qid, cid in qmap.items()}
    model = data.get("model")
    return FactAssessment(grounding=grounding, relations=relations, model=model if isinstance(model, str) else None)


def decide(
    assessment: FactAssessment,
    pinned_ids: set[str],
    supersede_threshold: float,
    duplicate_threshold: float,
) -> JevDecision:
    """Turn pairwise probabilities into one consolidation decision.

    Duplicate wins when it is at least as likely as supersession; a pinned
    memory is never a supersession target. Below-threshold supersession
    becomes ADD with a "possible conflict" reason so it can be reviewed.
    """
    dup_id, dup_p = None, 0.0
    sup_id, sup_p = None, 0.0
    for cid, ans in assessment.relations.items():
        p_dup = ans.probabilities.get("duplicate", 0.0)
        p_sup = ans.probabilities.get("supersedes", 0.0)
        if p_dup > dup_p:
            dup_id, dup_p = cid, p_dup
        if cid not in pinned_ids and p_sup > sup_p:
            sup_id, sup_p = cid, p_sup

    if dup_id is not None and dup_p >= duplicate_threshold and dup_p >= sup_p:
        return JevDecision("NOOP", dup_id, dup_p, f"jev: duplicate p={dup_p:.2f}")
    if sup_id is not None and sup_p >= supersede_threshold:
        return JevDecision("SUPERSEDE", sup_id, sup_p, f"jev: supersedes p={sup_p:.2f}")
    reason = "jev: new information"
    if sup_id is not None and sup_p >= 0.5:
        reason = f"jev: possible conflict with {sup_id} (p={sup_p:.2f}) below threshold"
    return JevDecision("ADD", None, 1.0 - max(dup_p, sup_p), reason)


class JevDecider:
    """Mode-aware facade used by the store pipeline. Never raises."""

    def __init__(self, settings: Any, client: TypeSafeClient | None = None) -> None:
        self.settings = settings
        self.mode: str = settings.typesafe_effective_mode
        self._client = client
        if self._client is None and self.mode != "off":
            self._client = TypeSafeClient(
                api_key=settings.typesafe_api_key,
                base_url=settings.typesafe_base_url,
                model=settings.typesafe_model,
                timeout=settings.typesafe_timeout,
                usd_per_request=float(getattr(settings, "typesafe_usd_per_request", 0.0) or 0.0),
            )

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "enforce") and self._client is not None

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce" and self._client is not None

    async def assess_fact(
        self,
        fact: str,
        source_text: str | None,
        candidates: Sequence[tuple[str, str]],
    ) -> FactAssessment | None:
        """One batched request for a fact. Returns None on any failure."""
        if not self.enabled or self._client is None:
            return None
        state, questions, qmap = build_fact_request(fact, source_text, candidates)
        if not questions:
            return None
        started = time.perf_counter()
        try:
            data = await self._client.system_one(state, questions)
            assessment = parse_fact_response(data, questions, qmap)
        except TypeSafeError as e:
            metrics.incr("jev_errors_total")
            log.warning("jev_fact_assessment_failed", error=str(e), questions=len(questions))
            return None
        except Exception as e:  # noqa: BLE001 — Jev must never break a store
            metrics.incr("jev_errors_total")
            log.warning("jev_fact_assessment_failed", error=type(e).__name__, questions=len(questions))
            return None
        assessment.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return assessment

    async def entity_coreference(
        self,
        mention: dict[str, Any],
        candidates: Sequence[tuple[str, dict[str, Any]]],
    ) -> tuple[dict[str, float], float] | None:
        """P(same entity) per candidate id, plus latency ms. None on failure."""
        if not self.enabled or self._client is None or not candidates:
            return None
        questions: dict[str, Any] = {}
        qmap: dict[str, str] = {}
        for i, (cid, entity) in enumerate(candidates):
            qid = f"same_{i}"
            qmap[qid] = cid
            questions[qid] = {
                "type": "noul",
                "instructions": {"existing_entity": entity, "question": COREF_QUESTION},
            }
        started = time.perf_counter()
        try:
            data = await self._client.system_one({"new_entity": mention}, questions)
            probs = {cid: _parse_noul(data["answers"][qid]) for qid, cid in qmap.items()}
        except Exception as e:  # noqa: BLE001
            metrics.incr("jev_errors_total")
            log.warning("jev_entity_coreference_failed", error=str(e) if isinstance(e, TypeSafeError) else type(e).__name__)
            return None
        return probs, round((time.perf_counter() - started) * 1000, 1)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
