# Import & Export

Remembra supports importing memories from various sources and exporting in multiple formats.

## Import Sources

### ChatGPT Conversations

Import your ChatGPT conversation history:

```bash
# Export from ChatGPT: Settings → Data Controls → Export Data
# You'll receive a ZIP file with conversations.json

curl -X POST http://localhost:8787/api/v1/transfer/import \
  -H "X-API-Key: your_api_key" \
  -F "file=@conversations.json" \
  -F "source=chatgpt" \
  -F "user_id=user_123"
```

**Python SDK:**
```python
from remembra import Memory

memory = Memory(base_url="http://localhost:8787", user_id="user_123")

# Import ChatGPT export
result = memory.import_from("chatgpt", "path/to/conversations.json")
print(f"Imported {result.count} memories")
```

### Claude Conversations

Import Claude conversation exports:

```bash
curl -X POST http://localhost:8787/api/v1/transfer/import \
  -H "X-API-Key: your_api_key" \
  -F "file=@claude_conversations.json" \
  -F "source=claude" \
  -F "user_id=user_123"
```

### Plain Text

Import from plain text files (one memory per paragraph or line):

```bash
curl -X POST http://localhost:8787/api/v1/transfer/import \
  -H "X-API-Key: your_api_key" \
  -F "file=@notes.txt" \
  -F "source=plaintext" \
  -F "user_id=user_123" \
  -F "delimiter=paragraph"  # or "line"
```

### JSON Format

Import structured JSON data:

```json
[
  {
    "content": "User prefers dark mode",
    "metadata": {"source": "settings"},
    "created_at": "2026-01-15T10:00:00Z"
  },
  {
    "content": "User works at Acme Corp",
    "metadata": {"source": "profile"}
  }
]
```

```bash
curl -X POST http://localhost:8787/api/v1/transfer/import \
  -H "X-API-Key: your_api_key" \
  -F "file=@memories.json" \
  -F "source=json" \
  -F "user_id=user_123"
```

### JSONL (JSON Lines)

For large imports, use streaming JSONL format:

```jsonl
{"content": "Memory 1", "metadata": {}}
{"content": "Memory 2", "metadata": {}}
{"content": "Memory 3", "metadata": {}}
```

```bash
curl -X POST http://localhost:8787/api/v1/transfer/import \
  -H "X-API-Key: your_api_key" \
  -F "file=@memories.jsonl" \
  -F "source=jsonl" \
  -F "user_id=user_123"
```

### CSV

Import from spreadsheets:

```csv
content,metadata.source,metadata.category
"User prefers dark mode",settings,preferences
"User works at Acme Corp",profile,employment
```

```bash
curl -X POST http://localhost:8787/api/v1/transfer/import \
  -H "X-API-Key: your_api_key" \
  -F "file=@memories.csv" \
  -F "source=csv" \
  -F "user_id=user_123"
```

## Export Formats

### JSON Export

Full-fidelity export with all metadata:

```bash
curl "http://localhost:8787/api/v1/transfer/export?format=json&user_id=user_123" \
  -H "X-API-Key: your_api_key" \
  -o memories.json
```

**Output:**
```json
{
  "version": "1.0",
  "exported_at": "2026-03-02T12:00:00Z",
  "user_id": "user_123",
  "count": 150,
  "memories": [
    {
      "id": "mem_abc123",
      "content": "User prefers dark mode",
      "extracted_facts": ["User prefers dark mode"],
      "entities": [{"name": "User", "type": "person"}],
      "metadata": {"source": "settings"},
      "created_at": "2026-01-15T10:00:00Z",
      "updated_at": "2026-01-15T10:00:00Z"
    }
  ]
}
```

### JSONL Export

Streaming-friendly format for large datasets:

```bash
curl "http://localhost:8787/api/v1/transfer/export?format=jsonl&user_id=user_123" \
  -H "X-API-Key: your_api_key" \
  -o memories.jsonl
```

### CSV Export

Spreadsheet-compatible export:

```bash
curl "http://localhost:8787/api/v1/transfer/export?format=csv&user_id=user_123" \
  -H "X-API-Key: your_api_key" \
  -o memories.csv
```

## Filtering Exports

### By Date Range

```bash
curl "http://localhost:8787/api/v1/transfer/export?format=json&user_id=user_123&from=2026-01-01&to=2026-02-01" \
  -H "X-API-Key: your_api_key"
```

### By Project

```bash
curl "http://localhost:8787/api/v1/transfer/export?format=json&user_id=user_123&project=my_project" \
  -H "X-API-Key: your_api_key"
```

### Include/Exclude Entities

```bash
# Include entity data
curl "http://localhost:8787/api/v1/transfer/export?format=json&include_entities=true" \
  -H "X-API-Key: your_api_key"
```

## Bulk Operations

### Import Progress

For large imports, track progress:

```bash
# Start async import
curl -X POST http://localhost:8787/api/v1/transfer/import/async \
  -H "X-API-Key: your_api_key" \
  -F "file=@large_dataset.jsonl" \
  -F "source=jsonl"

# Response
{"job_id": "import_abc123", "status": "processing"}

# Check progress
curl http://localhost:8787/api/v1/transfer/import/status/import_abc123 \
  -H "X-API-Key: your_api_key"

# Response
{"job_id": "import_abc123", "status": "completed", "processed": 10000, "total": 10000}
```

## Data Migration

### Between Remembra Instances

```bash
# Export from source
curl "http://source:8787/api/v1/transfer/export?format=jsonl" \
  -H "X-API-Key: source_key" \
  -o backup.jsonl

# Import to destination
curl -X POST http://destination:8787/api/v1/transfer/import \
  -H "X-API-Key: dest_key" \
  -F "file=@backup.jsonl" \
  -F "source=jsonl"
```

## Best Practices

1. **Use JSONL for large datasets** - Streams efficiently, handles millions of records
2. **Include metadata** - Makes memories more searchable
3. **Test with small samples first** - Validate format before bulk import
4. **Schedule exports** - Regular backups prevent data loss
5. **Preserve timestamps** - Include `created_at` for accurate temporal queries
