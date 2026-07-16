"""Memory Fidelity Benchmark — does your memory layer keep what you actually said?

Measures, per provider:

- **verbatim retention** — after storing a statement, can the EXACT original
  text be retrieved? (whitespace-normalized equality)
- **number fidelity** — do the specific figures in the statement survive
  ungarbled in at least one stored record?
- **invented numbers** — figures appearing in stored records that were never
  in the source (the most dangerous hallucination class).
- **groundedness** — mean content-word overlap of every stored record against
  its source; records under 0.5 are counted as drifted.
- **recall sanity** — a paraphrased probe query returns something grounded in
  the source (fidelity must not cost retrievability).

Providers are pluggable adapters. Included: Remembra (any live server) and
Mem0 OSS (local, same OpenAI models — see class docstring for fairness notes).
Zep/Supermemory adapters can be added when hosted API keys are available.

Usage:
    python benchmarks/fidelity/run_fidelity.py --provider remembra \
        --base-url https://api.remembra.dev --api-key rem_...
    python benchmarks/fidelity/run_fidelity.py --provider mem0   # needs mem0ai + OPENAI_API_KEY

Stdlib-only for the runner + Remembra adapter; the Mem0 adapter lazily
imports `mem0`.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
WORD_RE = re.compile(r"[a-z0-9:.]+")
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    return " ".join(text.split()).strip().lower().rstrip(".")


def overlap(fact: str, source_text: str) -> float:
    """Content-word overlap — mirrors remembra.services.memory._fact_source_overlap."""
    source_words = set(re.findall(r"[a-z0-9]+", source_text.lower()))
    fact_words = [w for w in re.findall(r"[a-z0-9]+", fact.lower()) if len(w) >= 3 or w.isdigit()]
    if not fact_words:
        return 1.0
    return sum(1 for w in fact_words if w in source_words) / len(fact_words)


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUM_RE.findall(text)}


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class RemembraAdapter:
    """Talks to any live Remembra server over REST (stdlib only)."""

    name = "remembra"

    def __init__(self, base_url: str, api_key: str, project: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.project = project or f"fidelity-{uuid.uuid4().hex[:8]}"
        self.stored_ids: list[str] = []

    def _req(self, method: str, path: str, body: dict | None = None, retries: int = 4) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(retries):
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={
                    "X-API-Key": self.key,
                    "Content-Type": "application/json",
                    # Cloudflare's browser integrity check 403s (error 1010)
                    # the default Python-urllib user agent on GET requests.
                    "User-Agent": "remembra-fidelity-bench/1.0",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode(errors="replace")[:300]
                except Exception:
                    pass
                if e.code in (429, 403, 502, 503) and attempt < retries - 1:
                    # 429: provider rate limit. 403: usually an edge/WAF
                    # false-positive on specific content — back off and retry.
                    print(f"    [retry {attempt+1}] {e.code} on {method} {path}: {err_body[:120]}")
                    time.sleep(6 * (attempt + 1))
                    continue
                raise RuntimeError(f"{e.code} on {method} {path}: {err_body}") from e
        return None

    def store(self, item_id: str, content: str) -> None:
        resp = self._req("POST", "/api/v1/memories", {
            "content": content,
            "project_id": self.project,
            "metadata": {"bench_item": item_id},
        })
        for key in ("id", "source_id"):
            if resp.get(key):
                self.stored_ids.append(resp[key])
        time.sleep(1.2)  # stay under the 30/min store limit

    def records_by_item(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        offset = 0
        while True:
            page = self._req("GET", f"/api/v1/memories?project_id={self.project}&limit=100&offset={offset}") or []
            for m in page:
                self.stored_ids.append(m["id"])  # ensures cleanup even on resumed runs
                item = (m.get("metadata") or {}).get("bench_item")
                if item:
                    out.setdefault(item, []).append(m["content"])
            if len(page) < 100:
                break
            offset += 100
        return out

    def recall(self, probe: str, limit: int = 5) -> list[str]:
        resp = self._req("POST", "/api/v1/memories/recall", {
            "query": probe, "project_id": self.project, "limit": limit,
        })
        items = resp if isinstance(resp, list) else (resp.get("memories") or resp.get("results") or [])
        return [m.get("content", "") for m in items]

    def cleanup(self) -> None:
        for mid in set(self.stored_ids):
            try:
                self._req("DELETE", f"/api/v1/memories/{mid}", retries=2)
                time.sleep(0.3)
            except Exception:
                pass


class Mem0Adapter:
    """Mem0 OSS (pip `mem0ai`) run locally with the same model class Remembra
    uses (gpt-4o-mini extraction, text-embedding-3-small embeddings) so the
    comparison isolates the memory-layer design, not the models.

    Fairness note: this benchmarks the open-source pipeline, not Mem0's hosted
    platform, which may differ. Results are labeled `mem0-oss` accordingly.
    """

    name = "mem0-oss"

    def __init__(self, data_dir: str) -> None:
        from mem0 import Memory  # lazy import

        self.user = "fidelity-bench"
        self.memory = Memory.from_config({
            "vector_store": {
                "provider": "qdrant",
                "config": {"path": data_dir, "collection_name": "fidelity", "on_disk": True},
            },
            "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
            "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
        })

    def store(self, item_id: str, content: str) -> None:
        self.memory.add(content, user_id=self.user, metadata={"bench_item": item_id})

    def records_by_item(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        # mem0 >= 2.0 requires entity params via filters=
        res = self.memory.get_all(filters={"user_id": self.user}, limit=1000)
        rows = res.get("results", res) if isinstance(res, dict) else res
        for m in rows:
            item = (m.get("metadata") or {}).get("bench_item")
            if item:
                out.setdefault(item, []).append(m.get("memory") or m.get("text") or "")
        return out

    def recall(self, probe: str, limit: int = 5) -> list[str]:
        res = self.memory.search(probe, filters={"user_id": self.user}, limit=limit)
        rows = res.get("results", res) if isinstance(res, dict) else res
        return [m.get("memory") or m.get("text") or "" for m in rows]

    def cleanup(self) -> None:
        try:
            self.memory.delete_all(user_id=self.user)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def evaluate(adapter: Any, items: list[dict], keep: bool = False, skip_store: bool = False) -> dict:
    t0 = time.time()
    skipped: list[str] = []
    if skip_store:
        print(f"[{adapter.name}] resuming — scoring already-stored data")
    else:
        print(f"[{adapter.name}] storing {len(items)} items...")
        for it in items:
            try:
                adapter.store(it["id"], it["content"])
                print(f"  stored {it['id']}")
            except Exception as e:
                # One blocked/failed item (e.g. an edge-WAF false positive) must
                # not kill the whole run — score it as skipped and continue.
                skipped.append(it["id"])
                print(f"  SKIPPED {it['id']}: {e}")
        items = [it for it in items if it["id"] not in skipped]
    store_secs = time.time() - t0

    print(f"[{adapter.name}] collecting stored records...")
    time.sleep(3)  # let any background enrichment land
    records = adapter.records_by_item()

    per_item: list[dict] = []
    for it in items:
        source_text = it["content"]
        recs = records.get(it["id"], [])
        rec_norms = {normalize(r) for r in recs}
        verbatim = normalize(source_text) in rec_norms

        expected = {n.replace(",", "") for n in it["numbers"]}
        found_nums: set[str] = set()
        for r in recs:
            found_nums |= numbers_in(r)
        nums_kept = expected & found_nums
        invented = {n for n in found_nums - numbers_in(source_text) if len(n) > 1}

        overlaps = [overlap(r, source_text) for r in recs] or [0.0]
        drifted = sum(1 for o in overlaps if o < 0.5)

        try:
            hits = adapter.recall(it["probe"])
            recall_hit = any(overlap(h, source_text) >= 0.4 for h in hits if h)
            recall_error = False
        except Exception as e:
            # Recall is a sanity metric, not the fidelity core — a failing
            # query must not void the whole run. Record it and move on.
            print(f"    recall error on {it['id']}: {e}")
            recall_hit = False
            recall_error = True

        per_item.append({
            "id": it["id"],
            "records": len(recs),
            "verbatim_retained": verbatim,
            "numbers_expected": len(expected),
            "numbers_kept": len(nums_kept),
            "numbers_invented": sorted(invented),
            "mean_groundedness": round(sum(overlaps) / len(overlaps), 3),
            "drifted_records": drifted,
            "recall_hit": recall_hit,
            "recall_error": recall_error,
        })
        print(f"  scored {it['id']}: verbatim={verbatim} nums={len(nums_kept)}/{len(expected)} invented={sorted(invented)}")

    n = len(per_item)
    num_expected = sum(p["numbers_expected"] for p in per_item) or 1
    summary = {
        "provider": adapter.name,
        "items": n,
        "verbatim_retention_pct": round(100 * sum(p["verbatim_retained"] for p in per_item) / n, 1),
        "number_fidelity_pct": round(100 * sum(p["numbers_kept"] for p in per_item) / num_expected, 1),
        "items_with_invented_numbers": sum(1 for p in per_item if p["numbers_invented"]),
        "mean_groundedness": round(sum(p["mean_groundedness"] for p in per_item) / n, 3),
        "total_drifted_records": sum(p["drifted_records"] for p in per_item),
        "recall_hit_pct": round(100 * sum(p["recall_hit"] for p in per_item) / n, 1),
        "recall_errors": sum(p.get("recall_error", False) for p in per_item),
        "store_seconds_total": round(store_secs, 1),
        "skipped_items": skipped,
    }

    if not keep:
        print(f"[{adapter.name}] cleaning up...")
        adapter.cleanup()

    return {"summary": summary, "per_item": per_item}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["remembra", "mem0"])
    ap.add_argument("--base-url", default="https://api.remembra.dev")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--data-dir", default="/tmp/mem0-fidelity")
    ap.add_argument("--keep", action="store_true", help="skip cleanup")
    ap.add_argument("--project", default="", help="reuse an existing remembra project (resume)")
    ap.add_argument("--skip-store", action="store_true", help="score existing data without re-storing")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    corpus = json.loads((HERE / "corpus.json").read_text())
    items = corpus["items"]

    if args.provider == "remembra":
        adapter: Any = RemembraAdapter(args.base_url, args.api_key, project=args.project or None)
    else:
        adapter = Mem0Adapter(args.data_dir)

    result = evaluate(adapter, items, keep=args.keep, skip_store=args.skip_store)
    result["corpus_version"] = corpus["version"]

    out = Path(args.out) if args.out else HERE / "results" / f"{adapter.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result["summary"], indent=2))
    print(f"written: {out}")


if __name__ == "__main__":
    main()
