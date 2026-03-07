#!/usr/bin/env python3
"""
LOCOMO Benchmark Runner for Remembra
=====================================

Runs the LoCoMo (Long Conversation Memory) benchmark against a Remembra server.

This script:
1. Loads the LOCOMO dataset (10 conversations from locomo10.json)
2. Ingests each conversation into Remembra via the REST API
3. Runs all QA questions against Remembra's recall endpoint
4. Scores answers using the official LOCOMO evaluation methodology
5. Outputs per-category and overall accuracy

Requirements:
    pip install httpx openai nltk

Usage:
    # 1. Clone the LOCOMO dataset
    git clone https://github.com/snap-research/locomo.git /tmp/locomo

    # 2. Start Remembra server
    docker compose up -d

    # 3. Run the benchmark
    python benchmarks/locomo_runner.py \
        --data /tmp/locomo/data/locomo10.json \
        --remembra-url http://localhost:8787 \
        --judge-model gpt-4o-mini \
        --output benchmarks/results.json

Category mapping (from LOCOMO code, NOT the paper):
    1 = Multi-hop      (synthesize info across multiple sessions)
    2 = Single-hop     (direct fact retrieval from one session)
    3 = Temporal        (time-related reasoning)
    4 = Open-domain     (requires world knowledge)
    5 = Adversarial     (trick questions; answer is unanswerable)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import string
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# LOCOMO category mapping (from the evaluation.py source code)
# ---------------------------------------------------------------------------
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "single-hop",
    3: "temporal",
    4: "open-domain",
    5: "adversarial",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class QAResult:
    """Result for a single QA evaluation."""

    conversation_id: str
    question: str
    ground_truth: str
    prediction: str
    category: int
    category_name: str
    score: float
    evidence: list[str]
    recall_context: str
    memories_used: int
    latency_ms: float


@dataclass
class CategoryStats:
    """Aggregated stats for one question category."""

    category: int
    category_name: str
    count: int = 0
    total_score: float = 0.0
    accuracy: float = 0.0
    avg_latency_ms: float = 0.0


@dataclass
class BenchmarkResult:
    """Full benchmark result."""

    timestamp: str = ""
    remembra_url: str = ""
    judge_model: str = ""
    scoring_method: str = ""
    conversations_ingested: int = 0
    total_questions: int = 0
    overall_accuracy: float = 0.0
    overall_accuracy_excl_adversarial: float = 0.0
    ingestion_time_s: float = 0.0
    evaluation_time_s: float = 0.0
    categories: dict[str, dict] = field(default_factory=dict)
    per_question: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LOCOMO-official scoring functions (ported from evaluation.py)
# ---------------------------------------------------------------------------


def _normalize_answer(text: str) -> str:
    """Normalize answer text following LOCOMO's methodology."""
    # Remove commas
    text = text.replace(",", "")
    # Lowercase
    text = text.lower()
    # Remove punctuation
    text = text.translate(str.maketrans("", "", string.punctuation))
    # Remove articles and conjunctions
    stop_words = {"a", "an", "the", "and"}
    tokens = text.split()
    tokens = [t for t in tokens if t not in stop_words]
    # Collapse whitespace
    return " ".join(tokens)


def _stem_tokens(tokens: list[str]) -> list[str]:
    """Apply Porter stemming to tokens."""
    try:
        from nltk.stem import PorterStemmer

        stemmer = PorterStemmer()
        return [stemmer.stem(t) for t in tokens]
    except ImportError:
        # Fallback: no stemming
        return tokens


def _f1_score(prediction: str, ground_truth: str) -> float:
    """Compute token-level F1 score (official LOCOMO method)."""
    pred_normalized = _normalize_answer(prediction)
    gt_normalized = _normalize_answer(ground_truth)

    pred_tokens = _stem_tokens(pred_normalized.split())
    gt_tokens = _stem_tokens(gt_normalized.split())

    if not pred_tokens or not gt_tokens:
        return float(pred_normalized == gt_normalized)

    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_common = sum(common.values())

    if num_common == 0:
        return 0.0

    precision = num_common / len(pred_tokens)
    recall = num_common / len(gt_tokens)

    return (2 * precision * recall) / (precision + recall)


