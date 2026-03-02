# JavaScript / TypeScript SDK

Complete reference for the `@remembra/client` package.

Works in Node.js 18+, Deno, Bun, and modern browsers.

## Installation

=== "npm"

    ```bash
    npm install @remembra/client
    ```

=== "yarn"

    ```bash
    yarn add @remembra/client
    ```

=== "pnpm"

    ```bash
    pnpm add @remembra/client
    ```

=== "Deno"

    ```typescript
    import { Remembra } from "npm:@remembra/client";
    ```

## Quick Start

```typescript
import { Remembra } from '@remembra/client';

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

Delete memories (GDPR-compliant).

```typescript
// By ID
await memory.forget({ memoryId: '01HQ...' });

// By entity
await memory.forget({ entity: 'Acme Corp' });

// All memories (careful!)
await memory.forget({ all: true });
```

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
import { Remembra, RemembraError } from '@remembra/client';

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

`@remembra/client` has zero runtime dependencies. It uses the native `fetch()` API available in:

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
} from '@remembra/client';
```
