# Import & Export

Remembra supports importing memories from various sources and exporting in multiple formats.

## Two ways in

| Route | Body | Size limit |
|-------|------|------------|
| `POST /api/v1/transfer/import/file?format=<format>` | the file, as multipart form field `file` | 50 MB |
| `POST /api/v1/transfer/import` | JSON: `{"format": "...", "data": "<the file's text>"}` | 8 MiB request, 8,000,000 characters of `data` |

Anything larger than the inline limit goes to `/transfer/import/file`. `format` is one of `json`, `jsonl`,
`csv`, `chatgpt`, `claude`, `plaintext`; for plain text, `split_mode` (`paragraph`, `line`, `heading`, `none`)
says where one memory ends. Both routes take an optional `project_id`.

## Import Sources

### ChatGPT Conversations

Import your ChatGPT conversation history:

```bash
# Export from ChatGPT: Settings → Data Controls → Export Data
# You'll receive a ZIP file with conversations.json

curl -X POST "http://localhost:8787/api/v1/transfer/import/file?format=chatgpt" \
  -H "X-API-Key: your_api_key" \
  -F "file=@conversations.json"
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
curl -X POST "http://localhost:8787/api/v1/transfer/import/file?format=claude" \
  -H "X-API-Key: your_api_key" \
  -F "file=@claude_conversations.json"
```

### Plain Text

Import from plain text files (one memory per paragraph or line):

```bash
curl -X POST "http://localhost:8787/api/v1/transfer/import/file?format=plaintext&split_mode=paragraph" \
  -H "X-API-Key: your_api_key" \
  -F "file=@notes.txt"  # split_mode: paragraph, line, heading or none
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
curl -X POST "http://localhost:8787/api/v1/transfer/import/file?format=json" \
  -H "X-API-Key: your_api_key" \
  -F "file=@memories.json"
```

### JSONL (JSON Lines)

For large imports, use streaming JSONL format:

```jsonl
{"content": "Memory 1", "metadata": {}}
{"content": "Memory 2", "metadata": {}}
{"content": "Memory 3", "metadata": {}}
```

```bash
curl -X POST "http://localhost:8787/api/v1/transfer/import/file?format=jsonl" \
  -H "X-API-Key: your_api_key" \
  -F "file=@memories.jsonl"
```

### CSV

Import from spreadsheets:

```csv
content,metadata.source,metadata.category
"User prefers dark mode",settings,preferences
"User works at Acme Corp",profile,employment
```

```bash
curl -X POST "http://localhost:8787/api/v1/transfer/import/file?format=csv" \
  -H "X-API-Key: your_api_key" \
  -F "file=@memories.csv"
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

### Large imports

An import runs in the request: `/transfer/import/file` takes a file up to 50 MB and answers with the
counts (`imported`, `skipped`, `errors`). Split a larger export into several files. `POST /api/v1/memories/bulk`
stores up to 100 pre-structured memories per call on paid plans (10 on Free), up to 50,000 characters each
(8,000 on Free), in an 8 MiB request, without fact extraction.

### Agent-scoped keys

A key scoped to one agent imports as that agent. Every memory it imports gets that `agent_id`. If any item's
`metadata.agent_id` (or the `X-Remembra-Agent-Id` header) names another agent, the whole import is refused
(403) and nothing is stored. To keep other agents' ids, import with an unscoped key.

## Data Migration

### Between Remembra Instances

```bash
# Export from source
curl "http://source:8787/api/v1/transfer/export?format=jsonl" \
  -H "X-API-Key: source_key" \
  -o backup.jsonl

# Import to destination
curl -X POST "http://destination:8787/api/v1/transfer/import/file?format=jsonl" \
  -H "X-API-Key: dest_key" \
  -F "file=@backup.jsonl"
```

## Best Practices

1. **Use JSONL for large datasets** - Streams efficiently, handles millions of records
2. **Include metadata** - Makes memories more searchable
3. **Test with small samples first** - Validate format before bulk import
4. **Schedule exports** - Regular backups prevent data loss
5. **Preserve timestamps** - Include `created_at` for accurate temporal queries
