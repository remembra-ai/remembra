# Remembra: Intelligent Memory Extraction

## Research Summary

Based on analysis of Mem0 architecture and best practices, here's how we should build LLM-powered extraction for Remembra.

---

## The Problem

Current implementation just splits text on periods:
```python
# Current (dumb)
facts = [s.strip() for s in content.split('.') if s.strip()]
```

This fails because:
- "John called yesterday, he's the new CTO" → splits wrong
- Misses implicit facts
- No entity extraction
- No relationship capture
- Can't handle updates (duplicate facts pile up)

---

## The Solution: Two-Phase Pipeline

### Phase 1: Extraction
Take raw input → LLM extracts atomic facts

```
INPUT: "Hey, talked to Sarah today. She's moving to the Denver 
        office next month. Oh and she mentioned John got promoted 
        to VP of Sales."

OUTPUT (atomic facts):
- "Sarah is moving to Denver office"
- "Sarah's move is scheduled for next month"  
- "John was promoted to VP of Sales"
```

### Phase 2: Memory Update
For each extracted fact:
1. Vector search for similar existing memories
2. LLM decides action: ADD | UPDATE | DELETE | NOOP
3. Execute action

```
NEW FACT: "John is VP of Sales"
EXISTING: "John is Sales Director"

LLM DECISION: UPDATE (merge into "John is VP of Sales, promoted from Sales Director")
```

---

## Extraction Prompt Design

```
SYSTEM: You are a memory extraction engine. Extract atomic facts from 
conversations. Each fact should be:
- Self-contained (understandable without context)
- Specific (include names, dates, numbers when present)
- Actionable (useful for future recall)

Do NOT extract:
- Greetings or filler ("hi", "thanks", "okay")
- Opinions without substance
- Duplicate information

OUTPUT FORMAT: JSON array of facts
["fact 1", "fact 2", ...]

USER: Extract facts from this conversation:
{content}
```

---

## Update Decision Prompt

```
SYSTEM: You are a memory consolidation engine. Given a new fact and 
existing similar memories, decide the best action:

- ADD: New fact, no similar memories exist
- UPDATE: Fact updates/enhances existing memory (provide merged text)
- DELETE: Fact contradicts existing memory (mark old for deletion)
- NOOP: Fact already captured, no change needed

USER:
NEW FACT: {new_fact}

SIMILAR EXISTING MEMORIES:
{existing_memories}

Respond with JSON:
{
  "action": "ADD|UPDATE|DELETE|NOOP",
  "memory_id": "id if UPDATE/DELETE, null if ADD",
  "content": "final fact text if ADD/UPDATE"
}
```

---

## Entity Extraction (Phase 2 Enhancement)

Extract entities with types:
- PERSON: John, Sarah, Dr. Martinez
- ORGANIZATION: Acme Corp, DolphyTech
- LOCATION: Denver, Jamaica
- DATE: next month, March 1st
- PRODUCT: Remembra, ChartHustle

Store in SQLite with relationships:
```sql
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    aliases TEXT,  -- JSON array
    created_at TIMESTAMP
);

CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    subject_id TEXT,
    predicate TEXT,  -- "works_at", "manages", "located_in"
    object_id TEXT,
    created_at TIMESTAMP
);
```

---

## Implementation Plan

### Step 1: LLM Extraction Service (2 hours)
- Create `extraction.py` module
- Implement extraction prompt
- Parse LLM response to facts
- Handle errors gracefully

### Step 2: Memory Update Logic (2 hours)
- Add similarity search before insert
- Implement update decision prompt
- Handle ADD/UPDATE/DELETE/NOOP
- Maintain history log

### Step 3: Integration (1 hour)
- Wire into existing `store()` flow
- Add config for LLM provider
- Add toggle for smart vs simple extraction

### Step 4: Testing (1 hour)
- Unit tests for extraction
- Integration tests for full flow
- Edge cases (empty, duplicates, contradictions)

---

## Cost Considerations

Each store() call with LLM extraction:
- GPT-4o-mini: ~$0.0003 per call
- Claude Haiku: ~$0.0004 per call
- Local (Ollama): $0

At 1000 memories/day = $0.30-0.40/day

**Recommendation:** Default to GPT-4o-mini for quality/cost balance, support local models for privacy-first users.

---

## Config Options

```python
class ExtractionConfig:
    enabled: bool = True                    # Toggle smart extraction
    provider: str = "openai"                # openai, anthropic, ollama
    model: str = "gpt-4o-mini"              # Model for extraction
    similarity_threshold: float = 0.85      # When to check for updates
    max_facts_per_input: int = 10           # Limit extraction
    enable_entities: bool = False           # Phase 2 feature
```

---

## Success Metrics

1. **Extraction Quality**
   - Facts are atomic and self-contained
   - No duplicate facts stored
   - Contradictions resolved

2. **Performance**
   - < 500ms added latency for extraction
   - < 100ms for update decision

3. **Cost Efficiency**
   - < $0.001 per memory stored
   - 90%+ reduction vs full-context approach

---

## Next Steps

1. [ ] Build extraction.py with LLM prompts
2. [ ] Add update decision logic to store()
3. [ ] Test with real conversations
4. [ ] Benchmark quality vs current approach
5. [ ] Add entity extraction (Phase 2)
