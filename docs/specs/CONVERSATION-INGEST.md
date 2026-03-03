# Conversation Ingestion API Specification

**Version:** 1.0
**Status:** Draft
**Author:** General (AI) + Mani
**Date:** 2026-03-03

## Overview

Add a universal conversation ingestion endpoint that accepts chat transcripts and automatically extracts facts, entities, and context worth remembering.

This enables any AI tool (Clawdbot, LangChain, CrewAI, custom bots) to passively feed conversations to Remembra and build long-term memory without manual `store` calls.

## Problem

Currently, AI tools must:
1. Decide what's worth remembering
2. Manually call `/memories` with specific content
3. Handle extraction client-side

This fails because:
- AI agents forget to store important info
- Context compaction loses history before storage
- Manual storage is inconsistent
- Every tool reimplements extraction logic

## Solution

**New Endpoint:** `POST /api/v1/ingest/conversation`

Accept raw conversation → Extract facts/entities → Dedupe → Store → Return summary.

```
┌─────────────────┐     ┌──────────────────────────────────────────┐
│  Clawdbot       │     │               Remembra                   │
│  LangChain      │ ──► │  /ingest/conversation                    │
│  CrewAI         │     │    ├─► Parse messages                    │
│  Any AI Tool    │     │    ├─► Extract facts (LLM)               │
│                 │     │    ├─► Extract entities                  │
│                 │     │    ├─► Dedupe against existing           │
│                 │     │    ├─► Resolve conflicts                 │
│                 │     │    └─► Store memories                    │
└─────────────────┘     └──────────────────────────────────────────┘
```

## API Design

### Request

```http
POST /api/v1/ingest/conversation
Authorization: Bearer <api_key>
Content-Type: application/json

{
  "messages": [
    {
      "role": "user",
      "content": "My wife Suzan and I are planning a trip to Jamaica in April",
      "timestamp": "2026-03-03T01:45:00Z",  // optional
      "name": "Mani"  // optional, for multi-user chats
    },
    {
      "role": "assistant", 
      "content": "That sounds great! How long are you planning to stay?",
      "timestamp": "2026-03-03T01:45:05Z"
    },
    {
      "role": "user",
      "content": "About 2 weeks. We're staying at the Sandals in Montego Bay.",
      "name": "Mani"
    }
  ],
  "session_id": "telegram-1006700997-2026-03-03",  // optional grouping
  "project_id": "default",  // namespace
  "context": {  // optional metadata
    "channel": "telegram",
    "user_timezone": "America/New_York"
  },
  "options": {
    "extract_from": "both",  // "user" | "assistant" | "both"
    "min_importance": 0.5,   // 0.0-1.0, filter low-value facts
    "dedupe": true,          // check against existing memories
    "store": true            // false = dry run, just show extractions
  }
}
```

### Response

```json
{
  "status": "ok",
  "session_id": "telegram-1006700997-2026-03-03",
  "extracted": {
    "facts": [
      {
        "content": "Mani's wife is named Suzan",
        "confidence": 0.95,
        "source_message": 0,
        "stored": true,
        "memory_id": "abc-123"
      },
      {
        "content": "Mani is planning a trip to Jamaica in April 2026",
        "confidence": 0.90,
        "source_message": 0,
        "stored": true,
        "memory_id": "abc-124"
      },
      {
        "content": "Mani's Jamaica trip is 2 weeks long",
        "confidence": 0.85,
        "source_message": 2,
        "stored": true,
        "memory_id": "abc-125"
      },
      {
        "content": "Mani is staying at Sandals Montego Bay in Jamaica",
        "confidence": 0.90,
        "source_message": 2,
        "stored": true,
        "memory_id": "abc-126"
      }
    ],
    "entities": [
      {"name": "Suzan", "type": "person", "relationship": "wife of user"},
      {"name": "Mani", "type": "person", "relationship": "user"},
      {"name": "Jamaica", "type": "location"},
      {"name": "Sandals Montego Bay", "type": "location", "subtype": "hotel"}
    ],
    "skipped": [
      {
        "content": "Assistant asked about trip duration",
        "reason": "low_importance",
        "importance": 0.2
      }
    ],
    "deduped": [
      {
        "content": "Suzan is Mani's wife",
        "existing_memory_id": "xyz-789",
        "action": "merged"
      }
    ]
  },
  "stats": {
    "messages_processed": 3,
    "facts_extracted": 5,
    "facts_stored": 4,
    "facts_deduped": 1,
    "facts_skipped": 1,
    "entities_found": 4,
    "processing_time_ms": 1250
  }
}
```

