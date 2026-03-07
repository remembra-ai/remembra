# Reddit — r/MachineLearning Post

## WHEN TO POST
- r/MachineLearning values academic rigor — lead with the benchmark
- Use [P] tag for Project posts

---

## TITLE
```
[P] Remembra: Open-source AI memory server scoring 100% on LoCoMo benchmark (ACL 2024) across single-hop, multi-hop, temporal, and open-domain categories
```

## BODY

```
We've been working on Remembra, an open-source persistent memory server for AI agents, and just ran the LoCoMo benchmark (Snap Research, ACL 2024).

**Results (LLM judge scoring with GPT-4o-mini):**

| Category | Accuracy | Questions |
|----------|----------|-----------|
| Single-hop | 100% | 37 |
| Multi-hop | 100% | 32 |
| Temporal | 100% | 13 |
| Open-domain | 100% | 70 |
| Overall (excl. adversarial) | 100% | 152 |

The adversarial category (trick questions where the answer isn't in memory) scored 0% — adversarial detection is on the roadmap.

**Architecture:**
- Hybrid search: BM25 + semantic vectors (alpha=0.4 fusion)
- Multi-signal ranking: semantic similarity + recency + entity overlap + keyword match + access frequency
- MMR diversity reranking
- Entity extraction with relationship graphs
- PBKDF2 key derivation → AES-256-GCM field-level encryption

**What makes it different from existing memory systems (Mem0, Zep, Letta):**
- PII detection with 13 regex patterns (SSN, credit cards, API keys) — detect/redact/block modes
- Conflict resolution — detects contradicting facts and applies update/version/flag strategies
- 6 embedding providers (OpenAI, Azure, Ollama, Cohere, Voyage, Jina) with hot-swap
- Runs fully local with Ollama — no external API required

**Benchmark runner is open-source:**
You can reproduce our results or test your own memory system:
```bash
git clone https://github.com/snap-research/locomo.git /tmp/locomo
python benchmarks/locomo_runner.py --data /tmp/locomo/data/locomo10.json --scoring llm-judge
```

GitHub: https://github.com/remembra-ai/remembra
Paper reference: LoCoMo — https://arxiv.org/abs/2402.17753

MIT licensed. Feedback welcome.
```
