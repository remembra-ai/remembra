"""Harness for cost-protection tests: production routes, real MemoryService, real metering.

The FastAPI routers, the plan gate, the UsageMeter ledger, the enrichment
queue and the MemoryService store pipeline all run for real over SQLite. Only
the network edges are faked: embeddings and Qdrant (in-process), and the
OpenAI chat client — a counting fake that returns a real-shaped ``usage``
block so dollar metering and reservation settlement are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from remembra.api.v1 import agent_session, auth, billing, cloud, inbox, ingest, memories, transfer
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier
from remembra.cloud.ratelimit import CloudRateLimiter, set_cloud_rate_limiter
from remembra.core import ai_spend
from remembra.core.enrichment_queue import EnrichmentQueue, set_enrichment_queue
from remembra.core.tasks import get_task_registry
from remembra.extraction import background
from remembra.inbox.manager import InboxManager
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.conversation_ingest import ConversationIngestService
from remembra.services.memory import MemoryService
from tests._ingest_fakes import HashEmbeddings, MemQdrant
from tests.security_harness import Harness, make_settings, secure_app

ROUTERS = [
    auth.router,
    memories.router,
    cloud.router,
    billing.router,
    agent_session.router,
    inbox.router,
    ingest.router,
    transfer.router,
]

# Per call: 10,000 prompt + 2,000 completion tokens on gpt-4o-mini = $0.0027.
PROMPT_TOKENS = 10_000
COMPLETION_TOKENS = 2_000
USD_PER_CALL = (PROMPT_TOKENS * 0.15 + COMPLETION_TOKENS * 0.60) / 1_000_000


class CountingLLM:
    """Stand-in for ``AsyncOpenAI`` shared by every LLM client of the service.

    Replies with one JSON object that every caller can parse (no facts, no
    entities, ADD), records each call, and reports token usage like the API.
    ``on_call`` (async) runs before replying — used to observe ledger state at
    the moment an LLM call is made.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.on_call: Any = None
        # Facts returned by the next fact-extraction calls (one list per call).
        self.extraction_facts: list[list[str]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        from remembra.extraction.extractor import EXTRACTION_SYSTEM_PROMPT

        self.calls.append(kwargs)
        if self.on_call is not None:
            await self.on_call(kwargs)
        facts: list[str] = []
        system = next((m["content"] for m in kwargs.get("messages", []) if m.get("role") == "system"), "")
        if system == EXTRACTION_SYSTEM_PROMPT and self.extraction_facts:
            facts = self.extraction_facts.pop(0)
        reply = {
            "facts": facts,
            "entities": [],
            "relationships": [],
            "match": False,
            "action": "ADD",
            "confidence": 0.0,
            "reason": "fake",
        }
        message = SimpleNamespace(content=json.dumps(reply), tool_calls=None)
        usage = SimpleNamespace(
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    def systems(self) -> list[str]:
        return [m["content"][:60] for c in self.calls for m in c["messages"] if m["role"] == "system"]


@dataclass
class CostHarness:
    h: Harness
    meter: UsageMeter
    service: MemoryService
    llm: CountingLLM
    queue: EnrichmentQueue

    async def account(
        self, email: str, *, verified: bool = True, plan: PlanTier = PlanTier.FREE, **sub: Any
    ) -> tuple[str, dict[str, str]]:
        """A user with an API key on ``plan``; returns (user_id, headers)."""
        uid = await self.h.create_user(email, verified=verified)
        key, _ = await self.h.api_key(uid, "editor")
        if plan != PlanTier.FREE or sub:
            await self.meter.apply_subscription(uid, plan, **sub)
        return uid, {"X-API-Key": key}

    async def set_credits_used(self, uid: str, used: int) -> None:
        account = await self.meter.get_account(uid)
        await self.h.db.conn.execute(
            "INSERT INTO cloud_credit_periods (user_id, period_key, credits_used) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, period_key) DO UPDATE SET credits_used = excluded.credits_used",
            (uid, account.period.key, used),
        )
        await self.h.db.conn.commit()

    async def settle_all(self, timeout: float = 5.0) -> None:
        """Wait for background enrichment AND every credit reservation to settle."""
        await background.drain(timeout=timeout)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            cursor = await self.h.db.conn.execute("SELECT COUNT(*) FROM cloud_credit_reservations WHERE status = 'open'")
            settling = "credit_settle" in get_task_registry().names()
            if (await cursor.fetchone())[0] == 0 and not settling:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("credit reservations did not settle")
            await asyncio.sleep(0.01)

    async def ledger(self, uid: str) -> dict[str, Any]:
        account = await self.meter.get_account(uid)
        balance = await self.meter.get_credit_balance(account)
        return {"used": balance.used, "reserved": balance.reserved, "remaining": balance.remaining, "usd": balance.llm_usd}


@asynccontextmanager
async def cost_app(tmp_path: Any, **overrides: Any) -> AsyncIterator[CostHarness]:
    base: dict[str, Any] = {
        "cloud_enabled": True,
        "openai_api_key": "test",
        "enable_entity_resolution": True,
        "typesafe_mode": "off",
        "consolidation_threshold": 0.1,
        "async_enrichment": False,
    }
    base.update(overrides)
    settings = make_settings(**base)
    queue = EnrichmentQueue(global_concurrency=16, default_concurrency=4, max_pending_per_tenant=200)
    set_enrichment_queue(queue)
    set_cloud_rate_limiter(CloudRateLimiter())
    try:
        async with secure_app(tmp_path, ROUTERS, settings=settings) as h:
            meter = UsageMeter(h.db)
            await meter.init_schema()
            h.app.state.usage_meter = meter
            ai_spend.set_attribution_policy(meter)
            service = MemoryService(settings=settings, qdrant=MemQdrant(), db=h.db, embeddings=HashEmbeddings())  # type: ignore[arg-type]
            llm = CountingLLM()
            for client_owner in (service.extractor, service.consolidator, service.entity_extractor, service.entity_matcher):
                client_owner._client = llm  # type: ignore[union-attr]
            h.app.state.memory_service = service
            h.app.state.sanitizer = ContentSanitizer()
            inbox_manager = InboxManager(h.db)
            await inbox_manager.init_schema()
            h.app.state.inbox_manager = inbox_manager
            conversation = ConversationIngestService(settings=settings, memory_service=service)
            conversation._client = llm  # type: ignore[assignment]
            h.app.state.conversation_ingest = conversation
            harness = CostHarness(h=h, meter=meter, service=service, llm=llm, queue=queue)
            try:
                yield harness
            finally:
                await background.drain(timeout=5.0)
    finally:
        ai_spend.set_attribution_policy(None)
        set_enrichment_queue(None)
        set_cloud_rate_limiter(None)