def eval_question(prediction: str, ground_truth: str, category: int) -> float:
    """Evaluate a single QA pair using category-specific logic."""
    if category == 5:
        # Adversarial: binary check for refusal phrases
        pred_lower = prediction.lower()
        refusal_phrases = [
            "no information available",
            "not mentioned",
            "no information",
            "not available",
            "cannot determine",
            "no evidence",
            "not enough information",
            "insufficient information",
            "don't have information",
            "do not have information",
            "no relevant",
            "unable to determine",
            "not specified",
            "no data",
            "unknown",
        ]
        return 1.0 if any(phrase in pred_lower for phrase in refusal_phrases) else 0.0

    if category == 3:
        # Temporal: only use text before first semicolon in ground truth
        ground_truth = ground_truth.split(";")[0].strip()

    if category == 1:
        # Multi-hop: split ground truth by commas, compute mean of max F1 per sub-answer
        sub_answers = [a.strip() for a in ground_truth.split(",") if a.strip()]
        if not sub_answers:
            return _f1_score(prediction, ground_truth)
        scores = [_f1_score(prediction, sub) for sub in sub_answers]
        return sum(scores) / len(scores)

    # Categories 2, 4: standard F1
    return _f1_score(prediction, ground_truth)


# ---------------------------------------------------------------------------
# LLM Judge scoring (alternative to token F1)
# ---------------------------------------------------------------------------


def _llm_judge_score(
    question: str,
    ground_truth: str,
    prediction: str,
    category: int,
    client: "openai.OpenAI",
    model: str,
) -> float:
    """Use an LLM to judge if the prediction correctly answers the question."""
    if category == 5:
        # For adversarial, use the standard refusal-phrase check
        return eval_question(prediction, ground_truth, category)

    prompt = f"""You are an evaluation judge for a memory benchmark.

Given a question, the ground truth answer, and a model's prediction, determine if the prediction is CORRECT or INCORRECT.

A prediction is CORRECT if it conveys the same meaning as the ground truth, even if worded differently.
A prediction is INCORRECT if it gives wrong information, is missing key facts, or is irrelevant.

Question: {question}
Ground Truth: {ground_truth}
Prediction: {prediction}

Respond with ONLY one word: CORRECT or INCORRECT"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=10,
        )
        verdict = response.choices[0].message.content.strip().upper()
        return 1.0 if "CORRECT" in verdict else 0.0
    except Exception as e:
        print(f"  [judge error] {e} — falling back to F1")
        return eval_question(prediction, ground_truth, category)


# ---------------------------------------------------------------------------
# Remembra API client
# ---------------------------------------------------------------------------


class RemembraClient:
    """Thin wrapper around Remembra REST API for benchmarking."""

    def __init__(self, base_url: str, api_key: str | None = None, project: str = "locomo-bench"):
        self.base_url = base_url.rstrip("/")
        self.project = project
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.Client(timeout=120.0, headers=headers)

    def health(self) -> dict:
        r = self._client.get(f"{self.base_url}/health")
        r.raise_for_status()
        return r.json()

    def store(self, content: str, metadata: dict | None = None) -> dict:
        payload = {
            "content": content,
            "project_id": self.project,
        }
        if metadata:
            payload["metadata"] = metadata
        r = self._client.post(f"{self.base_url}/api/v1/memories", json=payload)
        r.raise_for_status()
        return r.json()

    def ingest_conversation(
        self,
        messages: list[dict],
        session_id: str | None = None,
    ) -> dict:
        payload = {
            "messages": messages,
            "project_id": self.project,
            "options": {
                "extract_from": "both",
                "min_importance": 0.3,
                "dedupe": True,
                "store": True,
                "infer": True,
            },
        }
        if session_id:
            payload["session_id"] = session_id
        r = self._client.post(f"{self.base_url}/api/v1/ingest/conversation", json=payload)
        r.raise_for_status()
        return r.json()

    def recall(self, query: str, limit: int = 10, threshold: float = 0.3) -> dict:
        payload = {
            "query": query,
            "project_id": self.project,
            "limit": limit,
            "threshold": threshold,
        }
        r = self._client.post(f"{self.base_url}/api/v1/memories/recall", json=payload)
        r.raise_for_status()
        return r.json()

    def forget_all(self) -> dict:
        r = self._client.delete(
            f"{self.base_url}/api/v1/memories",
            params={"all_memories": "true"},
        )
        r.raise_for_status()
        return r.json()

    def close(self):
        self._client.close()


# ---------------------------------------------------------------------------
# LOCOMO data loading
# ---------------------------------------------------------------------------


def load_locomo(path: str) -> list[dict]:
    """Load the LOCOMO dataset from JSON."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"Loaded {len(data)} conversations from {path}")
    return data


