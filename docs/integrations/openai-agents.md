# OpenAI Agents SDK

Give an [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
agent persistent memory: `RemembraSession` implements the SDK's `Session`
protocol, so conversation history is stored in Remembra and recalled
automatically across runs — with per-session isolation and entity resolution.

## Install

```bash
pip install remembra openai-agents
```

## Use it

Pass a `RemembraSession` to `Runner.run(...)`. The SDK reads and writes the
conversation through it automatically:

```python
from agents import Agent, Runner
from remembra.integrations.openai_agents import RemembraSession

session = RemembraSession(
    session_id="conversation_123",
    base_url="https://api.remembra.dev",
    api_key="rem_...",            # omit for a local, auth-disabled server
    user_id="user_42",
)

agent = Agent(name="Assistant", instructions="Be helpful and concise.")

# First turn
await Runner.run(agent, "My name is Alice and I love hiking", session=session)

# Later — even in a new process — the agent recalls the history:
result = await Runner.run(agent, "What's my name?", session=session)
print(result.final_output)   # "Your name is Alice."
```

## How it behaves

`RemembraSession` implements the full `Session` protocol:

| Method | Behavior |
| --- | --- |
| `add_items(items)` | Each item stored **atomically** (never split or merged) with a monotonic sequence for exact ordering. |
| `get_items(limit=None)` | Items oldest-first; with `limit`, the latest *N*. |
| `pop_item()` | Removes and returns the most recent item (e.g. for retries). |
| `clear_session()` | Deletes **only this session's** items — never the user's other memory. |

- **Isolation** is by `session_id` within a `(user_id, project)` namespace.
- **Restart-safe ordering**: a fresh `RemembraSession` over an existing
  conversation continues the sequence rather than restarting.
- `get_items` returns up to the 50 most-recent items per read; `clear_session`
  removes all of them. Window or summarize very long conversations.
- Pass `ttl="30d"` to auto-expire items.

## Recall across sessions

Because every item is a Remembra memory, you can also recall across *all* of a
user's sessions (not just the current one) — useful for a "what do we know
about this user?" tool your agent can call. Use the
[Python client](../sdks/python-sdk.md) directly for that.
