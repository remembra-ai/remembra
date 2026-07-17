# LangChain

Give any LangChain app persistent, cross-session memory backed by Remembra —
chat history that survives restarts, with entity resolution, semantic recall,
and per-session isolation.

## Install

```bash
pip install remembra langchain-core
```

Point it at a running Remembra server (see [Deploying](../DEPLOYING.md) or use
the managed cloud at `https://api.remembra.dev`).

## Chat message history

`RemembraChatMessageHistory` implements LangChain's `BaseChatMessageHistory`,
so it drops into `RunnableWithMessageHistory` and any chain that takes a
history factory.

```python
from remembra.integrations.langchain import RemembraChatMessageHistory

history = RemembraChatMessageHistory(
    base_url="https://api.remembra.dev",
    api_key="rem_...",           # omit for a local, auth-disabled server
    user_id="user_123",
    session_id="conversation_abc",
)

history.add_user_message("My name is Alice and I love hiking")
history.add_ai_message("Nice to meet you, Alice!")

for msg in history.messages:
    print(type(msg).__name__, msg.content)
# HumanMessage My name is Alice and I love hiking
# AIMessage    Nice to meet you, Alice!
```

Messages are stored **atomically** (one message = one memory, never split or
merged) and reconstructed faithfully as the original `HumanMessage` /
`AIMessage` / `SystemMessage` types.

### Wiring into a chain

```python
from langchain_core.runnables.history import RunnableWithMessageHistory

chain_with_memory = RunnableWithMessageHistory(
    chain,
    lambda session_id: RemembraChatMessageHistory(
        base_url="https://api.remembra.dev",
        api_key="rem_...",
        user_id="user_123",
        session_id=session_id,
    ),
    input_messages_key="input",
    history_messages_key="history",
)

chain_with_memory.invoke(
    {"input": "What did I say my name was?"},
    config={"configurable": {"session_id": "conversation_abc"}},
)
```

### Clearing a session

`history.clear()` deletes **only that session's** messages — other sessions
and the rest of the user's memory are untouched.

```python
history.clear()   # removes conversation_abc; conversation_xyz is unaffected
```

## Notes & limits

- **Isolation** is by `session_id` within a `(user_id, project)` namespace.
  Use a distinct `session_id` per conversation.
- **`messages` returns up to the 50 most recent** messages of a session in one
  read (the recall API caps a single page at 50). `clear()` deletes *all*
  messages regardless of count. For very long histories, window the
  conversation or summarize older turns.
- **TTL**: pass `ttl="30d"` to auto-expire messages.
- Every stored message carries `session_id`, `role`, and `sequence` metadata,
  so history is reconstructed in order.

## Semantic memory (beyond chat history)

`RemembraMemory` recalls over everything a user has stored — not just the
current chat — and returns it as a prompt variable you can inject into a
template:

```python
from remembra.integrations.langchain import RemembraMemory

mem = RemembraMemory(
    base_url="https://api.remembra.dev",
    api_key="rem_...",
    user_id="user_123",
    memory_key="history",   # the prompt variable name to populate
)

mem.save("The user is planning a trip to Japan in spring")

# load() returns {memory_key: synthesized_context} for prompt injection
variables = mem.load("what are the user's travel plans?")
# {"history": "The user is planning a trip to Japan in spring."}
```
