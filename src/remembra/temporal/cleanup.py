"""
Background cleanup job for expired and decayed memories.

This module provides scheduled tasks for:
1. Hard TTL expiration (delete memories past expires_at)
2. Soft decay pruning (mark/archive low-relevance memories)
3. Cleanup metrics and logging
"""

import asyncio
from datetime import datetime
from typing import Any

import structlog

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
        database,  # Database instance
        qdrant_store,  # QdrantStore instance
        config: DecayConfig | None = None,
        auto_delete_expired: bool = True,
        auto_prune_decayed: bool = False,  # Conservative default
        prune_to_archive: bool = True,  # Archive instead of delete
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
        """
        self.db = database
        self.qdrant = qdrant_store
        self.config = config or DEFAULT_CONFIG
        self.auto_delete_expired = auto_delete_expired
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
        start_time = datetime.utcnow()
        
        results = {
            "run_id": self._run_count,
            "started_at": start_time.isoformat(),
            "dry_run": dry_run,
            "expired_found": 0,
            "expired_deleted": 0,
            "decayed_found": 0,
            "decayed_pruned": 0,
            "decayed_archived": 0,
            "errors": [],
        }
        
        try:
            # 1. Handle TTL-expired memories
            if self.auto_delete_expired:
                expired_result = await self._cleanup_expired(
                    user_id=user_id,
                    project_id=project_id,
                    dry_run=dry_run,
                )
                results["expired_found"] = expired_result["found"]
                results["expired_deleted"] = expired_result["deleted"]
                results["errors"].extend(expired_result.get("errors", []))
            
            # 2. Handle decayed memories (soft prune)
            if self.auto_prune_decayed:
                decayed_result = await self._cleanup_decayed(
                    user_id=user_id,
                    project_id=project_id,
                    dry_run=dry_run,
                )
                results["decayed_found"] = decayed_result["found"]
                results["decayed_pruned"] = decayed_result["pruned"]
                results["decayed_archived"] = decayed_result.get("archived", 0)
                results["errors"].extend(decayed_result.get("errors", []))
            
            # Update metrics
            self._last_run = start_time
            self._total_expired_deleted += results["expired_deleted"]
            self._total_decayed_pruned += results["decayed_pruned"]
            
            results["completed_at"] = datetime.utcnow().isoformat()
            results["duration_ms"] = int(
                (datetime.utcnow() - start_time).total_seconds() * 1000
            )
            
            log.info(
                "cleanup_completed",
                expired_deleted=results["expired_deleted"],
                decayed_pruned=results["decayed_pruned"],
                duration_ms=results["duration_ms"],
                dry_run=dry_run,
            )
            
        except Exception as e:
            log.error("cleanup_failed", error=str(e))
            results["errors"].append(str(e))
        
        return results
    
    async def _cleanup_expired(
        self,
        user_id: str | None,
        project_id: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Delete memories past their TTL expiration."""
        result = {"found": 0, "deleted": 0, "errors": []}
        
        try:
            # Get expired memory IDs
            expired_ids = await self.db.get_expired_memories(
                user_id=user_id,
                project_id=project_id or "default",
            )
            result["found"] = len(expired_ids)
            
            if dry_run:
                log.info("dry_run_expired", count=len(expired_ids), ids=expired_ids[:10])
                return result
            
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
                    
                    result["deleted"] += 1
                    
                except Exception as e:
                    log.warning("delete_expired_failed", memory_id=memory_id, error=str(e))
                    result["errors"].append(f"Failed to delete {memory_id}: {e}")
            
            log.info("expired_cleanup_done", found=result["found"], deleted=result["deleted"])
            
        except Exception as e:
            log.error("expired_cleanup_error", error=str(e))
            result["errors"].append(str(e))
        
        return result
    
    async def _cleanup_decayed(
        self,
        user_id: str | None,
        project_id: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Handle memories that have decayed below threshold."""
        result = {"found": 0, "pruned": 0, "archived": 0, "errors": []}
        
        try:
            # Get all memories with decay info
            memories = await self.db.get_memories_with_decay_info(
                user_id=user_id or "",  # Empty string gets all if not specified
                project_id=project_id or "default",
                limit=1000,  # Process in batches
            )
            
            # Calculate decay and find candidates for pruning
            prune_candidates = []
            for memory in memories:
                decay_info = calculate_memory_decay_info(memory, self.config)
                if decay_info["should_prune"] and not decay_info["is_expired"]:
                    # Decayed but not TTL-expired
                    prune_candidates.append({
                        "id": memory["id"],
                        "relevance": decay_info["relevance_score"],
                        "days_since_access": decay_info["days_since_access"],
                    })
            
            result["found"] = len(prune_candidates)
            
            if dry_run:
                log.info(
                    "dry_run_decayed",
                    count=len(prune_candidates),
                    samples=prune_candidates[:5],
                )
                return result
            
            # Handle decayed memories
            for candidate in prune_candidates:
                try:
                    memory_id = candidate["id"]
                    
                    if self.prune_to_archive:
                        # Archive instead of delete (future: move to cold storage)
                        # For now, just mark as archived in metadata
                        await self._archive_memory(memory_id)
                        result["archived"] += 1
                    else:
                        # Hard delete
                        await self.db.delete_memory(memory_id)
                        if self.qdrant:
                            await self.qdrant.delete(memory_id)
                        await self.db.delete_memory_fts(memory_id)
                        result["pruned"] += 1
                        
                except Exception as e:
                    log.warning("prune_decayed_failed", memory_id=memory_id, error=str(e))
                    result["errors"].append(f"Failed to prune {memory_id}: {e}")
            
            log.info(
                "decayed_cleanup_done",
                found=result["found"],
                pruned=result["pruned"],
                archived=result["archived"],
            )
            
        except Exception as e:
            log.error("decayed_cleanup_error", error=str(e))
            result["errors"].append(str(e))
        
        return result
    
    async def _archive_memory(self, memory_id: str) -> None:
        """
        Archive a memory (soft delete).
        
        For now, this updates metadata to mark as archived.
        Future: Move to cold storage / separate archive table.
        """
        # Get current memory
        memory = await self.db.get_memory(memory_id)
        if not memory:
            return
        
        # Update metadata to mark as archived
        import json
        metadata = json.loads(memory.get("metadata", "{}") or "{}")
        metadata["_archived"] = True
        metadata["_archived_at"] = datetime.utcnow().isoformat()
        metadata["_archive_reason"] = "decay_threshold"
        
        # Update in database
        await self.db.conn.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(metadata), memory_id),
        )
        await self.db.conn.commit()
        
        log.debug("memory_archived", memory_id=memory_id)
    
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
            memory_reports.append({
                "id": memory["id"],
                "content_preview": memory.get("content", "")[:100] + "...",
                **decay_info,
            })
            
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
