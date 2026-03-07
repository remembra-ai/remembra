# Remembra Benchmarks

Benchmark Remembra against academic memory evaluation datasets.

## LOCOMO Benchmark

[LoCoMo](https://github.com/snap-research/locomo) (Long Conversation Memory) is an academic benchmark from Snap Research (ACL 2024) that evaluates AI memory systems on 10 multi-session conversations with ~2,000 QA questions across 5 categories.

### Question Categories

| Category | Type | What it tests |
|----------|------|---------------|
| 1 | Multi-hop | Synthesizing facts across multiple sessions |
| 2 | Single-hop | Direct fact retrieval from a single session |
| 3 | Temporal | Time-related reasoning ("When did X happen?") |
| 4 | Open-domain | Combining conversation memory with world knowledge |
| 5 | Adversarial | Trick questions — the answer is NOT in the conversation |

### Quick Start

```bash
# 1. Clone the LOCOMO dataset
git clone https://github.com/snap-research/locomo.git /tmp/locomo

# 2. Install dependencies
pip install httpx openai nltk

# 3. Start Remembra
docker compose up -d

# 4. Run the benchmark (token F1 scoring — no API key needed for scoring)
python benchmarks/locomo_runner.py \
  --data /tmp/locomo/data/locomo10.json \
  --remembra-url http://localhost:8787

# 5. Run with LLM judge (more accurate, ~$2 in API costs)
OPENAI_API_KEY=sk-... python benchmarks/locomo_runner.py \
  --data /tmp/locomo/data/locomo10.json \
  --scoring llm-judge \
  --judge-model gpt-4o-mini
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--data` | (required) | Path to `locomo10.json` |
| `--remembra-url` | `http://localhost:8787` | Remembra server URL |
| `--api-key` | `$REMEMBRA_API_KEY` | API key (if auth is enabled) |
| `--project` | `locomo-bench` | Project ID for memory isolation |
| `--scoring` | `f1` | `f1` (token F1) or `llm-judge` (GPT-4 judge) |
| `--judge-model` | `gpt-4o-mini` | Model for LLM judge scoring |
| `--output` | `benchmarks/results_<ts>.json` | Output file path |
| `--max-conversations` | all | Limit to first N conversations |
| `--skip-adversarial` | off | Skip category 5 questions |
| `--skip-ingestion` | off | Skip ingestion (reuse existing memories) |
| `--clean` | off | Delete all memories before ingestion |
| `--batch-store` | off | Use individual store API instead of ingest |
| `--recall-limit` | 10 | Memories retrieved per query |
| `--batch-size` | 20 | Messages per ingest batch |

### Scoring Methods

**Token F1** (`--scoring f1`): The official LOCOMO scoring method. Computes token-level F1 between prediction and ground truth after normalization and Porter stemming. Free, no API key needed for scoring. Strict — semantically correct answers with different wording score lower.

**LLM Judge** (`--scoring llm-judge`): Uses GPT-4o-mini to judge if the prediction is semantically correct. More lenient and accurate for natural language answers. Costs ~$2 for the full benchmark. Requires `OPENAI_API_KEY`.

### Estimated Costs & Time

| Component | Cost | Time |
|-----------|------|------|
| Ingestion (embeddings) | $2-5 (OpenAI) or free (Ollama) | 10-30 min |
| Evaluation (recall) | Free (no LLM needed) | 5-15 min |
| LLM Judge scoring | ~$2 (gpt-4o-mini) | 5-10 min |
| **Total** | **$2-7** | **20-55 min** |

### Sample Output

```
======================================================================
  LOCOMO BENCHMARK RESULTS — Remembra
======================================================================
  Server:          http://localhost:8787
  Scoring:         f1
  Conversations:   10
  Total Questions:  1986
  Ingestion Time:  847.2s
  Evaluation Time: 412.5s
----------------------------------------------------------------------
  Category           Count   Accuracy    Avg Latency
----------------------------------------------------------------------
  multi-hop            282      XX.XX%       XXX.Xms
  single-hop           841      XX.XX%       XXX.Xms
  temporal             321      XX.XX%       XXX.Xms
  open-domain           96      XX.XX%       XXX.Xms
  adversarial          446      XX.XX%       XXX.Xms
----------------------------------------------------------------------
  OVERALL             1986      XX.XX%
  OVERALL (excl adv)            XX.XX%
======================================================================
```

### Tips

- **First run**: Use `--max-conversations 1` to test with a single conversation before running the full benchmark.
- **Re-run evaluation only**: Use `--skip-ingestion` to reuse already-ingested memories.
- **Clean slate**: Use `--clean` to wipe memories from a previous run.
- **Rate limits**: The runner handles 429s automatically with backoff. If you hit persistent rate limits, increase `--batch-size` or add delays.
- **Ollama embeddings**: Use Ollama as your embedding provider for free local inference.

### Other Benchmarks (Coming Soon)

- **LongMemEval** — ICLR 2025, 500 questions testing long-term memory
- **ConvoMem** — Salesforce Research, 75K questions across diverse conversations
