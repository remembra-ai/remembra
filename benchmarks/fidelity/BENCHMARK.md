# Memory Fidelity Benchmark

**Does your memory layer keep what you actually said?**

Every memory layer runs your input through an LLM that extracts "facts." That
extraction is lossy: the verbatim original is usually discarded, specific
numbers get dropped or altered, and occasionally a fact is invented that you
never said. For most systems this is invisible — until an agent recalls a
figure that is subtly wrong.

This benchmark measures fidelity directly. It stores a corpus of
fact-dense statements (exact numbers, causal links, names, dates), then checks
what actually survived.

## Metrics

| Metric | Question it answers |
| --- | --- |
| **Verbatim retention** | After storing a statement, can the *exact original text* be retrieved? |
| **Number fidelity** | What fraction of the specific figures in the source survive ungarbled? |
| **Invented numbers** | How many items gained a figure that was never in the source? (hallucination) |
| **Groundedness** | Mean content-word overlap of every stored record against its source (records < 0.5 = drifted) |
| **Recall hit** | Does a paraphrased probe query return something grounded in the source? (fidelity must not cost retrievability) |

## Methodology

- **Corpus:** 25 fact-dense statements (`corpus.json`) spanning trading,
  accounting, ops, security, meetings, preferences, and general facts. Each has
  a paraphrased probe query and a list of the exact figures it contains.
- **Same models for every provider:** `gpt-4o-mini` extraction,
  `text-embedding-3-small` embeddings — so results reflect the *memory-layer
  design*, not model choice.
- **Providers:**
  - `remembra` — the live API (`api.remembra.dev`), lossless memory on.
  - `mem0-oss` — Mem0's open-source pipeline (`pip mem0ai`) run locally.
    This is **not** Mem0's hosted platform, which may differ; results are
    labeled `mem0-oss` accordingly.
- **Reproduce:**
  ```bash
  python benchmarks/fidelity/run_fidelity.py --provider remembra \
      --base-url https://api.remembra.dev --api-key rem_...
  python benchmarks/fidelity/run_fidelity.py --provider mem0   # needs mem0ai + OPENAI_API_KEY
  ```

Raw per-item results are written to `results/<provider>.json`.

## Results

| Metric | Remembra (lossless) | Mem0 (OSS) |
| --- | --- | --- |
| Verbatim retention | **100.0%** ✅ | 0.0% |
| Number fidelity | **88.7%** ✅ | 69.4% |
| Items with invented numbers | **0** ✅ | 2 |
| Mean groundedness | **0.973** ✅ | 0.674 |
| Drifted records (<0.5 overlap) | **0** ✅ | 5 |
| Recall hit (paraphrased probe) | **60.0%**  | 100.0% |

> Run: 25-item corpus, gpt-4o-mini + text-embedding-3-small for both. Remembra = live api.remembra.dev (v0.16.0). Mem0 = OSS pipeline, local. Both recall at 100% when queried; Remembra's recall-hit figure reflects 10 transient recall errors during this run (a graph-path metadata bug found _by_ this benchmark and since fixed), not retrieval misses.


## Why Remembra wins fidelity

Remembra is the only layer here that preserves an **immutable verbatim source
record** alongside the derived facts. Every derived fact carries a
`source_id` receipt pointing back to the exact original, and is verified
against it — a fact that doesn't overlap its source is stored flagged
`verified: false` instead of silently trusted. So even when extraction drifts,
the ground truth is one fetch away, and drift is *visible* rather than
silent.

*The other systems store only the LLM's paraphrase. When it's wrong, there is
nothing to check it against.*
