# Remembra TypeScript SDK

TypeScript/JavaScript SDK for [Remembra](https://remembra.dev) - the AI Memory Layer.

[![npm version](https://badge.fury.io/js/remembra.svg)](https://www.npmjs.com/package/remembra)
[![PyPI version](https://badge.fury.io/py/remembra.svg)](https://pypi.org/project/remembra/)

## What's New in v0.12.0

- **⏰ Expiry** — `store(content, { ttl: '36h' })`. This SDK takes only `ttl`; the REST API also takes
  `expires_at`. Temporal phrases ("meeting tomorrow") set a TTL only in the Python SDK.
- **🔒 Strict Mode 410 GONE** — On a server with `REMEMBRA_STRICT_MODE=true`, `get()` of an expired memory throws a
  `RemembraError` with `status` 410.

> **Note:** This SDK is a client for the Remembra REST API. It needs Node.js 18 or later and uses only `fetch`, so
> Deno and Bun should work, but they have not been tested. In a browser, the hosted API accepts requests only from
> Remembra's own sites (`https://app.remembra.dev`, `https://remembra.dev`), so a browser app needs a self-hosted
> server with `REMEMBRA_CORS_ORIGINS` set. For AI agent setup (Claude, Codex, Cursor), use the Python package:
> `pipx install --force 'remembra[mcp]>=0.16'`, then `remembra-install --all`.

## Installation

```bash
npm install remembra
# or
yarn add remembra
# or
pnpm add remembra
```

This README describes the SDK in this repository (0.13.2). As of 2026-09-27 the latest release on npm is 0.12.1,
and no npm release has the checks described under `forget()` below: its `forget({ entity })` sends the delete
without reading the server version first. On a server older than 0.16.1 that call deleted every memory in the
account, so update the server before you delete by entity with the npm release.

## Quick Start

```typescript
import { Remembra } from 'remembra';

// Initialize client
const memory = new Remembra({
  url: 'http://localhost:8787',  // Self-hosted
  apiKey: 'your-api-key',        // Optional for self-hosted
  userId: 'user_123',
});

// Store a memory
const stored = await memory.store('User prefers dark mode and hates long emails');
console.log(stored.extracted_facts);
// ['User prefers dark mode', 'User hates long emails']

// Recall memories
const result = await memory.recall('What are user preferences?');
console.log(result.context);
// 'User prefers dark mode. User hates long emails.'
```

## Conversation Ingestion

Automatically extract memories from conversations:

```typescript
const result = await memory.ingestConversation([
  { role: 'user', content: 'My wife Suzan and I are planning a trip to Japan' },
  { role: 'assistant', content: 'That sounds exciting! When are you going?' },
  { role: 'user', content: 'We are thinking April next year' },
], {
  minImportance: 0.5,
});

console.log(`Extracted: ${result.stats.facts_extracted} facts`);
console.log(`Stored: ${result.stats.facts_stored} memories`);

// Facts extracted:
// - "User's wife is named Suzan"
// - "User is planning a trip to Japan in April"
```

## API Reference

### Constructor

```typescript
new Remembra(config: RemembraConfig)
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `url` | string | `http://localhost:8787` | Remembra server URL |
| `apiKey` | string | - | API key for authentication |
| `userId` | string | **required** | User ID for memory isolation |
| `project` | string | `default` | Project namespace |
| `timeout` | number | `30000` | Request timeout (ms) |
| `debug` | boolean | `false` | Enable debug logging |

### Methods

#### `store(content, options?)`

Store a new memory.

```typescript
const result = await memory.store('John is the CEO of Acme Corp', {
  metadata: { source: 'meeting' },
  ttl: '30d',  // Expires in 30 days
});
```

A TTL is a number and a unit: `s`, `min` (or `m`), `h`, `d`, `w`, `mo` (30 days) or `y` (365 days).
Servers 0.16.1 and earlier read `m` as months and only whole numbers, so use whole `h`, `d`, `w` or `y` values
with them.

#### `recall(query, options?)`

Recall relevant memories.

```typescript
const result = await memory.recall('Who is John?', {
  limit: 10,
  threshold: 0.5,
  maxTokens: 800,  // Optional cap on the context string
});

console.log(result.context);   // Synthesized context
console.log(result.memories);  // Individual memories
console.log(result.entities);  // Related entities
```

This SDK has no `slim` option. The MCP `recall_memories` tool has one; on the REST API, `slim` only caps the
context at 800 tokens and still returns memories and entities.

#### `ingestConversation(messages, options?)`

Ingest a conversation and extract memories.

```typescript
const result = await memory.ingestConversation(messages, {
  sessionId: 'session_123',
  extractFrom: 'both',    // 'user' | 'assistant' | 'both'
  minImportance: 0.5,
  dedupe: true,
  store: true,            // false for dry-run
  infer: true,            // false to store raw messages
});
```

#### `forget(options)`

Delete memories. Give exactly one of `memoryId`, `entity` or `allMemories: true`; anything else throws a
`ValidationError` before a request is sent. A delete by entity reads the server version first and is not sent to a
server older than 0.16.1 (or to a pre-release or dev build of 0.16.1).

```typescript
// Delete specific memory
await memory.forget({ memoryId: 'mem_123' });

// Delete the memories linked to an entity (exact name or alias), in one project
await memory.forget({ entity: 'John', projectId: 'work' });

// Delete every memory, entity and relationship in the account (explicit only)
await memory.forget({ allMemories: true });
```

`entity` deletes only memories that entity extraction linked to that entity, in every project unless
`projectId` is given, then the entity itself once no memory mentions it.

The server deletes the memories and their vectors at once. Conflict records that quoted the text stay until the
account is erased, and database copies taken when a new server build starts keep deleted data until 3 newer copies
exist.

#### `get(memoryId)`

Get a specific memory by ID.

```typescript
const mem = await memory.get('mem_123');
```

#### `health()`

Check server health.

```typescript
const health = await memory.health();
```

## Error Handling

```typescript
import { 
  RemembraError,
  AuthenticationError,
  RateLimitError,
  ValidationError,
} from 'remembra';

try {
  await memory.store(content);
} catch (error) {
  if (error instanceof AuthenticationError) {
    // Handle auth error
  } else if (error instanceof RateLimitError) {
    // Wait and retry
    console.log(`Retry after ${error.retryAfter} seconds`);
  } else if (error instanceof ValidationError) {
    // Handle validation error
  } else if (error instanceof RemembraError) {
    // Generic Remembra error
    console.log(error.status, error.message);
  }
}
```

## License

MIT
