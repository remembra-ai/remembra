# Agent integrations

Ready-to-install pieces that connect AI clients to Remembra's agent session API
(`/api/v1/session/brief`, `/api/v1/session/status`, `/api/v1/timeline`).

| Path | What it is | Install |
|---|---|---|
| `claude-code/session_start.py` | Legacy Claude Code **SessionStart** hook for machines without `remembra-relay`. Prints the server's rendered brief (handoff, inbox, status, recent handoffs and checkpoints). Standard library only. Prefer `remembra-relay connect --apply`, which installs `remembra-relay brief --hook claude-code` and replaces this script. | See `docs/integrations/claude-code.md`. |
| `clawd-hooks/session-recall/` | Clawdbot `agent:bootstrap` hook. Fetches the brief and prepends it as `_SESSION_BRIEF.md`. | Copy the folder over `~/clawd/hooks/session-recall/`. |
| `clawdbot-plugin/` | Clawdbot plugin v2.1. Adds session brief, status, and inbox tools, stamps provenance, and guards forget-all. | Copy `index.ts`, `clawdbot.plugin.json`, and `package.json` over `~/.clawdbot/extensions/remembra/`, then add `agentId` (and optionally `projectAliases`) to the plugin config. |

Everything these pieces hand to a model was written by other agents and tools, so it is
data, not instructions. The hooks print the server's rendered brief, which keeps that
text inside one `<remembra-data untrusted="true">` block under the brief's trust policy
(low-trust lines withheld, command-shaped lines flagged). The plugin wraps every tool
result that returns stored content in the same block. None of them adds a directive of
its own. Update installed copies when this folder changes: an older copy passes other
agents' messages to the model unframed.

The hook and the plugin are covered by tests that run them against the real
API routes: `tests/test_session_start_hook.py` and `tests/test_node_integrations.py`.
The Node tests need Node 22.6 or later, for its built-in TypeScript type stripping.
