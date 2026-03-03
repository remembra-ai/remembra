# 🔬 PHASE 1 RESEARCH: Conversation Ingestion
**Date:** March 3, 2026  
**Status:** Research Complete → Ready to Build

---

## 📊 CODEBASE AUDIT SUMMARY

### Existing Components (What We Have)

| Component | Location | Status | Notes |
|-----------|----------|--------|-------|
| **FactExtractor** | `extraction/extractor.py` | ✅ Complete | Extracts atomic facts from text, LLM-powered |
| **EntityExtractor** | `extraction/entities.py` | ✅ Complete | Extracts PERSON, ORG, LOCATION + relationships |
| **MemoryConsolidator** | `extraction/consolidator.py` | ✅ Complete | ADD/UPDATE/DELETE/NOOP decisions via LLM |
| **ConflictManager** | `extraction/conflicts.py` | ✅ Complete | SQLite-backed conflict tracking |
| **EntityMatcher** | `extraction/matcher.py` | ✅ Complete | Entity alias matching |
| **MemoryService** | `services/memory.py` | ✅ Complete | Orchestrates store/recall/update/forget |
| **Ingest Router** | `api/v1/ingest.py` | ⚠️ Partial | Only has `/changelog` endpoint |

### File Structure
```
src/remembra/
├── api/v1/
│   ├── ingest.py          ← ADD: /conversation endpoint here
│   ├── memories.py        ← Reference: patterns for routes
│   └── ...
├── extraction/
│   ├── extractor.py       ← FactExtractor (ready to use)
│   ├── entities.py        ← EntityExtractor (ready to use)
│   ├── consolidator.py    ← MemoryConsolidator (ready to use)
│   ├── conflicts.py       ← ConflictManager (ready to use)
│   └── matcher.py         ← EntityMatcher (ready to use)
├── models/
│   └── memory.py          ← ADD: conversation request/response models
├── services/
│   └── memory.py          ← MemoryService (orchestration layer)
└── ingestion/
    └── changelog.py       ← Reference: ingestion pattern
```

---

## 🔍 EXISTING CODE ANALYSIS

### 1. FactExtractor (`extraction/extractor.py`)

**What it does:**
- Takes raw text → extracts atomic facts
- Uses GPT-4o-mini with structured JSON output
- Has fallback to simple sentence splitting

**Current prompt approach:**
```python
# Single-text extraction (NOT conversation-aware)
EXTRACTION_USER_PROMPT = """Extract memorable facts from this text:
{content}
Remember: Only extract facts worth remembering long-term. Return JSON with "facts" array."""
```

**Gap:** No conversation context (speaker roles, message history, rolling summary)

**What we need:** Enhanced prompt that takes Mem0's three-context approach:
1. Latest user-assistant pair
2. Rolling conversation summary
3. Recent messages (last ~10)

---

### 2. EntityExtractor (`extraction/entities.py`)

**What it does:**
- Extracts entities: PERSON, ORG, LOCATION, DATE, PRODUCT, MONEY, CONCEPT
- Extracts relationships: WORKS_AT, MANAGES, SPOUSE_OF, etc.
- Returns `ExtractionResult(entities, relationships)`

**Key classes:**
```python
@dataclass
class ExtractedEntity:
    name: str
    type: str  # "PERSON", "ORG", etc.
    description: str
    aliases: list[str]

@dataclass
class ExtractedRelationship:
    subject: str
    predicate: str  # "WORKS_AT", "SPOUSE_OF", etc.
    object: str

@dataclass
class ExtractionResult:
    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]
```

**Status:** Ready to use as-is

---

### 3. MemoryConsolidator (`extraction/consolidator.py`)

**What it does:**
- Takes new fact + list of similar existing memories
- LLM decides: ADD, UPDATE, DELETE, or NOOP
- Returns merged content if UPDATE