def extract_sessions(conversation: dict) -> list[tuple[str, str, list[dict]]]:
    """Extract ordered sessions from a conversation object.

    Returns list of (session_key, session_datetime, turns).
    """
    conv_data = conversation.get("conversation", conversation)

    # Find all session keys
    session_keys = sorted(
        [k for k in conv_data if re.match(r"^session_\d+$", k)],
        key=lambda k: int(k.split("_")[1]),
    )

    sessions = []
    for key in session_keys:
        dt_key = f"{key}_date_time"
        dt_str = conv_data.get(dt_key, "")
        turns = conv_data[key]
        if isinstance(turns, list) and turns:
            sessions.append((key, dt_str, turns))

    return sessions


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_conversation(
    client: RemembraClient,
    conv: dict,
    conv_index: int,
    batch_size: int = 20,
    use_batch_store: bool = False,
) -> dict:
    """Ingest a single LOCOMO conversation into Remembra.

    Two strategies:
    1. ingest_conversation API — sends messages in batches per session
    2. batch store — stores each turn as an individual memory (simpler but less intelligent)
    """
    conv_id = conv.get("sample_id", f"conv-{conv_index}")
    conv_data = conv.get("conversation", conv)
    speaker_a = conv_data.get("speaker_a", "Speaker A")
    speaker_b = conv_data.get("speaker_b", "Speaker B")

    sessions = extract_sessions(conv)
    stats = {
        "conversation_id": conv_id,
        "sessions": len(sessions),
        "turns_ingested": 0,
        "facts_extracted": 0,
        "facts_stored": 0,
        "entities_found": 0,
        "errors": [],
    }

    for session_key, session_dt, turns in sessions:
        # Build messages array for this session
        messages = []
        for turn in turns:
            speaker = turn.get("speaker", "unknown")
            text = turn.get("text", "")
            if not text:
                continue

            # Map speaker to user/assistant roles for the conversation ingest API
            role = "user" if speaker == speaker_a else "assistant"

            msg = {
                "role": role,
                "content": text,
                "name": speaker,
            }
            if session_dt:
                msg["timestamp"] = session_dt

            messages.append(msg)

        if not messages:
            continue

        if use_batch_store:
            # Strategy 2: Store each turn as an individual memory
            for msg in messages:
                try:
                    content = f"[{msg.get('name', 'unknown')}]: {msg['content']}"
                    metadata = {
                        "conversation_id": conv_id,
                        "session": session_key,
                        "speaker": msg.get("name", "unknown"),
                    }
                    if session_dt:
                        metadata["timestamp"] = session_dt
                    client.store(content, metadata=metadata)
                    stats["turns_ingested"] += 1
                except Exception as e:
                    stats["errors"].append(f"{session_key}: {e}")
        else:
            # Strategy 1: Use conversation ingest API (batched)
            for i in range(0, len(messages), batch_size):
                batch = messages[i : i + batch_size]
                try:
                    result = client.ingest_conversation(
                        messages=batch,
                        session_id=f"{conv_id}_{session_key}",
                    )
                    s = result.get("stats", {})
                    stats["turns_ingested"] += s.get("messages_processed", len(batch))
                    stats["facts_extracted"] += s.get("facts_extracted", 0)
                    stats["facts_stored"] += s.get("facts_stored", 0)
                    stats["entities_found"] += s.get("entities_found", 0)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        # Rate limited — wait and retry once
                        print(f"    Rate limited on {session_key}, waiting 5s...")
                        time.sleep(5)
                        try:
                            result = client.ingest_conversation(
                                messages=batch,
                                session_id=f"{conv_id}_{session_key}",
                            )
                            s = result.get("stats", {})
                            stats["turns_ingested"] += s.get("messages_processed", len(batch))
                            stats["facts_extracted"] += s.get("facts_extracted", 0)
                            stats["facts_stored"] += s.get("facts_stored", 0)
                        except Exception as e2:
                            stats["errors"].append(f"{session_key} batch {i}: {e2}")
                    else:
                        stats["errors"].append(f"{session_key} batch {i}: {e}")
                except Exception as e:
                    stats["errors"].append(f"{session_key} batch {i}: {e}")

            # Small delay between sessions to avoid rate limits
            time.sleep(0.5)

    return stats


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_conversation(
    client: RemembraClient,
    conv: dict,
    conv_index: int,
    scoring: str = "f1",
    judge_client: "openai.OpenAI | None" = None,
    judge_model: str = "gpt-4o-mini",
    recall_limit: int = 10,
) -> list[QAResult]:
    """Evaluate all QA questions for one conversation."""
    conv_id = conv.get("sample_id", f"conv-{conv_index}")
    qa_pairs = conv.get("qa", [])

    if not qa_pairs:
        print(f"  No QA pairs for {conv_id}")
        return []

    results = []
    for i, qa in enumerate(qa_pairs):
        question = qa.get("question", "")
        ground_truth = qa.get("answer", "")
        category = qa.get("category", 0)
        evidence = qa.get("evidence", [])

        if not question:
            continue

        # Query Remembra
        t0 = time.time()
        try:
            recall_result = client.recall(question, limit=recall_limit)
            context = recall_result.get("context", "")
            memories = recall_result.get("memories", [])
        except Exception as e:
            context = ""
            memories = []
            print(f"  [recall error] Q{i+1}: {e}")
        latency_ms = (time.time() - t0) * 1000

        # Use the synthesized context as the model's "prediction"
        prediction = context

        # Score
        if scoring == "llm-judge" and judge_client is not None:
            score = _llm_judge_score(
                question, ground_truth, prediction, category, judge_client, judge_model
            )
        else:
            score = eval_question(prediction, ground_truth, category)

        results.append(
            QAResult(
                conversation_id=conv_id,
                question=question,
                ground_truth=ground_truth,
                prediction=prediction,
                category=category,
                category_name=CATEGORY_NAMES.get(category, f"unknown-{category}"),
                score=score,
                evidence=evidence,
                recall_context=context[:500],  # Truncate for output
                memories_used=len(memories),
                latency_ms=round(latency_ms, 1),
            )
        )

        # Rate limit protection
        if (i + 1) % 20 == 0:
            time.sleep(1)

    return results


