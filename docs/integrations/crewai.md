# CrewAI

Use Remembra as the memory backend for CrewAI crews — persistent short-term,
long-term, and entity memory shared across runs, with per-type isolation and
safe, scoped resets.

## Install

```bash
pip install remembra crewai
```

## Wire it into a crew

`RemembraStorage` implements the storage interface CrewAI's memory classes
expect. Create one storage per memory type (they stay isolated from each
other even under the same user):

```python
from crewai import Crew
from crewai.memory import ShortTermMemory, LongTermMemory, EntityMemory
from remembra.integrations.crewai import RemembraStorage

def storage(mem_type: str) -> RemembraStorage:
    return RemembraStorage(
        base_url="https://api.remembra.dev",
        api_key="rem_...",          # omit for a local, auth-disabled server
        user_id="crew_user",
        type=mem_type,
    )

crew = Crew(
    agents=[...],
    tasks=[...],
    memory=True,
    short_term_memory=ShortTermMemory(storage=storage("short_term")),
    long_term_memory=LongTermMemory(storage=storage("long_term")),
    entity_memory=EntityMemory(storage=storage("entity")),
)
```

## How it behaves

- **Each memory item is stored atomically** (one item = one memory, never
  fact-split or merged). A task result, an entity description, and a
  short-term observation each stay distinct and recallable.
- **Types are isolated.** Short-term, long-term, and entity memories share the
  `(user_id, project)` namespace but are tagged and filtered by type — a
  short-term search never returns long-term or entity items.
- **`reset()` is scoped.** Resetting one storage deletes only *that* memory
  type; the crew's other memories and anything else under the user are
  untouched.
- **Short-term memory auto-expires** after 24h (TTL); long-term and entity
  memory persist until reset.

## Direct use

```python
from remembra.integrations.crewai import RemembraStorage

mem = RemembraStorage(base_url="https://api.remembra.dev", api_key="rem_...",
                      user_id="crew_user", type="long_term")

mem.save("Task: research competitors. Output: found 5 rivals with pricing")
results = mem.search("what did we learn about competitors?", limit=5)
for r in results:
    print(r["score"], r["context"])

mem.reset()   # clears only long-term memory
```

`search()` returns a list of `{"context", "metadata", "score"}` dicts. The
default relevance threshold is `0.35`; pass `score_threshold=` to tune it.
