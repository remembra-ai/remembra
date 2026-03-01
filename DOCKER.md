# Docker Deployment Guide

Self-host Remembra in 5 minutes with Docker.

## Quick Start

```bash
# Clone the repo
git clone https://github.com/remembra/remembra.git
cd remembra

# Start all services
docker compose up -d

# Check status
docker compose ps
```

**Access:**
- API & Dashboard: http://localhost:8787
- API Docs: http://localhost:8787/docs

## Configuration

Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
```

### Essential Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `REMEMBRA_PORT` | API port | 8787 |
| `OPENAI_API_KEY` | For better fact extraction | - |
| `REMEMBRA_REQUIRE_API_KEY` | Require auth | true |

### Using OpenAI for Embeddings

For better semantic search, use OpenAI embeddings:

```env
OPENAI_API_KEY=sk-...
REMEMBRA_EMBEDDING_MODEL=text-embedding-3-small
```

### Using Local Embeddings (Free)

Default uses local sentence-transformers (no API key needed):

```env
REMEMBRA_EMBEDDING_MODEL=all-MiniLM-L6-v2
```

## First API Key

Create your first API key:

```bash
# Connect to container
docker exec -it remembra python -c "
from remembra.auth.keys import APIKeyManager
import asyncio
async def main():
    mgr = APIKeyManager('sqlite:////data/remembra.db')
    await mgr.init()
    key = await mgr.create_key('admin', 'Admin Key')
    print(f'Your API Key: {key}')
asyncio.run(main())
"
```

Save this key - you'll need it for all API requests.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Network                        │
│                                                         │
│  ┌─────────────────┐      ┌─────────────────┐          │
│  │    Remembra     │      │     Qdrant      │          │
│  │   (API + UI)    │─────▶│  (Vector DB)    │          │
│  │   Port 8787     │      │   Port 6333     │          │
│  └────────┬────────┘      └─────────────────┘          │
│           │                                             │
│           ▼                                             │
│  ┌─────────────────┐                                   │
│  │    SQLite       │                                   │
│  │  (Metadata)     │                                   │
│  │  /data/remembra │                                   │
│  └─────────────────┘                                   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## Data Persistence

Data is stored in Docker volumes:
- `remembra_data` - SQLite database, API keys, audit logs
- `remembra_qdrant_data` - Vector embeddings

To backup:

```bash
# Stop services
docker compose down

# Backup volumes
docker run --rm -v remembra_data:/data -v $(pwd):/backup alpine \
  tar cvf /backup/remembra_backup.tar /data

docker run --rm -v remembra_qdrant_data:/data -v $(pwd):/backup alpine \
  tar cvf /backup/qdrant_backup.tar /data
```

## Production Deployment

### With Traefik (HTTPS)

Add to `docker-compose.yml`:

```yaml
services:
  remembra:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.remembra.rule=Host(`memory.yourdomain.com`)"
      - "traefik.http.routers.remembra.tls.certresolver=letsencrypt"
```

### Resource Limits

```yaml
services:
  remembra:
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '2'
```

## Troubleshooting

### Check logs

```bash
docker compose logs remembra
docker compose logs qdrant
```

### Health check

```bash
curl http://localhost:8787/health
```

### Restart services

```bash
docker compose restart
```

### Reset everything

```bash
docker compose down -v  # ⚠️ Deletes all data
docker compose up -d
```

## Upgrading

```bash
# Pull latest images
docker compose pull

# Restart with new version
docker compose up -d

# Check version
curl http://localhost:8787/health
```
