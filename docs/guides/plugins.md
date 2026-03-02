# Plugins

Remembra's plugin system allows you to extend functionality with custom logic that runs on memory events.

## Overview

Plugins can:

- **Transform memories** before storage
- **Enrich data** with external sources
- **Trigger actions** on memory events
- **Integrate with external services**

## Built-in Plugins

### Auto Tagger

Automatically tags memories based on content:

```python
# Enable in config
REMEMBRA_PLUGINS=auto_tagger
```

**Configuration:**
```yaml
plugins:
  auto_tagger:
    enabled: true
    categories:
      - work
      - personal
      - health
      - finance
```

**Result:**
```json
{
  "content": "Had a productive meeting with the sales team",
  "metadata": {
    "auto_tags": ["work", "meetings"]
  }
}
```

### Recall Logger

Logs all recall queries for analytics:

```python
REMEMBRA_PLUGINS=auto_tagger,recall_logger
```

**Configuration:**
```yaml
plugins:
  recall_logger:
    enabled: true
    log_file: "/var/log/remembra/recalls.jsonl"
    include_results: false  # Privacy-friendly
```

**Log Output:**
```jsonl
{"timestamp": "2026-03-02T12:00:00Z", "user_id": "user_123", "query": "preferences", "results_count": 5, "latency_ms": 45}
```

### Slack Notifier

Send Slack notifications on memory events:

```yaml
plugins:
  slack_notifier:
    enabled: true
    webhook_url: "https://hooks.slack.com/services/YOUR/WEBHOOK"
    events:
      - memory.created
    channel: "#ai-memories"
```

## Creating Custom Plugins

### Plugin Structure

```python
# my_plugin.py
from remembra.plugins.base import Plugin, PluginConfig
from remembra.plugins import MemoryEvent

class MyPluginConfig(PluginConfig):
    api_key: str
    threshold: float = 0.5

class MyPlugin(Plugin):
    name = "my_plugin"
    config_class = MyPluginConfig
    
    async def on_memory_created(self, event: MemoryEvent) -> MemoryEvent:
        """Called when a memory is created."""
        # Modify the memory
        event.memory.metadata["processed_by"] = self.name
        
        # Call external API
        enrichment = await self.enrich(event.memory.content)
        event.memory.metadata["enrichment"] = enrichment
        
        return event
    
    async def on_memory_recalled(self, event: MemoryEvent) -> MemoryEvent:
        """Called when memories are recalled."""
        # Log or modify recall results
        return event
    
    async def enrich(self, content: str) -> dict:
        """Custom enrichment logic."""
        # Your API calls here
        return {"sentiment": "positive"}
```

### Registering Plugins

```python
# In your startup code
from remembra.plugins import PluginManager
from my_plugin import MyPlugin

manager = PluginManager()
manager.register(MyPlugin, config={"api_key": "..."})
```

Or via configuration:

```yaml
plugins:
  my_plugin:
    enabled: true
    api_key: "${MY_PLUGIN_API_KEY}"
    threshold: 0.7
```

## Plugin Events

| Event | When | Use Case |
|-------|------|----------|
| `on_memory_created` | After memory stored | Enrichment, notifications |
| `on_memory_updated` | After memory updated | Sync to external systems |
| `on_memory_deleted` | After memory deleted | Cleanup, audit |
| `on_memory_recalled` | After recall query | Analytics, caching |
| `on_entity_created` | After entity extracted | CRM sync |
| `on_entity_merged` | After entities merged | Data cleanup |

## Plugin Lifecycle

```
1. Plugin loaded at startup
2. Configuration validated
3. initialize() called
4. Event handlers registered
5. Events dispatched as they occur
6. shutdown() called on server stop
```

### Lifecycle Methods

```python
class MyPlugin(Plugin):
    async def initialize(self):
        """Called once at startup."""
        self.client = await create_client()
    
    async def shutdown(self):
        """Called on graceful shutdown."""
        await self.client.close()
    
    async def health_check(self) -> bool:
        """Called periodically for health monitoring."""
        return await self.client.ping()
```

## Best Practices

1. **Keep plugins fast** - Don't block memory operations with slow API calls
2. **Use async** - All plugin methods should be async
3. **Handle errors gracefully** - Don't let plugin failures break core functionality
4. **Log appropriately** - Use structured logging for debugging
5. **Validate config** - Use Pydantic models for configuration

## Example: Sentiment Analysis Plugin

```python
from remembra.plugins.base import Plugin, PluginConfig
from remembra.plugins import MemoryEvent
import httpx

class SentimentConfig(PluginConfig):
    api_url: str = "https://api.sentiment.ai/analyze"
    api_key: str

class SentimentPlugin(Plugin):
    name = "sentiment"
    config_class = SentimentConfig
    
    async def initialize(self):
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.config.api_key}"}
        )
    
    async def on_memory_created(self, event: MemoryEvent) -> MemoryEvent:
        response = await self.client.post(
            self.config.api_url,
            json={"text": event.memory.content}
        )
        result = response.json()
        
        event.memory.metadata["sentiment"] = {
            "score": result["score"],
            "label": result["label"]
        }
        
        return event
    
    async def shutdown(self):
        await self.client.aclose()
```

## Debugging Plugins

Enable plugin debug logging:

```bash
REMEMBRA_LOG_LEVEL=debug
REMEMBRA_PLUGIN_DEBUG=true
```

View plugin status:

```bash
curl http://localhost:8787/api/v1/plugins/status \
  -H "X-API-Key: your_admin_key"
```

```json
{
  "plugins": [
    {
      "name": "auto_tagger",
      "enabled": true,
      "healthy": true,
      "events_processed": 1523
    },
    {
      "name": "sentiment",
      "enabled": true,
      "healthy": true,
      "events_processed": 1523,
      "avg_latency_ms": 45
    }
  ]
}
```
