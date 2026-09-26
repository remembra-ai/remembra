# Remembra and other handoff tools

*Checked on 2026-09-25. "Not checked" means we have not verified it, so we don't claim it either way. Every fact about another project links to the page we read it on that day; star counts
come from the GitHub API that day. Projects move fast, so check their pages before you decide. If we got
something wrong about your project, open an issue and we will fix it.*

Remembra Relay is not the only way to carry work from one coding agent to the next. Several good tools do some
of it, most of them free and local. This page says what each one does, where Remembra Relay differs, and when
one of the others is the better choice.

## What Remembra Relay does

When an agent's session ends, `remembra-relay close` reads facts from git (branch, commits, changed and
uncommitted files, unpushed commits) and, for Claude Code, from the local session transcript (commands, test
runs, open items), and saves one handoff to your Remembra server. The agent's own summary is optional and is
checked against those facts: the brief shows it as unverified, or as contradicted when git disagrees. The next
agent, in another tool or on another machine, gets a short brief at session start. Every handoff stays on the
trail. See the [Relay guide](../guides/relay.md).

Today only Claude Code's hooks are verified. The Codex, Cursor, Gemini CLI, Qwen Code and Kimi hooks follow each
tool's docs but have not been run against it yet; any MCP agent can call `session_brief` and `close_session`
directly.

## At a glance

| | Where it keeps state | Across machines | Where handoff facts come from | Summary checked against facts | Durable trail | License |
|---|---|---|---|---|---|---|
| **Remembra Relay** | Your Remembra server (hosted or self-hosted) | Yes, by git remote | git, plus the Claude Code transcript | Yes | Yes | MIT |
| [claude-mem](https://github.com/thedotmack/claude-mem) | A plugin in your agent | Not checked | LLM-compressed session memory | No | Not checked | Apache-2.0 |
| [agentmemory](https://github.com/rohitg00/agentmemory) | Local, with peer-to-peer sync | Via P2P sync | Memory with commit lookup | No | Partly | Apache-2.0 |
| [continues](https://github.com/yigitkonur/cli-continues) | Local files | No | The session transcript | No | No | MIT |
| [catchup](https://github.com/wilbeibi/catchup) | Local files | No | The session transcript | No | No | MIT |
| [waybill](https://github.com/wardmos/waybill) | A local handoff bundle | No | Diff, commands and tests | No | No | Apache-2.0 |

## The tools, one by one

**claude-mem** ([repo](https://github.com/thedotmack/claude-mem), 94,704 stars on 2026-09-25) is the most
popular memory plugin for coding agents. It compresses what happened in a session with an LLM and brings it
back later, and it works with Claude Code, Codex and Cursor. If you want one agent to remember a project on
one machine, with no account, it is the obvious place to start.

**agentmemory** ([repo](https://github.com/rohitg00/agentmemory), 28,860 stars on 2026-09-25) is the closest to
Remembra Relay. Its README lists a `/handoff` command and `session_handoff`, `memory_lease` and
`memory_commit_lookup` tools, with hooks for Claude Code, Codex, Copilot CLI and more, and peer-to-peer sync
between machines. It is free and local-first.

**continues** ([repo](https://github.com/yigitkonur/cli-continues), about 1.5k stars, last push 2026-05-07)
reads the session files of 16 agents and hands a session from one to another on the same machine. It is the
tool to reach for when you hit a limit in one CLI and want to keep going in another right now.

**catchup** ([repo](https://github.com/wilbeibi/catchup), 73 stars) does the same for 11 agents, locally.

**waybill** ([repo](https://github.com/wardmos/waybill), 95 stars) writes a local handoff bundle from the diff,
the commands run and the tests, and treats the bundle as untrusted when the next agent reads it.

**relay-dev** ([repo](https://github.com/Manavarya09/relay), 39 stars) is a local handoff CLI that detects
rate limits. Its name is close to ours; it is a different project.

## Claude Code's own features

Claude Code now has [agent teams](https://code.claude.com/docs/en/agent-teams),
[cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) and
[agent view](https://claude.com/blog/agent-view-in-claude-code). If every agent you run is Claude Code, start
there. They work only between Claude sessions, and the agent-teams docs note that two teammates editing the
same file leads to overwrites.

## Where Remembra Relay is different

- **Facts from git, not a paraphrase.** The done, not done and failing sections are read from git and test
  runs, not written by an LLM, and the agent's own summary is checked against them.
- **Across machines and vendors, with no local engine.** State lives on your Remembra server, so a laptop,
  a server and a cloud VM see the same trail. The next agent can be Claude Code, Codex or any MCP client.
- **Who wrote it.** With an agent-scoped key, a handoff is shown as key-verified, and that key cannot write as
  another agent.
- **Untrusted by default.** Everything another agent recorded reaches the next one inside an untrusted-data
  block, and text that looks like prompt injection is withheld.
- **A durable trail.** Handoffs don't expire; the trail keeps every one in order.

## When to pick something else

- You work on one machine, with no account and nothing leaving it: **continues**, **catchup** or **waybill**.
- You want the most-used memory plugin for one agent: **claude-mem**.
- You want handoffs and leases, free and local-first, with P2P sync: **agentmemory**.
- Every agent you run is Claude Code: start with **Claude Code's own features**.

Remembra Relay is for when the next agent is on another machine, from another vendor, or next week, and you
want the handoff to say what git says.
