# Crew mode: several coding agents on one repository

Crew mode lets Claude Code, Codex, Cursor, Gemini, Qwen and Kimi work on the same project at the
same time without stepping on each other. It builds on [Relay](../guides/relay.md): every agent
still starts with a brief and ends with a handoff, and Crew mode adds the parts that matter when
more than one agent is live:

- **Zones.** You say which folders belong together (`pos`, `payroll`, `billing`). The first agent to
  write in a zone holds it; the others are told "DO NOT TOUCH" and their writes there are refused.
- **Batons.** When an agent stops (out of credits, crashed, closed), its claims are kept for the work,
  not released. Its uncommitted work is saved to a git ref, and the next agent you choose picks up the
  baton with the code, the task and a report of where it stopped.
- **Tasks and reports.** A task links to zones and acceptance checks. "Done" needs a report
  (done / not done / failing / next), with each item labelled by where it came from.
- **A live view.** The dashboard's **Crews** section shows who is working where, what is held, who
  went quiet and what needs you, over a live connection. Email and a signed webhook tell you in real
  time when a baton needs a hand or a collision happens.

Crew mode is **off by default** on a server (`REMEMBRA_CREW_MODE`, see
[Running the server](#running-the-server)). Nothing on your machine changes until you run
`remembra-crew connect --apply` and confirm at a terminal.

---

## Trust model: what it can and cannot stop

Read this before relying on Crew mode.

Every agent on your machine runs shell commands as **your** user and holds an API key. Crew mode's
local parts (the Claude Code hooks, the git hooks, the `remembra-crewd` daemon, the read-only fence)
therefore **coordinate cooperative agents**. They stop honest mistakes: an agent editing the POS
folder another agent holds, committing over its work, or pushing it. **They cannot stop a determined
or prompt-injected agent on the same machine.** Such an agent can, with enough effort, run code the
gate cannot parse, kill the daemon, read the local key files, `chmod` a fenced file back or push with
the git hooks switched off.

What Crew mode does about that:

1. **Detects and records it.** A write the gate did not see still shows up: after every shell command
   the working tree is diffed, `pre-push` checks every commit in the push range (merge, rebase,
   cherry-pick and `--no-verify` commits included), the daemon re-checks the gate file's hash every
   30 s and the git hooks at every session start and heartbeat. Breaches, tamper attempts and bypasses
   become events in an append-only, hash-chained log, moments on the dashboard, and real-time alerts.
2. **Keeps the guarantees that matter on the server**, where a local agent cannot switch them off:
    - **Human-only actions need a dashboard login.** Overrides, hand-overs, freezes, waivers, approving
      a change that loosens `zones.yml`, bypass codes, settings and pausing an agent are accepted only
      from a dashboard session (sensitive ones need a login from the last 15 minutes). API keys, even
      admin keys, are never "human".
    - **Tenancy.** Every crew and every claim, task or message id is checked against your account and
      project access; a key restricted to one project cannot see or create crews for another.
    - **The policy protects itself.** `.remembra/**`, the crew entries in your agent settings, the git
      hooks and `~/.remembra/**` form a built-in zone no agent can claim. A `zones.yml` change that
      removes or weakens protection stays **pending** until you approve it in the dashboard.
    - **Agent text is data, not instructions.** Messages, task titles and handoff notes written by one
      agent reach another only inside the `<remembra-data untrusted="true">` block. A "decision" an
      agent records stays *proposed* until you confirm it; it never reaches a brief before that.
3. **Never pretends otherwise.** Every enforcement badge in the dashboard and this page say which layer
   applies.

A server-side required check on your git host (a GitHub check run that fails a push touching a zone
held by someone else) is the layer an agent cannot remove locally. It is **not in this release**; it
is planned for the next one. Until then, the push gate is also local.

### Enforcement by agent

| Agent | Before a write | At commit | At push | After the fact |
|---|---|---|---|---|
| Claude Code (hooks) | **enforced** (edits, shell writes, MCP writes, other agents' checkouts) | enforced | enforced | detected |
| Codex / Gemini / Qwen / Kimi, verified with `remembra-crew verify --agent <name>` | enforced where verified (Codex: shell commands only) | enforced | enforced | detected |
| The same agents, not verified, in their own worktree | read-only fence (cooperative) | enforced | enforced | detected |
| Cursor | read-only fence | enforced | enforced | detected |
| MCP-only agents | advisory (`crew_guard`, refused claims) | enforced if the git hooks are installed | enforced if the git hooks are installed | detected from checkpoints and close |
| Any agent using a code you issued | allowed, recorded | allowed, recorded | allowed, recorded | moment + audit |

"Enforced" here means enforced **for an agent that cooperates**, in the sense above. Local
enforcement coordinates cooperative agents. It cannot stop an agent deliberately working around it on
your machine; every bypass is recorded.

---

## Before you start

- A Remembra server with Crew mode on (`REMEMBRA_CREW_MODE=true`), and Relay working on this machine:
  `remembra-relay brief` should print a brief. See [Relay setup](../guides/relay.md#setup) for the key.
- macOS or Linux (Windows is not supported yet), git, and one **worktree per agent** (two agents in
  one checkout are allowed but warned: a shared checkout cannot tell whose edit is whose for certain).
- Your plan's crew limits: see [Cloud plans](../reference/plans-and-credits.md). Over the live-session
  limit an agent joins in observe-only mode: it still sees DO NOT TOUCH and is still refused by others'
  claims, it just cannot claim. Protection is never lowered by a plan limit.

## Setup

### 1. Connect this machine (asks before it writes)

```bash
remembra-crew connect                       # dry run: prints the diff of every file it would write
remembra-crew connect --apply               # same diff, then asks you at the terminal
```

`connect` writes nothing until you confirm at an interactive terminal (`--yes` skips only the question,
never the terminal check, so an agent cannot run it for you). It keeps a backup of each file it
changes, is safe to re-run, and replaces the plain Relay hooks. What it can install:

| What | Where | Option |
|---|---|---|
| The gate (`crew-gate.py`, standard library only) | `~/.remembra/crew/bin/` | always |
| Claude Code crew hooks (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, StopFailure, PreCompact, SessionEnd) | `~/.claude/settings.json` | default (`--scope global`); in repos without crew mode the hooks exit in under 10 ms |
| The same hooks for chosen repos only | `<repo>/.claude/settings.json` (or `settings.local.json` if the former is tracked) | `--scope project --repo PATH` |
| Observe-mode crew hooks, not yet verified for Crew mode: Codex (its Relay hooks are verified), Cursor, Gemini, Qwen, Kimi | their own config files | `--include-unverified` |
| The `remembra-crewd` supervisor | launchd LaunchAgent (macOS) or systemd `--user` unit (Linux) | on unless `--no-service` |
| `pre-commit`, `prepare-commit-msg`, `pre-push` gates, chained after Husky, lefthook or existing hooks | the repo's hooks directory, never committed | `--git-hooks --repo PATH` |
| The Relay + Crew section of an AGENTS.md | the file you name | `--agents-md PATH` |

A typical first run:

```bash
remembra-crew connect --git-hooks --repo ~/code/yaadbooks --apply
```

### 2. Turn Crew mode on for a repository and declare zones

A repository is a crew repository when it has a `.remembra/` folder (or when its project already has a
crew). Declare zones in `.remembra/zones.yml` and **commit it on the default branch**: the daemon only
uploads the committed version, or one you push yourself with `remembra-crew zones push` at a terminal
while a crew session is live.

```yaml
version: 1
zones:
  pos:
    title: POS
    include: [src/app/pos/**]
    fail_closed: true          # stays denied to others even when the server cannot be reached
  payroll: [src/app/payroll/**]
  reports:
    include: [src/app/reports/**]
    exclude: [src/app/reports/fixtures/**]
  database:
    include: [supabase/**]
    services: ["schema:main"]
    commands: ["supabase db push *"]   # argv-prefix patterns with * (never regexes)
commons:
  package.json: plain
  supabase/migrations/**: append_only
ignore: [docs/**]
```

Every key is checked; an unknown key is an error, never ignored, so a typo cannot weaken protection.
If a second agent joins a repository that has **no** zones, Crew mode applies temporary zones from the
top-level folders at once (announced in the brief, undone in one click in the dashboard). Tightening
the file applies on upload; loosening it (removing a zone, lowering enforcement, removing
`protected`/`fail_closed`/`reserve_for`) waits for your approval under **Needs you**.

### 3. Check it

```bash
remembra-crew doctor        # gate intact, crewd running, server reachable, commit gate per session
remembra-crew status        # inside a session: YOU line, DO NOT TOUCH, offered batons
```

Start a Claude Code session in the repository. Its first message carries the brief plus a crew block,
and the dashboard's **Crews** section shows its lane. For another agent, prove its hooks on this
machine before trusting them:

```bash
remembra-crew verify --agent codex     # asks you to run one prompt, then checks the file stayed unchanged
```

Only a passed round trip switches that agent from observe to enforce.

Finally, in the dashboard add an email address or a webhook for notifications, so collisions, tamper
attempts, bypasses and batons waiting for pickup reach you in real time. Your own login email works at
once; any other address first gets a confirmation code, and nothing is sent to it until you enter the
code in the dashboard. A webhook must answer a signed challenge before it is saved.

## Everyday use

Agents get the rules in their brief and the AGENTS.md section. The commands act for the agent
session they run inside (found from the process tree, never from an environment variable). From your
own terminal, `status` lists this machine's crew sessions, `doctor` checks the runtime, and
`zones push` uploads the working copy of `zones.yml` (it needs a live crew session on this machine
and uses that session's checkout).

| Command | What it does |
|---|---|
| `remembra-crew status` / `whereami` | Who holds what, your own claims and task, unread items |
| `remembra-crew claim <zone\|path> [--wait N]` | Claim before starting on a new area; `--wait` queues behind the holder |
| `remembra-crew release <zone> [--baton]` | Release, or hand the zone on with the saved work |
| `remembra-crew adopt <T-n>` | Take a baton that was **offered to this session** and restore its saved work |
| `remembra-crew task list\|create\|start\|update\|block\|unblock\|release` | Tasks; `start` claims the task's zones together |
| `remembra-crew checkpoint` | Record progress now (it also happens after commits and pushes) |
| `remembra-crew report <T-n> --done … --not-done … --failing … --next …` | The completion report a task needs |
| `remembra-crew say "…" [--to @agent] [--wait N]` | Post to the crew channel, optionally wait for a reply |
| `remembra-crew watch` | Follow the crew's event feed in a terminal |

MCP-only agents use the same actions as MCP tools: `crew_status`, `crew_claim`, `crew_guard`,
`crew_task`, `crew_say`, `crew_checkpoint` and `crew_report`.

### When an agent stops

- **Out of credits or rate-limited.** Claude Code reports it through its StopFailure hook; for other
  agents a transcript detector notices a limit message followed by five minutes of silence. The session
  becomes *quota blocked*, its uncommitted work is saved to `refs/remembra/baton/…`, its task becomes
  *stalled* with a report, and you get a **baton available** notice.
- **Crashed or closed.** A dead process is noticed within 30 s and handled the same way. A machine that
  goes quiet (lid closed, network lost) is *not* treated as a stall for 30 minutes, and never releases
  anything by itself.
- **Picking it up.** A new session in the same checkout picks the baton up automatically. In another
  checkout the baton is **offered** in that session's brief and only then can it adopt it, or you hand
  it over from the dashboard. Task batons never expire silently: you are reminded at 24 h and 72 h.
- **Coming back.** If the same session resumes before anyone adopted, it takes its claims back and the
  stall report is marked superseded.

### Sub-agents

A sub-agent can be a crew session of its own. A client joins it with `parent_session_id` set to the
live session that started it (same account, same crew), proving the link with that session's token
(`X-Remembra-Crew-Session`) or the parent's own agent key, and it gets its own callsign, heartbeats,
claims, tasks and checkpoints: what it holds is in its own name. The session that started it stays
accountable for it:

- every event the sub-agent causes names that parent session (`actor.parent_session_id`);
- the parent session may release the sub-agent's claims and block, unblock or release its tasks,
  which no other agent session can;
- when the parent session ends, its running sub-agents end with it: their claims are released, or
  kept for pickup with a stall report when a task is unfinished, as their own exit would do;
- the snapshot lists each sub-agent right after its parent, and the dashboard nests its lane under the
  parent's lane (`sub-agent of cc-1`), while the parent's lane lists its running sub-agents.

The Claude Code hooks start one crew session per Claude Code session. Its sub-agents (the Task tool)
run inside that session's process, so the gate counts their edits as that session's.

## Your controls (dashboard only)

Only a dashboard login can override or hand over a claim, freeze or protect a zone, pause an agent,
waive a report, confirm a decision, approve a `zones.yml` change, set up notifications, or switch a
project to **observe** (log would-be denials instead of refusing).

**Bypass codes.** There is no environment variable an agent can set to switch Crew mode off. When you
need to push past the gate yourself, issue a code in the project's **Policy** panel: it is single use,
scoped to one session and one surface (`commit`, `push`, or `write:<zone>` for edits in that zone
only), valid for up to 15 minutes, and the panel shows the exact command
(`REMEMBRA_BYPASS=<code> git push`). A code issued for `push` does not open a commit or an edit. When the server cannot be reached, `remembra-crew bypass --session <callsign>` asks for
confirmation at an interactive terminal. Every use is an audit entry, a moment and an alert.

## Removing it from a machine

Exit the agents' sessions first: the daemon lifts the read-only fence when a session ends, and after
an uninstall nothing does. Then:

```bash
remembra-crew connect --uninstall                # dry run of what would be removed
remembra-crew connect --uninstall --apply        # asks at the terminal
```

This removes the gate, the crew hook entries, the LaunchAgent or systemd unit and the git hooks that
`connect` installed. It does **not** put the plain Relay hooks back; run `remembra-relay connect --apply`
afterwards if you want Relay without Crew mode. If a fence was still up, the files it made read-only are
listed with their original modes in `~/.remembra/crew/fence/`; restore them with `chmod u+w`. Your
repositories, `zones.yml` and the server's crew history are untouched.

---

## Running the server

This section is for self-hosters and operators. Remembra Cloud's own procedure is in
[Deploying](../DEPLOYING.md#crew-mode).

### The feature flag

| Variable | Default | Meaning |
|---|---|---|
| `REMEMBRA_CREW_MODE` | `false` | `1`, `true`, `yes` or `on` (any case) turns Crew mode on at startup: the `/api/v1/crews…` routes, the crew WebSocket frames, the MCP crew tools, the crew block in briefs, and the background jobs (event bus, outbox worker, reaper, retention, notifications). Anything else is off. Read once at startup, so a change needs a restart. |
| `REMEMBRA_CREW_DB_PATH` | next to the main database | Where `crew.db` lives. Leave it unset so one volume and one backup cover both files. |
| `REMEMBRA_CREW_DB_TAILER` | `false` | Only for more than one server process: each process then tails `crew.db` for events written by the others. The default deployment runs one process. |
| `REMEMBRA_CREW_LIVECHECK_BLOCKED_CIDRS` | none | Extra networks the acceptance live checks must never fetch (private, loopback and metadata ranges are always blocked). |
| `REMEMBRA_DASHBOARD_URL` | `https://app.remembra.dev` | Base URL for links in crew notifications. |
| `REMEMBRA_NOTIFY_SIGNING_KEY` | the JWT secret | Key the per-target webhook signing secrets are derived from. Changing it (or the JWT secret when this is unset) changes every webhook's secret. |

With the flag off, the server behaves exactly as it does without Crew mode: the crew routes return 404,
`crew.db` is not opened, and memory, Relay and the dashboard work as before. `GET /health/ready` shows
the state under `components.crew`: `disabled`, or `ok` with `schema_version` and `latest_version`
(`degraded` if `crew.db` cannot be read or is behind the code's migrations).

### What is stored where

Crew state (crews, sessions, zones, claims, tasks, reports, messages, the event log) lives in
**`crew.db`**, a separate SQLite file with its own connection, so crew writes never wait behind memory
writes. The main database gains one additive migration (**v5**: project and crew columns on the agent
inbox). Nothing existing is changed or removed, and older releases keep working on a v5 database.

When an account is deleted, account erasure covers `crew.db` too. The crews it owns go with every
row in them. In a crew someone else owns, its sessions, messages, checkpoints, reports, claims and
decisions no human adopted are deleted. Tasks and decisions in force stay as that owner's history,
with the account's name removed. That crew's event log keeps its hash chain: each event carrying the
account's id, sessions, messages, hosts or email keeps its sequence number, type, time and chain
links, and loses its actor, references, summary and payload. The nightly chain check still passes.

### Backups

Back up **both** files. They sit in the same directory by default.

- **Litestream (Remembra Cloud image).** `scripts/cloud-entrypoint.sh` replicates the main database to
  `LITESTREAM_REPLICA_URL` and `crew.db` to `LITESTREAM_CREW_REPLICA_URL`, which defaults to a sibling
  of the main replica (`s3://bucket/remembra` → `s3://bucket/remembra-crew`). On a boot with an empty
  volume it restores both. A failed `crew.db` restore stops the container when Crew mode is on, exactly
  like the main database (`LITESTREAM_ALLOW_EMPTY_START=1` overrides both).
- **Snapshots (any install).** A consistent copy of both files, safe while the server is running:

    ```bash
    python -m remembra.storage.snapshot create --out /data/backups   # prints the manifest as JSON
    python -m remembra.storage.snapshot verify /data/backups/remembra-snapshot-<stamp>
    ```

    Each snapshot holds `remembra.db`, `crew.db` (when it exists) and `manifest.json` with checksums,
    schema versions and row counts. Take one before turning Crew mode on and before upgrades.
- **Volume backups** (Docker named volume, disk snapshot) cover both files as long as `crew.db` was
  not moved elsewhere with `REMEMBRA_CREW_DB_PATH`.

### Restoring

Stop the server first: a running process keeps writing to the files it has open.

```bash
python -m remembra.storage.snapshot restore /data/backups/remembra-snapshot-<stamp>           # refuses if files exist
python -m remembra.storage.snapshot restore /data/backups/remembra-snapshot-<stamp> --force   # keeps them as *.pre-restore-<stamp>
```

The restore verifies the snapshot first, never deletes anything, and restores only the databases the
snapshot contains (a snapshot from before Crew mode leaves `crew.db` alone). The two files may be
restored from slightly different moments: effects that cross them (memory promotions, relay handoffs)
go through an idempotent outbox, so a replay never duplicates them. Agents reconnect on their own; a
session whose token is older than the restored state simply joins again.

### Rolling back

Crew mode is additive, so rollback never needs a data migration:

1. **Switch the flag off** (`REMEMBRA_CREW_MODE=false`) and restart. Crew routes disappear, briefs lose
   the crew block, MCP crew tools answer "unavailable", and `crew.db` is left as it was: switching the
   flag on again brings every crew back. New agent sessions stop joining and get the plain Relay brief.
   Sessions that joined earlier keep enforcing their last local view until they end, and about nine
   minutes after the switch they also stop writing in zones they held (their lease can no longer be
   renewed), so restart those agents.
2. **Stop the local side** on each machine where it matters: `remembra-crew connect --uninstall --apply`.
3. **Go back to an earlier release** if the problem is in shared code. A release from before Crew mode
   runs on the v5 main database (tested); `crew.db` is simply ignored. Inbox messages sent while rolled
   back have no project scope and stay visible to keys without a project restriction.
4. **Restore a snapshot** only if data itself is wrong, as above.

## Troubleshooting

| Symptom | Check |
|---|---|
| No crew block in the brief | Is the server's flag on (`/health/ready` → `components.crew.status` is `ok`)? Does the repo have `.remembra/`? `remembra-crew doctor` |
| "crew unavailable: crewd did not start" | `remembra-crew doctor`; on macOS `launchctl list \| grep remembra`, on Linux `systemctl --user status remembra-crewd` |
| A write is refused and the message names another agent | Working as intended: ask with `remembra-crew say`, work elsewhere, or hand the zone over in the dashboard |
| Refused again and again where you want the agent to proceed | Hand the zone over or override the claim in the dashboard; for a one-off push issue a bypass code; to stop refusing altogether switch the project to observe |
| "commit gate: missing" on a lane | A tool reinstalled the git hooks (Husky, lefthook). The daemon repairs it when you installed the hooks with `connect`; otherwise re-run `connect --git-hooks` |
| `zones.yml` change has no effect | It must be committed on the default branch, and loosening changes wait for approval under **Needs you** |
