# Docker Deployment

Production-ready Docker deployment for Remembra.

## Quick Start

```bash
docker run -d \
  --name remembra \
  -p 8787:8787 \
  -e OPENAI_API_KEY=sk-your-key \
  -v remembra-data:/app/data \
  remembra/remembra
```

## Zero-Config Quick Start

The fastest way to try Remembra — no API keys required. This uses Ollama for local embeddings and entity extraction.

**One-line install:**

```bash
curl -sSL https://get.remembra.dev/quickstart.sh | bash
```

This pulls and starts [`docker-compose.quickstart.yml`](https://github.com/remembra-ai/remembra/blob/main/docker-compose.quickstart.yml), which runs 3 services:

| Service | Port | Purpose |
|---------|------|---------|
| **qdrant** | `6333` | Vector database for semantic search |
| **ollama** | `11434` | Local embeddings and LLM (no API key needed) |
| **remembra** | `8787` | Memory server |

Once running, connect your MCP client to `http://localhost:8787` and start storing memories immediately.

> **Note:** The quickstart configuration uses Ollama for both embeddings and entity extraction, so no external API keys are required. For production use, see the standard or production compose files below.

### Docker Compose Profiles

Remembra ships with 3 compose files for different use cases:

| File | Use Case | Description |
|------|----------|-------------|
| `docker-compose.quickstart.yml` | Learning / Evaluation | Zero-config setup with Ollama — no API keys needed |
| `docker-compose.yml` | Standard Development | Configurable providers, external API keys supported |
| `docker-compose.prod.yml` | Production | Hardened security, auth enabled, rate limiting, health checks |

## Docker Compose (Recommended)

For production, use Docker Compose with persistent storage:

```yaml title="docker-compose.yml"
version: '3.8'

services:
  remembra:
    image: remembra/remembra:latest
    ports:
      - "8787:8787"
    environment:
      # Required
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      
      # Database
      - REMEMBRA_DATABASE_PATH=/app/data/remembra.db
      
      # Qdrant (built-in)
      - QDRANT_HOST=qdrant
      - QDRANT_PORT=6333
      
      # Security (enable in production!)
      - REMEMBRA_AUTH_ENABLED=true
      - REMEMBRA_AUTH_MASTER_KEY=${REMEMBRA_MASTER_KEY}
      
      # Performance
      - REMEMBRA_RATE_LIMIT_ENABLED=true
    volumes:
      - remembra-data:/app/data
    depends_on:
      - qdrant
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8787/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant-data:/qdrant/storage
    ports:
      - "6333:6333"

volumes:
  remembra-data:
  qdrant-data:
```

Start with:

```bash
# Create .env file
echo "OPENAI_API_KEY=sk-your-key" > .env
echo "REMEMBRA_MASTER_KEY=$(openssl rand -hex 32)" >> .env

# Start services
docker-compose up -d
```

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key for embeddings | `sk-...` |

### Providers

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EMBEDDING_PROVIDER` | `openai` | Embedding provider (`openai`, `ollama`, `cohere`, `voyage`, `jina`, `azure`) |
| `REMEMBRA_LLM_PROVIDER` | `openai` | LLM for entity extraction (`openai`, `anthropic`, `ollama`) |
| `ANTHROPIC_API_KEY` | - | API key for Anthropic entity extraction |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_DATABASE_PATH` | `./remembra.db` | SQLite database path |
| `QDRANT_HOST` | `localhost` | Qdrant host |
| `QDRANT_PORT` | `6333` | Qdrant port |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_AUTH_ENABLED` | `true` | Enable API key auth |
| `REMEMBRA_AUTH_MASTER_KEY` | - | Master admin key |
| `REMEMBRA_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |

### Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_EXTRACTION_MODEL` | `gpt-4o-mini` | Model for fact extraction |
| `REMEMBRA_SMART_EXTRACTION_ENABLED` | `true` | Enable LLM extraction |

### Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `REMEMBRA_HYBRID_SEARCH_ENABLED` | `true` | Enable hybrid search |
| `REMEMBRA_RERANK_ENABLED` | `false` | Enable CrossEncoder reranking |
| `REMEMBRA_DEFAULT_MAX_TOKENS` | `4000` | Max context tokens |

See [Configuration Reference](../reference/configuration.md) for all options.

## Production Checklist

- [ ] Set `REMEMBRA_AUTH_ENABLED=true`
- [ ] Generate strong `REMEMBRA_AUTH_MASTER_KEY`
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

Note: Use Redis for rate limiting when scaling horizontally:

```bash
REMEMBRA_RATE_LIMIT_STORAGE=redis://redis:6379
```

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
- Missing `OPENAI_API_KEY`
- Port 8787 already in use
- Insufficient permissions for volume mount

### Qdrant connection failed

Ensure Qdrant is running and accessible:

```bash
docker-compose ps
curl http://localhost:6333/health
```

### Out of memory

Increase Docker memory limit or reduce `REMEMBRA_DEFAULT_MAX_TOKENS`.
