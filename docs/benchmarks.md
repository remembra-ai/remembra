# Benchmarks

No valid benchmark result has been published yet.

In March 2026 we ran 1 of the 10 [LoCoMo](https://github.com/snap-research/locomo) conversations (199 questions) against Remembra Cloud. Its scores (76% overall, 100% outside the adversarial questions) are **not valid**: the judge counted every INCORRECT verdict as correct, and the run scored the raw recall context instead of a generated answer. Both bugs are fixed in `benchmarks/locomo_runner.py` (commit a19fb29). A fresh run over all 10 conversations has not been done yet; when it is, the results and the exact command will be published here.

---

## Run It Yourself

### LOCOMO Benchmark

[LoCoMo](https://github.com/snap-research/locomo) (Long Conversation Memory) is an academic benchmark from Snap Research (ACL 2024) that evaluates AI memory systems on 10 multi-session conversations with ~2,000 QA questions.

#### Quick Start

```bash
# 1. Clone the LOCOMO dataset
git clone https://github.com/snap-research/locomo.git /tmp/locomo

# 2. Install dependencies
pip install httpx openai nltk

# 3. Start Remembra
docker compose up -d

# 4. Run the benchmark (token F1 scoring — no API key needed)
python benchmarks/locomo_runner.py \
  --data /tmp/locomo/data/locomo10.json \
  --remembra-url http://localhost:8787

# 5. Or run with LLM judge (more accurate, ~$2 in API costs)
OPENAI_API_KEY=sk-... python benchmarks/locomo_runner.py \
  --data /tmp/locomo/data/locomo10.json \
  --scoring llm-judge \
  --judge-model gpt-4o-mini
```

### Question Categories

| Category | Type | What it tests |
|----------|------|---------------|
| 1 | Multi-hop | Synthesizing facts across multiple sessions |
| 2 | Single-hop | Direct fact retrieval from a single session |
| 3 | Temporal | Time-related reasoning |
| 4 | Open-domain | Combining conversation memory with world knowledge |
| 5 | Adversarial | Trick questions — answer is NOT in the conversation |

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
| `--recall-limit` | 10 | Memories retrieved per query |

### Scoring Methods

=== "Token F1 (Free)"

    ```bash
    python benchmarks/locomo_runner.py \
      --data /tmp/locomo/data/locomo10.json \
      --scoring f1
    ```
    
    The official LOCOMO scoring method. Computes token-level F1 between prediction and ground truth after normalization and Porter stemming. **Free, no API key needed.**

=== "LLM Judge (~$2)"

    ```bash
    OPENAI_API_KEY=sk-... python benchmarks/locomo_runner.py \
      --data /tmp/locomo/data/locomo10.json \
      --scoring llm-judge \
      --judge-model gpt-4o-mini
    ```
    
    Uses GPT-4o-mini to judge if the prediction is semantically correct. More lenient and accurate for natural language answers.

### Estimated Costs & Time

| Component | Cost | Time |
|-----------|------|------|
| Ingestion (embeddings) | $2-5 (OpenAI) or free (Ollama) | 10-30 min |
| Evaluation (recall) | Free | 5-15 min |
| LLM Judge scoring | ~$2 (gpt-4o-mini) | 5-10 min |
| **Total** | **$2-7** | **20-55 min** |

### Sample Output

The March 2026 run above (one conversation), as the runner printed it. These scores are **not valid** (see the top of this page); the output only shows the format:

```
======================================================================
  LOCOMO BENCHMARK RESULTS — Remembra
======================================================================
  Server:          https://api.remembra.dev
  Scoring:         llm-judge
  Judge Model:     gpt-4o-mini
  Conversations:   1
  Total Questions:  199
  Ingestion Time:  631.6s
  Evaluation Time: 263.8s
----------------------------------------------------------------------
  Category             Count   Accuracy    Avg Latency
----------------------------------------------------------------------
  multi-hop               32    100.00%       883.5ms
  single-hop              37    100.00%       885.0ms
  temporal                13    100.00%       870.4ms
  open-domain             70    100.00%       867.7ms
  adversarial             47      0.00%       883.4ms
----------------------------------------------------------------------
  OVERALL                199     76.38%
  OVERALL (excl adv)            100.00%
======================================================================
```

### Tips

- **First run**: Use `--max-conversations 1` to test with a single conversation before running the full benchmark
- **Re-run evaluation only**: Use `--skip-ingestion` to reuse already-ingested memories
- **Clean slate**: Use `--clean` to wipe memories from a previous run
- **Ollama embeddings**: Use Ollama as your embedding provider for free local inference

---

## Performance

We have not published load-test results. Latency and throughput depend on your hardware, your embedding provider and how many agents write at once (SQLite serializes writes). Measure your own deployment before relying on any numbers.
