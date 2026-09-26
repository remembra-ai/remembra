# remembra-mcp

Starts the [Remembra](https://remembra.dev) MCP server over stdio. It is a
launcher: installing it installs `remembra[mcp]` at the same version, and the
`remembra-mcp` command runs `remembra.mcp.server`.

```bash
REMEMBRA_URL=https://api.remembra.dev REMEMBRA_API_KEY=rem_... uvx remembra-mcp
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `REMEMBRA_API_KEY` | yes | Your API key (dashboard, Settings > API keys). Keep it secret. |
| `REMEMBRA_URL` | yes | `https://api.remembra.dev` for the hosted service, or your own server. Unset, it falls back to `http://localhost:8787`. |
| `REMEMBRA_AGENT_ID` | no | The name this agent signs handoffs with, e.g. `claude-code` or `codex`. |

The tools let an agent read the brief another agent left for a repository
(`session_brief`) and leave its own handoff when it stops (`close_session`),
alongside memory store and recall. Source, docs and issues:
https://github.com/remembra-ai/remembra

<!-- mcp-name: io.github.remembra-ai/remembra -->
