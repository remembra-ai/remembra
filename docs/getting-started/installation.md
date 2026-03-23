# Installation

Multiple ways to install and run Remembra (v0.12.0).

## Quick Start (Recommended)

Get Remembra running with a single command. No API keys needed -- this installs Remembra, Qdrant, and Ollama via Docker Compose for a fully local setup.

```bash
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
```

This sets up everything automatically: Remembra server on port 8787, Qdrant for vector storage, and Ollama for local embeddings and entity extraction.

## Docker Compose (Zero Config)

If you already have Docker Compose and prefer to run it directly:

```bash
docker compose -f docker-compose.quickstart.yml up -d
```

This starts the same stack as the quick start script (Remembra + Qdrant + Ollama) with no API keys required.

## Docker

Run the Remembra container standalone. Requires an API key for embeddings.

```bash
docker run -d \
  --name remembra \
  -p 8787:8787 \
  -e OPENAI_API_KEY=sk-your-key \
  -v remembra-data:/app/data \
  remembra/remembra
```

See [Docker Guide](docker.md) for production configuration.

## Python Package

### SDK + CLI Tools (Recommended)

Install Remembra with all CLI tools:

```bash
pip install remembra
```

This includes:
- **`remembra-install`** — Configure all your AI agents with one command
- **`remembra-doctor`** — Diagnose connection issues
- **`remembra-bridge`** — Tunnel for sandboxed agents
- **`remembra-mcp`** — MCP server for Claude/Cursor

### Configure Your AI Agents

After installing, set up all your AI tools:

```bash
# Auto-detect and configure all agents
remembra-install --all --api-key YOUR_API_KEY

# Verify setup
remembra-doctor all
```

### Full Server

To run your own Remembra server:

```bash
pip install "remembra[server]"
```

Then start it:

```bash
export OPENAI_API_KEY=sk-your-key
python -m remembra.server
```

### With Reranking (Optional)

For better recall quality with CrossEncoder reranking:

```bash
pip install "remembra[server,rerank]"
```

## From Source

For development or customization:

```bash
# Clone the repo
git clone https://github.com/remembra-ai/remembra
cd remembra

# Install with uv (recommended)
uv sync --all-extras

# Or with pip
pip install -e ".[server,rerank,dev]"

# Run tests
pytest

# Start the server
python -m remembra.server
```

## Dependencies

### Required

- **Python 3.10+**
- **Qdrant** - Vector database (bundled in Docker, or run separately)
- **Embedding provider** - One of:
    - **Ollama** (local, no API key needed) -- used automatically with the quick start
    - **OpenAI API key** -- for cloud-based embeddings and extraction

### Optional

- **Ollama** - Local embeddings and extraction (no API costs, no API key needed)
- **Cohere** - Alternative embeddings
- **Anthropic** - For entity extraction via Claude
- **Voyage** - Alternative embeddings
- **Jina** - Alternative embeddings
- **Redis** - For rate limiting at scale

## Embedding Providers

Remembra supports multiple embedding providers:

=== "OpenAI (Default)"

    ```bash
    export OPENAI_API_KEY=sk-your-key
    export REMEMBRA_EMBEDDING_PROVIDER=openai
    export REMEMBRA_EMBEDDING_MODEL=text-embedding-3-small
    ```

=== "Ollama (Local)"

    ```bash
    # Start Ollama first
    ollama pull nomic-embed-text

    export REMEMBRA_EMBEDDING_PROVIDER=ollama
    export REMEMBRA_EMBEDDING_MODEL=nomic-embed-text
    export OLLAMA_BASE_URL=http://localhost:11434
    ```

=== "Cohere"

    ```bash
    export COHERE_API_KEY=your-key
    export REMEMBRA_EMBEDDING_PROVIDER=cohere
    export REMEMBRA_EMBEDDING_MODEL=embed-english-v3.0
    ```

=== "Voyage"

    ```bash
    export VOYAGE_API_KEY=your-key
    export REMEMBRA_EMBEDDING_PROVIDER=voyage
    export REMEMBRA_EMBEDDING_MODEL=voyage-3
    ```

=== "Jina"

    ```bash
    export JINA_API_KEY=your-key
    export REMEMBRA_EMBEDDING_PROVIDER=jina
    export REMEMBRA_EMBEDDING_MODEL=jina-embeddings-v3
    ```

## LLM Providers (Entity Extraction)

Remembra uses an LLM for entity extraction. Supported providers:

=== "OpenAI (Default)"

    ```bash
    export REMEMBRA_LLM_PROVIDER=openai
    export OPENAI_API_KEY=sk-your-key
    ```

=== "Ollama (Local)"

    ```bash
    # No API key needed -- runs locally
    export REMEMBRA_LLM_PROVIDER=ollama
    export OLLAMA_BASE_URL=http://localhost:11434
    ```

=== "Anthropic"

    ```bash
    # Anthropic (for entity extraction)
    export REMEMBRA_LLM_PROVIDER=anthropic
    export ANTHROPIC_API_KEY=your-key
    ```

## Verifying Installation

### Check Server Health

```bash
curl http://localhost:8787/health
```

Expected response:

```json
{
  "status": "ok",
  "version": "0.12.0",
  "dependencies": {
    "qdrant": {"status": "ok"}
  }
}
```

### Verify Agent Setup

```bash
remembra-doctor all
```

This checks all configured agents and reports any issues.

### Test the SDK

```python
from remembra import Memory

memory = Memory(
    base_url="http://localhost:8787",
    user_id="test"
)

# Store and recall
memory.store("Test memory")
result = memory.recall("test")
print(result)  # Should return the test memory
```

## Troubleshooting

### Run Diagnostics First

```bash
remembra-doctor all
```

This identifies most common issues automatically.

### "Connection refused" error

Make sure the server is running:

```bash
docker ps  # Check if container is running
docker logs remembra  # Check for errors
```

### "API key not set" error

If using OpenAI, set your API key:

```bash
export OPENAI_API_KEY=sk-your-key
```

If you don't have an API key, use the zero-config quick start which uses Ollama locally and requires no API keys.

### Sandboxed agent can't connect

Some agents (Codex, Claude Code) run in sandboxes. Use the bridge:

```bash
# Start the bridge
remembra-bridge --url https://api.remembra.dev --api-key YOUR_KEY

# Configure agents to use the bridge
remembra-install --all --url http://localhost:8766
```

### Qdrant connection issues

If running Qdrant separately, ensure it's accessible:

```bash
export QDRANT_HOST=localhost
export QDRANT_PORT=6333
```

## Next Steps

- [Docker Guide](docker.md) - Production deployment
- [Configuration Reference](../reference/configuration.md) - All environment variables
- [Python SDK](../guides/python-sdk.md) - Full SDK documentation