**Key classes:**
```python
class ConsolidationAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NOOP = "NOOP"

@dataclass
class ExistingMemory:
    id: str
    content: str
    score: float = 0.0

@dataclass
class ConsolidationResult:
    action: ConsolidationAction
    target_id: str | None
    content: str | None
    reason: str
```

**Usage:**
```python
consolidator = MemoryConsolidator()
result = await consolidator.consolidate(
    new_fact="John is VP of Sales",
    existing=[ExistingMemory(id="m1", content="John is Sales Director", score=0.85)]
)
# result.action == ConsolidationAction.UPDATE
# result.content == "John is VP of Sales (promoted from Sales Director)"
```

**Status:** Ready to use as-is

---

### 4. ConflictManager (`extraction/conflicts.py`)

**What it does:**
- Records conflicts when contradictions detected
- SQLite-backed with full CRUD
- Supports resolution strategies: UPDATE, VERSION, FLAG

**Key classes:**
```python
class ConflictStrategy(str, Enum):
    UPDATE = "update"   # Overwrite old memory
    VERSION = "version" # Keep both, tag as conflicting
    FLAG = "flag"       # Store new, mark both for review

@dataclass
class MemoryConflict:
    id: str
    user_id: str
    project_id: str
    new_fact: str
    existing_memory_id: str
    existing_content: str
    similarity_score: float
    reason: str
    strategy_applied: ConflictStrategy
    status: ConflictStatus  # OPEN, RESOLVED, DISMISSED
```

**Status:** Ready to use as-is

---

### 5. MemoryService (`services/memory.py`)

**What it does:**
- Orchestrates all memory operations
- Already has extraction → consolidation → storage flow
- Already initializes all extractors

**Current store flow:**
```python
async def store(self, request: StoreRequest, ...) -> StoreResponse:
    # 1. Extract atomic facts
    extracted_facts = await self.extractor.extract(request.content)
    
    # 2. For each fact, run consolidation
    for fact in extracted_facts:
        fact_result = await self._store_single_fact(fact, ...)
    
    # 3. Return stored facts
    return StoreResponse(id=memory_id, extracted_facts=stored_facts, entities=[])
```

**Gap:** No conversation-specific processing

---

### 6. Existing Ingest Endpoint (`api/v1/ingest.py`)

**Pattern to follow:**
```python
@router.post(
    "/changelog",
    response_model=ChangelogIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("10/minute")
async def ingest_changelog(
    request: Request,
    body: ChangelogIngestRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> ChangelogIngestResponse:
```

**Conventions:**
- Router prefix: `/ingest`
- Rate limiting via `@limiter.limit()`
- Dependencies: `MemoryServiceDep`, `AuditLoggerDep`, `CurrentUser`
- Response models in request file

---

## 🎯 WHAT TO BUILD

### New Endpoint: `POST /api/v1/ingest/conversation`

**File:** `src/remembra/api/v1/ingest.py` (add to existing)

### Request Model
```python
class Message(BaseModel):
    """A single message in a conversation."""
    role: str = Field(description="'user' | 'assistant' | 'system'")
    content: str
    timestamp: datetime | None = None
    name: str | None = Field(default=None, description="Speaker name if known")

class IngestOptions(BaseModel):
    """Options for conversation ingestion."""
    extract_from: str = Field(
        default="user",
        description="'user' | 'assistant' | 'all' - which messages to extract from"
    )
    min_importance: float = Field(
        default=0.3,
        ge=0.0, le=1.0,
        description="Minimum importance threshold for facts"
    )
    dedupe: bool = Field(default=True, description="Enable deduplication")
    store: bool = Field(default=True, description="False = dry-run mode")
    infer: bool = Field(
        default=True,
        description="True = full extraction, False = store raw messages"
    )

class ConversationIngestRequest(BaseModel):
    """Request to ingest a conversation."""
    messages: list[Message] = Field(min_length=1)
    session_id: str | None = Field(default=None, description="Conversation session ID")
    project_id: str = Field(default="default")
    context: dict[str, Any] | None = Field(
        default=None,
        description="Context metadata (channel, timezone, etc.)"
    )
    options: IngestOptions = Field(default_factory=IngestOptions)
```

