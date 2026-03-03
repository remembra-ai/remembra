# Conversation Ingestion

**NEW in v0.7.0** — Automatically extract memories from chat conversations.

This is Remembra's answer to Mem0's conversation parsing — but with full control over extraction and deduplication.

## Overview

Instead of manually storing individual facts, pipe entire conversations through the ingestion service:

```python
from remembra import Memory

memory = Memory(user_id="user_123")

# Ingest a full conversation
result = await memory.ingest_conversation([
    {"role": "user", "content": "My name is John and I work at Acme Corp"},
    {"role": "assistant", "content": "Nice to meet you John! What do you do at Acme?"},
    {"role": "user", "content": "I'm the CTO. We're building AI tools."},
])

print(result.stats)
# IngestStats(messages_processed=3, facts_extracted=4, entities_found=2, duplicates_skipped=0)
```

## How It Works

The ingestion pipeline:

1. **Message Parsing** — Filter and prepare messages
2. **Fact Extraction** — LLM extracts atomic facts from conversation
3. **Entity Extraction** — Identify people, organizations, locations
4. **Deduplication** — Skip facts that already exist in memory
5. **Storage** — Store new memories with proper metadata

## API Reference

### Endpoint

```http
POST /api/v1/memories/ingest
```

### Request Body

```json
{
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "options": {
    "extract_entities": true,
    "deduplicate": true,
    "min_importance": 0.3
  }
}
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `extract_entities` | bool | `true` | Extract entities and relationships |
| `deduplicate` | bool | `true` | Check for existing memories |
| `min_importance` | float | `0.3` | Minimum importance score to store |
| `source` | string | `"conversation"` | Source tag for memories |

### Response

```json
{
  "success": true,
  "stats": {
    "messages_processed": 10,
    "facts_extracted": 8,
    "entities_found": 3,
    "duplicates_skipped": 2,
    "memories_stored": 6,
    "processing_time_ms": 1250
  },
  "memories": ["mem_abc123", "mem_def456", ...]
}
```

## Python SDK

```python
from remembra import Memory
from remembra.models import ConversationMessage, IngestOptions

memory = Memory(user_id="user_123")

# Basic usage
result = await memory.ingest_conversation(messages)

# With options
result = await memory.ingest_conversation(
    messages=[
        ConversationMessage(role="user", content="..."),
        ConversationMessage(role="assistant", content="..."),
    ],
    options=IngestOptions(
        extract_entities=True,
        deduplicate=True,
        min_importance=0.5,
    )
)
```

## TypeScript SDK

```typescript
import { Remembra } from '@remembra/client';

const memory = new Remembra({ url: 'http://localhost:8787' });

const result = await memory.ingestConversation({
  messages: [
    { role: 'user', content: 'My name is Sarah...' },
    { role: 'assistant', content: 'Nice to meet you!' },
  ],
  options: {
    extractEntities: true,
    deduplicate: true,
  },
});

console.log(result.stats.factsExtracted); // 4
```

## MCP Server

When using Remembra with Claude Code or Cursor, conversation ingestion happens automatically when you use the `remembra_ingest` tool:

```
Claude: I'll store our entire conversation context.
[Tool: remembra_ingest]
✓ Ingested 12 messages → 8 new memories
```

## Best Practices

### When to Use Ingestion

- **End of session** — Ingest the full conversation when done
- **Periodic checkpoints** — Ingest every N messages
- **On topic change** — Ingest before switching contexts

### What Gets Extracted

The LLM extracts:

- **Facts** — "John is the CTO of Acme Corp"
- **Preferences** — "User prefers dark mode"
- **Relationships** — "John works with Sarah"
- **Temporal info** — "Meeting scheduled for Friday"

### What Gets Filtered

Automatically skipped:

- Greetings and small talk
- Questions without answers
- Redundant information
- Low-importance chatter

## Comparison with Manual Storage

| Approach | Pros | Cons |
|----------|------|------|
| Manual `store()` | Precise control | Requires explicit calls |
| Conversation Ingestion | Automatic, comprehensive | May extract unwanted info |

**Recommendation:** Use both. Manual `store()` for critical facts, ingestion for general context.

## Related

- [Entity Resolution](./entity-resolution.md) — How entities are linked
- [Sleep-Time Compute](./sleep-time-compute.md) — Background deduplication
- [Security](./security.md) — PII detection and anomaly monitoring
