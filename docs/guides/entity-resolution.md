# Entity Resolution

Remembra's killer feature: understanding that "Adam", "Mr. Smith", and "my husband" are the same person.

## How It Works

```
Input: "Had lunch with Adam today. Mr. Smith mentioned he's starting a new job."

Extraction:
  - Entity: "Adam Smith" (PERSON)
  - Aliases: ["Adam", "Mr. Smith"]
  
Storage:
  - Memory: "Adam Smith is starting a new job"
  - Entity linked to memory
```

## Entity Types

| Type | Examples |
|------|----------|
| `PERSON` | John Smith, Dr. Jones, Mom |
| `ORG` | Google, Acme Corp, FBI |
| `LOCATION` | New York, Paris, "the office" |
| `PRODUCT` | iPhone, Model 3, GPT-4 |
| `EVENT` | Q4 Review, Wedding, Conference |

## Alias Detection

Remembra automatically detects when different names refer to the same entity:

```python
memory.store("Met with David Kim from Acme today")
memory.store("Mr. Kim said the deal is approved")
memory.store("David mentioned they need the contract by Friday")

# All three are linked to the same entity: David Kim
```

### Matching Strategies

1. **Exact Match**: "David Kim" = "David Kim"
2. **Partial Match**: "David" → "David Kim" (if only one David)
3. **Title Match**: "Mr. Kim" → "David Kim"
4. **Nickname Match**: "Dave" → "David Kim" (configurable)
5. **Semantic Match**: Uses embedding similarity for fuzzy matching

## Relationships

Entities are connected through relationships extracted from context:

| Relationship | Example |
|--------------|---------|
| `WORKS_AT` | "John works at Google" |
| `KNOWS` | "Met Sarah through Mike" |
| `REPORTS_TO` | "Alice reports to Bob" |
| `SPOUSE_OF` | "John's wife Sarah" |
| `LOCATED_IN` | "Our NYC office" |
| `PART_OF` | "Marketing is part of Growth" |

### Example Graph

```
    [John Smith]
        │
    WORKS_AT
        │
        ▼
    [Google] ◄── LOCATED_IN ── [Mountain View]
        │
    PART_OF
        │
        ▼
    [Alphabet]
```

## Querying with Entities

### Find Related Memories

```python
# Query mentions "the company" - finds Google memories
context = memory.recall("What do I know about the company John works for?")
# Returns memories about Google, even if "Google" wasn't in the query
```

### Graph-Aware Retrieval

When enabled, recall traverses the entity graph:

```python
# Direct query about John
context = memory.recall("What's John working on?")

# Graph expands to find:
# - Memories about John directly
# - Memories about Google (where John works)
# - Memories about projects at Google
# - Memories about John's team members
```

### Configuring Traversal

```bash
# How many hops to traverse
REMEMBRA_GRAPH_TRAVERSAL_DEPTH=2  # Default

# Disable graph retrieval
REMEMBRA_GRAPH_RETRIEVAL_ENABLED=false
```

## Entity API

### List Entities

```python
entities = memory.get_entities()
for e in entities:
    print(f"{e['name']} ({e['type']}): {e['aliases']}")
```

### Get Relationships

```python
# Get all relationships for an entity
rels = memory.get_entity_relationships(entity_id="ent_123")

# Output:
# John Smith --WORKS_AT--> Google
# John Smith --KNOWS--> Sarah Chen
# John Smith --REPORTS_TO--> Mike Johnson
```

### Get Entity Memories

```python
# Find all memories mentioning an entity
memories = memory.get_entity_memories(entity_id="ent_123")
```

## Dashboard Visualization

The Remembra dashboard includes an interactive entity graph:

- **Nodes**: Entities (color-coded by type)
- **Edges**: Relationships
- **Click**: View entity details and linked memories
- **Search**: Find entities by name

## Best Practices

### 1. Introduce Entities Clearly

```python
# ✅ Good - Clear introduction
memory.store("John Smith is our new VP of Engineering at Google")

# ❌ Vague - Hard to resolve later
memory.store("He said the project is delayed")
```

### 2. Include Context for Resolution

```python
# ✅ Good - Context helps matching
memory.store("Meeting with John (from the sales team)")

# ❌ Ambiguous - Which John?
memory.store("Meeting with John")
```

### 3. Use Consistent Naming

```python
# ✅ Pick one and stick with it
memory.store("David mentioned...")
memory.store("David confirmed...")

# ❌ Avoid switching without context
memory.store("Dave said...")
memory.store("Mr. Kim replied...")
```

### 4. Explicitly State Relationships

```python
# ✅ Clear relationship
memory.store("Sarah Chen is John's manager")

# ❌ Implicit - harder to extract
memory.store("Talked to Sarah about John's performance review")
```

## Configuration

```bash
# Enable/disable entity extraction
REMEMBRA_ENTITY_EXTRACTION_ENABLED=true

# Matching threshold (0-1, higher = stricter)
REMEMBRA_ENTITY_MATCHING_THRESHOLD=0.85

# Graph traversal depth
REMEMBRA_GRAPH_TRAVERSAL_DEPTH=2
```

## Limitations

- **Ambiguous Pronouns**: "He" and "she" aren't resolved automatically
- **Cross-Project**: Entities are scoped to user+project
- **Performance**: Very large graphs (10k+ entities) may need tuning
- **Language**: Currently optimized for English