### Response Model
```python
class ExtractedFactResult(BaseModel):
    """A fact extracted from conversation."""
    content: str
    confidence: float
    importance: float
    source_message_index: int
    speaker: str | None
    action: str  # ADD, UPDATE, NOOP
    memory_id: str | None

class ConversationIngestResponse(BaseModel):
    """Response from conversation ingestion."""
    success: bool
    facts_extracted: int
    facts_stored: int
    facts_deduped: int
    facts_skipped: int
    entities_found: int
    extracted_facts: list[ExtractedFactResult]
    entities: list[EntityRef]
    processing_time_ms: int
    errors: list[str] = Field(default_factory=list)
```

---

## 🔧 IMPLEMENTATION APPROACH

### Option A: Enhance FactExtractor with Conversation Mode
Add conversation-aware extraction to existing `FactExtractor`:

```python
# In extraction/extractor.py - ADD new method

CONVERSATION_EXTRACTION_PROMPT = """You are a Personal Information Organizer...
(Mem0-style three-context prompt)
"""

class FactExtractor:
    # ... existing code ...
    
    async def extract_from_conversation(
        self,
        messages: list[dict],
        extract_from: str = "user",  # "user" | "assistant" | "all"
    ) -> list[ExtractedFact]:
        """
        Extract facts from a conversation with context awareness.
        
        Uses Mem0's three-context approach:
        1. Latest user-assistant pair
        2. Rolling summary (generated on-the-fly)
        3. Recent messages (last 10)
        """
        # Build context
        context = self._build_conversation_context(messages)
        
        # Filter messages by extract_from
        target_messages = self._filter_messages(messages, extract_from)
        
        # Extract with enhanced prompt
        ...
```

**Pros:** 
- Keeps extraction logic together
- Easy to maintain

**Cons:**
- Makes FactExtractor more complex

### Option B: New ConversationProcessor Class (RECOMMENDED)
Create dedicated processor that orchestrates existing components:

```python
# NEW FILE: src/remembra/ingestion/conversation.py

class ConversationProcessor:
    """
    Processes conversations into memories.
    Orchestrates: FactExtractor, EntityExtractor, MemoryConsolidator
    """
    
    def __init__(
        self,
        extractor: FactExtractor,
        entity_extractor: EntityExtractor,
        consolidator: MemoryConsolidator,
        conflict_manager: ConflictManager,
        settings: Settings,
    ):
        self.extractor = extractor
        self.entity_extractor = entity_extractor
        self.consolidator = consolidator
        self.conflict_manager = conflict_manager
        self.settings = settings
    
    async def process(
        self,
        messages: list[Message],
        user_id: str,
        project_id: str,
        options: IngestOptions,
    ) -> ConversationProcessResult:
        """
        Main processing pipeline:
        1. Build conversation context
        2. Extract facts with conversation awareness
        3. Extract entities
        4. For each fact: dedup via consolidator
        5. Return results
        """
        ...
```

**Pros:**
- Clean separation of concerns
- Follows existing patterns (ChangelogParser)
- Easy to test independently

**Cons:**
- New file to maintain

---

## 📝 DETAILED IMPLEMENTATION PLAN

### Step 1: Create Models (30 min)
**File:** `src/remembra/api/v1/ingest.py`
- Add `Message`, `IngestOptions`, `ConversationIngestRequest`
- Add `ExtractedFactResult`, `ConversationIngestResponse`

### Step 2: Create ConversationProcessor (2-3 hours)
**File:** `src/remembra/ingestion/conversation.py`

1. **Context Builder**
```python
def _build_context(self, messages: list[Message]) -> ConversationContext:
    """Build Mem0-style three-context structure."""
    return ConversationContext(
        latest_pair=self._get_latest_pair(messages),
        summary=self._generate_summary(messages),
        recent=messages[-10:],
    )
```