## Extraction Logic

### Phase 1: Message Analysis
```python
# Combine messages into analyzable chunks
# Preserve speaker attribution
# Handle multi-turn context
```

### Phase 2: Fact Extraction (LLM)
Use existing `FactExtractor` with enhanced prompt:

```
CONVERSATION EXTRACTION RULES:
1. Extract facts from BOTH user and assistant messages
2. Attribute facts to the correct speaker
3. Resolve pronouns using conversation context
4. Convert relative times using provided timestamps
5. Prioritize: decisions, preferences, facts, plans, relationships
6. Score importance 0.0-1.0 based on long-term value
```

### Phase 3: Entity Extraction
Use existing `EntityExtractor`:
- People (with relationships)
- Organizations
- Locations
- Products/Projects
- Dates/Events

### Phase 4: Deduplication
Check extracted facts against existing memories:
- Exact match → skip
- Semantic similarity >0.9 → merge/update
- Contradiction → flag conflict, use ConflictManager

### Phase 5: Storage
Store each fact as a memory with:
- `source: "conversation_ingest"`
- `session_id` for grouping
- `metadata.source_message_index`
- `metadata.speaker`
- `metadata.importance`

## Integration Patterns

### Pattern 1: Real-time Streaming
Tool sends messages as they happen:
```python
# After each exchange
remembra.ingest_conversation(
    messages=[last_user_msg, last_assistant_msg],
    session_id="session-123"
)
```

### Pattern 2: Session Batches
Tool sends full session at end:
```python
# When session ends or on schedule
remembra.ingest_conversation(
    messages=session.get_all_messages(),
    session_id="session-123"
)
```

### Pattern 3: Webhook Push
Remembra receives webhook from tool:
```python
# Tool configures webhook to POST to Remembra
POST /api/v1/ingest/webhook
{
  "source": "clawdbot",
  "event": "session_end",
  "messages": [...]
}
```

## Configuration Options

### User-Level Settings
```json
{
  "auto_ingest": {
    "enabled": true,
    "min_importance": 0.6,
    "extract_from": "both",
    "max_facts_per_session": 50,
    "dedupe_window": "7d"
  }
}
```

### Per-Request Overrides
All settings can be overridden in request `options`.

## Rate Limits

| Tier | Requests/min | Messages/request | Max facts/day |
|------|-------------|------------------|---------------|
| Free | 5 | 50 | 100 |
| Pro | 30 | 200 | 5,000 |
| Enterprise | 100 | 1000 | Unlimited |

## Implementation Plan

### Phase 1: Core Endpoint (Week 1)
- [ ] `POST /ingest/conversation` endpoint
- [ ] Request/response models
- [ ] Basic message parsing
- [ ] Hook into existing FactExtractor

### Phase 2: Enhanced Extraction (Week 2)
- [ ] Conversation-aware extraction prompt
- [ ] Speaker attribution
- [ ] Importance scoring
- [ ] Relative time resolution

### Phase 3: Deduplication (Week 3)
- [ ] Semantic similarity check
- [ ] Merge logic
- [ ] Conflict flagging

### Phase 4: Integrations (Week 4)
- [ ] Webhook receiver
- [ ] Clawdbot plugin
- [ ] MCP server update
- [ ] Python SDK method

## SDK Usage (After Implementation)

```python
from remembra import Remembra

client = Remembra(api_key="...")

# Ingest a conversation
result = client.ingest_conversation(
    messages=[
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."},
    ],
    session_id="my-session",
    options={"min_importance": 0.6}
)

print(f"Stored {result.stats.facts_stored} facts")
```

## Open Questions

1. **Privacy:** Should we support PII redaction before storage?
2. **Retention:** Different TTL for conversation-sourced vs manual memories?
3. **Attribution:** How to handle multi-user conversations?
4. **Streaming:** SSE for real-time extraction feedback?

---

## Appendix: Message Schema

```python
class ConversationMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: datetime | None = None
    name: str | None = None  # Speaker name for multi-user
    metadata: dict | None = None

class ConversationIngestRequest(BaseModel):
    messages: list[ConversationMessage]
    session_id: str | None = None
    project_id: str = "default"
    context: dict | None = None
    options: IngestOptions | None = None

class IngestOptions(BaseModel):
    extract_from: Literal["user", "assistant", "both"] = "both"
    min_importance: float = 0.5
    dedupe: bool = True
    store: bool = True  # False for dry run
```
