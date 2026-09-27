# Docker Deployment

Run Remembra with Docker.

## Zero-Config Quick Start

The fastest way to try Remembra, with no API keys. It uses Ollama for local embeddings.

**One-line install:**

```bash
curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash
```

This pulls and starts [`docker-compose.quickstart.yml`](https://github.com/remembra-ai/remembra/blob/main/docker-compose.quickstart.yml), which runs 3 services:

| Service | Port | Purpose |
|---------|------|---------|
| **qdrant** | `6333` | Vector database for semantic search |
| **ollama** | `11434` | Local embeddings (no API key needed) |
| **remembra** | `8787` | Memory server, with auth and rate limiting off |

Once running, connect your MCP client to `http://localhost:8787` and start storing memories.

> **Note:** LLM fact and entity extraction are off in the quickstart until you add an OpenAI key
> (`REMEMBRA_OPENAI_API_KEY`), or set `REMEMBRA_LLM_PROVIDER=ollama` and pull a chat model for entity extraction.
> Auth is off, so do not expose it to a network. For production use, see the standard or production compose
> files below.

### Docker Compose Files

Remembra ships with 4 compose files:

| File | Use Case | Description |
|------|----------|-------------|
| `docker-compose.quickstart.yml` | Learning / Evaluation | Zero-config setup with Ollama, no API keys, auth off |
| `docker-compose.yml` | Standard | Remembra and Qdrant; configurable providers, auth on |
| `docker-compose.prod.yml` | Production | Auth on, health checks, reranking off, requires `REMEMBRA_OPENAI_API_KEY` |
| `docker-compose.mcp.yml` | Remote MCP | Runs `remembra-mcp` as a streamable-HTTP MCP server on port 8765 |

## Docker Compose

The image does not include Qdrant, so run one next to it. The repository's `docker-compose.yml` does this. A
minimal file:

```yaml title="docker-compose.yml"
services:
  remembra:
    image: remembra/remembra:latest
    ports:
      - "8787:8787"
    environment:
      # Settings use the REMEMBRA_ prefix; a bare OPENAI_API_KEY is ignored
      - REMEMBRA_OPENAI_API_KEY=${REMEMBRA_OPENAI_API_KEY}

      # Database: the image keeps SQLite at /data/remembra.db
      - REMEMBRA_DATABASE_URL=sqlite:////data/remembra.db

      # Qdrant (a separate container)
      - REMEMBRA_QDRANT_URL=http://qdrant:6333

      # Security
      - REMEMBRA_AUTH_ENABLED=true
      - REMEMBRA_AUTH_MASTER_KEY=${REMEMBRA_AUTH_MASTER_KEY}
      - REMEMBRA_JWT_SECRET=${REMEMBRA_JWT_SECRET}
      - REMEMBRA_RATE_LIMIT_ENABLED=true
    volumes:
      - remembra-data:/data
    depends_on:
      - qdrant

  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant-data:/qdrant/storage

volumes:
  remembra-data:
  qdrant-data:
```

The image has its own health check (`GET /health`).

Start with:

```bash
# Create .env file
echo "REMEMBRA_OPENAI_API_KEY=sk-your-key" > .env
echo "REMEMBRA_AUTH_MASTER_KEY=$(openssl rand -hex 32)" >> .env
echo "REMEMBRA_JWT_SECRET=$(openssl rand -hex 32)" >> .env

# Start services
docker compose up -d
```

## Environment Variables

### Required (with the default OpenAI provider)

| Variable | Description | Example |
|----------|-------------|---------|
| `REMEMBRA_OPENAI_API_KEY` | OpenAI API key for embeddings and extraction | `sk-...` |

### Providers

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | Embedding provider (`openai`, `azure_openai`, `ollama`, `cohere`, `voyage`, `jina`) |
| `REMEMBRA_LLM_PROVIDER` | `openai` | Backend for entity extraction only (`openai`, `anthropic`, `ollama`) |
| `REMEMBRA_LLM_MODEL` | `gpt-4o-mini` | Entity-extraction model, used only when `REMEMBRA_EXTRACTION_MODEL` does not fit the provider (e.g. `claude-haiku-4-5` with `anthropic`) |
| `REMEMBRA_ANTHROPIC_API_KEY` | - | API key for Anthropic entity extraction |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DATABASE_URL` | `sqlite:////data/remembra.db` in the image | SQLite database |
| `REMEMBRA_QDRANT_URL` | `http://localhost:6333` in the image | Qdrant server |
| `REMEMBRA_QDRANT_API_KEY` | - | Qdrant API key (if secured) |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTH_ENABLED` | `true` | Enable API key auth |
| `REMEMBRA_AUTH_MASTER_KEY` | - | Master key (creates keys, including admin keys) |
| `REMEMBRA_JWT_SECRET` | - | Secret for dashboard sign-in tokens (JWTs). The server does not start without a unique value of 32+ characters (unless `REMEMBRA_DEBUG=true`) |
| `REMEMBRA_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |

### Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EXTRACTION_MODEL` | `gpt-4o-mini` | OpenAI model for fact extraction, consolidation, entity matching and conversation ingest (must be an OpenAI model) |
| `REMEMBRA_SMART_EXTRACTION_ENABLED` | `true` | Enable LLM extraction |

### Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_ENABLE_HYBRID_SEARCH` | `true` | Enable hybrid search |
| `REMEMBRA_ENABLE_RERANKING` | `true` | CrossEncoder reranking. It runs only when the image was built with the `rerank` extra (the default image is not); `docker-compose.yml` and `docker-compose.prod.yml` set it to `false` |
| `REMEMBRA_CONTEXT_MAX_TOKENS` | `4000` | Max context tokens |

See [Configuration Reference](../reference/configuration.md) for all options.

## Production Checklist

- [ ] Set `REMEMBRA_AUTH_ENABLED=true`
- [ ] Generate strong `REMEMBRA_AUTH_MASTER_KEY` and `REMEMBRA_JWT_SECRET`
- [ ] Enable rate limiting
- [ ] Use persistent volumes for data
- [ ] Set up health checks
- [ ] Configure reverse proxy (nginx/traefik) with HTTPS
- [ ] Set up backups for volumes

## Reverse Proxy (HTTPS)

Example nginx configuration:

```nginx
server {
    listen 443 ssl http2;
    server_name memory.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Resource Requirements

| Workload | CPU | Memory | Storage |
|----------|-----|--------|---------|
| Development | 1 core | 1GB | 1GB |
| Small (< 10k memories) | 2 cores | 2GB | 5GB |
| Medium (10k-100k) | 4 cores | 4GB | 20GB |
| Large (100k+) | 8+ cores | 8GB+ | 50GB+ |

## Scaling

### Horizontal Scaling

For high availability, run multiple Remembra instances behind a load balancer:

```yaml
services:
  remembra:
    deploy:
      replicas: 3
```

Note: Use Redis for rate limiting when scaling horizontally (it needs the `redis` package from the `cloud`
extra):

```bash
REMEMBRA_RATE_LIMIT_STORAGE=redis://redis:6379
```

Real-time (WebSocket) connections are closed at once only by the process that handled a key revocation or
sign-out. Connections held by the other processes get no further events and close within 30 seconds.

### Qdrant Clustering

For large deployments, use Qdrant's distributed mode. See [Qdrant documentation](https://qdrant.tech/documentation/guides/distributed_deployment/).

## Backup & Restore

### Backup

```bash
# Stop containers (optional, for consistent backup)
docker-compose stop

# Backup volumes
docker run --rm \
  -v remembra-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/remembra-backup.tar.gz /data

docker run --rm \
  -v qdrant-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/qdrant-backup.tar.gz /data

# Restart
docker-compose start
```

### Restore

```bash
docker run --rm \
  -v remembra-data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/remembra-backup.tar.gz -C /

docker run --rm \
  -v qdrant-data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/qdrant-backup.tar.gz -C /
```

## Troubleshooting

### Container won't start

```bash
docker logs remembra
```

Common issues:
- Missing `REMEMBRA_OPENAI_API_KEY` (a bare `OPENAI_API_KEY` is ignored)
- Missing or short `REMEMBRA_JWT_SECRET`
- Port 8787 already in use
- Insufficient permissions for volume mount

### Qdrant connection failed

Ensure Qdrant is running and accessible:

```bash
docker-compose ps
curl http://localhost:6333/health
```

### Out of memory

Increase Docker memory limit or reduce `REMEMBRA_CONTEXT_MAX_TOKENS`.
