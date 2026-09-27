# Plugins

!!! warning "Not wired up yet"
    Remembra has a plugin framework, but the server never calls a plugin's hooks yet. A plugin can be listed
    and activated, and then it does nothing: no memory store, recall, delete, entity or conflict event reaches
    it. This page describes the framework as it is in the code, so you know what exists.

## What exists

- A base class, `RemembraPlugin` (`remembra.plugins.base`), with async hooks: `on_store`, `on_recall`,
  `on_delete`, `on_entity`, `on_conflict`, plus `on_activate` and `on_deactivate`.
- A `PluginManager` that keeps the active plugins and would run each hook in turn, skipping a plugin that
  fails. Nothing in the server calls it yet.
- Three built-in plugins, registered at startup but not active:

| Plugin | What it is written to do |
|--------|--------------------------|
| `auto-tagger` | Add rule-based tags to a memory's metadata when it is stored |
| `recall-logger` | Log recall queries for analytics |
| `slack-notifier` | Post to a Slack webhook when a memory is stored or a conflict is found |

## Writing a plugin

```python
from remembra.plugins.base import RemembraPlugin, MemoryEvent

class MyPlugin(RemembraPlugin):
    name = "my-plugin"
    version = "1.0.0"
    description = "Tags memories that mention a customer"

    async def on_store(self, event: MemoryEvent) -> MemoryEvent:
        if "customer" in event.content.lower():
            event.metadata["tagged_by"] = self.name
        return event
```

A `MemoryEvent` carries `memory_id`, `content`, `user_id`, `project_id`, `metadata`, `extracted_facts`, `source`,
`trust_score` and `created_at`.

## API

Paths are under `/api/v1/plugins`.

| Method | Path | Who | What it does |
|--------|------|-----|--------------|
| GET | `/plugins` | Any signed-in key | List active plugins |
| GET | `/plugins/registry` | Any signed-in key | List plugin classes that can be activated |
| GET | `/plugins/{name}` | Any signed-in key | One active plugin |
| POST | `/plugins/activate` | Superadmin | Activate a registered plugin, with its config |
| PATCH | `/plugins/{name}` | Superadmin | Turn an active plugin on or off |
| DELETE | `/plugins/{name}` | Superadmin | Deactivate a plugin |

Plugins would run for every account on the server, so activating them is superadmin-only. The admin routes
check superadmin before anything else and answer `403` to anyone else.

## Related

- [Webhooks](webhooks.md) — the working way to react to memory events today
