# JavaScript / TypeScript SDK

Reference for the `remembra` package.

!!! note "npm and this page"
    This page describes the SDK in the repository (`sdk/typescript`, version 0.13.2). The package on npm is still
    0.12.1: it does not check the server version before a delete by entity (see [forget()](#forget)). Until a
    newer npm release, update your server to 0.16.1 before deleting by entity from TypeScript.

Works in Node.js 18+. The SDK uses only `fetch`, so Deno and Bun should work, but they are untested. In a
browser, the hosted API accepts only Remembra's own origins (`https://app.remembra.dev`, `https://remembra.dev`),
so a browser app needs a self-hosted server with `REMEMBRA_CORS_ORIGINS` set to its origin.

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

## Quick Start

```typescript
import { Remembra } from 'remembra';

const memory = new Remembra({
  url: 'http://localhost:8787',
  apiKey: 'rem_xxx',     // when the server has auth on
  userId: 'user_123',    // required
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
| `url` | `string` | `http://localhost:8787` | Server URL |
| `apiKey` | `string` | — | API key for authentication |
| `userId` | `string` | — | **Required** (the constructor throws without it). With auth on, the server takes the user from the API key |
| `project` | `string` | `"default"` | Project namespace |
| `timeout` | `number` | `30000` | Request timeout (ms) |
| `debug` | `boolean` | `false` | Log requests to the console |

## Methods

### store()

Store a memory with automatic fact and entity extraction.

```typescript
// Simple string
await memory.store('User prefers dark mode');

// With options
await memory.store('Meeting notes: decided to use PostgreSQL', {
  metadata: { source: 'meeting', date: '2024-03-01' },
  ttl: '30d',
});
```

**Returns:** `StoreResult`

```typescript
{
  id: string;
  extracted_facts: string[];
  entities: EntityRef[];
}
```

### recall()

Search memories using hybrid search (semantic + keyword).

```typescript
// Simple query
const result = await memory.recall('What are user preferences?');
console.log(result.context);    // context string for an LLM prompt
console.log(result.memories);   // array of matching memories

// With options
const decisions = await memory.recall('project decisions', {
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
  context: string;        // context string for an LLM prompt
  memories: Memory[];     // individual matching memories
  entities: EntityRef[];  // related entities
}
```

The TypeScript SDK has no `slim` option. The REST API and the Python SDK take `slim=true`, which caps the context
at 800 tokens.

### get()

Get a specific memory by ID.

```typescript
const detail = await memory.get('01HQ...');
console.log(detail.content);
```

### forget()

Delete memories. They leave the database and the vector store at once. Conflict records that quoted the text
are removed only when the account is deleted, and copies in backups age out (see Retention on
[remembra.dev/security](https://remembra.dev/security#retention)).

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
    A server before 0.16.1 deleted every memory in the account for a delete by `entity`. In the repository's
    SDK, `forget({ entity })` reads the server version from `/health` first and throws a `RemembraError` with code
    `SERVER_TOO_OLD`, deleting nothing, when the server is older than 0.16.1, reports a pre-release or dev build
    of 0.16.1, or reports no version. The npm package (0.12.1) does not have this check.

**Returns:** `ForgetResult`

```typescript
{
  deleted_memories: number;
  deleted_entities: number;
  deleted_relationships: number;
}
```

### ingestConversation()

Extract memories from a chat conversation. See [Conversation Ingestion](conversation-ingestion.md).

```typescript
const result = await memory.ingestConversation(
  [
    { role: 'user', content: 'My name is Sarah and I lead the design team' },
    { role: 'assistant', content: 'Nice to meet you, Sarah!' },
  ],
  { minImportance: 0.5 },
);
console.log(result.stats.facts_stored);
```

### listEntities()

```typescript
const entities = await memory.listEntities();
for (const e of entities) {
  console.log(`${e.canonical_name} (${e.type})`);
}
```

### health()

Check server health.

```typescript
const health = await memory.health();
console.log(health.status);   // "ok" or "degraded"
console.log(health.version);
```

Entity relationships, decay reports, cleanup, changelog ingest and user profiles have no TypeScript methods. Use
the REST API for them (see the [REST API guide](rest-api.md)).

## Error Handling

Errors are thrown as `RemembraError` or one of its subclasses: `AuthenticationError` (401), `NotFoundError` (404),
`ValidationError` (422, or a bad call caught before sending), `RateLimitError` (429, with `retryAfter`),
`ServerError` (500), `NetworkError` and `TimeoutError`.

```typescript
import { Remembra, RemembraError, RateLimitError } from 'remembra';

try {
  await memory.store('some content');
} catch (error) {
  if (error instanceof RateLimitError) {
    console.log(`Retry after ${error.retryAfter}s`);
  } else if (error instanceof RemembraError) {
    console.log(error.message); // Human-readable message
    console.log(error.status);  // HTTP status (e.g., 401, 429)
    console.log(error.code);    // e.g. "AUTH_ERROR", "SERVER_TOO_OLD"
  }
}
```

## TypeScript Types

These types are exported:

```typescript
import type {
  RemembraConfig,
  StoreOptions,
  StoreResult,
  RecallOptions,
  RecallResult,
  ForgetOptions,
  ForgetResult,
  Message,
  IngestOptions,
  IngestResult,
  Memory,
  EntityRef,
} from 'remembra';
```

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
