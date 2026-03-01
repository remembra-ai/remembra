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
│  Memory(user_id="a")  Memory(user_id="b")  Memory(...)  │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Remembra   │
                    │  (shared)   │
                    └─────────────┘
```

## User Isolation

Each user_id has completely isolated memories:

```python
# User A's memory
memory_a = Memory(base_url="...", user_id="user_a")
memory_a.store("My favorite color is blue")

# User B's memory
memory_b = Memory(base_url="...", user_id="user_b")
memory_b.store("My favorite color is red")

# User A can't see User B's memories
memory_a.recall("favorite color")  # Returns "blue"
memory_b.recall("favorite color")  # Returns "red"
```

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
        user_id = get_current_user_id()  # Your auth logic
        g.memory = Memory(
            base_url=REMEMBRA_URL,
            user_id=user_id,
            api_key=REMEMBRA_API_KEY
        )
    return g.memory

@app.route('/chat', methods=['POST'])
def chat():
    memory = get_memory()
    message = request.json['message']
    
    # Recall context for this user
    context = memory.recall(message, limit=5)
    
    # Generate response (your LLM logic)
    response = generate_response(message, context)
    
    # Store for this user
    memory.store(message)
    
    return jsonify({'response': response})

@app.route('/forget', methods=['POST'])
def forget():
    """GDPR: Delete all user data."""
    memory = get_memory()
    memory.forget(all=True)
    return jsonify({'status': 'deleted'})
```

### FastAPI Application

```python
from fastapi import FastAPI, Depends, Request
from remembra import Memory

app = FastAPI()

def get_user_id(request: Request) -> str:
    """Extract user ID from auth token."""
    # Your auth logic here
    return request.state.user_id

def get_memory(user_id: str = Depends(get_user_id)) -> Memory:
    """Dependency injection for memory."""
    return Memory(
        base_url="http://localhost:8787",
        user_id=user_id,
        api_key=REMEMBRA_API_KEY
    )

@app.post("/chat")
async def chat(
    message: str,
    memory: Memory = Depends(get_memory)
):
    context = memory.recall(message)
    response = await generate_response(message, context)
    memory.store(message)
    return {"response": response}

@app.delete("/user/data")
async def delete_user_data(memory: Memory = Depends(get_memory)):
    """GDPR deletion endpoint."""
    memory.forget(all=True)
    return {"status": "deleted"}
```

## Multi-Tenancy

For B2B SaaS with organization-level isolation:

```python
# Option 1: Composite user_id
memory = Memory(
    user_id=f"{org_id}:{user_id}",  # "acme:user_123"
    project="saas"
)

# Option 2: Separate projects per org
memory = Memory(
    user_id=user_id,
    project=org_id  # "acme"
)
```

### Organization-Wide Memory

```python
# Personal memory (only this user)
personal_memory = Memory(user_id=user_id, project=f"{org_id}_personal")

# Org-wide memory (shared within org)
org_memory = Memory(user_id=org_id, project=f"{org_id}_shared")

# Query both
def recall_with_org_context(query: str):
    personal = personal_memory.recall(query, limit=3)
    org = org_memory.recall(query, limit=3)
    return f"Personal: {personal}\n\nOrg: {org}"
```

## API Key Management

### Per-User Keys

```python
# Create key for new user
def create_user_memory_key(user_id: str) -> str:
    response = requests.post(
        f"{REMEMBRA_URL}/api/v1/keys",
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        json={
            "user_id": user_id,
            "name": f"Key for {user_id}"
        }
    )
    return response.json()["key"]

# Store in your database
user.remembra_key = create_user_memory_key(user.id)
user.save()
```

### Shared Application Key

```python
# Single key for all users (simpler, but less isolated)
APP_KEY = os.environ["REMEMBRA_APP_KEY"]

# User isolation via user_id (enforced by Remembra)
memory = Memory(
    base_url="...",
    user_id=user_id,
    api_key=APP_KEY  # Same key, different user_id
)
```

## GDPR Compliance

### Data Export

```python
def export_user_data(user_id: str) -> dict:
    memory = Memory(base_url="...", user_id=user_id)
    
    # Get all memories
    all_memories = memory.get_all_memories()
    
    # Get entities
    entities = memory.get_entities()
    
    return {
        "user_id": user_id,
        "memories": all_memories,
        "entities": entities,
        "exported_at": datetime.now().isoformat()
    }
```

### Data Deletion

```python
def delete_user_data(user_id: str):
    memory = Memory(base_url="...", user_id=user_id)
    
    # Delete all memories
    memory.forget(all=True)
    
    # Log for compliance
    audit_log.record(
        action="user_data_deleted",
        user_id=user_id,
        timestamp=datetime.now()
    )
```

### Right to Rectification

```python
def update_user_memory(user_id: str, memory_id: str, new_content: str):
    memory = Memory(base_url="...", user_id=user_id)
    memory.update(memory_id, new_content)
```

## Scaling Considerations

### Connection Pooling

```python
from functools import lru_cache

@lru_cache(maxsize=100)
def get_memory_client(user_id: str) -> Memory:
    """Cache memory clients per user."""
    return Memory(
        base_url="http://localhost:8787",
        user_id=user_id,
        api_key=APP_KEY
    )
```

### Rate Limit Handling

```python
from remembra.exceptions import RateLimitError
import time

def store_with_retry(memory: Memory, content: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return memory.store(content)
        except RateLimitError as e:
            if attempt < max_retries - 1:
                time.sleep(e.retry_after)
            else:
                raise
```

### Async Operations

```python
from remembra import AsyncMemory
import asyncio

async def batch_store(user_id: str, contents: list[str]):
    memory = AsyncMemory(base_url="...", user_id=user_id)
    tasks = [memory.store(c) for c in contents]
    return await asyncio.gather(*tasks)
```
