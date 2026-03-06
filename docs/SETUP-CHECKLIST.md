# Setup Verification Checklist

Use this checklist to verify your Remembra installation is working correctly. Run through each step after deployment.

---

## Prerequisites

Before starting, you need:
- [ ] Remembra running (Docker or bare metal)
- [ ] Access to the API (usually port 8787)
- [ ] `curl` and `jq` installed for testing
- [ ] Your admin email and password

Set these variables for the tests below:
```bash
export API="https://your-remembra-instance.com/api/v1"
export EMAIL="your@email.com"
export PASSWORD="yourpassword"
```

---

## 1. Health Check ✓

**What we're testing:** Basic connectivity and dependencies

```bash
curl -s "${API%/v1}/health" | jq '.'
```

**Expected result:**
```json
{
  "status": "ok",
  "version": "0.7.x",
  "dependencies": {
    "qdrant": { "status": "ok" }
  }
}
```

**If it fails:**
- `Connection refused` → Remembra not running or wrong URL
- `qdrant: error` → See [Qdrant Connection Issues](TROUBLESHOOTING.md#4-qdrant-connection-issues)

---

## 2. Authentication ✓

**What we're testing:** User login and JWT token generation

```bash
# Get auth token
TOKEN=$(curl -s -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r '.access_token')

echo "Token: ${TOKEN:0:20}..."  # Should show start of token
```

**Expected result:** A JWT token starting with `eyJ...`

**If it fails:**
- `Invalid credentials` → Check email/password
- `null` token → Check if user exists, try signup first

---

## 3. Store Memory ✓

**What we're testing:** Memory storage pipeline (SQLite + Qdrant)

```bash
STORE_RESULT=$(curl -s -X POST "$API/memories" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "Setup test: The quick brown fox jumps over the lazy dog."}')

echo "$STORE_RESULT" | jq '.'
MEMORY_ID=$(echo "$STORE_RESULT" | jq -r '.id')
echo "Memory ID: $MEMORY_ID"
```

**Expected result:**
```json
{
  "id": "uuid-here",
  "extracted_facts": ["The quick brown fox jumps over the lazy dog."],
  "entities": []
}
```

**If it fails:**
- `500 error` → Check logs for Qdrant/embedding issues
- `401 Unauthorized` → Token expired or invalid

---

## 4. Recall Memory ✓

**What we're testing:** Vector search in Qdrant

```bash
# Wait for indexing
sleep 2

curl -s -X POST "$API/memories/recall" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "quick brown fox", "limit": 5, "threshold": 0.3}' | jq '.'
```

**Expected result:**
```json
{
  "context": "[date] (XX%) The quick brown fox...",
  "memories": [
    {
      "id": "your-memory-id",
      "relevance": 0.85,
      "content": "The quick brown fox jumps over the lazy dog.",
      "created_at": "..."
    }
  ],
  "entities": []
}
```

**If it fails:**
- Empty memories array → Qdrant vectors missing (see [Troubleshooting #1](TROUBLESHOOTING.md#1-recall-returns-empty-results))
- Low relevance scores → Embedding model may differ from search

---

## 5. Timeline ✓

**What we're testing:** SQLite storage and timeline API

```bash
curl -s "$API/debug/timeline?page=1&page_size=5" \
  -H "Authorization: Bearer $TOKEN" | jq '{total, first_memory: .memories[0].content}'
```

**Expected result:**
```json
{
  "total": 1,
  "first_memory": "The quick brown fox jumps over the lazy dog."
}
```

---

## 6. Entity Extraction ✓

**What we're testing:** NLP entity extraction pipeline

```bash
# Store a memory with entities
curl -s -X POST "$API/memories" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "John Smith works at Google in San Francisco."}' | jq '.'

# Check entities were extracted
sleep 2
curl -s "$API/entities?limit=10" \
  -H "Authorization: Bearer $TOKEN" | jq '.entities[] | {name: .canonical_name, type}'
```

**Expected result:**
```json
{"name": "John Smith", "type": "person"}
{"name": "Google", "type": "company"}
{"name": "San Francisco", "type": "location"}
```

---

## 7. Entity Graph ✓

**What we're testing:** Graph visualization endpoint

```bash
curl -s "$API/debug/entities/graph" \
  -H "Authorization: Bearer $TOKEN" | jq '.stats'
```

**Expected result:**
```json
{
  "total_nodes": 3,
  "total_edges": 2,
  "entity_types": {
    "person": 1,
    "company": 1,
    "location": 1
  }
}
```

---

## 8. Analytics ✓

**What we're testing:** Usage metrics and analytics

```bash
curl -s "$API/debug/analytics" \
  -H "Authorization: Bearer $TOKEN" | jq '{memories: .total_memories, entities: .total_entities, stores_today}'
```

**Expected result:**
```json
{
  "memories": 2,
  "entities": 3,
  "stores_today": 2
}
```

---

## 9. API Keys ✓

**What we're testing:** API key management

```bash
# Create an API key
curl -s -X POST "$API/keys" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "test-key", "permission": "editor"}' | jq '{id, key: .key[0:20]}'

# List keys
curl -s "$API/keys" \
  -H "Authorization: Bearer $TOKEN" | jq '.keys | length'
```

---

## 10. Cleanup Test Data ✓

**What we're testing:** Memory deletion

```bash
# Delete the test memory
curl -s -X DELETE "$API/memories/$MEMORY_ID" \
  -H "Authorization: Bearer $TOKEN"

echo "Test memory deleted"
```

---

## All Checks Passed? 🎉

Your Remembra installation is working correctly! You can now:

1. **Integrate with your app** - Use the API keys you created
2. **Set up Clawdbot** - Connect your AI assistant
3. **Configure webhooks** - Get notified on memory events
4. **Invite team members** - Multi-user support is ready

## Some Checks Failed?

See the [Troubleshooting Guide](TROUBLESHOOTING.md) for detailed solutions to common issues.

---

*Checklist version: 1.0 | March 6, 2026*