2. **Enhanced Extraction**
```python
async def _extract_facts(
    self,
    context: ConversationContext,
    extract_from: str,
) -> list[ExtractedFact]:
    """Extract facts using conversation-aware prompt."""
    prompt = CONVERSATION_EXTRACTION_PROMPT.format(
        latest_pair=context.latest_pair,
        summary=context.summary,
        recent=context.recent,
        extract_from=extract_from,
    )
    # Call LLM
    ...
```

3. **Deduplication Loop**
```python
async def _process_facts(
    self,
    facts: list[ExtractedFact],
    user_id: str,
    project_id: str,
    options: IngestOptions,
) -> ProcessingResult:
    """Process each fact through consolidation."""
    results = []
    
    for fact in facts:
        # Search similar
        similar = await self._find_similar(fact.content, user_id)
        
        # Consolidate
        decision = await self.consolidator.consolidate(
            new_fact=fact.content,
            existing=similar,
        )
        
        # Execute decision
        result = await self._execute_decision(decision, fact, user_id, project_id)
        results.append(result)
    
    return ProcessingResult(results)
```

### Step 3: Create Endpoint (1 hour)
**File:** `src/remembra/api/v1/ingest.py`

```python
@router.post(
    "/conversation",
    response_model=ConversationIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a conversation and extract memories",
)
@limiter.limit("20/minute")
async def ingest_conversation(
    request: Request,
    body: ConversationIngestRequest,
    memory_service: MemoryServiceDep,
    audit_logger: AuditLoggerDep,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> ConversationIngestResponse:
    """
    Parse a conversation and automatically extract memorable facts.
    
    Supports:
    - Role-based messages (user/assistant/system)
    - Speaker attribution
    - Automatic deduplication
    - Entity extraction
    
    Use `options.store=false` for dry-run mode.
    Use `options.infer=false` to store raw messages without extraction.
    """
    ...
```

### Step 4: Wire to MemoryService (30 min)
**File:** `src/remembra/services/memory.py`

Add method to use ConversationProcessor for storage:
```python
async def store_from_conversation(
    self,
    messages: list[Message],
    user_id: str,
    project_id: str,
    options: IngestOptions,
) -> ConversationStoreResult:
    """Store memories extracted from a conversation."""
    processor = ConversationProcessor(
        extractor=self.extractor,
        entity_extractor=self.entity_extractor,
        consolidator=self.consolidator,
        conflict_manager=self.conflict_manager,
        settings=self.settings,
    )
    return await processor.process(messages, user_id, project_id, options)
```

### Step 5: Update MCP Server (30 min)
**File:** `src/remembra/mcp/server.py`

Add `ingest_conversation` tool to MCP server.

### Step 6: Tests (1-2 hours)
**File:** `tests/test_conversation_ingestion.py`

---

## ✅ SUCCESS CRITERIA

- [ ] `POST /api/v1/ingest/conversation` accepts message array
- [ ] Facts extracted with speaker attribution
- [ ] Entities extracted with relationships
- [ ] Deduplication via LLM consolidator (not rules)
- [ ] `options.infer=false` stores raw messages
- [ ] `options.store=false` returns extraction without storing
- [ ] Rate limiting active
- [ ] Audit logging
- [ ] Webhook dispatch on completion

---

## 🕐 TIME ESTIMATE

| Task | Time |
|------|------|
| Models (request/response) | 30 min |
| ConversationProcessor class | 2-3 hours |
| API endpoint | 1 hour |
| Wire to MemoryService | 30 min |
| MCP server update | 30 min |
| Tests | 1-2 hours |
| **Total** | **6-8 hours** |

---

## 🔄 NEXT STEPS

1. **Confirm approach** - Option B (ConversationProcessor) recommended
2. **Start with models** - Define request/response first
3. **Build processor** - Core logic
4. **Wire endpoint** - Following existing patterns
5. **Test** - Unit + integration tests

Ready to build when you give the go-ahead! 🎖️