# ---------------------------------------------------------------------------
# Aggregation & reporting
# ---------------------------------------------------------------------------


def aggregate_results(all_results: list[QAResult]) -> BenchmarkResult:
    """Aggregate per-question results into category stats."""
    result = BenchmarkResult(
        timestamp=datetime.utcnow().isoformat() + "Z",
        total_questions=len(all_results),
    )

    # Group by category
    by_category: dict[int, list[QAResult]] = defaultdict(list)
    for r in all_results:
        by_category[r.category].append(r)

    total_score = 0.0
    total_count = 0
    total_score_excl_adv = 0.0
    total_count_excl_adv = 0

    for cat_id in sorted(by_category.keys()):
        cat_results = by_category[cat_id]
        cat_name = CATEGORY_NAMES.get(cat_id, f"unknown-{cat_id}")
        count = len(cat_results)
        score_sum = sum(r.score for r in cat_results)
        accuracy = score_sum / count if count > 0 else 0.0
        avg_latency = sum(r.latency_ms for r in cat_results) / count if count > 0 else 0.0

        result.categories[cat_name] = {
            "category_id": cat_id,
            "count": count,
            "accuracy": round(accuracy * 100, 2),
            "avg_latency_ms": round(avg_latency, 1),
        }

        total_score += score_sum
        total_count += count

        if cat_id != 5:
            total_score_excl_adv += score_sum
            total_count_excl_adv += count

    result.overall_accuracy = round(
        (total_score / total_count * 100) if total_count > 0 else 0.0, 2
    )
    result.overall_accuracy_excl_adversarial = round(
        (total_score_excl_adv / total_count_excl_adv * 100) if total_count_excl_adv > 0 else 0.0,
        2,
    )

    result.per_question = [asdict(r) for r in all_results]

    return result


