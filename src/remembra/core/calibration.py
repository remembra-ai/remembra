"""
Calibration cache for p99 latency metrics.

Persists calibration results to avoid re-measuring on every cold start.
Based on @aipracticalist feedback (2026-03).

Features:
- Caches p99 latency for recall, store operations
- Invalidates on config change (model, embedding dim, etc.)
- Runs calibration on reconnect/resume, not just startup
"""

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Default cache location
DEFAULT_CACHE_PATH = Path.home() / ".remembra" / "calibration.json"


@dataclass
class CalibrationResult:
    """Cached calibration metrics."""

    # Latency percentiles (in milliseconds)
    recall_p50_ms: float = 0.0
    recall_p95_ms: float = 0.0
    recall_p99_ms: float = 0.0
    store_p50_ms: float = 0.0
    store_p95_ms: float = 0.0
    store_p99_ms: float = 0.0

    # Throughput estimates
    max_concurrent_recall: int = 50
    max_concurrent_store: int = 20

    # Calibration metadata
    calibrated_at: str = ""
    config_hash: str = ""
    sample_count: int = 0
    is_valid: bool = False


@dataclass
class CalibrationConfig:
    """Configuration that affects calibration validity."""

    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    qdrant_collection: str = "memories"
    enable_hybrid: bool = True
    enable_reranking: bool = True

    def compute_hash(self) -> str:
        """Compute hash of config for invalidation detection."""
        config_str = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(config_str.encode()).hexdigest()[:16]


class CalibrationCache:
    """
    Manages calibration results persistence.

    Usage:
        cache = CalibrationCache()

        # Check if cached calibration is valid
        if cache.is_valid(current_config):
            metrics = cache.load()
        else:
            metrics = run_calibration()
            cache.save(metrics, current_config)
    """

    def __init__(self, cache_path: Path | str | None = None):
        """
        Initialize calibration cache.

        Args:
            cache_path: Path to cache file. Defaults to ~/.remembra/calibration.json
        """
        self.cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
        self._cache_enabled = self._ensure_cache_dir()

    def _ensure_cache_dir(self) -> bool:
        """Create cache directory if it doesn't exist.
        
        Returns:
            True if cache directory is writable, False otherwise.
        """
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            return True
        except (PermissionError, OSError) as e:
            log.warning(
                "calibration_cache_disabled",
                error=str(e),
                path=str(self.cache_path),
            )
            return False

    def load(self) -> CalibrationResult | None:
        """
        Load cached calibration result.

        Returns:
            CalibrationResult if cache exists, None otherwise.
        """
        if not self._cache_enabled:
            log.debug("calibration_cache_miss", reason="cache_disabled")
            return None
            
        if not self.cache_path.exists():
            log.debug("calibration_cache_miss", reason="file_not_found")
            return None

        try:
            with open(self.cache_path) as f:
                data = json.load(f)

            result = CalibrationResult(**data)
            log.debug(
                "calibration_cache_loaded",
                calibrated_at=result.calibrated_at,
                recall_p99_ms=result.recall_p99_ms,
            )
            return result

        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.warning("calibration_cache_corrupt", error=str(e))
            return None

    def save(self, result: CalibrationResult, config: CalibrationConfig) -> None:
        """
        Save calibration result to cache.

        Args:
            result: Calibration metrics to cache
            config: Current config (used for invalidation hash)
        """
        if not self._cache_enabled:
            log.debug("calibration_cache_skip_save", reason="cache_disabled")
            return
            
        result.calibrated_at = datetime.utcnow().isoformat()
        result.config_hash = config.compute_hash()
        result.is_valid = True

        try:
            with open(self.cache_path, "w") as f:
                json.dump(asdict(result), f, indent=2)

            log.info(
                "calibration_cache_saved",
                recall_p99_ms=result.recall_p99_ms,
                store_p99_ms=result.store_p99_ms,
                config_hash=result.config_hash,
            )
        except OSError as e:
            log.error("calibration_cache_save_failed", error=str(e))

    def is_valid(
        self,
        config: CalibrationConfig,
        max_age: timedelta = timedelta(hours=24),
    ) -> bool:
        """
        Check if cached calibration is valid for current config.

        Args:
            config: Current configuration to compare against
            max_age: Maximum age before cache is considered stale

        Returns:
            True if cache is valid and matches current config
        """
        result = self.load()
        if not result:
            return False

        # Check config hash match
        current_hash = config.compute_hash()
        if result.config_hash != current_hash:
            log.debug(
                "calibration_cache_invalidated",
                reason="config_changed",
                cached_hash=result.config_hash,
                current_hash=current_hash,
            )
            return False

        # Check age
        if result.calibrated_at:
            try:
                calibrated_time = datetime.fromisoformat(result.calibrated_at)
                age = datetime.utcnow() - calibrated_time
                if age > max_age:
                    log.debug(
                        "calibration_cache_invalidated",
                        reason="too_old",
                        age_hours=age.total_seconds() / 3600,
                    )
                    return False
            except ValueError:
                pass

        return result.is_valid

    def invalidate(self) -> None:
        """Force invalidate the cache."""
        if self.cache_path.exists():
            self.cache_path.unlink()
            log.info("calibration_cache_invalidated", reason="manual")


