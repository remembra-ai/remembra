"""Tests for the PluginManager — registration, dispatch, and lifecycle."""

import pytest
from unittest.mock import AsyncMock

from remembra.plugins.base import (
    RemembraPlugin,
    MemoryEvent,
    RecallEvent,
    EntityEvent,
    ConflictEvent,
)
from remembra.plugins.manager import PluginManager


# ---------------------------------------------------------------------------
# Test plugin implementations
# ---------------------------------------------------------------------------


class DummyPlugin(RemembraPlugin):
    """Minimal plugin for testing."""

    name = "dummy"
    version = "1.0.0"
    description = "A test plugin"
    author = "test"


class UppercasePlugin(RemembraPlugin):
    """Plugin that uppercases memory content on store."""

    name = "uppercaser"
    version = "0.1.0"
    description = "Uppercases content"
    author = "test"

    async def on_store(self, event: MemoryEvent) -> MemoryEvent:
        event.content = event.content.upper()
        return event


class TaggingPlugin(RemembraPlugin):
    """Plugin that adds a tag to metadata on store."""

    name = "tagger"
    version = "0.1.0"
    description = "Adds tags"
    author = "test"

    async def on_store(self, event: MemoryEvent) -> MemoryEvent:
        event.metadata["tagged"] = True
        return event

    async def on_recall(self, event: RecallEvent) -> RecallEvent:
        event.context = "enriched"
        return event


class FailingPlugin(RemembraPlugin):
    """Plugin that always raises on_store — for error handling tests."""

    name = "failing"
    version = "0.0.1"
    description = "Fails on purpose"
    author = "test"

    async def on_store(self, event: MemoryEvent) -> MemoryEvent:
        raise RuntimeError("Intentional failure")


class FailActivationPlugin(RemembraPlugin):
    """Plugin that fails to activate."""

    name = "fail-activate"
    version = "0.0.1"
    description = "Fails activation"
    author = "test"

    async def on_activate(self) -> None:
        raise RuntimeError("Cannot activate")


# ---------------------------------------------------------------------------
# Helper to make events
# ---------------------------------------------------------------------------


def _store_event(**overrides) -> MemoryEvent:
    defaults = dict(
        memory_id="mem-1",
        content="Alice is CEO",
        user_id="user-1",
        project_id="default",
    )
    defaults.update(overrides)
    return MemoryEvent(**defaults)


def _recall_event(**overrides) -> RecallEvent:
    defaults = dict(
        query="Who is Alice?",
        user_id="user-1",
        project_id="default",
    )
    defaults.update(overrides)
    return RecallEvent(**defaults)


def _entity_event(**overrides) -> EntityEvent:
    defaults = dict(
        entity_id="ent-1",
        canonical_name="Alice",
        entity_type="PERSON",
        user_id="user-1",
        project_id="default",
    )
    defaults.update(overrides)
    return EntityEvent(**defaults)


