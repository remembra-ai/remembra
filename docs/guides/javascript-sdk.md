# JavaScript / TypeScript SDK

Complete reference for the `remembra` package.

Works in Node.js 18+, Deno, Bun, and modern browsers.

## Installation

=== "npm"

    ```bash
    npm install remembra
    ```

=== "yarn"

    ```bash
    yarn add remembra
    ```

=== "pnpm"

    ```bash
    pnpm add remembra
    ```

=== "Deno"

    ```typescript
    import { Remembra } from "npm:remembra";
    ```

## Quick Start

```typescript
import { Remembra } from 'remembra';

const memory = new Remembra({
  url: 'http://localhost:8787',
  apiKey: 'rem_xxx',     // optional for self-hosted
  userId: 'user_123',    // optional
  project: 'my_app',     // optional
});

// Store
const stored = await memory.store('Alice is the CTO of Acme Corp');
console.log(stored.extracted_facts);
// → ["Alice is the CTO of Acme Corp."]

// Recall
const result = await memory.recall('Who leads Acme?');
console.log(result.context);
// → "Alice is the CTO of Acme Corp."

// Forget
await memory.forget({ memoryId: stored.id });
```

## Constructor

```typescript
new Remembra(config: RemembraConfig)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `string` | — | **Required.** Server URL |
| `apiKey` | `string` | — | API key for authentication |
| `userId` | `string` | `"default"` | User ID for memory isolation |
| `project` | `string` | `"default"` | Project namespace |
| `timeout` | `number` | `30000` | Request timeout (ms) |

## Core Methods

### store()

Store a memory with automatic fact and entity extraction.

```typescript
// Simple string
await memory.store('User prefers dark mode');

// With options
await memory.store({
  content: 'Meeting notes: decided to use PostgreSQL',
  metadata: { source: 'meeting', date: '2024-03-01' },
  ttl: '30d',
  project: 'backend',  // override default project
});
```

**Returns:** `StoreResult`

```typescript
{
  id: string;
  extracted_facts: string[];
  entities: EntityItem[];
}
```

### recall()

Search memories using hybrid search (semantic + keyword).

```typescript
// Simple query
const result = await memory.recall('What are user preferences?');
console.log(result.context);    // synthesized context string
console.log(result.memories);   // array of matching memories

// With options
const result = await memory.recall({
  query: 'project decisions',
  limit: 10,
  threshold: 0.6,
  maxTokens: 4000,
  enableHybrid: true,
  enableRerank: true,
});
```

**Returns:** `RecallResult`

```typescript
{
  context: string;        // synthesized context for LLM injection
  memories: MemoryItem[]; // individual matching memories
  entities: EntityItem[]; // related entities
}
```

### get()

Get a specific memory by ID.

```typescript
const detail = await memory.get('01HQ...');
console.log(detail.content);
console.log(detail.entities);
console.log(detail.access_count);
```

**Returns:** `MemoryDetail` with full metadata.

### forget()

Delete memories. They leave the database and the vector store at once; copies in backups age out (see Retention on [remembra.dev/security](https://remembra.dev/security#retention)).

```typescript
// By ID
await memory.forget({ memoryId: '01HQ...' });

// The memories linked to an entity, in one project
await memory.forget({ entity: 'John', projectId: 'work' });

// Everything in the account (explicit only)
await memory.forget({ allMemories: true });
```

Give exactly one of `memoryId`, `entity` or `allMemories: true`. Any other call throws a `ValidationError` and
sends nothing: no call deletes everything unless it says `allMemories: true`. The server deletes only the
memories of the account your API key belongs to.

- `entity` deletes the memories that entity extraction linked to the entity with that exact name or alias (any
  case; never a partial name), in every project, or only in `projectId` when you pass it. The entity and its
  relationships go too once no memory mentions it. A memory that names the entity but was never linked to it
  stays.
- `allMemories: true` deletes every memory, entity and relationship in the account. A project-scoped API key
  cannot use it.

!!! warning "Servers before 0.16.1"
    A server before 0.16.1 deleted every memory in the account for a delete by `entity`. `forget({ entity })`
    reads the server version from `/health` first and throws a `RemembraError` with code `SERVER_TOO_OLD`,
    deleting nothing, when the server is older than 0.16.1 or does not report a version.

**Returns:** `ForgetResult`

```typescript
{
  deleted_memories: number;
  deleted_entities: number;
  deleted_relationships: number;
}
```

### health()

Check server health.

```typescript
const health = await memory.health();
console.log(health.status);  // "ok" | "degraded" | "down"
console.log(health.version);
```

## Entity Methods

### listEntities()

```typescript
const result = await memory.listEntities({
  type: 'person',  // "person" | "company" | "location" | "concept"
  limit: 50,
});

