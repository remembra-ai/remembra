# Installation

Multiple ways to install and run Remembra.

## Docker (Recommended)

The easiest way to get started. Everything is bundled.

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

### SDK Only (Client)

If you just need the client SDK to connect to an existing Remembra server:

```bash
pip install remembra
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
git clone https://github.com/remembra/remembra
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
- **OpenAI API key** - For embeddings and extraction

### Optional

- **Ollama** - Local embeddings (no API costs)
- **Cohere** - Alternative embeddings
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

## Verifying Installation

### Check Server Health

```bash
curl http://localhost:8787/health
```

Expected response:

```json
{
  "status": "healthy",
  "version": "0.6.3",
  "qdrant": "connected",
  "database": "connected"
}
```

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

### "Connection refused" error

Make sure the server is running:

```bash
docker ps  # Check if container is running
docker logs remembra  # Check for errors
```

### "API key not set" error

Set your OpenAI API key:

```bash
export OPENAI_API_KEY=sk-your-key
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
