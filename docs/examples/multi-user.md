# Multi-User Application

Building a SaaS application with per-user memory.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   User A     │     │   User B     │     │   User C     │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│                    Your Application                      │
│                                                          │
│  Memory(api_key=key_a)  Memory(api_key=key_b)  ...       │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Remembra   │
                    │  (shared)   │
                    └─────────────┘
```

## User Isolation

With auth on, the server takes the user from the API key. The `user_id` you pass to `Memory(...)` is not used
for isolation: two clients with the same key share one set of memories, whatever `user_id` they send. With auth
off, every request belongs to one default user.

So give each end user their own API key. Create it with the master key, for that user's `user_id`:

```python
import requests

REMEMBRA_URL = "http://localhost:8787"

def create_user_memory_key(user_id: str) -> str:
    response = requests.post(
        f"{REMEMBRA_URL}/api/v1/keys",
        headers={"X-API-Key": MASTER_KEY},
        json={"user_id": user_id, "name": f"Key for {user_id}", "role": "editor"},
    )
    response.raise_for_status()
    return response.json()["key"]  # shown once: store it in your database

# User A's memory
memory_a = Memory(base_url=REMEMBRA_URL, api_key=key_for_user_a)
memory_a.store("My favorite color is blue")

# User B's memory
memory_b = Memory(base_url=REMEMBRA_URL, api_key=key_for_user_b)
memory_b.store("My favorite color is red")

# Each key sees only its own user's memories
memory_a.recall("favorite color").context  # mentions blue
memory_b.recall("favorite color").context  # mentions red
```

Keep the keys server-side, in your database. Never send them to the browser.

## Implementation

### Flask Application

```python
from flask import Flask, request, jsonify, g
from remembra import Memory

app = Flask(__name__)
REMEMBRA_URL = "http://localhost:8787"

def get_memory() -> Memory:
    """Get memory instance for current user."""
    if 'memory' not in g:
        user = get_current_user()  # Your auth logic
        g.memory = Memory(base_url=REMEMBRA_URL, api_key=user.remembra_key)
    return g.memory

@app.route('/chat', methods=['POST'])
def chat():
    memory = get_memory()
    message = request.json['message']

    # Recall context for this user
    context = memory.recall(message, limit=5).context

    # Generate response (your LLM logic)
    response = generate_response(message, context)

    # Store for this user
    memory.store(message)

    return jsonify({'response': response})

@app.route('/forget', methods=['POST'])
def forget():
    """Delete all of this user's memories."""
    memory = get_memory()
    memory.forget(all_memories=True)
    return jsonify({'status': 'deleted'})
```

### FastAPI Application

```python
from fastapi import FastAPI, Depends, Request
from remembra import Memory

app = FastAPI()

def get_user_key(request: Request) -> str:
    """Look up the signed-in user's Remembra key."""
    # Your auth logic here
    return request.state.user.remembra_key

def get_memory(key: str = Depends(get_user_key)) -> Memory:
    """Dependency injection for memory."""
    return Memory(base_url="http://localhost:8787", api_key=key)

@app.post("/chat")
async def chat(
    message: str,
    memory: Memory = Depends(get_memory)
):
    context = memory.recall(message).context
    response = await generate_response(message, context)
    memory.store(message)
    return {"response": response}

@app.delete("/user/data")
async def delete_user_data(memory: Memory = Depends(get_memory)):
    """Delete all of this user's memories."""
    memory.forget(all_memories=True)
    return {"status": "deleted"}
```

The SDK is synchronous; in an async app, run its calls in a thread pool if they must not block.

## Multi-Tenancy

For B2B SaaS with organization-level isolation, use projects inside each user's account, or one account per
organization:

```python
# Option 1: one Remembra user per organization, a project per person
memory = Memory(base_url=REMEMBRA_URL, api_key=org.remembra_key, project=f"user-{user_id}")

# Option 2: one Remembra user per person, a project per organization
memory = Memory(base_url=REMEMBRA_URL, api_key=user.remembra_key, project=org_id)
```

With option 1, every key of the organization can read every project unless you create project-restricted keys
(`"project_ids": ["user-123"]` when you create the key).

### Organization-Wide Memory

```python
# Personal memory (a project only this user's key may use)
personal_memory = Memory(base_url=REMEMBRA_URL, api_key=personal_key, project=f"{org_id}_personal_{user_id}")

# Org-wide memory (shared within the org)
org_memory = Memory(base_url=REMEMBRA_URL, api_key=org_key, project=f"{org_id}_shared")

# Query both
def recall_with_org_context(query: str):
    personal = personal_memory.recall(query, limit=3).context
    org = org_memory.recall(query, limit=3).context
    return f"Personal: {personal}\n\nOrg: {org}"
```

## Data Requests

### Data Export

```python
def export_user_data(key: str, project: str = "default") -> str:
    response = requests.get(
        f"{REMEMBRA_URL}/api/v1/transfer/export",
        headers={"X-API-Key": key},
        params={"format": "json", "project_id": project},
    )
    response.raise_for_status()
    return response.text
```

The export covers memories, one project per request, up to 100,000 memories, with stored credentials redacted.
Entities are not in it; read them with `GET /api/v1/entities`.

### Data Deletion

```python
def delete_user_data(key: str):
    memory = Memory(base_url=REMEMBRA_URL, api_key=key)

    # Delete all of this user's memories, entities and relationships
    memory.forget(all_memories=True)

    # Log for your own records
    audit_log.record(action="user_data_deleted", timestamp=datetime.now())
```

Then revoke the user's key: `DELETE /api/v1/keys/{key_id}`.

### Right to Rectification

```python
def update_user_memory(key: str, memory_id: str, new_content: str):
    memory = Memory(base_url=REMEMBRA_URL, api_key=key)
    memory.update(memory_id, new_content)
```

## Scaling Considerations

### Client Reuse

```python
from functools import lru_cache

@lru_cache(maxsize=100)
def get_memory_client(key: str) -> Memory:
    """Cache memory clients per key."""
    return Memory(base_url="http://localhost:8787", api_key=key)
```

### Rate Limit Handling

```python
import time
from remembra import MemoryError

def store_with_retry(memory: Memory, content: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return memory.store(content)
        except MemoryError as e:
            if e.status_code == 429 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
```

### Many Stores at Once

`POST /api/v1/memories/batch` stores several memories in one request. See the [REST API guide](../guides/rest-api.md).
