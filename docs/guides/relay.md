# Remembra Relay: session continuity across agents

Every agent leaves a trail when it stops: who it was, what it did, what it did
not finish, what is failing, and where it left off. The next agent, whatever the
tool, machine, or checkout location, picks that up at session start.

## How it works

1. **Project identity.** A project is identified by the repository, not by the folder it sits in.
   The git remote is normalized (`https://github.com/Acme/Widget.git`,
   `git@github.com:acme/widget`, `ssh://git@github.com:22/acme/widget` are all
   `github.com/acme/widget`). The root commit and the path are fallbacks. The same repo on a laptop,
   an external drive, a server, or in a worktree resolves to the same project.
2. **Close-out.** When a session ends, `remembra-relay close` gathers facts mechanically from git (branch,
   head, this session's commits, changed and uncommitted files, diff stat, unpushed commits) and,
   for Claude Code, from the session transcript (shell commands and exit codes, test runs, edited files,
   open todo items). The raw transcript never leaves the machine. The server stores **one** handoff
   with fixed sections: *Done · Not done / open · Failing / errors · Next step*. An agent-written
   summary is optional and is checked against the facts. It is shown as *unverified* or *contradicted*.
3. **Pickup.** At session start, `remembra-relay brief` (or the `session_brief` MCP tool) leads with one line:

   ```
   Last session: claude-code, 2h ago, on main@1d50ae3: done: … / NOT done: … / failing: … / next: …
   ```

   The line is followed by your unread inbox, status values, linked projects' latest handoffs, and recent memories.
   The brief is capped at about 1500 tokens.

## Setup

```bash
remembra-relay connect            # dry run: shows exactly what would change
remembra-relay connect --apply    # writes the hooks (backups kept as *.bak-relay-<time>)
```

`connect` reads your existing Remembra config (`REMEMBRA_URL` / `REMEMBRA_API_KEY`, the `remembra` MCP
server in `~/.claude.json` or `~/.codex/config.toml`, or `~/.remembra/credentials`). It never writes API
keys into new places.

| Agent | Hooks | Status |
|-------|-------|--------|
| Claude Code | `~/.claude/settings.json` SessionStart → `brief`, SessionEnd → `close` (transcript parsed) | verified |
| Codex CLI | `~/.codex/hooks.json` SessionStart / SessionEnd | unverified |
| Cursor | `~/.cursor/hooks.json` sessionStart / sessionEnd | unverified |
| Gemini CLI | `~/.gemini/settings.json` SessionStart / SessionEnd (JSON-only stdout) | unverified |
| Qwen Code | `~/.qwen/settings.json` SessionStart / SessionEnd (JSON-only stdout) | unverified |
| Kimi Code | `~/.kimi/config.toml` `[[hooks]]` | unverified |

Unverified adapters are dry-run only unless you pass `--include-unverified`. For agents without hooks,
`connect --agents-md PATH --apply` adds a short marked section to an `AGENTS.md`. Any MCP-capable agent is
also told by the MCP server to call `session_brief` at start and `close_session` before finishing.

## CLI

```text
remembra-relay brief   [--agent X] [--cwd DIR] [--hook NAME] [--format text|json|hook-json|cursor-json]
remembra-relay close   [--agent X] [--session-id S] [--cwd DIR] [--transcript PATH] [--reason R]
                       [--summary S] [--notes N] [--next STEP] [--todo ITEM]... [--dry-run]
remembra-relay trail   [--cwd DIR] [--project P] [--limit N] [--format text|json]
remembra-relay resolve [--cwd DIR] [--project P] [--bind]
remembra-relay connect [--apply] [--agent NAME]... [--include-unverified] [--agents-md PATH]
```

`brief`, `close` and `trail` are safe to run as hooks. They finish within 10 seconds, always exit 0, and
report problems on stderr. Outside a git repository the working directory identifies the project and
`REMEMBRA_PROJECT` names it. Set `REMEMBRA_RELAY_PROJECT` to name a new repository's project.
Use `resolve --project clawdbot --bind` to point an existing checkout at an existing project.

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/v1/projects/resolve` | `{git_remote?, root_commit?, root_path?, repo_name?, host?, hint_project?, bind?}` → `{project_id, created, persisted, fingerprint, kind, bound}` |
| POST / GET / DELETE | `/api/v1/projects/links` | link projects (`from_project`, `to_project`, `relation`) |
| POST | `/api/v1/session/close` | `{agent_id, session_id, project_id \| project:{locator}, facts:{…}, summary?, end_reason?}` → handoff id + rendered text |
| GET | `/api/v1/session/brief` | `project_id` or locator params → brief JSON + `rendered` |
| GET | `/api/v1/trail` | handoffs + checkpoints across agents, newest first |

Closing again with the same `(agent_id, session_id)` updates that session's handoff: the previous version
is superseded, never duplicated. Each close also sets the `last_agent:<project>` and `branch:<project>`
status keys.

**Attribution.** A key created with `agent_id` (`POST /api/v1/keys {"agent_id": "codex"}`) is
agent-scoped. Its close-outs are attributed to that agent, and a different id in the body or the
`X-Remembra-Agent-Id` header is rejected. Project-restricted keys can only resolve into, close, brief and
see links for their own projects. Every stored string passes secret redaction and the server's PII policy.
