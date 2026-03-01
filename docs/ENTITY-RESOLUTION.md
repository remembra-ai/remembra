# Remembra: Entity Resolution Design (Week 5)

## Research Summary

Based on analysis of LINK-KG, Relik, Neo4j patterns, and academic research.

---

## The Problem

Currently, Remembra treats each memory as isolated text:

```
Memory 1: "John is the CEO of Acme Corp"
Memory 2: "Mr. Smith mentioned the quarterly targets"
Memory 3: "The CEO's wife Lisa is planning a trip"

Query: "Tell me about John Smith"
→ Only finds Memory 1 (direct match)
→ MISSES that John = Mr. Smith = "the CEO"
```

---

## The Solution: Entity Resolution Pipeline

### Phase 1: Entity Extraction
Extract entities with types from each memory:

```
INPUT: "John Smith is the CEO of Acme Corp. His wife Lisa works at Google."

EXTRACTED ENTITIES:
- PERSON: "John Smith" (role: CEO)
- ORG: "Acme Corp"
- PERSON: "Lisa" (relationship: wife of John Smith)
- ORG: "Google"

EXTRACTED RELATIONSHIPS:
- John Smith → WORKS_AT → Acme Corp
- John Smith → ROLE → CEO
- Lisa → SPOUSE_OF → John Smith
- Lisa → WORKS_AT → Google
```

### Phase 2: Entity Matching (Coreference Resolution)
Link mentions to canonical entities:

```
NEW MENTION: "Mr. Smith"
EXISTING ENTITIES: ["John Smith (CEO)", "Lisa Smith", "Bob Smith"]

MATCHING:
- Context: "Mr. Smith mentioned the quarterly targets"
- Role context: CEO → quarterly targets fits
- MATCH: "Mr. Smith" → John Smith (confidence: 0.92)

RESULT:
- Add alias "Mr. Smith" to John Smith entity
- Link memory to John Smith entity
```

### Phase 3: Graph Storage
Store entities and relationships in SQLite:

```sql
-- Entities table
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    type TEXT NOT NULL,  -- PERSON, ORG, LOCATION, DATE, etc.
    aliases TEXT,        -- JSON array ["John", "Mr. Smith", "the CEO"]
    description TEXT,    -- "CEO of Acme Corp"
    embedding BLOB,      -- For similarity matching
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Relationships table
CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,  -- Entity ID
    predicate TEXT NOT NULL,   -- WORKS_AT, SPOUSE_OF, MANAGES, etc.
    object_id TEXT NOT NULL,   -- Entity ID or value
    confidence REAL,
    source_memory_id TEXT,
    created_at TIMESTAMP,
    FOREIGN KEY (subject_id) REFERENCES entities(id),
    FOREIGN KEY (object_id) REFERENCES entities(id)
);

-- Memory-Entity links
CREATE TABLE memory_entities (
    memory_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    mention_text TEXT,        -- Original text that mentioned this entity
    PRIMARY KEY (memory_id, entity_id)
);
```

### Phase 4: Enhanced Recall
Use entity graph to find related memories:

```
Query: "Tell me about John Smith"

Step 1: Find entity
  → John Smith (id: ent_123)
  → Aliases: ["John", "Mr. Smith", "the CEO"]

Step 2: Find related entities
  → Lisa (SPOUSE_OF John Smith)
  → Acme Corp (John Smith WORKS_AT)

Step 3: Find all memories linked to these entities
  → Memory 1: "John is the CEO of Acme Corp"
  → Memory 2: "Mr. Smith mentioned the quarterly targets"
  → Memory 3: "The CEO's wife Lisa is planning a trip"

Step 4: Combine with semantic search
  → Ranked by relevance + entity connections
```

---

## Entity Extraction Prompt

```
SYSTEM: You are an entity extraction engine. Extract entities and relationships from text.

ENTITY TYPES:
- PERSON: People's names (include titles, roles if mentioned)
- ORG: Companies, organizations, teams
- LOCATION: Cities, countries, addresses
- DATE: Dates, time periods
- PRODUCT: Products, services, projects
- CONCEPT: Abstract concepts, topics

OUTPUT FORMAT:
{
  "entities": [
    {
      "name": "John Smith",
      "type": "PERSON",
      "description": "CEO of Acme Corp",
      "aliases": ["John", "Mr. Smith"]
    }
  ],
  "relationships": [
    {
      "subject": "John Smith",
      "predicate": "WORKS_AT",
      "object": "Acme Corp"
    }
  ]
}

USER: Extract entities and relationships from:
{content}
```

---

## Entity Matching Prompt

```
SYSTEM: You are an entity matching engine. Determine if a new mention refers 
to an existing entity.

MATCHING RULES:
1. Consider name similarity (John Smith ↔ Mr. Smith ↔ J. Smith)
2. Consider context (role, location, relationships)
3. Consider type compatibility (PERSON can't match ORG)
4. Return confidence score (0.0 to 1.0)

OUTPUT FORMAT:
{
  "match": true|false,
  "entity_id": "existing entity id or null",
  "confidence": 0.0-1.0,
  "reason": "explanation"
}

If no match, return:
{
  "match": false,
  "entity_id": null,
  "new_entity": {
    "name": "canonical name",
    "type": "PERSON|ORG|...",
    "description": "brief description"
  }
}
```

---

## Implementation Plan

### Step 1: Database Schema (1 hour)
- Add entities, relationships, memory_entities tables
- Update Database class with new methods

### Step 2: Entity Extractor Module (3 hours)
- Create `extraction/entities.py`
- LLM prompt for entity extraction
- Parse and validate entity output

### Step 3: Entity Matcher Module (3 hours)
- Create `extraction/matcher.py`
- Embedding-based candidate retrieval
- LLM-powered matching decision
- Alias management

### Step 4: Integration with Store (2 hours)
- Extract entities during store()
- Match/create entities
- Store relationships
- Link memories to entities

### Step 5: Integration with Recall (2 hours)
- Entity-aware retrieval
- Graph traversal for related memories
- Combined ranking (semantic + entity)

### Step 6: Testing (2 hours)
- Unit tests for extraction
- Integration tests for matching
- End-to-end entity resolution tests

---

## Success Criteria

1. **Extraction Accuracy**
   - 90%+ of named entities correctly identified
   - Relationships correctly captured

2. **Matching Precision**
   - "John" correctly linked to "John Smith"
   - No false positives (different people merged)

3. **Recall Improvement**
   - Query "John Smith" finds ALL related memories
   - Includes memories mentioning "Mr. Smith", "the CEO"

4. **Performance**
   - < 1s additional latency for entity extraction
   - < 500ms for entity matching

---

## Cost Estimate

- GPT-4o-mini for extraction: ~$0.0005 per memory
- GPT-4o-mini for matching: ~$0.0003 per entity check
- Average memory with 3 entities: ~$0.0014 total
- 1000 memories/day: ~$1.40/day

---

## Version Target

**Remembra v0.3.0** - Entity Resolution
- Entity extraction from memories
- Coreference resolution (alias matching)
- Entity-aware recall
- Relationship storage
