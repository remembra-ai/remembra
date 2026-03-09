# Field Notes — Real-World Fixes & Tips

Lessons learned from deploying Remembra in production with AI coding tools and agent frameworks. These are battle-tested solutions to problems we've hit ourselves.

---

## 1. MCP Connection Refused (localhost vs Cloud)

**Symptom**: `mcp__remembra__recall_memories` returns `Connection refused`. Remembra tools appear in your AI tool but every call fails.

**Root Cause**: The MCP server is configured to connect to `http://localhost:8787` but Remembra isn't running locally. Common when switching from local dev to the cloud API, or after a machine restart.

**Fix**: Update your MCP config to point to the cloud API.

**Claude Code** (`~/.claude.json`):
```json
"mcpServers": {
  "remembra": {
    "type": "stdio",
    "command": "remembra-mcp",
    "args": [],
    "env": {
      "REMEMBRA_URL": "https://api.remembra.dev",
      "REMEMBRA_API_KEY": "your-api-key",
      "REMEMBRA_USER_ID": "your-user-id",
      "REMEMBRA_PROJECT": "your-project"
    }
  }
}
```

**Important**: MCP connections are established at startup. After editing the config, you must restart Claude Code (or your AI tool) for changes to take effect. The current session will keep using the old connection.

---

## 2. Context Lost Between Sessions

**Symptom**: Every new Claude Code session starts from scratch — the AI has no memory of previous work, your preferences, or project context.

**Root Cause**: Two things need to work together for persistent context:

1. **Claude Code auto memory** — The directory at `~/.claude/projects/<project-hash>/memory/` must contain a `MEMORY.md` file. Claude Code loads this automatically at session start.
2. **Remembra MCP** — Must be configured and reachable so `recall_memories` / `store_memory` tools function.

If the memory directory is empty AND Remembra isn't connected, you get total amnesia.

**Fix**:
1. Create a `MEMORY.md` in your project's memory directory with essential context (project overview, tech stack, key files, preferences).
2. Ensure Remembra MCP is configured and reachable (see fix #1 above).

**Finding your memory directory**:
```bash
# The path is derived from your project's absolute path
# Example: /Users/you/myproject → ~/.claude/projects/-Users-you-myproject/memory/
ls ~/.claude/projects/*/memory/
```

---

## 3. Agent Framework trim() Crash

**Symptom**: Agent fails with `TypeError: Cannot read properties of undefined (reading 'trim')`. Messages aren't processed. Error appears in gateway logs as "Embedded agent failed before reply."

**Root Cause**: Some agent frameworks call `.trim()` on values that can be `undefined` during message streaming. This happens when:
- The API returns an unexpected response format
- The agent state isn't fully initialized after a config-triggered restart
- `stripBlockTags()` returns `undefined` for empty/null input

**Affected code pattern**:
```javascript
// UNSAFE — crashes if stripBlockTags returns undefined
const next = ctx.stripBlockTags(buffer, opts).trim();

// SAFE — null-coalescing before trim
const next = (ctx.stripBlockTags(buffer, opts) ?? "").trim();
```

**Fix**: Add null-coalescing (`?? ""`) before every `.trim()` call on potentially undefined values. Common locations in agent frameworks:
- Message streaming handlers (text delta processing)
- Lane/session resolution (`key.trim()`)
- System prompt generation (tool names, workspace notes, owner numbers)
- Identity resolution (ack reactions)

**Tip**: Search for `.trim()` in your agent's dist files and check if each call site handles `undefined` input:
```bash
grep -rn '\.trim()' /path/to/agent/dist/agents/ | grep -v '?\.' | grep -v '??'
```
This finds `.trim()` calls that DON'T use optional chaining (`?.`) or null-coalescing (`??`).

---

## 4. Remembra Works in Agent Framework but Not in Claude Code (or Vice Versa)

**Symptom**: Remembra recall/store works when talking to your agent bot (e.g., via Telegram) but not in Claude Code, or the other way around.

**Root Cause**: These are separate integrations with separate configs:

| System | Config Location | How It Connects |
|--------|----------------|-----------------|
| Claude Code | `~/.claude.json` → `mcpServers.remembra` | stdio MCP via `remembra-mcp` binary |
| Clawdbot | `~/.clawdbot/clawdbot.json` → `plugins.entries.remembra` | Direct API via plugin |
| Cursor | `.cursor/mcp.json` | stdio MCP via `remembra-mcp` binary |

**Fix**: Check the config for the specific tool that's failing. They can point to different URLs, use different API keys, or have different project IDs. Make sure both are pointing to the same Remembra instance.

---

## 5. Session Recall Hook Fires but Agent Crashes

**Symptom**: Gateway log shows `[session-recall] Injected recall instruction into bootstrap` but immediately followed by an error.

**Root Cause**: The hook successfully injects the recall instruction, but the agent crashes while processing the response. This is usually the trim() bug (#3 above), not a problem with the hook or Remembra itself.

**How to verify**: Check the error log timestamp — if the hook injection and the crash are within 1-2 seconds, the crash is in message processing, not in recall.

---

## 6. Config Change Triggers Gateway Restart Loop

**Symptom**: Gateway keeps restarting. Log shows repeated `[reload] config change requires gateway restart`.

**Root Cause**: Writing to the config file (even just updating `meta.lastTouchedAt`) triggers a restart. If the restart itself writes to the config, you get a loop.

**Fix**:
- Use `clawdbot gateway restart` for a clean LaunchAgent restart
- Avoid programmatic writes to the config file during active sessions

---

## 7. Brave Search API Rate Limiting

**Symptom**: `web_search failed: Brave Search API error (429)` — floods the error log with rate limit errors.

**Root Cause**: Brave Search free plan has a 1 request/second rate limit. Agent frameworks that fire multiple parallel searches will hit this immediately.

**Fix**:
- Upgrade to a paid Brave Search plan
- Or configure your agent to use sequential (not parallel) web searches
- Or switch to a different search provider without per-second rate limits

---

## 8. Verifying Remembra Cloud API Health

Quick check that the API is reachable:
```bash
curl -s https://api.remembra.dev/health | python3 -m json.tool
```

Store a test memory:
```bash
curl -s -X POST https://api.remembra.dev/api/v1/memories \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"content": "Test memory", "user_id": "test", "project_id": "test"}'
```

Recall it:
```bash
curl -s -X POST https://api.remembra.dev/api/v1/memories/recall \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "test", "user_id": "test", "project_id": "test"}'
```

---

*Last updated: March 8, 2026*
*Based on production deployment with AI agent frameworks + Claude Code*
