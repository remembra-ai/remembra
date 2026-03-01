# Entity Graph

The knowledge graph behind Remembra's entity resolution.

## Overview

Remembra maintains a graph of entities and their relationships:

```
[John Smith] ─── WORKS_AT ──► [Google]
      │                           │
      │                      LOCATED_IN
   KNOWS                          │
      │                           ▼
      ▼                    [Mountain View]
[Sarah Chen] ─── MANAGES ──► [John Smith]
```

## Nodes (Entities)

Entities are extracted from memories and stored as nodes:

```json
{
  "id": "ent_123",
  "name": "John Smith",
  "type": "PERSON",
  "aliases": ["John", "Mr. Smith", "Johnny"],
  "created_at": "2026-03-01T10:00:00Z",
  "user_id": "user_123",
  "project": "default"
}
```

### Entity Types

| Type | Examples |
|------|----------|
| `PERSON` | John Smith, Dr. Jones, CEO |
| `ORG` | Google, FBI, Acme Corp |
| `LOCATION` | New York, Paris, "the office" |
| `PRODUCT` | iPhone, Tesla Model 3 |
| `EVENT` | Q4 Review, Wedding |
| `CONCEPT` | Machine Learning, Revenue |

## Edges (Relationships)

Relationships connect entities:

```json
{
  "id": "rel_456",
  "source_id": "ent_123",
  "target_id": "ent_789",
  "type": "WORKS_AT",
  "properties": {
    "role": "Senior Engineer",
    "since": "2024"
  }
}
```

### Relationship Types

| Type | Example |
|------|---------|
| `WORKS_AT` | John → WORKS_AT → Google |
| `MANAGES` | Sarah → MANAGES → John |
| `REPORTS_TO` | John → REPORTS_TO → Sarah |
| `KNOWS` | John → KNOWS → Mike |
| `SPOUSE_OF` | John → SPOUSE_OF → Jane |
| `LOCATED_IN` | Google → LOCATED_IN → California |
| `PART_OF` | Chrome → PART_OF → Google |
| `OWNS` | John → OWNS → Tesla |

## Alias Resolution

The graph enables alias matching:

```
Memory 1: "Met with David Kim at Acme"
Memory 2: "Mr. Kim approved the proposal"
Memory 3: "David's email is d.kim@acme.com"

Entity Created:
  name: "David Kim"
  aliases: ["David", "Mr. Kim", "d.kim"]
  
Query: "What do I know about Dave Kim?"
→ Matches via fuzzy alias matching
→ Returns all three memories
```

### Alias Sources

1. **Explicit**: Same sentence context
2. **Title patterns**: "Mr. Kim" → last name matching
3. **Partial match**: "David" when only one David exists
4. **Semantic**: Embedding similarity for fuzzy matching

## Graph-Aware Retrieval

When querying, the graph expands context:

### Depth 0 (No expansion)

```
Query: "What does John do?"
Returns: Memories directly mentioning John
```

### Depth 1 (One hop)

```
Query: "What does John do?"
Returns: 
  - Memories about John
  - Memories about Google (where John works)
  - Memories about Sarah (who John knows)
```

### Depth 2 (Two hops)

```
Query: "What does John do?"
Returns:
  - Memories about John
  - Memories about Google
  - Memories about other Google employees
  - Memories about Mountain View (where Google is)
```

### Configuration

```bash
REMEMBRA_GRAPH_RETRIEVAL_ENABLED=true
REMEMBRA_GRAPH_TRAVERSAL_DEPTH=2  # Max hops
```

## Querying the Graph

### List Entities

```python
entities = memory.get_entities()
for e in entities:
    print(f"{e['name']} ({e['type']})")
    print(f"  Aliases: {e['aliases']}")
```

### Get Relationships

```python
rels = memory.get_entity_relationships("ent_123")
for r in rels:
    print(f"{r['source']} --{r['type']}--> {r['target']}")
```

### Find Entity Memories

```python
# All memories linked to an entity
memories = memory.get_entity_memories("ent_123")
```

## Dashboard Visualization

The dashboard includes an interactive graph view:

- **Nodes**: Entities colored by type
- **Edges**: Relationships with labels
- **Hover**: Entity details
- **Click**: Filter memories by entity
- **Zoom/Pan**: Navigate large graphs

## Graph Storage

Currently stored in SQLite for simplicity:

```sql
-- Entities table
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    aliases JSON,
    user_id TEXT,
    project TEXT
);

-- Relationships table
CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    target_id TEXT,
    type TEXT,
    properties JSON
);

-- Memory-Entity links
CREATE TABLE memory_entity_links (
    memory_id TEXT,
    entity_id TEXT,
    PRIMARY KEY (memory_id, entity_id)
);
```

For large-scale deployments (100k+ entities), migration to Neo4j is planned.

## Best Practices

### 1. Introduce Entities Clearly

```python
# ✅ Good - Full context
memory.store("John Smith joined as VP of Engineering at Google")

# ❌ Vague - Hard to resolve
memory.store("He joined the company")
```

### 2. State Relationships Explicitly

```python
# ✅ Good - Clear relationship
memory.store("Sarah Chen is John's direct manager")

# ❌ Implicit - Might miss
memory.store("Had 1:1 with Sarah about John's project")
```

### 3. Use Consistent Names

Pick one canonical name and stick with it:

```python
# ✅ Consistent
memory.store("David mentioned...")
memory.store("David confirmed...")

# ❌ Switching causes fragmentation
memory.store("Dave said...")
memory.store("Mr. Kim replied...")
```

## Limitations

- **Pronouns**: "He/she" not auto-resolved (context required)
- **Scope**: Entities are per user+project
- **Scale**: SQLite handles ~10k entities well; beyond that, consider Neo4j
- **Language**: Optimized for English entity patterns
