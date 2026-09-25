# Agent integrations

Ready-to-install pieces that connect AI clients to Remembra's agent session API
(`/api/v1/session/brief`, `/api/v1/session/status`, `/api/v1/timeline`).

| Path | What it is | Install |
|---|---|---|
| `claude-code/session_start.py` | Claude Code **SessionStart** hook. Prints the session brief (handoff, inbox, status, recent memories) as context. Standard library only. | See `docs/integrations/claude-code.md`. |
| `clawd-hooks/session-recall/` | Clawdbot `agent:bootstrap` hook. Fetches the brief and prepends `_SESSION_BRIEF.md`. | Copy the folder over `~/clawd/hooks/session-recall/`. |
| `clawdbot-plugin/` | Clawdbot plugin v2. Adds session brief, status, and inbox tools, stamps provenance, and guards forget-all. | Copy `index.ts`, `clawdbot.plugin.json`, and `package.json` over `~/.clawdbot/extensions/remembra/`, then add `agentId` (and optionally `projectAliases`) to the plugin config. |

The hook and the plugin are covered by tests that run them against the real
API routes: `tests/test_session_start_hook.py` and `tests/test_node_integrations.py`.
The Node tests need Node 22.6 or later, for its built-in TypeScript type stripping.