class LatencyCollector:
    """
    Collects latency samples for calibration.

    Usage:
        collector = LatencyCollector()

        # Record samples
        with collector.measure("recall"):
            do_recall()

        # Get percentiles
        result = collector.compute_calibration()
    """

    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = {
            "recall": [],
            "store": [],
        }

    def record(self, operation: str, latency_ms: float) -> None:
        """Record a latency sample."""
        if operation in self._samples:
            self._samples[operation].append(latency_ms)

    class _Timer:
        """Context manager for timing operations."""

        def __init__(self, collector: "LatencyCollector", operation: str):
            self.collector = collector
            self.operation = operation
            self.start: float = 0.0

        def __enter__(self) -> "_Timer":
            self.start = time.perf_counter()
            return self

        def __exit__(self, *args: Any) -> None:
            elapsed_ms = (time.perf_counter() - self.start) * 1000
            self.collector.record(self.operation, elapsed_ms)

    def measure(self, operation: str) -> _Timer:
        """
        Context manager for measuring operation latency.

        Usage:
            with collector.measure("recall"):
                result = memory_service.recall(request)
        """
        return self._Timer(self, operation)

    def compute_calibration(self) -> CalibrationResult:
        """
        Compute calibration result from collected samples.

        Returns:
            CalibrationResult with computed percentiles
        """
        import statistics

        def percentile(data: list[float], p: float) -> float:
            """Compute percentile (p between 0 and 100)."""
            if not data:
                return 0.0
            sorted_data = sorted(data)
            k = (len(sorted_data) - 1) * p / 100
            f = int(k)
            c = f + 1 if f + 1 < len(sorted_data) else f
            return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)

        recall_samples = self._samples["recall"]
        store_samples = self._samples["store"]

        return CalibrationResult(
            recall_p50_ms=percentile(recall_samples, 50),
            recall_p95_ms=percentile(recall_samples, 95),
            recall_p99_ms=percentile(recall_samples, 99),
            store_p50_ms=percentile(store_samples, 50),
            store_p95_ms=percentile(store_samples, 95),
            store_p99_ms=percentile(store_samples, 99),
            sample_count=len(recall_samples) + len(store_samples),
        )

    def reset(self) -> None:
        """Clear all collected samples."""
        for key in self._samples:
            self._samples[key] = []


# Global calibration cache instance
_calibration_cache: CalibrationCache | None = None


def get_calibration_cache() -> CalibrationCache:
    """Get or create the global calibration cache instance."""
    global _calibration_cache
    if _calibration_cache is None:
        _calibration_cache = CalibrationCache()
    return _calibration_cache


async def run_calibration(
    memory_service: Any,
    num_samples: int = 10,
    test_queries: list[str] | None = None,
) -> CalibrationResult:
    """
    Run calibration by measuring actual operation latencies.

    Args:
        memory_service: MemoryService instance
        num_samples: Number of samples per operation
        test_queries: Test queries for recall (uses defaults if not provided)

    Returns:
        CalibrationResult with measured percentiles
    """
    from remembra.models.memory import RecallRequest

    collector = LatencyCollector()

    # Default test queries
    queries = test_queries or [
        "recent events",
        "important facts",
        "project status",
        "decisions made",
        "people mentioned",
    ]

    log.info("calibration_starting", num_samples=num_samples)

    # Measure recall latency
    for i in range(num_samples):
        query = queries[i % len(queries)]
        request = RecallRequest(query=query, limit=5)

        with collector.measure("recall"):
            try:
                await memory_service.recall(request)
            except Exception as e:
                log.debug("calibration_recall_error", error=str(e))

    result = collector.compute_calibration()
    log.info(
        "calibration_complete",
        recall_p99_ms=result.recall_p99_ms,
        sample_count=result.sample_count,
    )

    return result
