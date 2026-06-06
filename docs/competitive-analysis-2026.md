# Remembra — Competitive Landscape & Roadmap (2026)

A field scan of the AI agent-memory market and a prioritized plan to put Remembra
demonstrably above it. Sourced from competitor docs, papers, and benchmarks (June 2026).

## TL;DR

Remembra already ships a feature set most competitors don't: a 3-stage retrieval
pipeline (FTS5/BM25 + vector → cross-encoder rerank → graph expansion), entity/knowledge
graph, temporal decay + TTL + **cold archive**, **sleep-time consolidation**, conflict
resolution, multi-tenant RBAC/teams, AES-256-GCM at rest, 2FA, webhooks, MCP, and
**audio/meeting ingestion**. The last three (cold archive, sleep consolidation, audio
ingest) are near-unique. The gap to the leaders is **proof** (a published benchmark) and
a few high-leverage capabilities, not raw features.

## Where the field stands

| Capability | Remembra | Mem0 | Letta | Zep/Graphiti | Cognee | Supermemory |
|---|---|---|---|---|---|---|
| Hybrid (FTS+vector) | ✓ | ✓ | partial | ✓ | ✓ | ✓ |
| Cross-encoder rerank | ✓ | ✓ | – | – | partial | ✓ |
| Knowledge graph | ✓ | Pro+ | partial | ✓ | ✓ | partial |
| Bi-temporal | ✓ | partial | – | ✓ (core) | – | partial |
| Temporal decay | ✓ | – | – | ✓ | – | partial |
| Cold archive | ✓ | – | – | – | – | ✓ |
| Sleep consolidation | ✓ | – | – | – | – | – |
| Conflict resolution | ✓ | LLM | partial | temporal | partial | partial |
| Audio/meeting ingest | ✓ | – | – | – | – | – |
| MCP-native | ✓ | ✓ | emerging | – | ✓ | ✓ |
| Open source | – | – | ✓ | Graphiti | core | – |

### Benchmarks (2026 SOTA)
- **LoCoMo** (1,540 Qs, the conversational-memory standard): SOTA ≈ **92.5** (Mem0,
  Apr 2026); ByteRover 92.2; others 80–89. **Publish-target: 93.5+.**
- **LongMemEval** (500 Qs): cloud SOTA ≈ 94.4 (Mem0); MemPalace 96.6 (local-only).
- **BEAM** (1M–10M tokens, production realism): Mem0 64.1 / 48.6; field largely unbenchmarked.

### Competitor one-liners
- **Mem0** — primary threat; mature API, 21 integrations, token-efficient, SOC2/HIPAA.
  Gaps: pricing cliff ($19→$249 for graph), no consolidation, calls staleness "unresolved."
- **Letta/MemGPT** — open-source agent runtime, clean 3-tier memory. Gaps: no entity
  graph, no decay/TTL, MCP emerging, no published benchmarks.
- **Zep/Graphiti** — temporal-KG specialist (bi-temporal edges). Gaps: token bloat
  (~600K/conv), community edition deprecated, REST-only (no MCP).
- **Cognee** — well-funded (€7.5M seed), MCP-native, enterprise traction. Gaps: no
  published benchmarks, no decay, no audio.
- **Supermemory** — claims the benchmark trifecta but numbers are inconsistent and
  unverified; opaque (no public repo/docs).
- **OpenAI / Anthropic memory** — consumer/filesystem-grade; not API-first agent memory.

## Roadmap to surpass the field

### Quick wins (highest leverage first)
1. **Publish a reproducible LoCoMo benchmark** targeting 93.5+ with open methodology and
   code. This is the single biggest credibility move — the leaders compete on this number
   and Supermemory's opacity is an opening. *(Foundation: a recall-quality eval harness.)*
2. **Salience-aware memory** — pin (never-forget) + importance-weighted decay.
   ✅ **Shipped in 0.14.0.** Cognitive-science-aligned; none of the competitors expose
   user-controlled decay protection.
3. **Webhook events for memory lifecycle** (consolidation done, archive moved, conflict
   resolved) so agents can react to memory changes — closes the agent feedback loop.

### Medium effort
4. **Bi-temporal cold archive cost story** — benchmark tokens/query vs Zep's ~600K and
   publish the delta; tier hot/warm/cold explicitly.
5. **Audio/meeting ingest with diarization** — connect Otter/Fireflies/Krisp transcripts,
   map speakers to entities. This is open whitespace (no competitor has it).
6. **Per-tenant envelope encryption + key rotation** ("blind indexing") for regulated
   verticals (health/finance) — a compliance differentiator over Mem0's basic RBAC.

### Large effort
7. **Deepen sleep-time consolidation** — retroactive conflict resolution + redundancy
   compression on idle, measured against steady-state latency/footprint. This is the
   true differentiator; lead with it.
8. **First-class framework integrations** (LangGraph/Autogen) to become the shared brain
   for multi-agent systems.

## Positioning angles (true given the feature set)
- *"A memory control plane, not just a retriever."* Full visibility: graph, consolidation
  logs, archive tiers, conflict audit trail.
- *"Agents that actually remember meetings."* Native audio/diarization ingest.
- *"Temporal memory done right."* Cognitive decay + consolidation + cold archive +
  conflict-by-time.
- *"MCP-first."* One-click into Claude/Cursor and any MCP client.

---
*Compiled June 2026 from competitor documentation, arXiv papers, and public benchmarks.
Treat specific competitor numbers as point-in-time; re-verify before publishing claims.*