for (const entity of result.entities) {
  console.log(`${entity.canonical_name} (${entity.type})`);
}
```

### getEntityRelationships()

```typescript
const rels = await memory.getEntityRelationships('entity_123');
for (const r of rels.relationships) {
  console.log(`${r.from_entity_name} → ${r.type} → ${r.to_entity_name}`);
}
```

### getEntityMemories()

```typescript
const result = await memory.getEntityMemories('entity_123', { limit: 20 });
console.log(`${result.entity_name}: ${result.total} memories`);
```

## Temporal Methods

### decayReport()

See memory relevance scores and prune candidates.

```typescript
const report = await memory.decayReport({ limit: 100 });
console.log(`Total: ${report.total_memories}`);
console.log(`Prune candidates: ${report.prune_candidates}`);

for (const m of report.memories) {
  if (m.should_prune) {
    console.log(`${m.content_preview} — relevance: ${m.relevance_score}`);
  }
}
```

### cleanup()

Clean up expired and decayed memories.

```typescript
// Preview (dry run)
const preview = await memory.cleanup({ dryRun: true });
console.log(`Would delete ${preview.expired_found} expired memories`);

// Actually clean up
const result = await memory.cleanup({
  dryRun: false,
  includeDecayed: true,
});
```

## Ingest

### ingestChangelog()

Import a CHANGELOG.md as searchable memories.

```typescript
import { readFileSync } from 'fs';

const changelog = readFileSync('CHANGELOG.md', 'utf-8');
const result = await memory.ingestChangelog({
  content: changelog,
  projectName: 'MyProject',
  maxReleases: 20,
});

console.log(`Stored ${result.memories_stored} releases`);
```

## Error Handling

All errors are thrown as `RemembraError`:

```typescript
import { Remembra, RemembraError } from 'remembra';

try {
  await memory.store('some content');
} catch (error) {
  if (error instanceof RemembraError) {
    console.log(error.message);     // Human-readable message
    console.log(error.statusCode);  // HTTP status (e.g., 401, 429)
    console.log(error.detail);      // Server error detail
  }
}
```

Common status codes:

| Code | Meaning |
|------|---------|
| 401 | Invalid API key |
| 404 | Memory not found |
| 422 | Invalid request parameters |
| 429 | Rate limited |
| 500 | Server error |
| 503 | Server degraded (Qdrant down) |

## Zero Dependencies

`remembra` has zero runtime dependencies. It uses the native `fetch()` API available in:

- Node.js 18+
- Deno
- Bun
- All modern browsers

## TypeScript Types

All types are exported for full IntelliSense:

```typescript
import type {
  RemembraConfig,
  StoreResult,
  RecallResult,
  RecallOptions,
  ForgetResult,
  HealthResult,
  MemoryItem,
  MemoryDetail,
  EntityItem,
  EntityDetail,
  DecayInfo,
  DecayReportResult,
} from 'remembra';
```
_ߍ{i]<\Vmusx5
---

## User Profiles API (v0.12.0)

Get aggregated user intelligence.

```typescript
const profile = await memory.getUserProfile();

console.log(profile);
// {
//   user_id: "user_123",
//   memory_count: 47,
//   entity_breakdown: { PERSON: 12, ORG: 8, LOCATION: 5 },
//   top_topics: ["AI", "meetings", "projects"],
//   last_active: "2026-03-22T15:30:00Z",
//   aggregated_facts: [
//     "Works at Acme Corp as senior engineer",
//     "Prefers morning meetings"
//   ]
// }
```

---

## Slim Recall Mode (v0.12.0)

Get a smaller response: just the context string, without the metadata.

```typescript
// Standard recall (full response with metadata)
const full = await memory.recall('What does the user prefer?');
// { context: "...", memories: [...], entities: [...], ... }

// Slim mode (just the context)
const slim = await memory.recall('What does the user prefer?', { slim: true });
// "User prefers dark mode and morning meetings."
```

---

## Expiry (TTL)

`store()` takes a `ttl`: a number and a unit, such as `'36h'` or `'30d'`. Every unit is in
[TTL formats](temporal.md#ttl-formats).

```typescript
// Expires 36 hours after it is stored
await memory.store('Meeting tomorrow', { ttl: '36h' });
```

The TypeScript SDK has no `expiresAt` option. For an exact expiry time, send `expires_at` to the REST API
(`POST /api/v1/memories`).

It does not read temporal phrases either: Smart Auto-Forgetting ("Meeting tomorrow" gets a 36h TTL) is in the
Python SDK only, with `auto_expire_temporal=True`.

