# Installation

Ways to install and run Remembra.

## Quick Start (Recommended)

Get Remembra running with a single command. No API keys needed: this installs Remembra, Qdrant and Ollama via
Docker Compose, with auth off.

```bash
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
```

This sets up the Remembra server on port 8787, Qdrant for vector storage, and Ollama for local embeddings. LLM
fact and entity extraction stay off until you add an OpenAI key.

## Docker Compose (Zero Config)

If you already have Docker Compose and prefer to run it directly:

```bash
docker compose -f docker-compose.quickstart.yml up -d
```

This starts the same stack as the quick start script (Remembra + Qdrant + Ollama) with no API keys required.

## Docker

Run the Remembra container on its own. It needs an OpenAI key for embeddings and a Qdrant server, which the
image does not include.

```bash
docker run -d \
  --name remembra \
  -p 8787:8787 \
  -e REMEMBRA_OPENAI_API_KEY=sk-your-key \
  -e REMEMBRA_QDRANT_URL=http://your-qdrant:6333 \
  -e REMEMBRA_JWT_SECRET=$(openssl rand -hex 32) \
  -v remembra-data:/data \
  remembra/remembra
```

See [Docker Guide](docker.md) for production configuration.

## Python Package

### SDK + CLI Tools (Recommended)

Install Remembra with the MCP server and CLI tools:

```bash
pipx install --force 'remembra[mcp]>=0.16'
```

This includes:
- **`remembra-install`** — Configure the agents it supports with one command
- **`remembra-doctor`** — Diagnose connection issues
- **`remembra-bridge`** — Tunnel for sandboxed agents
- **`remembra-mcp`** — MCP server for Claude/Cursor (needs the `mcp` extra)
- **`remembra-relay`** — Session hooks for handoffs between agents

### Configure Your AI Agents

After installing, set up the agents it supports:

```bash
# Auto-detect and configure the supported agents (asks for the key, shows the changes, writes after a "y")
remembra-install --all

# Write the relay hooks, so handoffs are saved when a session ends
remembra-relay connect --apply

# Verify setup
remembra-doctor all
```

For a self-hosted server, add `--url <your server>` to `remembra-install`.

### Full Server

To run your own Remembra server:

```bash
pip install "remembra[server]"
```

Then start it (it needs a running Qdrant):

```bash
export REMEMBRA_OPENAI_API_KEY=sk-your-key
export REMEMBRA_QDRANT_URL=http://localhost:6333
export REMEMBRA_JWT_SECRET=$(openssl rand -hex 32)
remembra-server
```

Installed with pip, the server serves the API only. The dashboard comes with the Docker image.

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
remembra-server
```

## Dependencies

### Required

- **Python 3.11+**
- **Qdrant** - Vector database (a separate container in the compose files, or run it yourself)
- **Embedding provider** - One of:
    - **Ollama** (local, no API key needed) -- used automatically with the quick start
    - **OpenAI API key** -- for cloud-based embeddings and extraction

### Optional

- **Ollama** - Local embeddings, and entity extraction with a chat model (no API key needed)
- **Cohere** - Alternative embeddings
- **Anthropic** - For entity extraction via Claude
- **Voyage** - Alternative embeddings
- **Jina** - Alternative embeddings
- **Redis** - For rate limiting at scale

## Embedding Providers

Remembra supports multiple embedding providers:

=== "OpenAI (Default)"

    ```bash
    export REMEMBRA_OPENAI_API_KEY=sk-your-key
    export REMEMBRA_EMBEDDING_PROVIDER=openai
    export REMEMBRA_EMBEDDING_MODEL=text-embedding-3-small
    ```

=== "Ollama (Local)"

    ```bash
    # Start Ollama first
    ollama pull nomic-embed-text

    export REMEMBRA_EMBEDDING_PROVIDER=ollama
    export REMEMBRA_EMBEDDING_MODEL=nomic-embed-text
    export REMEMBRA_EMBEDDING_DIMENSIONS=768
    export REMEMBRA_OLLAMA_URL=http://localhost:11434
    ```

=== "Cohere"

    ```bash
    export REMEMBRA_COHERE_API_KEY=your-key
    export REMEMBRA_EMBEDDING_PROVIDER=cohere
    export REMEMBRA_EMBEDDING_MODEL=embed-english-v3.0
    ```

=== "Voyage"

    ```bash
    export REMEMBRA_VOYAGE_API_KEY=your-key
    export REMEMBRA_EMBEDDING_PROVIDER=voyage
    export REMEMBRA_EMBEDDING_MODEL=voyage-3
    ```

=== "Jina"

    ```bash
    export REMEMBRA_JINA_API_KEY=your-key
    export REMEMBRA_EMBEDDING_PROVIDER=jina
    export REMEMBRA_EMBEDDING_MODEL=jina-embeddings-v3
    ```

## LLM Providers (Entity Extraction)

`REMEMBRA_LLM_PROVIDER` picks the LLM for entity extraction only. Fact extraction, consolidation and entity
matching use OpenAI (`REMEMBRA_EXTRACTION_MODEL`). Supported providers:

=== "OpenAI (Default)"

    ```bash
    export REMEMBRA_LLM_PROVIDER=openai
    export REMEMBRA_OPENAI_API_KEY=sk-your-key
    ```

=== "Ollama (Local)"

    ```bash
    # No API key needed: runs locally with a chat model you have pulled
    export REMEMBRA_LLM_PROVIDER=ollama
    export REMEMBRA_LLM_MODEL=llama3.1
    export REMEMBRA_OLLAMA_URL=http://localhost:11434
    ```

=== "Anthropic"

    ```bash
    # Anthropic (for entity extraction); needs the anthropic extra
    export REMEMBRA_LLM_PROVIDER=anthropic
    export REMEMBRA_LLM_MODEL=claude-haiku-4-5
    export REMEMBRA_ANTHROPIC_API_KEY=your-key
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
  "version": "0.16.1",
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
    api_key="rem_...",  # when auth is on
)

# Store and recall
memory.store("Test memory")
result = memory.recall("test")
print(result.context)  # Should include the test memory
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

If using OpenAI, set your API key (a bare `OPENAI_API_KEY` is not read):

```bash
export REMEMBRA_OPENAI_API_KEY=sk-your-key
```

If you don't have an API key, use the zero-config quick start which uses Ollama locally and requires no API keys.

### Sandboxed agent can't connect

Some agents (Codex, Claude Code) run in sandboxes. Use the bridge:

```bash
# Start the bridge
read -rs REMEMBRA_API_KEY && export REMEMBRA_API_KEY   # paste the key: not shown, not in shell history
remembra-bridge --upstream https://api.remembra.dev   # listens on 127.0.0.1:9819

# Configure agents to use the bridge
remembra-install --all --url http://127.0.0.1:9819
```

### Qdrant connection issues

If running Qdrant separately, ensure it's accessible:

```bash
export REMEMBRA_QDRANT_URL=http://localhost:6333
```

## Next Steps

- [Docker Guide](docker.md) - Production deployment
- [Configuration Reference](../reference/configuration.md) - All environment variables
- [Python SDK](../guides/python-sdk.md) - Full SDK documentation
