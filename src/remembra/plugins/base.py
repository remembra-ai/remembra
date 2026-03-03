"""Base classes for the Remembra plugin system.

Every plugin subclasses ``RemembraPlugin`` and overrides the hooks it
cares about.  Hooks are ``async`` so plugins can do I/O (HTTP calls,
DB queries, etc.) without blocking the main request path.

Example
-------
::

    from remembra.plugins.base import RemembraPlugin, MemoryEvent, RecallEvent

    class SlackNotifierPlugin(RemembraPlugin):
        name = "slack-notifier"
        version = "1.0.0"
        description = "Posts to Slack when an important memory is stored."

        async def on_store(self, event: MemoryEvent) -> MemoryEvent:
            if "important" in (event.metadata or {}):
                await post_to_slack(event.content)
            return event
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Event data classes — immutable snapshots passed to hooks
# ---------------------------------------------------------------------------


@dataclass
class MemoryEvent:
    """Payload for store / delete hooks."""

    memory_id: str
    content: str
    user_id: str
    project_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    extracted_facts: list[str] = field(default_factory=list)
    source: str = "user_input"
    trust_score: float = 1.0
    created_at: datetime | None = None


@dataclass
class RecallEvent:
    """Payload for recall hooks."""

    query: str
    user_id: str
    project_id: str
    results: list[dict[str, Any]] = field(default_factory=list)
    context: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EntityEvent:
    """Payload for entity hooks."""

    entity_id: str
    canonical_name: str
    entity_type: str
    user_id: str
    project_id: str
    action: str = "created"  # created | merged | alias_added
    aliases: list[str] = field(default_factory=list)
    merged_from: str | None = None


@dataclass
class ConflictEvent:
    """Payload for conflict hooks."""

    conflict_id: str
    user_id: str
    project_id: str
    new_fact: str
    existing_content: str
    existing_memory_id: str
    similarity_score: float = 0.0
    strategy_applied: str = "update"
    status: str = "open"


# ---------------------------------------------------------------------------
# Plugin base class
# ---------------------------------------------------------------------------


class RemembraPlugin(ABC):
    """Abstract base class for all Remembra plugins.

    Subclass and override any hook methods you need.  Return the
    (optionally modified) event object from each hook.
    """

    # --- Plugin metadata (override in subclass) ---
    name: str = "unnamed-plugin"
    version: str = "0.0.0"
    description: str = ""
    author: str = ""

    # --- Configuration ---
    enabled: bool = True
    config: dict[str, Any] = {}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        if config:
            self.config = config

    # --- Lifecycle ---

    async def on_activate(self) -> None:
        """Called when the plugin is registered and activated."""
        pass

    async def on_deactivate(self) -> None:
        """Called when the plugin is deactivated or unloaded."""
        pass

    # --- Memory hooks ---

    async def on_store(self, event: MemoryEvent) -> MemoryEvent:
        """Called after a memory is stored.

        Can modify the event (e.g., enrich metadata) before it's
        returned.  The memory is already persisted at this point.
        """
        return event

    async def on_recall(self, event: RecallEvent) -> RecallEvent:
        """Called after recall results are assembled.

        Can rerank, filter, or augment results.
        """
        return event

    async def on_delete(self, event: MemoryEvent) -> MemoryEvent:
        """Called when a memory is deleted."""
        return event

    # --- Entity hooks ---

    async def on_entity(self, event: EntityEvent) -> EntityEvent:
        """Called when an entity is created, merged, or updated."""
        return event

    # --- Conflict hooks ---

    async def on_conflict(self, event: ConflictEvent) -> ConflictEvent:
        """Called when a memory conflict is detected."""
        return event

    # --- Serialisation ---

    def to_dict(self) -> dict[str, Any]:
        """Return plugin info as a dictionary."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "enabled": self.enabled,
        }
