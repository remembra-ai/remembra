# Conversation Ingestion

Added in v0.7.1: extract memories from chat conversations.

## Overview

Instead of storing facts one by one, send a whole conversation. The server pulls the facts out of it:

```python
from remembra import Memory

memory = Memory(api_key="rem_...")

# Ingest a full conversation
result = memory.ingest_conversation([
    {"role": "user", "content": "My name is John and I work at Acme Corp"},
    {"role": "assistant", "content": "Nice to meet you John! What do you do at Acme?"},
    {"role": "user", "content": "I'm the CTO. We're building AI tools."},
])

print(result.stats.facts_extracted, result.stats.facts_stored)
```

## How It Works

The ingestion pipeline:

1. **Message Parsing** — Keep the messages to extract from (`user`, `assistant` or both; system messages are
   left out unless you ask for them)
2. **Fact Extraction** — An LLM extracts atomic facts, each with an importance score
3. **Entity Extraction** — People, organizations and places are identified
4. **Deduplication** — A fact that is already known is skipped, or it supersedes the older memory
5. **Storage** — New facts at or above `min_importance` are stored

Extraction needs an OpenAI key on the server (`REMEMBRA_OPENAI_API_KEY`). With `infer: false`, the raw messages
are stored instead, with no extraction.

## API Reference

### Endpoint

```http
POST /api/v1/ingest/conversation
```

`POST /api/v1/ingest/conversation/stream` takes the same body and reports progress as server-sent events.

### Request Body

```json
{
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "session_id": "optional-session-id",
  "project_id": "default",
  "options": {
    "extract_from": "both",
    "min_importance": 0.5,
    "dedupe": true,
    "store": true,
    "infer": true
  }
}
```

Up to 200 messages per request.

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `extract_from` | string | `"both"` | `user`, `assistant` or `both` |
| `min_importance` | float | `0.5` | Minimum importance (0-1) for a fact to be stored |
| `dedupe` | bool | `true` | Check new facts against existing memories |
| `store` | bool | `true` | `false` is a dry run: facts are returned, nothing is stored |
| `infer` | bool | `true` | `false` stores the raw messages instead of extracted facts |
| `include_system` | bool | `false` | Also use system messages |

### Response

```json
{
  "status": "ok",
  "session_id": "optional-session-id",
  "facts": [
    {"content": "John is the CTO of Acme Corp", "importance": 0.8, "stored": true, "memory_id": "mem_abc123", "action": "..."}
  ],
  "entities": [{"name": "John", "type": "PERSON"}],
  "deduped": [],
  "stats": {
    "messages_processed": 3,
    "facts_extracted": 4,
    "facts_stored": 3,
    "facts_updated": 0,
    "facts_deduped": 1,
    "facts_skipped": 0,
    "facts_dropped": 0,
    "entities_found": 2,
    "processing_time_ms": 1250
  },
  "errors": []
}
```

## Python SDK

```python
result = memory.ingest_conversation(
    messages=[
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."},
    ],
    session_id="chat-42",
    extract_from="both",
    min_importance=0.5,
    dedupe=True,
    store=True,
    infer=True,
)
```

## TypeScript SDK

```typescript
import { Remembra } from 'remembra';

const memory = new Remembra({ url: 'http://localhost:8787', apiKey: 'rem_...', userId: 'user_123' });

const result = await memory.ingestConversation(
  [
    { role: 'user', content: 'My name is Sarah...' },
    { role: 'assistant', content: 'Nice to meet you!' },
  ],
  { minImportance: 0.5, dedupe: true },
);

console.log(result.stats.facts_extracted);
```

## MCP Server

With Claude Code, Cursor or another MCP client, the agent ingests a conversation when it calls the
`ingest_conversation` MCP tool. Nothing is ingested unless the agent calls it.

## Best Practices

### When to Use Ingestion

- **End of session** — Ingest the full conversation when done
- **Periodic checkpoints** — Ingest every N messages
- **On topic change** — Ingest before switching contexts

### What Gets Extracted

The LLM is asked to extract:

- **Facts** — "John is the CTO of Acme Corp"
- **Preferences** — "User prefers dark mode"
- **Relationships** — "John works with Sarah"
- **Temporal info** — "Meeting scheduled for Friday"

Facts below `min_importance`, such as greetings and small talk, are not stored.

## Comparison with Manual Storage

| Approach | Pros | Cons |
|----------|------|------|
| Manual `store()` | Precise control | Requires explicit calls |
| Conversation Ingestion | Fewer calls, whole conversations | May extract unwanted info |

**Recommendation:** Use both. Manual `store()` for critical facts, ingestion for general context.

## Related

- [Entity Resolution](./entity-resolution.md) — How entities are linked
- [Sleep-Time Compute](./sleep-time-compute.md) — Background passes
- [Security](./security.md) — PII detection and secret redaction
