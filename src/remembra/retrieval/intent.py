"""Query intent -> retrieval mode (RET-1 / UPG-4).

Callers rarely pick a ranking mode, so "what was I just working on" used to be
ranked with ``balanced`` weights and an old, highly similar memory beat the
fresh one. When the request leaves ``retrieval_mode`` unset (or ``auto``) the
server chooses:

1. **Deterministic rules** (always, no cost): recency wording -> ``debug``,
   history/trend wording -> ``strategic``, how-to/config wording ->
   ``operational``.
2. **Jev choice** (optional, only when no rule fired): one TypeSafe ``choice``
   question over the four modes. Respects ``typesafe_mode``:

   * ``off``     - never called; ``balanced``.
   * ``shadow``  - called off the request path; the decision is written to
     ``decision_log`` (``decision_type='query_intent'``) and NOT applied.
   * ``enforce`` - applied only when Jev's confidence >= the threshold;
     otherwise ``balanced``. Errors/timeouts fall back to ``balanced``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

MODES = ("debug", "operational", "strategic", "balanced")

MODE_CRITERIA: dict[str, str] = {
    "debug": "the user wants what happened most recently: current status, latest work, what they were just doing",
    "operational": "the user wants a stable fact, setting, procedure or how-to that does not depend on recency",
    "strategic": "the user wants history, patterns or how something evolved over a long period",
    "balanced": "none of the above clearly applies; a general lookup",
}
INTENT_QUESTION = "Which kind of memory lookup is `query` asking for?"

# Ordered: the first matching rule wins.
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "debug",
        "recency",
        re.compile(
            r"\b("
            r"just|recent(ly)?|latest|newest|lately|today|tonight|yesterday|this (morning|afternoon|evening|week)|"
            r"right now|currently|current(ly)? (status|state|task|work|focus|blocker)s?|status|"
            r"last (session|time|thing|few (hours|days))|so far|up to date|"
            r"what (was|am|were) (i|we) (just )?(doing|working on|up to)|where (did|do) (i|we) leave off|"
            r"pick up where|left off|in progress|next steps?"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "strategic",
        "history",
        re.compile(
            r"\b("
            r"history|historically|over time|evolv(e|ed|ing)|evolution|trend(s)?|pattern(s)?|"
            r"all the times|every time|ever|timeline|long[- ]term|over the (past|last) (months?|years?)|"
            r"last (month|year)|since (the )?(start|beginning)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "operational",
        "procedure",
        re.compile(
            r"\b("
            r"how (do|to|can|should) (i|we|you)?|how to|procedure|runbook|steps to|instructions|"
            r"config(uration|ured)?|set ?up|install(ation)?|command(s)? (for|to)|what('s| is) the (command|port|url|path)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
]


@dataclass(frozen=True)
class ModeDecision:
    mode: str
    source: str  # request | rules | jev | default
    rule: str | None = None
    confidence: float | None = None


def infer_mode_by_rules(query: str) -> ModeDecision | None:
    """The rule-based mode for ``query``, or None when no rule applies."""
    text = query or ""
    for mode, rule, pattern in _RULES:
        if pattern.search(text):
            return ModeDecision(mode=mode, source="rules", rule=rule)
    return None


class IntentRouter:
    """Resolves the ranking mode for a recall request. Never raises."""

    def __init__(
        self,
        settings: Any,
        jev: Any = None,
        *,
        log_decision: Callable[..., Awaitable[None]] | None = None,
        spawn: Callable[[Any, str], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.jev = jev
        self._log_decision = log_decision
        self._spawn = spawn

    @property
    def threshold(self) -> float:
        return float(getattr(self.settings, "typesafe_intent_threshold", 0.7))

    def _jev_client(self) -> Any:
        jev = self.jev
        if jev is None or not getattr(jev, "enabled", False):
            return None
        return getattr(jev, "_client", None)

    async def _ask_jev(self, query: str) -> tuple[str, float, dict[str, float], float] | None:
        """(choice, confidence, probabilities, latency_ms) or None on any failure."""
        client = self._jev_client()
        if client is None:
            return None
        from remembra.extraction import metrics
        from remembra.extraction.typesafe import TypeSafeError, _parse_choice

        started = time.perf_counter()
        try:
            data = await client.system_one(
                {"query": query[:2000]},
                {"mode": {"type": "choice", "instructions": INTENT_QUESTION, "criteria": MODE_CRITERIA}},
            )
            answer = _parse_choice(data["answers"]["mode"], list(MODE_CRITERIA))
        except TypeSafeError as e:
            metrics.incr("jev_errors_total")
            log.warning("jev_query_intent_failed", error=str(e))
            return None
        except Exception as e:  # noqa: BLE001 - intent routing must never break recall
            metrics.incr("jev_errors_total")
            log.warning("jev_query_intent_failed", error=type(e).__name__)
            return None
        latency = round((time.perf_counter() - started) * 1000, 1)
        return answer.choice, float(answer.confidence), answer.probabilities, latency

    async def _shadow(self, query: str, applied: ModeDecision, user_id: str, project_id: str | None) -> None:
        result = await self._ask_jev(query)
        if result is None or self._log_decision is None:
            return
        choice, confidence, probs, latency = result
        jev_label = choice if confidence >= self.threshold else "balanced"
        await self._log_decision(
            decision_type="query_intent",
            mode="shadow",
            user_id=user_id,
            project_id=project_id,
            memory_id=None,
            subject=query,
            llm_decision=f"{applied.source}:{applied.mode}",
            jev_decision=jev_label,
            jev_probs={"mode": probs, "confidence": confidence},
            agreed=jev_label == applied.mode,
            latency_ms=latency,
        )

    async def resolve(self, requested: str | None, query: str, user_id: str, project_id: str | None) -> ModeDecision:
        if requested and requested != "auto":
            return ModeDecision(mode=requested, source="request")
        ruled = infer_mode_by_rules(query)
        decision = ruled or ModeDecision(mode="balanced", source="default")
        jev_mode = getattr(self.jev, "mode", "off") if self.jev is not None else "off"
        if self._jev_client() is None or jev_mode == "off":
            return decision
        if jev_mode == "shadow":
            if self._spawn is not None:
                self._spawn(self._shadow(query, decision, user_id, project_id), "jev_query_intent_shadow")
            return decision
        # enforce: rules still win; Jev only decides when no rule fired.
        if ruled is not None:
            return ruled
        result = await self._ask_jev(query)
        if result is None:
            return decision
        choice, confidence, probs, latency = result
        applied = confidence >= self.threshold
        if self._log_decision is not None:
            await self._log_decision(
                decision_type="query_intent",
                mode="enforce",
                user_id=user_id,
                project_id=project_id,
                memory_id=None,
                subject=query,
                llm_decision=None,
                jev_decision=choice if applied else "balanced",
                jev_probs={"mode": probs, "confidence": confidence},
                agreed=None,
                latency_ms=latency,
            )
        if applied:
            return ModeDecision(mode=choice, source="jev", confidence=round(confidence, 4))
        return decision
