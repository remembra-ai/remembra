# OpenAI Agents SDK

Give an [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
agent persistent memory: `RemembraSession` implements the SDK's `Session`
protocol, so conversation history is stored in Remembra and recalled
automatically across runs, with per-session isolation. Items are stored as
written, so no facts or entities are extracted from them.

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
| `clear_session()` | Deletes the items this session stored, and nothing else. |

- **Isolation** is by `session_id` within the account's project (with auth on, the account is the API key's).
  Each item also carries `remembra_integration: "openai_agents"`, and
  `get_items`, `pop_item` and `clear_session` read and delete only memories
  with that marker (plus items stored by earlier versions, which carry
  `agent_item`). Another memory with the same `session_id`, such as an app
  note, is never returned, popped or deleted.
- **Restart-safe ordering**: a fresh `RemembraSession` over an existing
  conversation continues the sequence rather than restarting.
- `get_items` returns up to the 50 most-recent items per read; `clear_session`
  removes all of them. Window or summarize very long conversations.
- Pass `ttl="30d"` to auto-expire items.

!!! warning "remembra 0.16.1 and earlier"
    There, `clear_session()` deletes every memory in the user/project whose
    metadata `session_id` matches, including other code's, and `pop_item()`
    can delete one. With those versions, use a `session_id` that nothing else
    uses in the project.

## Recall across sessions

Because every item is a Remembra memory, you can also recall across *all* of a
user's sessions (not just the current one) — useful for a "what do we know
about this user?" tool your agent can call. Use the
[Python client](../sdks/python-sdk.md) directly for that.
