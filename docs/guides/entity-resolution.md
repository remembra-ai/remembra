# Entity Resolution

Remembra links the names in your memories to entities. An LLM matcher merges name variants that fit the
context, such as "Mr. Smith" and "John Smith". Resolving a mention like "my husband" to a named person is
best-effort and untested.

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

## Extraction Providers

Entity extraction supports multiple LLM providers. Configure which provider to use via the `REMEMBRA_LLM_PROVIDER` environment variable.

!!! note "New in v0.8.0"
    Previously entity extraction was limited to OpenAI. Now you can use Anthropic or local Ollama models.

### OpenAI (default)

```bash
REMEMBRA_LLM_PROVIDER=openai
REMEMBRA_OPENAI_API_KEY=sk-...
```

OpenAI is the default provider. Set `REMEMBRA_OPENAI_API_KEY` and entity extraction works out of the box. (A
bare `OPENAI_API_KEY` is not read.)

### Anthropic Claude

```bash
REMEMBRA_LLM_PROVIDER=anthropic
REMEMBRA_ANTHROPIC_API_KEY=sk-ant-...
REMEMBRA_LLM_MODEL=claude-haiku-4-5
```

Use Anthropic Claude for entity extraction. Requires `REMEMBRA_ANTHROPIC_API_KEY` and the `anthropic` extra.

### Ollama (local)

```bash
REMEMBRA_LLM_PROVIDER=ollama
REMEMBRA_OLLAMA_URL=http://localhost:11434  # default
REMEMBRA_LLM_MODEL=llama3.1                 # a chat model you have pulled
```

Run entity extraction locally with Ollama. No API key needed, just a running Ollama instance with the model
pulled.

The provider setting covers entity extraction only. Fact extraction, consolidation and entity matching use
OpenAI (`REMEMBRA_EXTRACTION_MODEL`) whatever it says.

### Provider Selection

The system uses a factory pattern to automatically create the right extractor based on your configuration. Simply set `REMEMBRA_LLM_PROVIDER` to your desired provider and the correct implementation is instantiated at startup. No additional code changes are required.

```
┌──────────────────┐
│  EntityExtractor │  (factory)
│    Factory       │
└────────┬─────────┘
         │
    ┌────┴─────┬──────────────┐
    ▼          ▼              ▼
┌────────┐ ┌──────────┐ ┌────────┐
│ OpenAI │ │ Anthropic│ │ Ollama │
│Extractor│ │ Extractor│ │Extractor│
└────────┘ └──────────┘ └────────┘
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

### How matching works

The existing entities closest to the new mention by name are ranked first. An LLM then decides whether the
mention is one of them, using the name, type and context ("David" or "Mr. Kim" → "David Kim"). It merges the
mention when its confidence is at least `REMEMBRA_ENTITY_MATCHING_THRESHOLD` (default `0.6`); otherwise a new
entity is created. Matching uses the OpenAI extraction model, so it needs an OpenAI key.

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
REMEMBRA_GRAPH_MAX_DEPTH=2  # Default

# Disable graph retrieval
REMEMBRA_ENABLE_GRAPH_RETRIEVAL=false
```

## Entity API

### List Entities

```python
result = memory.list_entities()
for e in result["entities"]:
    print(f"{e['canonical_name']} ({e['type']}): {e['aliases']}")
```

### Relationships and Memories of an Entity

The Python SDK has no methods for these. Use the REST API (any key with `entity:read`):

```http
GET /api/v1/entities/{entity_id}/relationships
GET /api/v1/entities/{entity_id}/memories
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
# Enable/disable entity extraction and matching
REMEMBRA_ENABLE_ENTITY_RESOLUTION=true

# Matching threshold (0-1, higher = stricter)
REMEMBRA_ENTITY_MATCHING_THRESHOLD=0.6

# Graph traversal depth
REMEMBRA_GRAPH_MAX_DEPTH=2
```

## Limitations

- **Ambiguous Pronouns**: "He" and "she" aren't resolved automatically
- **Cross-Project**: Entities are scoped to user+project
- **Performance**: Large graphs have not been measured
- **Relational mentions**: "my husband" or "the CEO" are resolved to a named person only on a best-effort basis
- **Language**: Currently optimized for English
