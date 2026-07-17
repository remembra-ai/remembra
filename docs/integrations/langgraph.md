# LangGraph

Back a [LangGraph](https://langchain-ai.github.io/langgraph/) agent's
**long-term memory** with Remembra. `RemembraStore` implements LangGraph's
`BaseStore` interface, so `put` / `get` / `search` / `delete` /
`list_namespaces` persist to Remembra with namespace scoping and semantic
search — memories that survive across threads, runs, and restarts.

## Install

```bash
pip install remembra langgraph
```

## Use it

Pass a `RemembraStore` when compiling your graph; tools and nodes receive it
as `store`:

```python
from langgraph.graph import StateGraph
from remembra.integrations.langgraph import RemembraStore

store = RemembraStore(
    base_url="https://api.remembra.dev",
    api_key="rem_...",            # omit for a local, auth-disabled server
    user_id="user_42",
)

# Store a durable memory under a namespace
store.put(("users", "alice", "memories"), "food", {"note": "loves margherita pizza"})

# Exact fetch
item = store.get(("users", "alice", "memories"), "food")

# Semantic search within a namespace prefix
hits = store.search(("users", "alice"), query="what does she like to eat?")
for h in hits:
    print(h.key, h.score, h.value)

graph = builder.compile(store=store)
```

Inside a graph node or tool:

```python
def remember(state, *, store):
    store.put(("users", state["user_id"]), "pref", {"note": state["preference"]})
    return state
```

## How it behaves

`RemembraStore` implements the full `BaseStore` op set (via `batch`/`abatch`):

| Op | Behavior |
| --- | --- |
| `put(namespace, key, value)` | Upsert — one memory per `(namespace, key)`, stored atomically. Re-putting a key replaces it (never duplicates). |
| `get(namespace, key)` | Exact fetch, returns an `Item` (or `None`). |
| `search(prefix, query=…, filter=…)` | Semantic search scoped to a namespace **prefix**; `filter` matches fields in the stored value. |
| `delete(namespace, key)` | Removes just that entry. |
| `list_namespaces()` | Distinct namespaces in the store. |
| `abatch` / async | Supported (the sync client is wrapped off the event loop). |

- **Namespace prefix search** works by matching each prefix level, so
  `search(("users", "alice"))` never returns items under `("users", "bob")`.
- **Store items are isolated** from the user's other Remembra memories — a
  search here never leaks unrelated memories.
- **Caps:** `search` and `list_namespaces` return up to 50 entries per call
  (the recall page size). For very large stores, narrow the namespace prefix.
- The `index` argument (per-field embedding control) isn't honored — all values
  are made searchable.
