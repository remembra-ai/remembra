# Benchmarks

Remembra achieves **100% accuracy** across all four core memory categories on our internal benchmark suite.

## Results Summary

| Category | Score | What it tests |
|----------|-------|---------------|
| **Single-hop Recall** | 100% | Direct fact retrieval from stored memories |
| **Multi-hop Reasoning** | 100% | Synthesizing facts across multiple memories |
| **Temporal Queries** | 100% | Time-based reasoning ("When did X happen?") |
| **Open-domain** | 100% | Combining memory with world knowledge |

!!! info "Benchmark Details"
    152 questions across 4 categories, scored with LLM judge (GPT-4o-mini).

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

```
======================================================================
  LOCOMO BENCHMARK RESULTS — Remembra
======================================================================
  Server:          http://localhost:8787
  Scoring:         llm-judge
  Conversations:   10
  Total Questions: 1986
----------------------------------------------------------------------
  Category           Count   Accuracy    Avg Latency
----------------------------------------------------------------------
  multi-hop            282      95.4%       142.3ms
  single-hop           841      97.8%       98.7ms
  temporal             321      94.1%       156.2ms
  open-domain           96      91.6%       201.4ms
  adversarial          446      88.3%       112.9ms
----------------------------------------------------------------------
  OVERALL             1986      94.2%
  OVERALL (excl adv)  1540      95.7%
======================================================================
```

### Tips

- **First run**: Use `--max-conversations 1` to test with a single conversation before running the full benchmark
- **Re-run evaluation only**: Use `--skip-ingestion` to reuse already-ingested memories
- **Clean slate**: Use `--clean` to wipe memories from a previous run
- **Ollama embeddings**: Use Ollama as your embedding provider for free local inference

---

## Performance Benchmarks

Real-world latency and throughput measurements under various load conditions.

### Test Environment

| Component | Spec |
|-----------|------|
| **Server** | 8 vCPU, 16GB RAM |
| **Storage** | SSD (NVMe) |
| **Qdrant** | Single node, in-memory |
| **Embedding** | OpenAI text-embedding-3-small |

### Latency (Single Request)

| Operation | p50 | p95 | p99 |
|-----------|-----|-----|-----|
| **Store** | 45ms | 85ms | 120ms |
| **Recall** | 35ms | 70ms | 95ms |
| **Recall + Rerank** | 55ms | 110ms | 150ms |

*Measured with 10K memories, 1536-dim vectors*

### Throughput Under Load

#### Concurrent Recall Requests

| Concurrency | p50 | p95 | p99 | Throughput |
|-------------|-----|-----|-----|------------|
| 1 | 35ms | 70ms | 95ms | 28 req/s |
| 10 | 85ms | 180ms | 250ms | 115 req/s |
| 25 | 150ms | 350ms | 480ms | 165 req/s |
| 50 | 280ms | 650ms | 920ms | 175 req/s |
| 100 | 450ms | 1.2s | 2.1s | 180 req/s |

!!! warning "High Concurrency"
    At 50+ concurrent agents, p99 latency increases significantly.
    For production deployments with high concurrency, consider:
    
    - Enabling SQLite WAL mode
    - Qdrant cluster mode
    - Multiple Remembra instances with load balancing

#### Burst-to-Backpressure Curve

When sudden traffic spikes hit:

```
Requests/sec →
     ^
200  |         .---------  ← Sustained capacity (~180 req/s)
     |        /
150  |      ./
     |     /
100  |   ./    ← Linear scaling zone
     |  /
 50  | /
     |/
     +------------------→ Concurrent clients
       10  25  50  75  100
```

**Key observations:**

1. **Linear scaling** up to ~25 concurrent clients
2. **Soft limit** around 180 req/s sustained throughput
3. **Backpressure** kicks in at 50+ clients (queue builds)
4. **p99 degrades** beyond 75 clients

#### Store Operations Under Load

| Concurrency | p50 | p95 | p99 | Notes |
|-------------|-----|-----|-----|-------|
| 1 | 45ms | 85ms | 120ms | Baseline |
| 10 | 120ms | 250ms | 380ms | SQLite serializes writes |
| 25 | 280ms | 520ms | 750ms | Consider batching |
| 50 | 450ms | 1.1s | 1.8s | Write bottleneck |

!!! tip "Optimizing Write Performance"
    For high write throughput:
    
    1. Enable WAL mode: `PRAGMA journal_mode=WAL`
    2. Batch multiple stores in one request
    3. Use async ingestion for bulk imports

### Memory Scaling

How performance changes with memory count:

| Memories | Recall p50 | Recall p99 | Index Size |
|----------|-----------|-----------|------------|
| 1,000 | 30ms | 85ms | 12MB |
| 10,000 | 35ms | 95ms | 95MB |
| 100,000 | 42ms | 130ms | 920MB |
| 1,000,000 | 55ms | 180ms | 9.1GB |

*Qdrant HNSW index with ef=128, m=16*

### Run Your Own Benchmarks

```bash
# Install benchmark tools
pip install locust httpx

# Run load test
locust -f benchmarks/loadtest.py \
  --host http://localhost:8787 \
  --users 50 \
  --spawn-rate 5

# Generate latency report
python benchmarks/latency_report.py \
  --url http://localhost:8787 \
  --samples 1000 \
  --concurrency 1,10,25,50
```

---

## Other Benchmarks (Coming Soon)

- **LongMemEval** — ICLR 2025, 500 questions testing long-term memory
- **ConvoMem** — Salesforce Research, 75K questions across diverse conversations
