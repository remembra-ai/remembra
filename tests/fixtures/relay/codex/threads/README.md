# Codex rollout heads, one per thread kind

Synthetic rollouts (fake ids, `/home/dev/work/widget`, no real content) for
`tests/test_relay_background_sessions.py`. Each file is the two records a Codex
rollout starts with: a `session_meta` and a `task_started` event. The
`session_meta` fields follow the shape Codex Desktop (codex-cli
0.155.0-alpha.16.4) writes for each kind of thread: `thread_source`, `source`
(a string, or `{"subagent": {"thread_spawn": {...}}}` for a sub-agent),
`originator`, and for a sub-agent `parent_thread_id` and `agent_nickname`.
A sub-agent's `session_id` is its parent's (`...a009`), not its own `id`: Codex
writes it that way, and a sub-agent's hooks carry that parent session id with
the sub-agent's own id as `agent_id` (see tests/test_relay_codex_live.py).

| File | Kind | Relay |
|---|---|---|
| `automation.jsonl` | a scheduled automation run | skipped (`automation`) |
| `subagent.jsonl` | a sub-agent thread | skipped (`subagent`) |
| `subagent_source_only.jsonl` | a sub-agent named only by its `source` object | skipped (`subagent`) |
| `user.jsonl` | a thread someone started | brief and handoff |
| `voice_chat.jsonl` | a voice chat | brief and handoff |
| `agent_created_thread.jsonl` | a thread an agent opened for the user | brief and handoff |
| `no_thread_source.jsonl` | an older `session_meta` without `thread_source` | brief and handoff |
| `malformed.jsonl` | a first line cut off mid-record | brief and handoff (kind unknown) |
