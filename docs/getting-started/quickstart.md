# Quickstart

Get Remembra running in 5 minutes.

## Zero-Config Quick Start

The fastest way to get started. One command installs Remembra, Qdrant, and Ollama via Docker Compose -- no API keys needed.

```bash
curl -sSL https://get.remembra.dev/quickstart.sh | bash
```

This script will:

- Pull and start **Remembra**, **Qdrant** (vector database), and **Ollama** (local embeddings/extraction) containers
- Configure everything to work together automatically
- Start the Remembra server on `http://localhost:8787`

No OpenAI or other API keys are required. Ollama runs entirely locally for both embeddings and entity extraction.

!!! tip "Already running?"
    Skip ahead to [Step 2: Verify It's Running](#step-2-verify-its-running) once the script completes.

---

## Step 1: Start Remembra (Manual Setup)

If you prefer to configure things yourself, choose one of the options below.

### Prerequisites

- Docker (recommended) or Python 3.10+
- OpenAI API key (for embeddings/extraction), or Ollama for a fully local setup

=== "Docker"

    ```bash
    docker run -d \
      -p 8787:8787 \
      -e OPENAI_API_KEY=sk-your-key \
      -v remembra-data:/app/data \
      remembra/remembra
    ```

=== "Docker Compose"

    Create `docker-compose.yml`:

    ```yaml
    version: '3.8'
    services:
      remembra:
        image: remembra/remembra
        ports:
          - "8787:8787"
        environment:
          - OPENAI_API_KEY=${OPENAI_API_KEY}
        volumes:
          - remembra-data:/app/data

    volumes:
      remembra-data:
    ```

    Then run:

    ```bash
    docker-compose up -d
    ```

=== "From Source"

    ```bash
    git clone https://github.com/remembra-ai/remembra
    cd remembra
    pip install -e ".[server]"

    export OPENAI_API_KEY=sk-your-key
    python -m remembra.server
    ```

## Step 2: Verify It's Running

```bash
curl http://localhost:8787/health
# {"status":"healthy","version":"0.8.0"}
```

Or open the dashboard: [http://localhost:8787](http://localhost:8787)

## Step 3: Install the SDK

```bash
pip install remembra
```

## Step 4: Store Your First Memory

```python
from remembra import Memory

# Connect to your Remembra instance
memory = Memory(
    base_url="http://localhost:8787",
    user_id="quickstart-user"
)

# Store a memory
memory.store("""
    Had a great meeting with Sarah from Acme Corp today.
    She mentioned they're looking for AI solutions for their
    customer support team. Budget is around $50k/year.
    Follow up next Tuesday.
""")

print("Memory stored!")
```

## Step 5: Recall Memories

```python
# Ask questions about your memories
context = memory.recall("What do I know about Acme Corp?")
print(context)
# Output: "Sarah from Acme Corp is looking for AI solutions 
#          for customer support. Budget: $50k/year. 
#          Follow up scheduled for Tuesday."
```

## What Just Happened?

1. **Smart Extraction**: Your messy text was transformed into clean facts
2. **Entity Resolution**: "Sarah" was identified as a PERSON, "Acme Corp" as an ORG
3. **Relationship Mapping**: Sarah → WORKS_AT → Acme Corp
4. **Vector Storage**: Facts embedded and stored for semantic search
5. **Recall**: Your query found the relevant memories

## Next Steps

- [Installation Guide](installation.md) - All installation options
- [Docker Deployment](docker.md) - Production Docker setup
- [Python SDK Guide](../guides/python-sdk.md) - Full SDK reference
- [Entity Resolution](../guides/entity-resolution.md) - How entity matching works

## Example: Building a Chatbot

```python
from remembra import Memory
import openai

memory = Memory(base_url="http://localhost:8787", user_id="user_123")

def chat(user_message: str) -> str:
    # Recall relevant context
    context = memory.recall(user_message, limit=5)
    
    # Build prompt with memory
    messages = [
        {"role": "system", "content": f"You are a helpful assistant. Context: {context}"},
        {"role": "user", "content": user_message}
    ]
    
    # Get response
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=messages
    )
    
    assistant_message = response.choices[0].message.content
    
    # Store the conversation
    memory.store(f"User: {user_message}\nAssistant: {assistant_message}")
    
    return assistant_message

# Chat with memory!
print(chat("My name is Alex and I love hiking"))
print(chat("What do you know about me?"))  # Remembers Alex loves hiking!
```

!!! tip "Pro Tip"
    Store important facts explicitly, not just conversation history. The extraction model works best with clear statements.
