"""
Sleep-Time Compute Worker.

Background consolidation agent that 'thinks' during idle time.
Inspired by Letta/MemGPT - even Mem0 doesn't have this.

Capabilities:
- Cross-session deduplication
- Entity alias resolution
- Graph relationship discovery
- Importance rescoring based on access patterns
- Memory decay cleanup

This runs between conversations to improve memory quality
without impacting real-time performance.
"""

from datetime import datetime, timedelta
from typing import Any

import structlog

from remembra.config import Settings
from remembra.extraction.consolidator import (
    ConsolidationAction,
    ExistingMemory,
)
from remembra.models.memory import ConsolidationReport

log = structlog.get_logger(__name__)


class SleepTimeWorker:
    """
    Background worker that processes memories during idle time.
    
    Runs consolidation passes to:
    1. Find and merge duplicate memories across sessions
    2. Resolve entity aliases (e.g., "my wife" = "Suzan")
    3. Discover new entity relationships from patterns
    4. Re-score importance based on actual access patterns
    5. Clean up decayed memories below threshold
    
    Usage:
        worker = SleepTimeWorker(settings, memory_service)
        report = await worker.run_consolidation(user_id="user_123")
    """
    
    def __init__(
        self,
        settings: Settings,
        memory_service: Any,  # Avoid circular import
    ):
        self.settings = settings
        self.memory_service = memory_service
        self.db = memory_service.db
        self.qdrant = memory_service.qdrant
        self.embeddings = memory_service.embeddings
        self.consolidator = memory_service.consolidator
        self.entity_matcher = memory_service.entity_matcher
        
        self.last_run: datetime | None = None
        self.running = False
        
        log.info(
            "sleep_time_worker_initialized",
            consolidation_threshold=settings.consolidation_threshold,
        )
    
    async def run_consolidation(
        self,
        user_id: str | None = None,
    ) -> ConsolidationReport:
        """
        Main consolidation pass. Runs all sub-tasks.
        
        Args:
            user_id: Optional - consolidate specific user only
                    If None, consolidates all users with recent activity
        
        Returns:
            ConsolidationReport with statistics
        """
        if self.running:
            log.warning("consolidation_already_running")
            return ConsolidationReport(
                errors=["Consolidation already in progress"],
            )
        
        self.running = True
        report = ConsolidationReport(started_at=datetime.utcnow())
        
        try:
            log.info(
                "sleep_time_consolidation_started",
                user_id=user_id or "all",
                since=self.last_run.isoformat() if self.last_run else "never",
            )
            
            # Get users to process
            if user_id:
                user_ids = [user_id]
            else:
                user_ids = await self._get_active_users()
            
            log.debug("processing_users", count=len(user_ids))
            
            for uid in user_ids:
                try:
                    user_report = await self._consolidate_user(uid)
                    
                    # Aggregate stats
                    report.memories_scanned += user_report.memories_scanned
                    report.duplicates_merged += user_report.duplicates_merged
                    report.entities_resolved += user_report.entities_resolved
                    report.relationships_discovered += user_report.relationships_discovered
                    report.importance_rescored += user_report.importance_rescored
                    report.memories_decayed += user_report.memories_decayed
                    report.errors.extend(user_report.errors)
                    
                except Exception as e:
                    log.error("user_consolidation_failed", user_id=uid, error=str(e))
                    report.errors.append(f"User {uid}: {str(e)}")
            
            report.completed_at = datetime.utcnow()
            self.last_run = report.completed_at
            
            log.info(
                "sleep_time_consolidation_completed",
                duration_ms=int((report.completed_at - report.started_at).total_seconds() * 1000),
                memories_scanned=report.memories_scanned,
                duplicates_merged=report.duplicates_merged,
                entities_resolved=report.entities_resolved,
                errors=len(report.errors),
            )
            
            return report
            
        finally:
            self.running = False
    
    async def _get_active_users(self) -> list[str]:
        """Get users with recent memory activity."""
        since = self.last_run or (datetime.utcnow() - timedelta(hours=24))
        
        try:
            cursor = await self.db.conn.execute(
                """
                SELECT DISTINCT user_id
                FROM memories
                WHERE created_at >= ? OR updated_at >= ?
                LIMIT 100
                """,
                (since.isoformat(), since.isoformat()),
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            log.error("get_active_users_failed", error=str(e))
            return []
    
    async def _consolidate_user(self, user_id: str) -> ConsolidationReport:
        """
        Run all consolidation passes for a single user.
        """
        report = ConsolidationReport(started_at=datetime.utcnow())
        
        # Get recent memories for this user
        memories = await self._get_user_memories(user_id)
        report.memories_scanned = len(memories)
        
        if not memories:
            return report
        
        # Pass 1: Deduplication
        duplicates = await self._dedup_pass(user_id, memories)
        report.duplicates_merged = duplicates
        
        # Pass 2: Entity resolution
        entities = await self._entity_resolution_pass(user_id)
        report.entities_resolved = entities
        
        # Pass 3: Importance rescoring
        rescored = await self._importance_rescore_pass(user_id)
        report.importance_rescored = rescored
        
        # Pass 4: Decay cleanup
        decayed = await self._decay_cleanup_pass(user_id)
        report.memories_decayed = decayed
        
        report.completed_at = datetime.utcnow()
        return report
    
    async def _get_user_memories(
        self,
        user_id: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Get recent memories for a user."""
        since = self.last_run or (datetime.utcnow() - timedelta(hours=24))
        
        try:
            cursor = await self.db.conn.execute(
                """
                SELECT id, content, metadata, created_at, access_count
                FROM memories
                WHERE user_id = ? AND (created_at >= ? OR updated_at >= ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, since.isoformat(), since.isoformat(), limit),
            )
            rows = await cursor.fetchall()
            
            columns = ["id", "content", "metadata", "created_at", "access_count"]
            return [dict(zip(columns, row)) for row in rows]
            
        except Exception as e:
            log.error("get_user_memories_failed", user_id=user_id, error=str(e))
            return []
    
    async def _dedup_pass(
        self,
        user_id: str,
        memories: list[dict[str, Any]],
    ) -> int:
        """
        Find and merge duplicate memories across sessions.
        
        Uses lower similarity threshold than real-time for thorough dedup.
        """
        merged_count = 0
        processed_ids = set()
        
        # Use lower threshold for background consolidation
        dedup_threshold = 0.85  # Higher similarity required for merge
        
        for memory in memories:
            if memory["id"] in processed_ids:
                continue
            
            try:
                # Generate embedding
                embedding = await self.embeddings.embed(memory["content"])
                
                # Search for similar memories
                similar = await self.qdrant.search(
                    embedding=embedding,
                    user_id=user_id,
                    limit=5,
                    threshold=dedup_threshold,
                )
                
                # Filter to exclude self and already processed
                candidates = [
                    s for s in similar
                    if s.get("id") != memory["id"]
                    and s.get("id") not in processed_ids
                    and s.get("score", 0) >= dedup_threshold
                ]
                
                if candidates:
                    # Use consolidator to decide merge
                    for candidate in candidates:
                        existing = ExistingMemory(
                            id=candidate["id"],
                            content=candidate.get("content", ""),
                            score=candidate.get("score", 0),
                        )
                        
                        result = await self.consolidator.consolidate(
                            new_fact=memory["content"],
                            existing=[existing],
                        )
                        
                        if result.action == ConsolidationAction.UPDATE:
                            # Merge: update existing, delete current
                            await self._merge_memories(
                                keep_id=candidate["id"],
                                delete_id=memory["id"],
                                merged_content=result.content,
                            )
                            merged_count += 1
                            processed_ids.add(memory["id"])
                            processed_ids.add(candidate["id"])
                            break
                        elif result.action == ConsolidationAction.NOOP:
                            # Duplicate: delete current
                            await self._delete_memory(memory["id"])
                            merged_count += 1
                            processed_ids.add(memory["id"])
                            break
                
                processed_ids.add(memory["id"])
                
            except Exception as e:
                log.debug("dedup_memory_failed", memory_id=memory["id"], error=str(e))
        
        return merged_count
    
    async def _entity_resolution_pass(self, user_id: str) -> int:
        """
        Resolve entity aliases across different sessions.
        
        Examples:
        - "my wife" in one session = "Suzan" in another
        - "John" = "John Smith" = "Mr. Smith"
        """
        resolved_count = 0
        
        try:
            # Get all entities for this user
            cursor = await self.db.conn.execute(
                """
                SELECT id, canonical_name, aliases, type
                FROM entities
                WHERE user_id = ?
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            
            if len(rows) < 2:
                return 0
            
            entities = []
            for row in rows:
                entities.append({
                    "id": row[0],
                    "name": row[1],
                    "aliases": row[2].split(",") if row[2] else [],
                    "type": row[3],
                })
            
            # Find potential matches using EntityMatcher
            for i, entity1 in enumerate(entities):
                for entity2 in entities[i+1:]:
                    # Only match same type
                    if entity1["type"] != entity2["type"]:
                        continue
                    
                    # Check if names are similar
                    if await self._entities_match(entity1, entity2):
                        await self._merge_entities(entity1["id"], entity2["id"])
                        resolved_count += 1
            
        except Exception as e:
            log.debug("entity_resolution_failed", user_id=user_id, error=str(e))
        
        return resolved_count
    
    async def _entities_match(
        self,
        entity1: dict[str, Any],
        entity2: dict[str, Any],
    ) -> bool:
        """Check if two entities are likely the same."""
        name1 = entity1["name"].lower()
        name2 = entity2["name"].lower()
        aliases1 = [a.lower() for a in entity1.get("aliases", [])]
        aliases2 = [a.lower() for a in entity2.get("aliases", [])]
        
        # Direct name match
        if name1 == name2:
            return True
        
        # Name in other's aliases
        if name1 in aliases2 or name2 in aliases1:
            return True
        
        # One name contains the other (e.g., "John" in "John Smith")
        if name1 in name2 or name2 in name1:
            return True
        
        return False
    
    async def _importance_rescore_pass(self, user_id: str) -> int:
        """
        Re-score importance based on actual access patterns.
        
        Frequently recalled memories get higher importance.
        """
        rescored_count = 0
        
        try:
            # Get memories with access counts
            cursor = await self.db.conn.execute(
                """
                SELECT id, access_count, metadata
                FROM memories
                WHERE user_id = ? AND access_count > 0
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            
            for row in rows:
                memory_id, access_count, metadata_str = row
                
                # Calculate importance boost based on access
                # More accesses = more important
                importance_boost = min(0.3, access_count * 0.05)  # Max 0.3 boost
                
                # Update metadata with new importance
                try:
                    import json
                    metadata = json.loads(metadata_str) if metadata_str else {}
                    current_importance = metadata.get("importance", 0.5)
                    new_importance = min(1.0, current_importance + importance_boost)
                    
                    if new_importance != current_importance:
                        metadata["importance"] = new_importance
                        metadata["importance_rescored_at"] = datetime.utcnow().isoformat()
                        
                        await self.db.conn.execute(
                            "UPDATE memories SET metadata = ? WHERE id = ?",
                            (json.dumps(metadata), memory_id),
                        )
                        rescored_count += 1
                        
                except Exception:
                    pass
            
            if rescored_count > 0:
                await self.db.conn.commit()
                
        except Exception as e:
            log.debug("importance_rescore_failed", user_id=user_id, error=str(e))
        
        return rescored_count
    
    async def _decay_cleanup_pass(self, user_id: str) -> int:
        """
        Remove memories that have decayed below threshold.
        
        Uses the temporal decay system to identify very old,
        unused memories that can be cleaned up.
        """
        cleaned_count = 0
        
        try:
            # Get very old memories with low access
            cutoff = datetime.utcnow() - timedelta(days=90)
            
            cursor = await self.db.conn.execute(
                """
                SELECT id
                FROM memories
                WHERE user_id = ?
                  AND created_at < ?
                  AND access_count = 0
                  AND expires_at IS NULL
                LIMIT 100
                """,
                (user_id, cutoff.isoformat()),
            )
            rows = await cursor.fetchall()
            
            for row in rows:
                memory_id = row[0]
                # Delete very old, never-accessed memories
                await self._delete_memory(memory_id)
                cleaned_count += 1
                
        except Exception as e:
            log.debug("decay_cleanup_failed", user_id=user_id, error=str(e))
        
        return cleaned_count
    
    async def _merge_memories(
        self,
        keep_id: str,
        delete_id: str,
        merged_content: str | None,
    ) -> None:
        """Merge two memories, keeping one and deleting the other."""
        try:
            if merged_content:
                await self.db.conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (merged_content, datetime.utcnow().isoformat(), keep_id),
                )
            
            await self._delete_memory(delete_id)
            await self.db.conn.commit()
            
        except Exception as e:
            log.error("merge_memories_failed", keep=keep_id, delete=delete_id, error=str(e))
    
    async def _merge_entities(self, keep_id: str, delete_id: str) -> None:
        """Merge two entities, keeping one and deleting the other."""
        try:
            # Transfer aliases from deleted to kept
            cursor = await self.db.conn.execute(
                "SELECT aliases FROM entities WHERE id = ?",
                (delete_id,),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                # Add aliases to kept entity
                await self.db.conn.execute(
                    """
                    UPDATE entities 
                    SET aliases = aliases || ',' || ?
                    WHERE id = ?
                    """,
                    (row[0], keep_id),
                )
            
            # Update memory references
            await self.db.conn.execute(
                """
                UPDATE memory_entities 
                SET entity_id = ? 
                WHERE entity_id = ?
                """,
                (keep_id, delete_id),
            )
            
            # Delete the duplicate entity
            await self.db.conn.execute(
                "DELETE FROM entities WHERE id = ?",
                (delete_id,),
            )
            
            await self.db.conn.commit()
            
        except Exception as e:
            log.error("merge_entities_failed", keep=keep_id, delete=delete_id, error=str(e))
    
    async def _delete_memory(self, memory_id: str) -> None:
        """Delete a memory from SQLite and Qdrant."""
        try:
            await self.db.conn.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,),
            )
            await self.db.conn.commit()
            
            # Also delete from Qdrant
            await self.qdrant.delete(memory_id)
            
        except Exception as e:
            log.error("delete_memory_failed", memory_id=memory_id, error=str(e))


# ============================================================================
# Convenience Function
# ============================================================================

async def run_sleep_time_consolidation(
    memory_service: Any,
    settings: Settings,
    user_id: str | None = None,
) -> ConsolidationReport:
    """
    Run sleep-time consolidation.
    
    Args:
        memory_service: MemoryService instance
        settings: Application settings
        user_id: Optional user to consolidate
        
    Returns:
        ConsolidationReport with statistics
    """
    worker = SleepTimeWorker(settings=settings, memory_service=memory_service)
    return await worker.run_consolidation(user_id=user_id)