def _conflict_event(**overrides) -> ConflictEvent:
    defaults = dict(
        conflict_id="conf-1",
        user_id="user-1",
        project_id="default",
        new_fact="Alice is CTO",
        existing_content="Alice is CEO",
        existing_memory_id="mem-old-1",
    )
    defaults.update(overrides)
    return ConflictEvent(**defaults)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPluginRegistration:
    async def test_register_plugin(self):
        mgr = PluginManager()
        await mgr.register(DummyPlugin())
        assert mgr.count == 1

    async def test_register_calls_on_activate(self):
        plugin = DummyPlugin()
        plugin.on_activate = AsyncMock()

        mgr = PluginManager()
        await mgr.register(plugin)
        plugin.on_activate.assert_called_once()

    async def test_register_duplicate_raises(self):
        mgr = PluginManager()
        await mgr.register(DummyPlugin())
        with pytest.raises(ValueError, match="already registered"):
            await mgr.register(DummyPlugin())

    async def test_register_activation_failure_raises(self):
        mgr = PluginManager()
        with pytest.raises(RuntimeError, match="Cannot activate"):
            await mgr.register(FailActivationPlugin())
        assert mgr.count == 0  # not added

    async def test_unregister_plugin(self):
        mgr = PluginManager()
        plugin = DummyPlugin()
        plugin.on_deactivate = AsyncMock()

        await mgr.register(plugin)
        result = await mgr.unregister("dummy")
        assert result is True
        assert mgr.count == 0
        plugin.on_deactivate.assert_called_once()

    async def test_unregister_nonexistent(self):
        mgr = PluginManager()
        result = await mgr.unregister("nope")
        assert result is False


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPluginQuery:
    async def test_list_plugins(self):
        mgr = PluginManager()
        await mgr.register(DummyPlugin())
        await mgr.register(UppercasePlugin())

        plugins = mgr.list_plugins()
        assert len(plugins) == 2
        names = {p["name"] for p in plugins}
        assert names == {"dummy", "uppercaser"}

    async def test_get_plugin(self):
        mgr = PluginManager()
        await mgr.register(DummyPlugin())

        plugin = mgr.get_plugin("dummy")
        assert plugin is not None
        assert plugin.name == "dummy"

    async def test_get_nonexistent_plugin(self):
        mgr = PluginManager()
        assert mgr.get_plugin("nope") is None

    async def test_count(self):
        mgr = PluginManager()
        assert mgr.count == 0
        await mgr.register(DummyPlugin())
        assert mgr.count == 1

    def test_register_class(self):
        mgr = PluginManager()
        mgr.register_class(DummyPlugin)
        registry = mgr.list_registry()
        assert len(registry) == 1
        assert registry[0]["name"] == "dummy"
        assert registry[0]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# Dispatch — pipeline pattern
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPluginDispatch:
    async def test_dispatch_store_single_plugin(self):
        mgr = PluginManager()
        await mgr.register(UppercasePlugin())

        event = _store_event(content="hello world")
        result = await mgr.dispatch_store(event)
        assert result.content == "HELLO WORLD"

    async def test_dispatch_store_pipeline(self):
        """Plugins are called in order — uppercaser then tagger."""
        mgr = PluginManager()
        await mgr.register(UppercasePlugin())
        await mgr.register(TaggingPlugin())

        event = _store_event(content="hello")
        result = await mgr.dispatch_store(event)
        assert result.content == "HELLO"  # uppercased first
        assert result.metadata.get("tagged") is True  # tagged second

    async def test_dispatch_store_skips_disabled_plugin(self):
        mgr = PluginManager()
        plugin = UppercasePlugin()
        plugin.enabled = False
        await mgr.register(plugin)

        event = _store_event(content="hello")
        result = await mgr.dispatch_store(event)
        assert result.content == "hello"  # not uppercased

    async def test_dispatch_store_continues_on_error(self):
        """A failing plugin doesn't stop the pipeline."""
        mgr = PluginManager()
        await mgr.register(FailingPlugin())
        await mgr.register(TaggingPlugin())

        event = _store_event()
        result = await mgr.dispatch_store(event)
        # Tagger still ran despite FailingPlugin crashing
        assert result.metadata.get("tagged") is True

    async def test_dispatch_recall(self):
        mgr = PluginManager()
        await mgr.register(TaggingPlugin())

        event = _recall_event()
        result = await mgr.dispatch_recall(event)
        assert result.context == "enriched"

    async def test_dispatch_delete(self):
        mgr = PluginManager()
        plugin = DummyPlugin()
        plugin.on_delete = AsyncMock(side_effect=lambda e: e)
        await mgr.register(plugin)

        event = _store_event()
        await mgr.dispatch_delete(event)
        plugin.on_delete.assert_called_once()

    async def test_dispatch_entity(self):
        mgr = PluginManager()
        plugin = DummyPlugin()
        plugin.on_entity = AsyncMock(side_effect=lambda e: e)
        await mgr.register(plugin)

        event = _entity_event()
        await mgr.dispatch_entity(event)
        plugin.on_entity.assert_called_once()

    async def test_dispatch_conflict(self):
        mgr = PluginManager()
        plugin = DummyPlugin()
        plugin.on_conflict = AsyncMock(side_effect=lambda e: e)
        await mgr.register(plugin)

        event = _conflict_event()
        await mgr.dispatch_conflict(event)
        plugin.on_conflict.assert_called_once()

    async def test_dispatch_no_plugins(self):
        """Empty manager returns the event unchanged."""
        mgr = PluginManager()
        event = _store_event(content="unchanged")
        result = await mgr.dispatch_store(event)
        assert result.content == "unchanged"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPluginShutdown:
    async def test_shutdown_deactivates_all(self):
        mgr = PluginManager()
        p1 = DummyPlugin()
        p1.name = "p1"
        p1.on_deactivate = AsyncMock()
        p2 = UppercasePlugin()
        p2.on_deactivate = AsyncMock()

        await mgr.register(p1)
        await mgr.register(p2)
        assert mgr.count == 2

        await mgr.shutdown()
        assert mgr.count == 0
        p1.on_deactivate.assert_called_once()
        p2.on_deactivate.assert_called_once()

    async def test_shutdown_continues_on_error(self):
        """Even if one plugin fails to deactivate, others still get shut down."""
        mgr = PluginManager()

        p1 = DummyPlugin()
        p1.name = "p1"
        p1.on_deactivate = AsyncMock(side_effect=RuntimeError("cleanup fail"))

        p2 = UppercasePlugin()
        p2.on_deactivate = AsyncMock()

        await mgr.register(p1)
        await mgr.register(p2)

        await mgr.shutdown()
        assert mgr.count == 0
        p2.on_deactivate.assert_called_once()


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------


class TestEventDataclasses:
    def test_memory_event_defaults(self):
        e = MemoryEvent(memory_id="m1", content="test", user_id="u1", project_id="p1")
        assert e.metadata == {}
        assert e.extracted_facts == []
        assert e.source == "user_input"
        assert e.trust_score == 1.0

    def test_recall_event_defaults(self):
        e = RecallEvent(query="q", user_id="u1", project_id="p1")
        assert e.results == []
        assert e.context == ""

    def test_entity_event_defaults(self):
        e = EntityEvent(
            entity_id="e1", canonical_name="Alice",
            entity_type="PERSON", user_id="u1", project_id="p1",
        )
        assert e.action == "created"
        assert e.aliases == []

    def test_conflict_event_defaults(self):
        e = ConflictEvent(
            conflict_id="c1", user_id="u1", project_id="p1",
            new_fact="new", existing_content="old", existing_memory_id="m1",
        )
        assert e.strategy_applied == "update"
        assert e.status == "open"


# ---------------------------------------------------------------------------
# RemembraPlugin base class
# ---------------------------------------------------------------------------


class TestRemembraPluginBase:
    def test_to_dict(self):
        plugin = DummyPlugin()
        d = plugin.to_dict()
        assert d["name"] == "dummy"
        assert d["version"] == "1.0.0"
        assert d["enabled"] is True

    def test_config_override(self):
        plugin = DummyPlugin(config={"key": "value"})
        assert plugin.config["key"] == "value"

    @pytest.mark.asyncio
    async def test_default_hooks_are_passthrough(self):
        plugin = DummyPlugin()
        event = _store_event()
        result = await plugin.on_store(event)
        assert result is event  # same object, unmodified

        recall = _recall_event()
        result = await plugin.on_recall(recall)
        assert result is recall
