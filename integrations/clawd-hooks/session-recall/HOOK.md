---
name: session-recall
description: "Fetches the Remembra session brief at agent bootstrap and injects it as the first bootstrap file"
metadata: {"clawdbot":{"emoji":"🧠","events":["agent:bootstrap"]}}
---

# Session Recall Hook

Makes session-start context automatic instead of an instruction the model may skip.

## What It Does

1. Listens for `agent:bootstrap`.
2. Calls `GET /api/v1/session/brief?project_id=<project>&agent_id=<agent>` on Remembra.
3. Prepends `_SESSION_BRIEF.md` to the bootstrap files: latest handoff, this agent's
   unread inbox (with inbox ids to ack), current status values, recent memories by time.
4. If Remembra is unreachable, prepends a short note telling the agent to call
   `remembra_session_brief` itself. It never blocks bootstrap.

## Configuration

Read from environment first, then from `~/.clawdbot/clawdbot.json`
(`plugins.entries.remembra.config`):

| Env | Plugin config | Default |
|---|---|---|
| `REMEMBRA_URL` | `apiUrl` | `http://localhost:8787` |
| `REMEMBRA_API_KEY` | `apiKey` | (required) |
| `REMEMBRA_PROJECT` | `projectId` | `default` |
| `REMEMBRA_AGENT_ID` | `agentId` | `clawdbot` |
| `REMEMBRA_PROJECT_ALIASES` | `projectAliases` | none |

`REMEMBRA_HOOK_CLAWDBOT_CONFIG` overrides the config file path.
