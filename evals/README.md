# Recall-quality eval

A small labeled test set that runs through the real store and recall pipeline
and reports how often the right memory comes back. It exists so a ranking or
retrieval change can be measured before it ships, and so CI fails when quality
drops.

```bash
python -m evals.recall_eval                      # keyless floor (hashed vectors)
python -m evals.recall_eval --embedder keyword-only   # embedding provider down
python -m evals.recall_eval --embedder openai    # real embeddings, needs OPENAI_API_KEY
python -m evals.recall_eval --mode strategic     # force a ranking preset
python -m evals.recall_eval --json out.json      # per-query results
```

## What runs

`MemoryService`, SQLite with FTS5 and Qdrant (qdrant-client's in-process
engine) are the shipped code. Facts are stored atomically, so this measures
retrieval and ranking, not LLM fact extraction. The cross-encoder reranker is
off (it needs the `rerank` extra).

| Tier | Embedder | Key | Gated in CI |
|---|---|---|---|
| `lexical-hash` | hashed bag-of-words, no semantics | no | yes |
| `keyword-only` | provider down for the whole run | no | yes |
| `openai` | `text-embedding-3-small` | yes | no |

The two keyless tiers are floors. They prove the pipeline around the vector
search (scoping, supersession, expiry, `as_of`, ranking) and keyword recall.
Only a real-embedding run says how well paraphrased questions are answered.

## Corpus

`corpus/memories.jsonl` holds 78 memories across two projects, each with an
age in days. `corpus/queries.jsonl` holds 59 queries with the memories that
answer them:

| Category | Checks |
|---|---|
| `single` | one fact, wording shared with the question |
| `paraphrase` | one fact, little or no shared wording |
| `multi` | two or three facts answer the question |
| `update_explicit` | a fact was replaced with `supersede`; the old one must not come back |
| `update_implicit` | an older and a newer fact are both stored; the newer should rank first |
| `as_of` | the question is asked as of a past date; the fact that held then must rank first |
| `expired` | the only matching memory has expired |
| `isolation` | the other project holds a closer match; it must not come back |
| `abstain` | nothing stored answers the question |

## Metrics

- `recall_at_5`, `hit_at_1`, `mrr`, `ndcg_at_5`: over the queries that have an answer.
- `forbidden_leak_rate`: share of queries that returned a superseded, expired or other-project memory. Must be 0.
- `stale_first_rate`: share of `update_implicit` queries where the out-of-date fact ranked above its replacement.
- `empty_when_unanswerable_rate`: share of unanswerable queries that returned nothing.

## The gate

`tests/test_recall_eval_gate.py` runs both keyless tiers and fails when a
gated metric falls more than 0.03 below `baseline.json`, when anything leaks,
or when an `as_of` query does not put the then-current fact first. After an
intended change, run `python -m evals.recall_eval --update-baseline` and
commit the new baseline in the same change.

## Adding cases

Add a line to `corpus/memories.jsonl` (`id`, `project`, `days_ago`, `content`,
optional `supersedes` or `expired_days_ago`) and to `corpus/queries.jsonl`
(`id`, `category`, `project`, `query`, `relevant`, optional `forbidden`,
`stale`, `as_of_days_ago`), then update the baseline. A replaced memory must
come after the one it supersedes.
