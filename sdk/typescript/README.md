# Remembra TypeScript SDK

TypeScript/JavaScript SDK for [Remembra](https://remembra.dev) - the AI Memory Layer.

[![npm version](https://badge.fury.io/js/remembra.svg)](https://www.npmjs.com/package/remembra)
[![PyPI version](https://badge.fury.io/py/remembra.svg)](https://pypi.org/project/remembra/)

## What's New in v0.12.0

- **👤 User Profiles** — Profile management with avatars and preferences
- **🧠 Smart Auto-Forgetting** — Human-like memory that naturally fades
- **⏰ Event-driven Expiry** — `expires_at` field for precise lifecycle control
- **🔒 Strict Mode 410 GONE** — Expired memories return proper HTTP 410
- **🌐 Browser Extension** — Access memories from any webpage

> **Note:** The TypeScript SDK is for client-side usage. For AI agent setup (Claude, Codex, Cursor), use the Python package: `pip install remembra && remembra-install --all`

## Installation

```bash
npm install remembra
# or
yarn add remembra
# or
pnpm add remembra
```

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

#### `recall(query, options?)`

Recall relevant memories.

```typescript
const result = await memory.recall('Who is John?', {
  limit: 10,
  threshold: 0.5,
  slim: false,  // Set true for 90% smaller response (context only)
});

console.log(result.context);   // Synthesized context
console.log(result.memories);  // Individual memories (omitted if slim=true)
console.log(result.entities);  // Related entities (omitted if slim=true)
```

**Slim mode** (v0.10.1+): For token-constrained environments, use `slim: true` to get only the context string:

```typescript
const result = await memory.recall('Who is John?', { slim: true });
// Returns just: { context: "John is the CEO of Acme Corp." }
```

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

Delete memories (GDPR-compliant).

```typescript
// Delete specific memory
await memory.forget({ memoryId: 'mem_123' });

// Delete all about an entity
await memory.forget({ entity: 'John' });

// Delete all user memories
await memory.forget();
```

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
