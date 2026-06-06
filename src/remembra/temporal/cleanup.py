"""
Background cleanup job for expired and decayed memories.

This module provides scheduled tasks for:
1. Hard TTL expiration (delete memories past expires_at)
2. Soft decay pruning (mark/archive low-relevance memories)
3. Adaptive threshold calibration
4. Cold archive management
5. Cleanup metrics and logging
"""

import asyncio
import hashlib
from datetime import datetime
from typing import Any

import structlog

from remembra.core.time import utcnow
from remembra.temporal.adaptive import AdaptiveThresholdManager
from remembra.temporal.decay import (
    DEFAULT_CONFIG,
    DecayConfig,
    calculate_memory_decay_info,
)

log = structlog.get_logger(__name__)


class TemporalCleanupJob:
    """
    Background job for memory cleanup based on TTL and decay.

    Features:
    - Hard delete expired memories (TTL)
    - Soft prune decayed memories (mark for review or archive)
    - Configurable thresholds and intervals
    - Metrics tracking
    """

    def __init__(
        self,
        database: Any,  # Database instance
        qdrant_store: Any,  # QdrantStore instance
        config: DecayConfig | None = None,
        auto_delete_expired: bool = True,
        auto_prune_decayed: bool = False,  # Conservative default
        prune_to_archive: bool = True,  # Archive instead of delete
        adaptive_manager: AdaptiveThresholdManager | None = None,
        use_adaptive_thresholds: bool = True,  # Use adaptive when available
    ) -> None:
        """
        Initialize cleanup job.

        Args:
            database: SQLite database instance
            qdrant_store: Qdrant vector store instance
            config: Decay configuration
            auto_delete_expired: Auto-delete memories past TTL
            auto_prune_decayed: Auto-prune decayed memories (careful!)
            prune_to_archive: Archive decayed memories instead of deleting
            adaptive_manager: Adaptive threshold manager for dynamic thresholds
            use_adaptive_thresholds: Whether to use adaptive thresholds
        """
        self.db = database
        self.qdrant = qdrant_store
        self.config = config or DEFAULT_CONFIG
        self.auto_delete_expired = auto_delete_expired
        self.adaptive_manager = adaptive_manager
        self.use_adaptive_thresholds = use_adaptive_thresholds
        self.auto_prune_decayed = auto_prune_decayed
        self.prune_to_archive = prune_to_archive

        # Metrics
        self._last_run: datetime | None = None
        self._total_expired_deleted: int = 0
        self._total_decayed_pruned: int = 0
        self._run_count: int = 0

    async def run_cleanup(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Run a single cleanup cycle.

        Args:
            user_id: Filter to specific user (optional)
            project_id: Filter to specific project (optional)
            dry_run: If True, don't actually delete anything

        Returns:
            Dict with cleanup statistics
        """
        self._run_count += 1
        start_time = utcnow()

        # Track errors separately for proper typing
        errors: list[str] = []
        expired_found = 0
        expired_deleted = 0
        decayed_found = 0
        decayed_pruned = 0
        decayed_archived = 0

        try:
            # 1. Handle TTL-expired memories
            if self.auto_delete_expired:
                expired_result = await self._cleanup_expired(
                    user_id=user_id,
                    project_id=project_id,
                    dry_run=dry_run,
                )
                expired_found = expired_result["found"]
                expired_deleted = expired_result["deleted"]
                errors.extend(expired_result.get("errors", []))

            # 2. Handle decayed memories (soft prune)
            if self.auto_prune_decayed:
                decayed_result = await self._cleanup_decayed(
                    user_id=user_id,
                    project_id=project_id,
                    dry_run=dry_run,
                )
                decayed_found = decayed_result["found"]
                decayed_pruned = decayed_result["pruned"]
                decayed_archived = decayed_result.get("archived", 0)
                errors.extend(decayed_result.get("errors", []))

            # Update metrics
            self._last_run = start_time
            self._total_expired_deleted += expired_deleted
            self._total_decayed_pruned += decayed_pruned

            log.info(
                "cleanup_completed",
                expired_deleted=expired_deleted,
                decayed_pruned=decayed_pruned,
                duration_ms=int((utcnow() - start_time).total_seconds() * 1000),
                dry_run=dry_run,
            )

        except Exception as e:
            log.error("cleanup_failed", error=str(e))
            errors.append(str(e))

        return {
            "run_id": self._run_count,
            "started_at": start_time.isoformat(),
            "dry_run": dry_run,
            "expired_found": expired_found,
            "expired_deleted": expired_deleted,
            "decayed_found": decayed_found,
            "decayed_pruned": decayed_pruned,
            "decayed_archived": decayed_archived,
            "errors": errors,
            "completed_at": utcnow().isoformat(),
            "duration_ms": int((utcnow() - start_time).total_seconds() * 1000),
        }

    async def _cleanup_expired(
        self,
        user_id: str | None,
        project_id: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Delete memories past their TTL expiration."""
        found = 0
        deleted = 0
        errors: list[str] = []

        try:
            # Get expired memory IDs
            expired_ids = await self.db.get_expired_memories(
                user_id=user_id,
                project_id=project_id or "default",
            )
            found = len(expired_ids)

            if dry_run:
                log.info("dry_run_expired", count=len(expired_ids), ids=expired_ids[:10])
                return {"found": found, "deleted": deleted, "errors": errors}

            # Delete each expired memory
            for memory_id in expired_ids:
                try:
                    # Delete from SQLite
                    await self.db.delete_memory(memory_id)

                    # Delete from Qdrant
                    if self.qdrant:
                        await self.qdrant.delete(memory_id)

                    # Delete from FTS index
                    await self.db.delete_memory_fts(memory_id)

                    deleted += 1

                except Exception as e:
                    log.warning("delete_expired_failed", memory_id=memory_id, error=str(e))
                    errors.append(f"Failed to delete {memory_id}: {e}")

            log.info("expired_cleanup_done", found=found, deleted=deleted)

        except Exception as e:
            log.error("expired_cleanup_error", error=str(e))
            errors.append(str(e))

        return {"found": found, "deleted": deleted, "errors": errors}

    async def _cleanup_decayed(
        self,
        user_id: str | None,
        project_id: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Handle memories that have decayed below threshold.

        Uses adaptive thresholds when available for smarter pruning
        based on session context and user behavior.
        """
        found = 0
        pruned = 0
        archived = 0
        errors: list[str] = []

        try:
            # Get all memories with decay info
            memories = await self.db.get_memories_with_decay_info(
                user_id=user_id or "",  # Empty string gets all if not specified
                project_id=project_id or "default",
                limit=1000,  # Process in batches
            )

            # Determine prune threshold
            # Use adaptive threshold if available, otherwise static config
            prune_threshold = self.config.prune_threshold
            if self.use_adaptive_thresholds and self.adaptive_manager and user_id:
                adaptive_threshold = self.adaptive_manager.calculate_threshold(
                    user_id=user_id,
                    project_id=project_id or "default",
                    memory_count=len(memories),
                )
                prune_threshold = adaptive_threshold
                log.debug(
                    "using_adaptive_threshold",
                    user_id=user_id,
                    static_threshold=self.config.prune_threshold,
                    adaptive_threshold=adaptive_threshold,
                )

            # Calculate decay and find candidates for pruning
            prune_candidates: list[dict[str, Any]] = []
            for memory in memories:
                # Pinned memories are protected from decay pruning entirely.
                if memory.get("pinned"):
                    continue
                decay_info = calculate_memory_decay_info(memory, self.config)
                # Use our adaptive threshold instead of the static should_prune
                relevance = decay_info["relevance_score"]
                is_below_threshold = relevance < prune_threshold
                if is_below_threshold and not decay_info["is_expired"]:
                    # Decayed but not TTL-expired
                    prune_candidates.append(
                        {
                            "id": memory["id"],
                            "relevance": relevance,
                            "days_since_access": decay_info["days_since_access"],
                            "threshold_used": prune_threshold,
                        }
                    )

            found = len(prune_candidates)

            if dry_run:
                log.info(
                    "dry_run_decayed",
                    count=len(prune_candidates),
                    samples=prune_candidates[:5],
                )
                return {"found": found, "pruned": pruned, "archived": archived, "errors": errors}

            # Handle decayed memories
            for candidate in prune_candidates:
                try:
                    memory_id = candidate["id"]

                    if self.prune_to_archive:
                        # Archive instead of delete (future: move to cold storage)
                        # For now, just mark as archived in metadata
                        await self._archive_memory(memory_id)
                        archived += 1
                    else:
                        # Hard delete
                        await self.db.delete_memory(memory_id)
                        if self.qdrant:
                            await self.qdrant.delete(memory_id)
                        await self.db.delete_memory_fts(memory_id)
                        pruned += 1

                except Exception as e:
                    log.warning("prune_decayed_failed", memory_id=memory_id, error=str(e))
                    errors.append(f"Failed to prune {memory_id}: {e}")

            log.info(
                "decayed_cleanup_done",
                found=found,
                pruned=pruned,
                archived=archived,
            )

        except Exception as e:
            log.error("decayed_cleanup_error", error=str(e))
            errors.append(str(e))

        return {"found": found, "pruned": pruned, "archived": archived, "errors": errors}

    async def _archive_memory(self, memory_id: str, reason: str = "decay_threshold") -> None:
        """
        Archive a memory to cold storage.

        Moves the memory to archived_memories table, removing it from
        active storage but keeping it queryable in the archive tier.
        """
        # Calculate final relevance score before archiving
        memory = await self.db.get_memory(memory_id)
        if not memory:
            return

        decay_info = calculate_memory_decay_info(memory, self.config)
        final_relevance = decay_info["relevance_score"]

        # Move to cold archive
        success = await self.db.archive_memory(
            memory_id=memory_id,
            reason=reason,
            final_relevance=final_relevance,
        )

        if success:
            # Also remove from Qdrant (archived memories don't get vector search)
            if self.qdrant:
                try:
                    await self.qdrant.delete(memory_id)
                except Exception as e:
                    log.warning("qdrant_archive_delete_failed", memory_id=memory_id, error=str(e))

            # Remove from FTS index
            try:
                await self.db.delete_memory_fts(memory_id)
            except Exception as e:
                log.warning("fts_archive_delete_failed", memory_id=memory_id, error=str(e))

            log.debug("memory_archived_to_cold_storage", memory_id=memory_id, reason=reason)

    async def get_decay_report(
        self,
        user_id: str,
        project_id: str = "default",
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Generate a decay report for a user's memories.

        Returns statistics and list of memories with decay info.
        """
        memories = await self.db.get_memories_with_decay_info(
            user_id=user_id,
            project_id=project_id,
            limit=limit,
        )

        memory_reports = []
        prune_candidates = 0
        avg_relevance = 0.0

        for memory in memories:
            decay_info = calculate_memory_decay_info(memory, self.config)
            # SECURITY: Never log content, only hash for correlation
            content = memory.get("content", "")
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16] if content else ""
            memory_reports.append(
                {
                    "id": memory["id"],
                    "content_hash": content_hash,  # Safe hash instead of content preview
                    "content_length": len(content),
                    **decay_info,
                }
            )

            avg_relevance += decay_info["relevance_score"]
            if decay_info["should_prune"]:
                prune_candidates += 1

        total = len(memories)
        avg_relevance = avg_relevance / total if total > 0 else 0

        return {
            "user_id": user_id,
            "project_id": project_id,
            "total_memories": total,
            "prune_candidates": prune_candidates,
            "average_relevance": round(avg_relevance, 4),
            "config": {
                "prune_threshold": self.config.prune_threshold,
                "base_decay_rate": self.config.base_decay_rate,
            },
            "memories": memory_reports,
        }

    def get_metrics(self) -> dict[str, Any]:
        """Get cleanup job metrics."""
        return {
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "run_count": self._run_count,
            "total_expired_deleted": self._total_expired_deleted,
            "total_decayed_pruned": self._total_decayed_pruned,
            "config": {
                "auto_delete_expired": self.auto_delete_expired,
                "auto_prune_decayed": self.auto_prune_decayed,
                "prune_to_archive": self.prune_to_archive,
                "prune_threshold": self.config.prune_threshold,
            },
        }


async def run_cleanup_loop(
    cleanup_job: TemporalCleanupJob,
    interval_seconds: int = 3600,  # Default: hourly
) -> None:
    """
    Run cleanup job in a loop (for background task).

    Args:
        cleanup_job: The cleanup job instance
        interval_seconds: Seconds between runs
    """
    log.info("cleanup_loop_started", interval=interval_seconds)

    while True:
        try:
            await cleanup_job.run_cleanup()
        except Exception as e:
            log.error("cleanup_loop_error", error=str(e))

        await asyncio.sleep(interval_seconds)
