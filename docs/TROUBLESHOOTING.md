# Troubleshooting Guide

This guide helps you diagnose and fix common issues with Remembra. Each section includes:
- **What you'll see** (symptoms)
- **Why it happens** (root cause)
- **How to check** (diagnosis steps)
- **How to fix it** (solution)

---

## Table of Contents

1. [Recall Returns Empty Results](#1-recall-returns-empty-results)
2. [Entity Graph Not Loading](#2-entity-graph-not-loading)
3. [Dashboard Shows Data but Search Doesn't Work](#3-dashboard-shows-data-but-search-doesnt-work)
4. [Qdrant Connection Issues](#4-qdrant-connection-issues)
5. [Authentication Errors](#5-authentication-errors)
6. [Rate Limiting Issues](#6-rate-limiting-issues)

---

## 1. Recall Returns Empty Results

### What You'll See
When you call the recall endpoint, you get empty results even though you know memories exist:

```json
{
  "context": "",
  "memories": [],
  "entities": []
}
```

But the Timeline shows memories, and the Analytics show a count.

### Why It Happens
Remembra stores data in two places:
1. **SQLite** - Stores memory text, metadata, timestamps
2. **Qdrant** - Stores vector embeddings for semantic search

If memories are in SQLite but NOT in Qdrant, Timeline works (reads from SQLite) but Recall fails (needs Qdrant vectors).

This happens when:
- Qdrant was down during memory storage
- The embedding API failed but the store continued
- Memories were imported/migrated without re-vectorization
- Network timeout during the Qdrant write

### How to Check

**Step 1: Verify memories exist in database**
```bash
# Set your API URL and get a token
API="https://your-remembra-instance.com/api/v1"
TOKEN=$(curl -s -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"your@email.com","password":"yourpassword"}' | jq -r '.access_token')

# Check timeline (reads from SQLite)
curl -s "$API/debug/timeline?page=1&page_size=5" \
  -H "Authorization: Bearer $TOKEN" | jq '.total'
# Expected: A number like 44 (your memory count)
```

**Step 2: Verify recall doesn't find them**
```bash
# Try to recall a memory you know exists
curl -s -X POST "$API/memories/recall" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "a word from your memory", "limit": 10, "threshold": 0.1}' | jq '.memories | length'
# If this returns 0, vectors are missing
```

**Step 3: Test with a NEW memory**
```bash
# Store a test memory
curl -s -X POST "$API/memories" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "Test memory created at 11:50 AM for debugging"}' | jq '.id'
# Note the ID

# Wait 2 seconds, then try to recall it
sleep 2
curl -s -X POST "$API/memories/recall" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "debugging 11:50", "limit": 5, "threshold": 0.1}' | jq '.memories | length'
# If this returns 1, new memories work - only old ones are broken
```

### How to Fix It

**Option A: Use the Admin Rebuild Endpoint (Recommended)**

This requires superadmin access (your email must be in `REMEMBRA_OWNER_EMAILS`).

```bash
# First, do a dry run to see what's missing
curl -s -X POST "$API/admin/rebuild-vectors?dry_run=true" \
  -H "Authorization: Bearer $TOKEN" | jq '.'

# You'll see output like:
# {
#   "dry_run": true,
#   "total_memories_checked": 48,
#   "missing_from_qdrant": 44,
#   "rebuilt": 0,
#   "missing_memories": [...]
# }

# If missing_from_qdrant > 0, run for real:
curl -s -X POST "$API/admin/rebuild-vectors?dry_run=false" \
  -H "Authorization: Bearer $TOKEN" | jq '.'

# This will re-embed all missing memories
```

**Option B: Manual Re-store (If No Admin Access)**

For each memory you need to fix:
1. Get the memory content from Timeline
2. Delete the old memory
3. Store it again

```bash
# Get memory content
CONTENT=$(curl -s "$API/memories/YOUR_MEMORY_ID" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.content')

# Delete old memory
curl -s -X DELETE "$API/memories/YOUR_MEMORY_ID" \
  -H "Authorization: Bearer $TOKEN"

# Store again (will create proper vector)
curl -s -X POST "$API/memories" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"content\": \"$CONTENT\"}"
```

---

## 2. Entity Graph Not Loading

### What You'll See
- Graph visualization shows "Loading..." forever
- Browser console shows many failed API requests
- Graph eventually errors or times out
- Other dashboard features work fine

### Why It Happens
In versions before 0.7.2, the EntityGraph component made separate API calls for each entity to fetch relationships. With 50+ entities, this caused:
- **Rate limiting**: Too many requests per minute
- **Slow loading**: 50 sequential requests = 50x latency
- **Browser throttling**: Browsers limit concurrent requests

### How to Check
Open browser developer tools (F12) → Network tab → Reload the graph page.

**Bad (old version):**
- You'll see 50+ requests to `/api/v1/entities/*/relationships`
- Many may show 429 (rate limited) or timeout

**Good (fixed version):**
- You'll see ONE request to `/api/v1/debug/entities/graph`
- Returns all data in single response

### How to Fix It

**If using hosted dashboard:**
Update to v0.7.2 or later - the fix is included.

**If self-hosting the dashboard:**

Update `dashboard/src/components/EntityGraph.tsx`:

```typescript
// Change the fetchGraphData function from this:
const fetchGraphData = async () => {
  const entitiesResponse = await api.listEntities(projectId, undefined, 200);
  for (const entity of entitiesResponse.entities.slice(0, 50)) {
    const relResponse = await api.getEntityRelationships(entity.id);
    // ... process each one
  }
};

// To this:
const fetchGraphData = async () => {
  const graphData = await api.getEntityGraph(projectId || 'default');
  // graphData.nodes = all entities
  // graphData.edges = all relationships
  // Single request, all data
};
```

Then rebuild and redeploy the dashboard:
```bash
cd dashboard
npm run build
# Copy dist/ to your static hosting
```

---

## 3. Dashboard Shows Data but Search Doesn't Work

### What You'll See
- Timeline tab shows your memories ✓
- Analytics shows correct counts ✓
- Entity list shows extracted entities ✓
- BUT: Search/recall returns nothing ✗
- BUT: Graph shows no connections ✗

### Why It Happens
This is a "split-brain" state where SQLite and Qdrant are out of sync:
- SQLite has memory metadata (text, dates, entities)
- Qdrant is missing vector embeddings

Timeline and Analytics read from SQLite → they work.
Search and Graph need Qdrant vectors → they fail.

### How to Check

**Step 1: Check the health endpoint**
```bash
curl -s "https://your-instance.com/health" | jq '.'
```

Expected healthy response:
```json
{
  "status": "ok",
  "version": "0.7.1",
  "dependencies": {
    "qdrant": {
      "status": "ok"
    }
  }
}
```

If Qdrant shows "error", see [Section 4: Qdrant Connection Issues](#4-qdrant-connection-issues).

**Step 2: Check memory counts match**
```bash
# SQLite count (via timeline)
curl -s "$API/debug/timeline" -H "Authorization: Bearer $TOKEN" | jq '.total'

# Analytics count
curl -s "$API/debug/analytics" -H "Authorization: Bearer $TOKEN" | jq '.total_memories'

# These should match. If they do but recall is empty, vectors are missing.
```

### How to Fix It

See [Section 1: Recall Returns Empty Results](#1-recall-returns-empty-results) for the rebuild-vectors solution.

---

## 4. Qdrant Connection Issues

### What You'll See
- Health check shows: `"qdrant": { "status": "error" }`
- Store operations fail with 500 errors
- Application logs show connection errors like:
  - `Connection refused`
  - `Name or service not known`
  - `timeout`

### Why It Happens

**Cause 1: Wrong Qdrant URL**

Your `QDRANT_URL` environment variable points to the wrong place.

```bash
# Check your current setting
echo $QDRANT_URL

# Common correct values:
# Local development: http://localhost:6333
# Docker Compose: http://qdrant:6333 (service name)
# Remote: https://your-qdrant-server.com:6333
```

**Cause 2: Qdrant not running**

The Qdrant service isn't started.

```bash
# Check if running (Docker)
docker ps | grep qdrant

# Start if needed
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant:latest

# Or with Docker Compose
docker-compose up -d qdrant
```

**Cause 3: Docker network isolation**

If Remembra and Qdrant are in different Docker networks, they can't communicate.

```yaml
# docker-compose.yml - CORRECT
services:
  remembra:
    environment:
      - QDRANT_URL=http://qdrant:6333  # Use service name
    depends_on:
      - qdrant
    networks:
      - app-network
  
  qdrant:
    image: qdrant/qdrant:latest
    networks:
      - app-network

networks:
  app-network:
    driver: bridge
```

```yaml
# WRONG - Using localhost inside container
services:
  remembra:
    environment:
      - QDRANT_URL=http://localhost:6333  # Won't work!
```

**Cause 4: Firewall blocking port 6333**

```bash
# Test connectivity
curl -s http://your-qdrant-host:6333/collections | jq '.'

# If this fails, check firewall rules
```

### How to Fix It

**Step 1: Verify Qdrant is reachable**
```bash
# From the machine running Remembra
curl -s http://QDRANT_HOST:6333/collections | jq '.'

# Should return something like:
# { "result": { "collections": [...] }, "status": "ok" }
```

**Step 2: Check the collection exists**
```bash
curl -s http://QDRANT_HOST:6333/collections/remembra | jq '.result.status'
# Should return: "green"
```

**Step 3: Restart Remembra**

After fixing the connection, restart to reinitialize:
```bash
docker restart remembra
# or
docker-compose restart remembra
```

Check logs for successful startup:
```bash
docker logs remembra 2>&1 | grep -i "qdrant\|collection\|initialized"
```

---

## 5. Authentication Errors

### What You'll See
- 401 Unauthorized on API calls
- "Invalid token" or "Token expired" errors
- Can't log in even with correct password

### Common Causes & Fixes

**Expired Token**

JWT tokens expire (default: 7 days). Get a new one:
```bash
TOKEN=$(curl -s -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"your@email.com","password":"yourpassword"}' | jq -r '.access_token')
```

**Wrong API Key Format**

API keys must be sent in the header, not the URL:
```bash
# CORRECT
curl -H "Authorization: Bearer YOUR_API_KEY" ...

# WRONG
curl "$API/memories?api_key=YOUR_KEY" ...
```

**JWT Secret Changed**

If `REMEMBRA_JWT_SECRET` changed, all existing tokens are invalidated. Users must log in again.

---

## 6. Rate Limiting Issues

### What You'll See
- 429 Too Many Requests errors
- "Rate limit exceeded" messages
- Requests work sometimes but fail when frequent

### Default Limits
| Endpoint | Limit |
|----------|-------|
| Store memory | 30/minute |
| Recall | 60/minute |
| Entity graph | 15/minute |
| Admin endpoints | 5-30/minute |

### How to Fix It

**Option 1: Slow down requests**

Add delays between requests:
```bash
for i in {1..10}; do
  curl -X POST "$API/memories" -H "..." -d "..."
  sleep 2  # Wait 2 seconds between requests
done
```

**Option 2: Use batch endpoints**

Instead of storing memories one by one:
```bash
# Use batch store (up to 100 at once)
curl -X POST "$API/memories/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"memories": [{"content": "fact 1"}, {"content": "fact 2"}]}'
```

**Option 3: Upgrade plan**

Higher tiers have higher rate limits.

---

## Still Stuck?

1. **Check logs**: `docker logs remembra 2>&1 | tail -100`
2. **Enable debug mode**: Set `REMEMBRA_LOG_LEVEL=debug` and restart
3. **Join Discord**: https://discord.gg/Bzv3JshRa3
4. **Open an issue**: https://github.com/remembradev/remembra/issues

When reporting issues, include:
- Remembra version (`/health` endpoint shows it)
- Error messages from logs
- Steps to reproduce
- Your deployment method (Docker, bare metal, cloud)

---

*Last updated: March 6, 2026 | Remembra v0.7.2*