def print_results(result: BenchmarkResult) -> None:
    """Print a formatted results table."""
    print("\n" + "=" * 70)
    print("  LOCOMO BENCHMARK RESULTS — Remembra")
    print("=" * 70)
    print(f"  Server:          {result.remembra_url}")
    print(f"  Scoring:         {result.scoring_method}")
    if result.judge_model and result.scoring_method == "llm-judge":
        print(f"  Judge Model:     {result.judge_model}")
    print(f"  Conversations:   {result.conversations_ingested}")
    print(f"  Total Questions:  {result.total_questions}")
    print(f"  Ingestion Time:  {result.ingestion_time_s:.1f}s")
    print(f"  Evaluation Time: {result.evaluation_time_s:.1f}s")
    print("-" * 70)
    print(f"  {'Category':<18} {'Count':>7} {'Accuracy':>10} {'Avg Latency':>14}")
    print("-" * 70)

    for cat_name, stats in result.categories.items():
        print(
            f"  {cat_name:<18} {stats['count']:>7} {stats['accuracy']:>9.2f}% {stats['avg_latency_ms']:>11.1f}ms"
        )

    print("-" * 70)
    print(f"  {'OVERALL':<18} {result.total_questions:>7} {result.overall_accuracy:>9.2f}%")
    print(
        f"  {'OVERALL (excl adv)':<18} {'':>7} {result.overall_accuracy_excl_adversarial:>9.2f}%"
    )
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Run the LOCOMO benchmark against a Remembra server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic run with token F1 scoring (no OpenAI key needed for scoring)
  python benchmarks/locomo_runner.py \\
    --data /tmp/locomo/data/locomo10.json \\
    --remembra-url http://localhost:8787

  # Run with LLM judge scoring (more accurate, costs ~$2)
  python benchmarks/locomo_runner.py \\
    --data /tmp/locomo/data/locomo10.json \\
    --remembra-url http://localhost:8787 \\
    --scoring llm-judge \\
    --judge-model gpt-4o-mini

  # Run a subset (first 2 conversations)
  python benchmarks/locomo_runner.py \\
    --data /tmp/locomo/data/locomo10.json \\
    --max-conversations 2

  # Skip adversarial questions
  python benchmarks/locomo_runner.py \\
    --data /tmp/locomo/data/locomo10.json \\
    --skip-adversarial
        """,
    )

    parser.add_argument(
        "--data",
        type=str,
        required=True,
        help="Path to locomo10.json",
    )
    parser.add_argument(
        "--remembra-url",
        type=str,
        default=os.getenv("REMEMBRA_URL", "http://localhost:8787"),
        help="Remembra server URL (default: $REMEMBRA_URL or http://localhost:8787)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("REMEMBRA_API_KEY"),
        help="Remembra API key (default: $REMEMBRA_API_KEY)",
    )
    parser.add_argument(
        "--project",
        type=str,
        default="locomo-bench",
        help="Remembra project ID for memory isolation (default: locomo-bench)",
    )
    parser.add_argument(
        "--scoring",
        choices=["f1", "llm-judge"],
        default="f1",
        help="Scoring method: 'f1' (LOCOMO-official token F1) or 'llm-judge' (GPT-4 judge)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default="gpt-4o-mini",
        help="Model for LLM judge scoring (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to save results JSON (default: benchmarks/results_<timestamp>.json)",
    )
    parser.add_argument(
        "--max-conversations",
        type=int,
        default=None,
        help="Limit to first N conversations (useful for testing)",
    )
    parser.add_argument(
        "--skip-adversarial",
        action="store_true",
        help="Skip category 5 (adversarial) questions",
    )
    parser.add_argument(
        "--skip-ingestion",
        action="store_true",
        help="Skip ingestion (use existing memories from a prior run)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete all existing memories in the project before ingestion",
    )
    parser.add_argument(
        "--batch-store",
        action="store_true",
        help="Use individual store API instead of conversation ingest API",
    )
    parser.add_argument(
        "--recall-limit",
        type=int,
        default=10,
        help="Number of memories to retrieve per recall query (default: 10)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Number of messages per ingest batch (default: 20)",
    )

    args = parser.parse_args()

    # Validate data file
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"Error: Data file not found: {data_path}")
        print("\nTo get the LOCOMO dataset:")
        print("  git clone https://github.com/snap-research/locomo.git /tmp/locomo")
        print("  # Then use --data /tmp/locomo/data/locomo10.json")
        sys.exit(1)

    # Initialize Remembra client
    client = RemembraClient(
        base_url=args.remembra_url,
        api_key=args.api_key,
        project=args.project,
    )

    # Health check
    try:
        health = client.health()
        print(f"Remembra server: {args.remembra_url} (healthy)")
    except Exception as e:
        print(f"Error: Cannot connect to Remembra at {args.remembra_url}")
        print(f"  {e}")
        print("\nMake sure the server is running:")
        print("  docker compose up -d")
        sys.exit(1)

    # Initialize LLM judge if needed
    judge_client = None
    if args.scoring == "llm-judge":
        try:
            import openai

            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                print("Error: --scoring llm-judge requires OPENAI_API_KEY environment variable")
                sys.exit(1)
            judge_client = openai.OpenAI(api_key=api_key)
            print(f"LLM Judge: {args.judge_model}")
        except ImportError:
            print("Error: --scoring llm-judge requires the openai package")
            print("  pip install openai")
            sys.exit(1)

    # Try to import nltk for stemming
    try:
        import nltk

        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt", quiet=True)
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError:
            try:
                nltk.download("punkt_tab", quiet=True)
            except Exception:
                pass
        print("NLTK stemmer: available")
    except ImportError:
        print("NLTK stemmer: not available (install nltk for better F1 scoring)")

    # Load dataset
    conversations = load_locomo(str(data_path))

    if args.max_conversations:
        conversations = conversations[: args.max_conversations]
        print(f"Using first {len(conversations)} conversations")

    # Clean existing memories if requested
    if args.clean and not args.skip_ingestion:
        print("\nCleaning existing memories...")
        try:
            result = client.forget_all()
            print(f"  Deleted {result.get('deleted_memories', '?')} memories")
        except Exception as e:
            print(f"  Warning: Clean failed: {e}")

    # -----------------------------------------------------------------------
    # Phase 1: Ingestion
    # -----------------------------------------------------------------------
    ingestion_stats = []

    if not args.skip_ingestion:
        print(f"\n{'='*50}")
        print("PHASE 1: INGESTION")
        print(f"{'='*50}")

        t_ingest_start = time.time()

        for i, conv in enumerate(conversations):
            conv_id = conv.get("sample_id", f"conv-{i}")
            sessions = extract_sessions(conv)
            total_turns = sum(len(turns) for _, _, turns in sessions)
            print(f"\n[{i+1}/{len(conversations)}] {conv_id} — {len(sessions)} sessions, ~{total_turns} turns")

            stats = ingest_conversation(
                client,
                conv,
                i,
                batch_size=args.batch_size,
                use_batch_store=args.batch_store,
            )
            ingestion_stats.append(stats)

            print(
                f"  Ingested: {stats['turns_ingested']} turns, "
                f"{stats['facts_extracted']} facts extracted, "
                f"{stats['facts_stored']} stored, "
                f"{stats['entities_found']} entities"
            )
            if stats["errors"]:
                print(f"  Errors: {len(stats['errors'])}")
                for err in stats["errors"][:3]:
                    print(f"    - {err}")

        ingestion_time = time.time() - t_ingest_start
        print(f"\nIngestion complete: {ingestion_time:.1f}s")
    else:
        print("\nSkipping ingestion (--skip-ingestion)")
        ingestion_time = 0.0

    # Give Remembra a moment to index
    if not args.skip_ingestion:
        print("Waiting 3s for indexing...")
        time.sleep(3)

    # -----------------------------------------------------------------------
    # Phase 2: Evaluation
    # -----------------------------------------------------------------------
    print(f"\n{'='*50}")
    print("PHASE 2: EVALUATION")
    print(f"{'='*50}")

    t_eval_start = time.time()
    all_results: list[QAResult] = []

    for i, conv in enumerate(conversations):
        conv_id = conv.get("sample_id", f"conv-{i}")
        qa_count = len(conv.get("qa", []))

        if args.skip_adversarial:
            qa_count = sum(1 for q in conv.get("qa", []) if q.get("category") != 5)

        print(f"\n[{i+1}/{len(conversations)}] {conv_id} — {qa_count} questions")

        # Filter adversarial if requested
        if args.skip_adversarial:
            original_qa = conv.get("qa", [])
            conv = dict(conv)
            conv["qa"] = [q for q in original_qa if q.get("category") != 5]

        results = evaluate_conversation(
            client,
            conv,
            i,
            scoring=args.scoring,
            judge_client=judge_client,
            judge_model=args.judge_model,
            recall_limit=args.recall_limit,
        )
        all_results.extend(results)

        # Print running stats for this conversation
        if results:
            conv_score = sum(r.score for r in results) / len(results)
            print(f"  Score: {conv_score*100:.1f}%  ({len(results)} questions)")

    evaluation_time = time.time() - t_eval_start
    print(f"\nEvaluation complete: {evaluation_time:.1f}s")

    # -----------------------------------------------------------------------
    # Phase 3: Results
    # -----------------------------------------------------------------------
    benchmark = aggregate_results(all_results)
    benchmark.remembra_url = args.remembra_url
    benchmark.judge_model = args.judge_model
    benchmark.scoring_method = args.scoring
    benchmark.conversations_ingested = len(conversations)
    benchmark.ingestion_time_s = round(ingestion_time, 1)
    benchmark.evaluation_time_s = round(evaluation_time, 1)

    print_results(benchmark)

    # Save results
    output_path = args.output
    if not output_path:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = f"benchmarks/results_{ts}.json"

    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save without per-question details for the summary file
    summary = {k: v for k, v in asdict(benchmark).items() if k != "per_question"}
    summary_path = output_path.replace(".json", "_summary.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(asdict(benchmark), f, indent=2, default=str)
    print(f"\nFull results saved to: {output_path}")

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Summary saved to: {summary_path}")

    # Cleanup
    client.close()

    return benchmark


if __name__ == "__main__":
    main()
