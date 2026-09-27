# Changelog

All notable changes to Remembra will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.16.1] - 2026-09-26

Relay fixes, three more agents verified, a doctor for when handoffs don't arrive, and a security sweep. Every
git repository now gets its own project, the brief skips sessions that did nothing, Codex automations and
sub-agents stay out of the trail, a hook that another agent runs is filed under that agent, and the Gemini
CLI, Qwen Code and Kimi Code hooks were run against the real tools. `remembra-relay doctor` (and the
`remembra_doctor`, `remembra_setup` and `remembra_help` MCP tools) says where a handoff went missing, and
remembra.dev has a setup guide written for your agent. Deleting by entity no longer deletes the whole account,
billing and plan limits are tighter, and the public pages now say only what the code does.

**Upgrading.** Run `remembra-relay connect --apply` once after upgrading. For the agents it finds, it writes
the Gemini CLI, Qwen Code and Kimi Code hooks (Kimi Code's go to `~/.kimi-code/config.toml`; the block an
earlier release wrote to `~/.kimi/config.toml` is removed). If 0.16.0 put several of your repositories in one
project, `remembra-relay projects split` shows how it would separate them; nothing changes until you add
`--apply`. If handoffs still don't arrive, run `remembra-relay doctor`.

**Running your own server.** The dashboard and docs images now run nginx as a non-root user on port 8080
instead of 80: point your proxy or port mapping at 8080. The database stays at schema version 10 (migration 10,
`account_reviews`, is listed below); the new billing and relay tables are created at start or on first use.
After upgrading, back up the database and run `python scripts/maintenance/redact_stored_secrets.py --apply` to
redact command-line credentials from handoffs stored before this release.

### Added

- **Verified hooks for Gemini CLI, Qwen Code and Kimi Code.** Each was run against the real tool (Gemini CLI
  0.61.0, Qwen Code 0.24.6, Kimi Code 2.1.1) with a temporary home and a local stand-in for the model: the
  hooks `connect` writes put the brief in the model's request and saved the handoff. The payloads are recorded
  under `tests/fixtures/relay/`, and `REMEMBRA_RELAY_LIVE=1` runs the three live tests again. A plain
  `remembra-relay connect --apply`, the last step of the one-line install, now writes their hooks. Cursor is
  the one adapter left unverified.
- **`remembra-relay projects split`** gives each repository its own project again when 0.16.0 put them all in
  one (see Fixed). It is a dry run by default: it lists every repository bound to your configured project,
  the project each would get, every handoff that would move with it and the evidence, and everything that
  stays and why. A handoff moves only on evidence: the location the server recorded with it, or, for one
  closed before 0.16.1, a commit it recorded (its own commits, else its HEAD) that `split` finds with git in
  exactly one checkout on this machine, when every other repository that may share that history (a fork or
  clone) was read too. Folder sessions, checkpoints and anything unmatched stay. `--apply` carries it out in
  one transaction, never while a close of the account is in flight, and logs each change under a batch id (and
  one audit event); running it again moves only what is left. API keys and connections restricted to the
  project would be refused in the split repositories: they are listed, and applying needs
  `--keys-lose-access` (or add the new projects to them first). `remembra-relay projects undo --apply` moves
  a batch back, including what the repository wrote in its new project since (another checkout's location,
  its handoffs), so it resolves to one project again. Nothing is ever deleted, only your own account's data
  is read or moved, and recorded paths, names and headlines in the output pass the brief's trust policy.
  API: `POST /api/v1/projects/split` and `POST /api/v1/projects/split/undo`. The log table (`relay_refiles`)
  is created on first use; no schema migration.
- **`remembra-relay doctor`: where the baton dropped.** When a brief doesn't arrive or a handoff never reaches
  the trail, the doctor reads this machine and your trail and prints an exchange slip: every agent as a station,
  the last handoff, each read with its result, and for each problem the evidence, one fix and the re-check.
  Each verdict is marked proven (`[!!]`) or inferred (`[??]`). It checks the key (and whether the server
  accepts it, a firewall in front of it answered instead, or the URL redirects or serves a page instead of the
  API), the unsent-handoff queue by cause, closes that keep failing (a close counts as working again once its
  handoff is on your trail; the background-close log is shown with secrets redacted), each agent's hooks
  (missing, from an older connect, calling a command that is gone, or never written because `connect` only
  ran as a dry run; an unverified adapter `connect --apply` left out is only a note), Codex
  hook trust (read from `~/.codex/config.toml` against the current hooks with the hash Codex computes; an
  unreadable file is "unchecked", never trusted), Codex automation runs, a `REMEMBRA_PROJECT` that sends every
  new repository to one project, and agents that picked up briefs but never handed off at all. It only reads:
  at most four GET requests with your own key, never a brief, recall or write, and it never prints a key, even
  one pasted into the server URL field or a hook command. `--agent`, `--no-server`, `--format json`; exit 1
  when something proven needs you.
- **MCP tools `remembra_doctor`, `remembra_setup` and `remembra_help`** (local MCP server): the same doctor
  inside your agent, the exact install and connect steps for this machine's OS and agents (steps already done
  are marked), and answers quoted from the relay guide and the plans page, or "can't confirm". Questions about
  privacy, security, hosting, retention, deleting account data, training or money get the page that governs
  them, never a quote. None of them changes anything; fixes that involve your key are for your own terminal.
  Claude Code: `/mcp__remembra__doctor`.
- **`remembra-relay connect` ends with "You still need to"** when something is left: saving a key, `--apply`
  after a dry run, unverified adapters it skipped, and trusting the hooks in Codex (checked, not assumed). When
  nothing is left it prints no list.
- **`why?` on the dashboard's setup checklist.** Every agent still waiting on Home gets a `why?` button that
  opens an exchange slip under its row: the three reads it made (your keys, the trail, the agent's own entries,
  GET only), the call marked proven or inferred, the one fix, and the doctor lines to copy for that agent's
  machine. Where it reaches a fault the doctor also sees, it uses the doctor's rule id, sentence and guide page
  (`KEY_MISSING`, `PICKS_UP_NEVER_CLOSES`, `CODEX_TRUST_MISSING`, `NOTHING_WAITING`, `HOOKS_NOT_FIRING`,
  `STALE_CHECKPOINT`). Until a Codex brief or close arrives, Codex's row carries a dim reminder of the hook
  trust step (the dashboard can't see Codex, so it never says Codex needs you).
- **remembra.dev for your agent.** The hero's install block has `terminal | your agent` tabs; the agent tab
  copies a prompt that points the agent at `remembra.dev/setup.md`, a step-by-step runbook that stops for the
  key and asks before each write, and runs `pipx ensurepath` even when pipx is already installed.
  `remembra.dev/llms.txt` and `llms-full.txt` (generated from the pages) are
  served too. setup.md, `remembra_setup` and the dashboard give the same commands in the same order, and a test
  holds them to it.

### Changed

- **Gemini CLI:** a BeforeAgent hook (`brief --once`) gives a session started by `/clear` its brief with the
  first prompt (Gemini drops that start's output), and `connect` says that Gemini runs hooks only in trusted
  folders. Gemini CLI is detected by its binary or `~/.gemini/settings.json`, not by the `~/.gemini` directory
  it shares with Antigravity, and `connect` follows `GEMINI_CLI_HOME`.
- **Qwen Code:** a turn that stops on a rate limit or billing error (StopFailure) and `/compress` (PreCompact)
  also write the handoff, as for Claude Code. One-shot `qwen -p` runs write none: Qwen never ends them.
- **Kimi Code:** the adapter targeted the archived Python kimi-cli (`~/.kimi/config.toml`), which no longer
  runs sessions, and put the brief on SessionStart, whose output Kimi throws away. It now writes
  `~/.kimi-code/config.toml` (or `$KIMI_CODE_HOME`): the brief comes with the first prompt of a session
  (UserPromptSubmit, `brief --once`) and the close on SessionEnd, with timeouts. Relay hooks that `kimi migrate`
  copied without their markers are removed, and a file Kimi would reject is never written. The dashboard calls
  it Kimi Code.
- The brief's queued-handoff and rejected-key notices end with "Ask your agent to run remembra_doctor, or run
  `remembra-relay doctor`." `connect`'s no-key warning leads with the same key command as its to-do list.
- The dashboard's install line without a known server is `remembra-install --all`, which keeps the server the
  machine already uses (Remembra Cloud on a first install); with one, as on every dashboard page, it passes
  `--url` as before.
- The MCP `store-summary` prompt closes the session with `close_session` and facts (it used to ask for a
  free-form `store_memory` handoff); `setup-check` also runs `remembra_doctor`.

### Fixed (Relay)

- **Every repository gets its own project, even with `REMEMBRA_PROJECT` set.** In 0.16.0 a configured
  project (for example an old `REMEMBRA_PROJECT=clawdbot` namespace in an MCP config) named every repository
  the server had not seen, so all of them shared one trail and a brief in one repository handed over another
  repository's work. Now a git repository always gets its own project; the configured project names only
  folders that are not repositories. `REMEMBRA_RELAY_PROJECT` keeps everything in one project on purpose.
  New installs (`REMEMBRA_PROJECT=default`) were not affected. Older clients keep their behaviour: the new
  client says which rule it follows (`hint_scope`, `git_repo`), and the server records where each session
  worked with its handoff. A folder under the configured project that later becomes a repository (`git init`)
  gets its own project too. A key restricted to projects keeps using its configured project for a new
  repository (it records nothing). When git does not answer in time, the client says it does not know
  instead of "not a repository", sends no configured project, and still sends the close (`repo` in
  `incomplete`). Use `projects split` (above) for repositories already bound together.
- **The brief's "Last session" is the last session that did something.** A handoff that recorded nothing
  (no commits, changes, tests, errors, todos, next step, summary or notes: an idle or automated session) no
  longer buries the one before it; the brief skips it and says how many it skipped. "Recent" lists only this
  project's handoffs and checkpoints (at most five), never the namespace's other memories, which an agent
  could read as this project's status. In a folder that is not a git repository the brief says so in one
  line and names where the last session worked (repository and path), instead of asking the agent to check
  "the repository"; in a repository it says so when the last session worked in a different one or in a
  folder. The agent's notes and summary are shown under "Last session". A recorded location, upstream name
  and every Recent handoff pass the same trust policy as the handoff text, in the text and the JSON, and the
  JSON no longer returns the location's fingerprint keys (a path key is scrubbed like the path under the
  account's PII policy). Recorded text stays inside the untrusted-data block.
- **An empty session leaves no handoff.** `close` sends nothing for a session that recorded nothing and
  writes one line to `relay.log`; `close --summary …` by hand still sends. A close that stopped on a usage or
  billing limit, or was written before context compaction, is sent and shown ("stopped: rate_limit"). A later
  empty close of a session that already sent one is sent, so it retires the earlier handoff instead of leaving
  it as the session's last word.
- **Codex automation runs and sub-agents no longer fill the trail.** Codex Desktop runs the relay hooks for
  every run of a scheduled automation that starts its own thread (dozens a day), and sub-agent threads run
  them too, so each one got a project brief in its prompt and left a handoff that buried the sessions people
  work in, and the next brief pointed at an automation run. The relay now reads the kind of thread from the
  first line of the Codex session file: for an automation run in its own thread or a sub-agent, `brief` and
  `close` do nothing (nothing sent, nothing queued) and write one line to `relay.log`. This also holds when
  Codex runs a copy of Claude Code's hooks. Threads you start, voice chats included, and `remembra-relay close`
  typed by hand work as before. A heartbeat automation, which posts into a thread that already exists, is not
  skipped: its turns share that thread's brief and handoff. Set `REMEMBRA_RELAY_INCLUDE_AUTOMATIONS=1` to keep
  automation handoffs.
- **Other agents' sessions are no longer filed as Claude Code.** Grok Build, Cursor (IDE and cursor-agent),
  Devin and Continue run the Claude Code hooks in `~/.claude/settings.json`, and `gemini hooks migrate`,
  `kimi migrate` and Grok's `/import-claude` copy them. Each such session wrote a `claude-code` handoff (a
  Cursor one under a project named after `~/.claude`), and a Grok session was read as a Claude transcript. The
  relay now names the agent running a hook from fields or variables only that agent sets: the brief does
  nothing there, and the close is saved under that agent, or not at all when the relay has no adapter for it
  yet. A session with its transcript under `~/.claude/projects` is always Claude Code's. `connect` points out
  relay hooks an import copied into another agent's config.
- **One handoff per session end.** Gemini CLI fires SessionEnd two or three times on exit, the last with empty
  stdin; a repeat of the same close within a minute is dropped, and Gemini's empty one too. Claude Code's
  StopFailure followed by its SessionEnd still writes both (the later one replaces the first). A skipped
  automation or sub-agent is never counted as a repeat, and a repeat is dropped before the empty-session check
  runs.
- **`remembra-relay connect`** exits 0 when every write succeeded and warns about a missing key once, at the
  end (it exited 1 and warned twice), and says "server: not configured" instead of `http://localhost:8787`
  when nothing is set up. It reads configs with comments or a byte order mark (Cursor, Gemini CLI and Qwen
  Code accept them; it said "cannot read" and exited 1), follows `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and
  `QWEN_HOME`, and keeps hooks an earlier `--include-unverified` run wrote on the current relay path. For an
  agent named with `--agent` that is not installed it writes nothing without `--force`, since the directory
  it created looked like an install; `disconnect --apply` removes the directories `connect` created. Cursor's
  `close` prints `{}` (Cursor logs empty output as a failed hook), `hook-json` is labelled with the hook's own
  event, and `brief --format additional-context-json` prints `{"additionalContext": ...}`.
- **Found while reviewing the above:**
  - Gemini CLI: a session resumed with `--resume` got no brief (Gemini restores the conversation without
    SessionStart's context, and the relay took the session for one that had it); it gets it again now, and
    nothing is fetched when the brief came with a prompt, which Gemini does restore. The interactive UI does
    not wait for SessionStart: a first prompt typed while the brief was slow went without it, and
    `gemini -i "…"` got it twice. The first hook to finish gives it, once.
  - A Kimi Code (or cursor-agent) session resumed and ended again within a minute lost its second handoff:
    with no transcript to measure, a repeat is now only a copy of the hook arriving within a few seconds.
  - Cursor running Claude Code's PreCompact hook saved an ordinary end; it is now saved as the session still
    open, before a compaction.
  - Kimi Code: `connect` and `disconnect` deleted the tables after a relay hook `kimi migrate` had copied, when
    their keys held `:` or `/` (`[providers."managed:kimi-code"]`, `[models."kimi-code/k3"]`), so Kimi lost its
    providers and models. Tables are now found the way TOML defines them, and nothing is written unless the
    only change is the relay's own `[[hooks]]`. An install path with an emoji (or DEL) no longer makes the
    TOML invalid. The archived kimi-cli's `kimi` no longer counts as Kimi Code being installed.
  - A config file that is a symbolic link (into a dotfiles repository) was replaced by a regular file; it is
    written through now, and never unlinked.
  - With `CLAUDE_CONFIG_DIR` (or `CODEX_HOME`, `QWEN_HOME`, `KIMI_CODE_HOME`, `GEMINI_CLI_HOME`) set,
    `disconnect` missed the hooks an earlier release had written to the default place; it removes them, and
    `connect` keeps them current.
  - `connect --agent gemini` wrote `~/.gemini/settings.json` into Antigravity's `~/.gemini` without `--force`;
    an agent that is not detected is written only with `--force`. An unverified adapter that `connect` skips
    anyway no longer fails the run when its config cannot be read.
  - A close sends at most what the server keeps (500 paths per file list, 200 commands, 100 test runs, ...), so
    a repository with thousands of changed files no longer sends a body of several megabytes.
  - On a branch longer than 255 characters (git takes longer names) the brief failed with 422. The server
    keeps the first 255 characters and drops a `/`, `.` or `.lock` the cut leaves at the end, so the stored
    name is still one git accepts; a name git refuses is refused whatever its length.
  - Recorded text could close the brief's untrusted-data block in Gemini CLI and Qwen Code by writing the close
    tag as `&lt;/remembra-data&gt;`, which is what those agents turn the real tag into. The server and the relay
    now neutralize the tag in that form (and its other character-reference spellings) too.

### Fixed (accounts, privacy and the site)

- **Deleting by entity could delete the whole account.** `DELETE /api/v1/memories?entity=John`, which the
  Python SDK's `forget(entity="John")` and the TypeScript SDK's `forget({ entity: 'John' })` send, deleted
  every memory, entity, relationship and decision log in the caller's account and reported a wrong count; with
  `project_id` it deleted every memory in that project. Now only `all_memories=true` deletes the whole account,
  and a delete by entity deletes what it names: the caller's memories linked to the entity with that exact name
  or alias (any case), in every project or only in `project_id`, then the entity and its relationships once no
  memory mentions it, with the true counts. A call that combines `memory_id`, `entity` and `all_memories=true`,
  or has a blank `entity` or `project_id`, is refused with 422 and deletes nothing, and a project-scoped key's
  delete by entity stays inside its projects. In both SDKs `forget()` now takes exactly one of a memory id, an
  entity (with an optional project) or all memories, as the server does; `forget()` with no arguments and the
  Python `user_id=` argument (the server never accepted either: the call failed with 422) fail before anything
  is sent. A delete by entity reads the server version first and is not sent to a server older than 0.16.1.
  The MCP `forget_memories` tool and the Clawdbot plugin can now delete by entity too, in one project and only
  after a dry run and a confirmation phrase, like their project wipe.
- **Sign in with Google or GitHub on older accounts.** An account whose email was never verified (accounts
  made before email verification existed) is now linked when Google, or GitHub with a verified primary email,
  confirms the address: the email becomes verified and the user is signed in. A one-time account check then
  lists everything on the account (API keys, connected apps, webhooks, other sign-in links, 2FA and the
  password set before); **Keep all** is one click and keeps exactly the list shown, and single items can be
  revoked. API keys and app connections keep working throughout; dashboard sessions opened before end. 2FA
  from before verification stays on only if the owner enters a current code. A check with nothing to list
  finishes silently. Only the sign-in that proved the email can act on it; every choice is audit-logged and
  emailed. Schema migration 10 (`account_reviews`). GitHub still never links by email into an account whose
  email is already verified.
- **Forgot password** on such an account no longer revokes keys, 2FA, app connections, webhooks and sign-in
  links. The reset verifies the email and the next sign-in shows the same account check.
- **PII redaction** no longer replaces the project number of a Google OAuth client id (and UUIDs or
  similar machine identifiers) with `[REDACTED_BANK_ACCOUNT]`. Account numbers written with a suffix
  (`123456789012-checking`, `...-SAV`, `ACCT-...-01`) are still redacted.
- **The install line connects.** On remembra.dev, the README and the docs, the copyable install ended in a bare
  `remembra-relay connect`, a dry run that writes nothing, so a new user following it stayed unconnected. Every
  block now asks for the free key first and ends in `remembra-relay connect --apply`; the dashboard's empty
  trail shows its full one-line install. The site header has **Sign in** next to **Start free** (first in
  the phone menu).
- **docs.remembra.dev no longer publishes repository notes** (the cloud runbook, an old self-host note, bug
  write-ups, a feedback transcript and the competitor scan): `mkdocs.yml` excludes them and a test keeps the
  list. The feedback transcript left the public repository.
- **Public copy says what the code does.** The Founding 100 price holds while the subscription stays active,
  and 14 days after it ends (as the Terms say), with no lifetime promise. Pages no longer claim that every agent
  or tool is covered: the session hooks are verified for Claude Code, Codex (a prerelease), Gemini CLI, Qwen
  Code and Kimi Code, Cursor's are not yet, and any MCP agent can call `session_brief` and `close_session`.
  Transcript facts are read from Codex rollouts as well as Claude Code transcripts, and the pages say so. The
  Claude and ChatGPT connector and the hosted remote MCP are marked as coming (the connector is off at
  api.remembra.dev). The Team plan lists what the teams API enforces (one pooled allowance, not a shared
  memory pool), and the dashboard's team role labels say what each role can do today. The PyPI summary
  describes Remembra Relay, and the MCP Registry text names the agent its handoffs are recorded under. The DPA
  page says a deleted account is erased automatically after 7 days (backups age out), the plans page says a
  new yearly bank unlocks after 14 days, and the durability page no longer promises atomic writes across
  SQLite, Qdrant and the keyword index. The SDK and REST guides show the delete calls the client and server
  have, the MCP pages count the 24 tools the server registers (21, and Marshal's three), the site's changelog
  states the 0.16.1 project rule, and reconstructed blog examples say so. `tests/test_site_truth_polish.py`
  scans every public file for these claims.

### Security

- **Billing.** The Paddle billing portal opens only for a Paddle customer whose email is the account's
  verified email, and a customer id that another account already holds is never recorded (the account is
  flagged instead, once per subscription, and a flag already waiting on the account is kept). One payer paying
  for several accounts, which 0.16.0 recorded on each of them, is not a conflict: their renewals change
  nothing. Checkout and the billing portal need a dashboard sign-in, not an API key; an API-key
  session in the dashboard shows "Sign in with email" instead of the billing buttons. New purchases stay on
  the account's own email. Paddle events are applied once each and in order, and a paid renewal that arrives
  late still records the period it paid for. A yearly plan's next credit bank unlocks only once its renewal is
  paid. A new purchase of the retired $49 and $199 prices grants nothing and is flagged. Trial codes are
  refused for paying subscribers.
- **Plan limits hold on every write path.** Superseding a memory counts as a store, restoring an archived
  memory checks the memory cap, a conversation ingest that falls back to storing the raw messages counts every
  message, and a session close counts toward the Free plan's daily cap on notes without enrichment. Memory-cap
  slots are reserved atomically, so parallel writes can't go past the cap.
- **Server.** Request bodies are capped before authentication, including under a path prefix: 1 MiB, 64 KiB
  for a urlencoded form, and more only where a route's own limits need it (8 MiB for a relay close, a batch or
  bulk store and an inline import, 12 MiB for a conversation ingest, 51 MiB for a file import). An inline
  import (`POST /api/v1/transfer/import`) takes at most 8,000,000 characters of data; a larger file goes to
  `/transfer/import/file`. The web framework and form parser are upgraded (FastAPI 0.141.1, Starlette 1.7.0,
  python-multipart 0.0.32). `GET
  /api/v1/admin/permissions` requires an admin, and the admin promo routes check the master key. While the
  one-time account check after a sign-in that verified the email is open, changing the password, deleting the
  account and turning 2FA off wait until it is done. Entity relationships and background entity merges stay
  inside one project, and the dashboard's entity graph (`GET /api/v1/debug/entities/graph`) returns only the
  caller's own relationships: a caller with no entities in scope got other accounts' (`max_edges=0` is refused).
- **Relay and agents.** Credentials typed on a command line (`curl -u`, `docker login -p`, `vercel -t`,
  password variables and the like) are redacted from a handoff before it is stored, and ordinary values (test
  counts, askpass helpers, paths) are left alone. Terminal control characters are stripped from handoff text
  when it is stored and before the CLI prints it. The legacy Claude Code SessionStart script, the Clawdbot
  hook, the Clawdbot plugin (2.1.0) and the dashboard's "Copy as a prompt" hand other agents' text to the
  model inside the untrusted-data block. The server refuses a branch name git would refuse, and the
  dashboard's "Continue" command never passes one that git would read as an option.
- **Images, CI and the repository.** The Docker images use pinned, maintained base images; the dashboard and
  docs images run nginx as a non-root user on port 8080, and docs.remembra.dev gets its own nginx config with
  security headers. The docs workflow's OIDC and Pages write permissions sit on its deploy job only, and
  workflow installs are hash-pinned. Dependabot watches `uv.lock` and the dashboard, and CI runs `pip-audit`.
  CI and the git hooks refuse private notes, a built docs site, office documents, public IP addresses,
  real-format API keys and personal details in test fixtures; internal runbooks left the repository and the
  benchmark corpus is synthetic. CI checks every commit of a pull request and of a direct push (a key added
  and removed within one push is still published), and the pre-commit hook reads file names with spaces.
  Run `./scripts/install-hooks.sh` in your clone to get the current hooks.

## [0.16.0] - 2026-09-26 - Remembra Relay

**Remembra Relay: one agent stops, the next one already knows.** When a session ends, `remembra-relay close`
saves a handoff built from facts it reads from git (and, for Claude Code and Codex, the session's commands and
test runs); the agent's own summary is checked against them. When the next session starts, in another tool or on
another machine, that agent gets a short brief. Every handoff stays on the trail.

```bash
pipx install --force 'remembra[mcp]>=0.16'
remembra-install --all            # asks for the key at a hidden prompt
remembra-relay connect            # dry run; add --apply to write the hooks
```

- **Handoff:** done / not done / failing / next step, from git and the Claude Code transcript or Codex rollout;
  neither leaves the machine. **Brief:** about 1,500 tokens, everything recorded wrapped as untrusted data.
  **Trail:** every handoff and checkpoint in order (`remembra-relay trail`, the dashboard's Trail page).
- **Agent-scoped keys:** a handoff closed with one is key-verified; that key cannot write as another agent.
- **Adapters:** Claude Code and Codex (codex-cli 0.155.0-alpha.16.4, a prerelease) verified. Cursor, Gemini CLI,
  Qwen Code and Kimi shipped unverified (left out of `connect` unless `--include-unverified`); any MCP agent can
  use `session_brief` and `close_session`.
- **Existing users:** subscribers from before this release keep their plan and price on a grandfathered tier
  (with a monthly AI ceiling; lower note caps only after notice); a repository the server has not seen joins
  your configured project; briefs wrap recorded text as untrusted; `POST /memories` drops the relay-only
  metadata keys. Details under Changed (breaking) below.
- Billing moves to Paddle (Merchant of Record); after checkout, buyers land on the dashboard home
  (`https://app.remembra.dev/?checkout=success`), and Paddle's payment link is the dashboard's `/pay` page.
- remembra.dev is now served by nginx from `landing/` with its own config (security headers, a hashed-script
  CSP, `/.well-known/security.txt`, redirects for old paths); new refund, subprocessor and DPA pages.
- The CSPs of remembra.dev and app.remembra.dev ship as `Content-Security-Policy-Report-Only` for launch,
  because the Paddle checkout hosts could not be verified against a live checkout; the other security headers
  are enforced. Browsers report violations to the new `POST /api/v1/csp-report`, which logs them
  (`csp_violation`, no query strings). docs/OPERATIONS.md says how to switch to enforcing.

### Added
- **Codex hooks verified.** `remembra-relay connect` now writes Codex's SessionStart, UserPromptSubmit and
  SessionEnd hooks by default, after a round trip with codex-cli 0.155.0-alpha.16.4, the prerelease bundled
  in ChatGPT.app: a Claude Code close replayed through its verified hook path (not a live Claude Code
  session), then a real `codex exec`, run against a local stand-in for the model, that received the brief,
  ran commands and left its own handoff; recorded under `tests/fixtures/relay/codex/`. No stable Codex
  release has been run, and hook trust was recorded as `/hooks` records it rather than through that screen. `connect` tells you to trust the hooks in Codex's `/hooks`, since Codex
  skips untrusted hooks without a message. Codex rollouts are parsed for commands, exit codes, test runs,
  edited files and plan steps. A session whose last turn stopped on Codex's usage limit is handed off as
  `ended: usage_limit`, with Codex's message first under the errors; the brief and the trail show it as
  `stopped: usage_limit`, like a Claude Code StopFailure.
- `brief --once`: the UserPromptSubmit hook delivers the brief when SessionStart did not fire (Codex
  auto-restoring a thread), once per session; a resumed Codex session does not get a second copy.
- `close` detaches for agents that do not wait for the end hook (Codex, Gemini CLI, Qwen Code, Cursor): it
  returns at once and posts from a background process (log: `~/.remembra/relay/last-detached-close.log`).
- **MCP Registry entry that starts the MCP server.** `server.json` now names the `remembra-mcp` launcher
  package (`uvx remembra-mcp`), declares `REMEMBRA_API_KEY` as a secret, and describes Remembra as
  cross-agent handoff. The previous entry ran the `remembra` web server. The release workflow publishes
  PyPI and the MCP Registry from one tag.

### Changed
- Gemini CLI hook timeouts are written in milliseconds (15000), Qwen Code's and Cursor's in seconds; the
  Cursor adapter prefers `session_id`, reads `transcript_path` and `CURSOR_PROJECT_DIR`. All three remain
  unverified: their payload fixtures are written from the docs, not recorded.
- Releases: every GitHub Action is pinned to a commit SHA, PyPI uses trusted publishing with attestations,
  and the publish jobs wait for approval in the `release` environment.

- **Sign in with GitHub and Google.** "Continue with Google" / "Continue with GitHub" on the dashboard's
  Sign in and Sign up pages (authorization code + PKCE; Google ID tokens verified against the JWKS with
  nonce). Only verified provider emails are accepted, and one account backs each verified email and provider
  account (`user_identities`). Google links into an existing account only when that account's email is
  verified; GitHub never links by email and is connected from **Settings → Security → Sign-in methods**
  (`POST /api/v1/auth/oauth/{provider}/link`, `GET`/`DELETE /api/v1/auth/identities`). The login code is bound
  to the browser by an `HttpOnly` cookie, so a code cannot be replayed from another browser (login CSRF). The
  owner is emailed when a provider is added. Providers without credentials are hidden and 404. Setup:
  `docs/guides/sign-in-providers.md`.
- `GET /api/v1/auth/providers`: enabled sign-in providers and the Turnstile site key; the Sign up page renders
  Cloudflare Turnstile when a site key is published, and checks the same password rules as the server.
- Email verification for `/cloud/signup` tenants (`/api/v1/cloud/verify-email/request` and `/confirm`); they
  are now held at the unverified-email credit cap until verified. Password signups get their verification
  link by email, completing a password reset verifies the email, and the dashboard has a `/verify-email` page
  and a **Resend verification email** button (Settings → Profile). One free account per verified email is
  enforced on every path (dashboard verify, API-signup verify, password reset, social sign-up).
- **Remembra Relay: session continuity across agents.** A connected agent leaves a structured handoff
  when it stops, and the next one picks it up at session start, in another tool, on another machine or in
  another checkout.
  - Location-independent project identity: `POST /api/v1/projects/resolve` maps a normalized git
    remote (then root commit, then path) to a per-user project id. The same repo on any machine,
    drive or worktree gets the same id. Adds project links (`/api/v1/projects/links`); a brief shows
    linked projects' latest handoff headlines.
  - `POST /api/v1/session/close`: session facts become ONE deterministic handoff
    (Done / Not done / Failing / Next step). The optional agent summary is grounding-checked against the facts.
    Idempotent per (agent, session). Upserts `last_agent:<project>` and `branch:<project>`.
  - `GET /api/v1/session/brief` accepts a location, leads with a "Last session: …" line and returns a
    compact `rendered` text (~1500 tokens). `GET /api/v1/trail` lists handoffs and checkpoints.
  - `remembra-relay` CLI (`brief`, `close`, `trail`, `resolve`, `connect`) gathers facts from git and
    Claude Code transcripts or Codex rollouts without uploading them. It is hook-safe (≤10 s, always exits 0).
    `connect` wires agent hooks through an adapter registry (Claude Code and Codex verified; Cursor, Gemini,
    Qwen and Kimi shipped unverified and dry-run only).
  - MCP: new `close_session` and `resolve_project` tools. `session_brief` is compact by default
    (`verbose=True` for the full JSON). The server instructions tell every MCP agent to brief at start and close before finishing.
  - Agent-scoped API keys (`agent_id` on key creation). Relay attribution comes from the key, not the request body.
- Migration 4: `project_fingerprints`, `project_links`, `api_keys.agent_id`.
- **Relay dashboard.** The signed-in dashboard is now mission control for Relay: Home (what changed since
  your last visit, the last handoff with a copyable continue command, unread messages, weekly recap, plan
  usage, a connect checklist), Trail (every handoff on a dashed rail, filterable by project and agent),
  Agents (activity per agent with a 14-day sparkline) and Inbox (write to an agent; the note leads its next
  brief). New read endpoints back it: `GET /api/v1/trail/summary`, `GET /api/v1/inbox/messages`,
  `GET /api/v1/inbox/summary`; `GET /api/v1/trail` gains `agent_id` and a per-item `detail`.
  Existing pages are restyled with the new light and dark tokens and work at phone width.

### Security
- A password reset on an account whose email was never verified now clears everything set up before it: API
  keys, sessions, 2FA, connector grants, webhooks and provider links. It protects a mailbox owner who takes
  back an address someone else pre-registered.
- Connecting Google or GitHub from Settings is bound to the browser that asked: `POST
  /api/v1/auth/oauth/{provider}/link` sets an `HttpOnly` cookie, and the start path is refused (and burned) in
  any other browser. Before, a start path minted for one account and opened by someone else attached their
  Google/GitHub identity to the first account (account-link CSRF).
- Paddle events only change the subscription an account holds. A cancel or update for another subscription no
  longer drops the account to Free or re-plans it, a new purchase is credited only with the server's checkout
  signature (or the account's own Paddle customer), a second subscription is flagged instead of overwriting
  the first, and checkout returns 409 while a subscription is active (legacy $49/$199 included).

### Fixed (deploy)
- `POST /mcp` (remote connector) returned 500 whenever rate limiting was on.
- An unreachable Redis rate-limit backend no longer turns rate-limited routes into 500s: limits fall back to
  process memory and `/health/ready` reports a degraded `rate_limit` component.
- Blank env values (`REMEMBRA_PUBLIC_URL=`, the `*_EFFECTIVE_AT` dates, list settings) no longer crash the
  boot, and list settings accept `a,b` as well as a JSON array.

### Changed (breaking)
- **`remembra-relay` / MCP location briefs: which project a git repository uses.** A repository the
  server has not seen joins the configured project (`REMEMBRA_RELAY_PROJECT`, else `REMEMBRA_PROJECT`
  from the environment, MCP env or credentials, unless it is `default`); only with nothing configured
  does it get its own per-repository project. Existing users keep one namespace; bind a repository
  elsewhere with `remembra-relay resolve --project <id> --bind`. `GET /session/brief` and `GET /trail`
  no longer record bindings (only close and `POST /projects/resolve` do), and a brief warns when the
  repository resolves to a project other than the configured one.
- Project-restricted API keys can no longer bind (`403`) or record new location bindings.
- The rendered brief wraps everything recorded by agents in `<remembra-data untrusted="true">…</remembra-data>`,
  marks agents as key-verified or self-declared, labels an agent's next step as an unverified
  suggestion, withholds low-trust text and flags a handoff from another branch/commit as possibly stale.
- `POST /memories` (and batch, bulk, PATCH, supersede) drop the relay-only metadata keys `relay` and
  `relay_key`; agent-scoped keys stamp their own `agent_id` on memories and inbox messages.
- MCP `session_brief` returns the pre-relay fields again by default, plus `brief`, `handoff_id` and
  `inbox_unread`; `compact=true` returns only the text brief (the unreleased `verbose` flag is gone).
  `close_session` and `store_memory` default to the project the last `session_brief` resolved.
- The SDK sends `X-Remembra-Agent-Id` only for ids the relay accepts (ASCII letters, digits and
  `._:@/+-`), so ids such as "Claude Desktop" no longer break requests; `GET /session/brief` accepts
  any agent id again.

**New plans and cost protection for Remembra Cloud.**

### Added
- **New plan catalog:** Free $0, Solo $12/mo or $120/yr, Pro $29/mo or $290/yr,
  Team $15/seat/mo or $150/seat/yr (3-seat minimum), Enterprise custom. Founding 100:
  Solo at $108/yr, annual only, first 100 accounts. Existing $49 Pro and $199 Team
  subscribers move to grandfathered `legacy_pro_49` / `legacy_team_199` tiers with
  $30 / $150 monthly AI ceilings; reduced memory caps wait for
  `REMEMBRA_MEMORY_CAP_NOTICE_EFFECTIVE_AT`.
- **Smart credits.** AI enrichment (extraction, consolidation, entity resolution) is
  metered in credits: `max(ceil(chars / 8000), actual LLM $ / 0.0025)` per store. A
  chunk-aware reservation (16 credits per 8K chunk) is taken before any LLM call on
  every write path, then settled from real OpenAI `usage` after the background work
  finishes and the rest refunded. Annual plans get the whole year's credits up front
  (configurable: `REMEMBRA_ANNUAL_CREDIT_UPFRONT_MONTHS`).
- **Degrade, never reject.** Out of credits (or with the global free-tier breaker
  open), writes are stored atomically without enrichment. Responses carry
  `X-Remembra-Enrichment: full|degraded|atomic` and `X-Remembra-Credits-Remaining`.
  A store is rejected (429) only at the memory cap or, on Free, past the daily
  unenriched-write cap.
- **Relay is free:** handoffs, checkpoints, status values, inbox messages, pickup
  briefs, trail reads and recalls never use credits (relay has a per-plan burst limit
  and a reported soft cap; recalls have monthly and burst limits).
- **Global free-tier circuit breaker:** once a month's free AI spend reaches
  max($50, 20% of last month's net paid revenue), all free enrichment degrades until
  month end.
- **Bounded enrichment queue** with per-tenant concurrency (Free 2, Solo 4, Pro/Team 8)
  and a global cap.
- **Signup hardening:** 3 signups/hour per /24, 20/day per email domain, optional
  Cloudflare Turnstile (`REMEMBRA_TURNSTILE_SECRET`), and (once enabled) new Free
  accounts hold 25 credits until the email is verified. Rate-limit storage can be Redis
  (`REMEMBRA_RATE_LIMIT_STORAGE=redis://...`; `redis` added to the `cloud` extra).
- `GET /api/v1/cloud/usage/summary` for the dashboard billing panel.

### Fixed
- **Claude Code's MCP server is written where Claude Code reads it.** `remembra-install` put
  Claude Code's `remembra` entry in `~/.claude/settings.json`, which Claude Code does not load
  MCP servers from, so `session_brief` and `close_session` never appeared there. It now writes
  the user-scope entry to `~/.claude.json` (the shape `claude mcp add --scope user` writes) and
  removes the old `settings.json` entry, which held the key.
- **Deleting an account cancels its paid plan** at Paddle before anything is deleted (a deleted
  account cannot reach Billing to cancel it). If Paddle does not confirm the cancel, the account is
  not deleted. What deletion then does is under "Added (wave 2)".
- `remembra-relay`: a queued handoff the server refuses (403, e.g. a key scoped to another
  agent) no longer holds back every other queued handoff; it stays queued and goes last.
- Printed install and connect diffs also hide secrets in `docker -e NAME=value` and
  `--header "Authorization: ..."` arguments, secret-named URL query parameters and
  multi-line TOML arrays and strings; also a JSON object passed as one argument
  (`--config '{"apiKey": ...}'`), token-shaped and UUID path segments of a URL
  (`https://actions.zapier.com/mcp/<token>/sse`), and JSON files with comments or
  trailing commas.
- `remembra-relay`: a queued handoff is sent only with the key of the config source
  that queued it, and only to the server it was queued for. A handoff queued before
  any key existed is kept for the server configured then (`REMEMBRA_URL`, else the
  local default), not sent to whichever server is set up next. `status` says why an
  entry is held.
- `remembra-install` without `--url` keeps the server already set up (`REMEMBRA_URL`,
  `~/.remembra/credentials`, an existing entry): re-running it no longer moves a
  self-hosted setup to api.remembra.dev while keeping that server's key.
- `remembra-install` refuses a value at the hidden prompt or on `--api-key-stdin`
  that is not shaped like a Remembra key (`rem_...`), without showing it.
- `remembra-install --remove` lists the `*.bak-*` backups that still hold the key;
  `--delete-backups` deletes them. The uninstall steps name them.
- `remembra-install` and `remembra-relay` answer `--version`.
- Windsurf: `remembra-install` wrote `~/.windsurf/mcp_config.json`, which Windsurf
  does not read. It now writes `~/.codeium/windsurf/mcp_config.json` (the file
  Windsurf's docs name for the editor), only with `--agent windsurf`: the path is
  unverified against Windsurf, so `--all` leaves it out.
- `remembra-bridge` accepts `--url` as well as `--upstream`; the docs showed `--url`
  and the wrong port (8766; it listens on 9819).
- The dashboard's "Use API Key instead" link no longer sits on top of the sign-in and
  sign-up buttons on phones: it is under the form instead of fixed to the corner.
- Privacy and subprocessor pages: the dashboard (app.remembra.dev) loads Google Fonts
  and Paddle.js on every page; security.html says what a release carries (PEP 740
  attestations on PyPI, provenance and an SBOM on the Docker image).
- Release workflow: the MCP Registry job waits for approval in the `release`
  environment like the other publish jobs, and the build job installs build and uv
  pinned by hash (`.github/release-requirements.txt`).
- Entity resolution (a ~3K-token LLM call) no longer runs for atomic stores:
  handoff, checkpoint, status, `skip_extraction` and degraded writes.
- **The credit reservation is a hard AI budget.** Every OpenAI, Anthropic and
  TypeSafe call is checked against the write's reservation before it is made; once
  it is used up the rest of the write stores verbatim facts and queued entity
  linking is skipped. Settlement never charges more than was reserved (excess spend
  is logged as platform loss), so `credits_used` cannot pass the plan ceiling.
  Sleep-time work is budgeted by the credits left.
- Stale-reservation expiry never releases a hold whose work is still running in
  this process; startup releases only holds opened before the process started, and
  expired-but-unsettled free holds keep counting toward the free breaker.
- A credit settle released after the task registry shut down is no longer lost:
  shutdown drains enrichment, then the registry, then every pending settle.
- **Paddle webhooks map the plan from the price ID only.** Unknown prices are
  ignored and logged instead of trusting browser-set `custom_data` (which could
  grant Enterprise, legacy tiers or Founding). Team grants exactly the seats paid
  for (a quantity below 3 is flagged, not rounded up). The Founding 100 cap is
  enforced atomically in the webhook (past the cap: plain Solo annual, flagged for
  refund). Client-side checkout lists only single-quantity plans; Team and
  Founding go through server checkout. Approved refunds and chargebacks
  (`adjustment.*`) end the plan and are subtracted from revenue.
- **Team members are billed to the owner's pooled account** (ledger, limits,
  memory cap), up to the seats paid for; new teams get the owner's paid seats.
- **Unverified-email credit hold is opt-in** (`REMEMBRA_UNVERIFIED_CREDIT_CAP_EFFECTIVE_AT`)
  and grandfathers accounts created before it; master-key `/cloud/signup` tenants
  (no user record) are exempt.
- TypeSafe (Jev) spend is metered: billed to the write on stores, skipped for Free
  recalls, recorded in paid AI spend for paid recalls.
- Free accounts: at most 300 stores without enrichment per UTC day (atomic, relay,
  degraded), and their embedding cost feeds the free breaker.
- Signup limits are charged only after Turnstile passes (a looser attempt cap runs
  first), so token-less requests cannot lock out a network or a company domain.
- Behind Cloudflare the client IP comes from `CF-Connecting-IP` when the forwarded
  chain reaches a Cloudflare edge range (`REMEMBRA_TRUST_CLOUDFLARE_PROXIES`).

### Added (relay launch)
- **Claude Code handoffs at a usage limit.** `remembra-relay connect` also runs `close` on Claude Code's
  StopFailure (`rate_limit`, `billing_error`, `account_on_hold`, `cloud_credential_error`) and PreCompact, so
  the handoff is written when work stops, not when the user later quits. The reason is read from `error`
  (what Claude Code 2.1.168 sends) or `error_type`; the brief says `stopped: rate_limit` and the trail
  headline starts with it. A later close of the same session supersedes it. `connect --apply` upgrades an
  existing SessionStart/SessionEnd install in place, with a backup.
- **No silent loss of a handoff.** A `close` that cannot be delivered is queued in
  `~/.remembra/relay/outbox/` (0600, atomic, bounded to 50 entries and 14 days, secrets redacted, never the
  key) and logged to `~/.remembra/relay/relay.log`; the next `brief` or `close` sends it, oldest first
  (a `close` sends it before its own handoff when time allows). A close carries the time its session ended
  (`closed_at`, clamped to the server clock and 15 days); the brief's "Last session" and the `last_agent`
  status follow that time, so a handoff delivered late never replaces a newer one, and the brief says when
  it arrived. The brief names queued handoffs and a rejected key in its first lines. New `remembra-relay status`: queue, last result
  per agent, and whether the server accepts the key.
- **`remembra-relay disconnect`** (dry run unless `--apply`, backups kept) removes the relay hooks, and
  **`remembra-install --remove`** the MCP entries. The relay guide and the dashboard (Settings → Account,
  also in the delete-account dialog) list the uninstall steps.

### Changed (relay launch)
- **`remembra-install` is a dry run by default**: it prints each change as a diff and writes with `--apply`
  (or a "y" on a terminal). The diff masks the Remembra key and hides every other secret in the file (other
  MCP servers' `env`/`headers` values, token/key/password settings, `--token`-style arguments, known
  credential formats); JSON is compared in the layout it is written in, so only the real change shows.
  `remembra-relay connect` / `disconnect` print their diffs the same way. A run that writes nothing exits 3, so the dashboard's
  `remembra-install ... && remembra-relay connect --apply` stops when you answer no. The key is saved to
  `~/.remembra/credentials` even when no agent config is found. It reads the key from `REMEMBRA_API_KEY`, a hidden prompt,
  `--api-key-stdin` or `~/.remembra/credentials`; `--api-key` still works but warns (shell history). Writes
  are atomic, keep a backup and leave the file 0600; each agent's entry gets its own `REMEMBRA_AGENT_ID`.
  `remembra-install-codex` no longer requires `--api-key`. `remembra-doctor` warns about a key file other
  users can read. The landing page and dashboard install lines no longer carry a key.
- The dashboard's install command now installs `remembra[mcp]>=0.16`, like the landing page and the guide
  (without the extra, `remembra-mcp` could not start).
- `remembra`, `remembra-server` and `remembra-mcp` name the missing extra and exit 1 instead of failing
  with `ModuleNotFoundError`; `remembra-mcp --help` and `--version` answer once its imports load.

### Added (wave 2)
- **Account deletion erases the account (R-11, R-23).** `DELETE /api/v1/auth/me` confirms with the password,
  or with a six-digit code emailed by `POST /api/v1/auth/me/deletion-code` for Google/GitHub sign-ins. Every
  subscription of the account that can still bill is cancelled immediately (502 and nothing deleted when
  Paddle does not confirm); the account is signed out everywhere, and after `REMEMBRA_ACCOUNT_ERASURE_GRACE_DAYS`
  (7) the erasure job removes every row and vector it owns, keeping a content-free receipt. Terms, Privacy and
  the dashboard say the same sentence. Main-DB migration 8 `account_erasure_and_founding_holds`.
- **Founding 100 closes at 100 (R-26)**: open checkouts hold a seat, a lapsed founder keeps the seat and price
  for 14 days, a payment past seat 100 is flagged `founding_over_cap` and alerted. The pricing page shows the
  seats left from `GET /api/v1/billing/founding`.
- **Yearly credit bank unlock (R-27)**: a new yearly plan unlocks its full bank 14 days after purchase; until
  then one month's credits are available. A refund after heavy use alerts the owner.
- **Brief trust policy (R-14, R-16)**: every agent-written line of the brief is scored; low-trust lines are
  withheld with an id to review, command- or URL-shaped text is flagged, and MCP tools return stored content
  inside the same untrusted data block. Inbox messages are redacted before storage and keep a trust score
  (main-DB migration 6 `agent_inbox_trust_score`).
- **Handoff health (R-21)**: each relay handoff gets a server grade (Ready, Ready with warnings, Incomplete,
  Conflicted, Blocked) shown in the brief, the trail and `remembra-relay close`.
- **Pickups (R-18)**: when a brief shows another agent's handoff, the pickup is recorded (ids and times only;
  main-DB migration 7 `relay_pickups`) and the trail shows who picked it up.
- **Transactional emails for Relay (R-29)**: welcome, verification, reset, key-created, plan-changed,
  payment-failed and subscription-ended emails, with a text part and Reply-To support@remembra.dev; no email
  carries an API key.

### Changed (wave 2)
- Session handoffs never count toward the notes-kept cap (R-17), and neither do the `last_agent:` and `branch:`
  status values a close writes; a project that holds only handoffs does not use a Free project slot.
- Social sign-in's callback is `/api/v1/auth/oauth/{provider}/callback` on the API host; an unchanged
  `/session/status` re-send is not charged; decay cleanup never archives relay handoffs or pinned rows; CI
  builds `Dockerfile.cloud` and boots it.
- Main-DB migration version 5 is left free for Crew mode (`crew_agent_inbox_scoping`); 6 to 9 apply before or
  after it (9 adds the `idx_memories_user_type` index the cap count uses).

### Fixed (launch review)
- Brief trust policy: pipe-to-shell through `sudo`/`env` or a shell's full path, download-then-run commands,
  hosts a download command names without a scheme, and links that leave the repository through `..`, `%2e`,
  a backslash or user info are flagged; reference-style and HTML images are removed from the brief text and
  every JSON field; injection text written with look-alike or fullwidth letters is withheld; the close
  response, trail and brief agree on the grade; the health line says when its facts were only reported by the
  agent. The trail carries the brief's verdict per entry, and the dashboard's Last handoff card shows the
  grade, pickups and a withheld or command notice; "Copy as a prompt" follows the same policy.
- MCP: `forget_memories` dry runs and `list_spaces` return their JSON in the untrusted data block; the stdio
  server logs to stderr, so a failed tool call no longer writes a log line into the JSON-RPC stream.
- Erasure keeps the rows an erased admin acted on in someone else's space or team (credited to the owner), and
  skips an account made active again. A payment that arrives for a deleted account is cancelled, not applied.
  Deletion releases a held Founding seat, says which subscription was cancelled when a later cancel fails,
  asks a team owner to confirm that the team ends, emails the erase date and undo address, and a sign-in to
  the deleted account says the same. The superadmin hard delete answers 503 (account kept deactivated and due)
  when the erasure fails.
- Founding 100: checkout needs a verified email, and one account gets one two-hour hold a day; a lapsed founder
  sees the offer and the date the seat is kept until even when the rest are taken. Pricing links carry the
  plan to the dashboard, and the seat counter treats `available: false` as closed.
- `remembra-relay close` queues a close the server has no route for (an API rolled back to an older build).
- The delete-account dialog opens over the whole page at any width.

## Remembra Cloud - 2026-07-16 (server release; the package carries it from 0.16.0)

These changes went live on Remembra Cloud on 2026-07-16. They were never published as a package of their
own: PyPI went from 0.13.2 to 0.16.0, the Relay release above, which includes all of them. (This section
was labelled 0.16.0 before the Relay release took that number.)

**Lossless memory + production reliability.** The theme of this release: what you
store is exactly what you can get back, and when something fails you can see why.
(Also promotes the previously-unreleased brain layer, 3D graph, and remote MCP —
all live on Remembra Cloud as of this release.)

### Added
- **Lossless memory (provenance-grade fidelity).** Until now, storing content ran it
  through LLM fact-extraction and kept only the derived facts — the verbatim original
  was discarded, and a drifted or hallucinated "fact" was indistinguishable from a real
  one. Now, whenever extraction derives facts, the **exact original text is preserved
  as an immutable source record** (`memory_type="source"`, keyword-searchable, never
  LLM-merged, no vector so it can't pollute semantic recall). Every derived fact
  carries a **receipt** — `metadata.source_id` pointing back to its source record —
  and is **lexically verified against the source**: facts whose content words don't
  appear in the original are stored flagged `verified=false` instead of silently
  trusted. Store responses now include `source_id`. Config: `enable_source_records`
  (default on), `fact_verification_threshold` (default 0.5).
- **Async enrichment mode (opt-in fast writes).** With `REMEMBRA_ASYNC_ENRICHMENT=true`,
  `store` persists the verbatim source and returns immediately (`enrichment: "pending"`);
  extraction/consolidation run in the background. Cuts store latency from seconds
  (full LLM pipeline in-request) to a single write. Off by default — the store
  response contract changes (derived facts land after the response).
- **Request IDs everywhere.** Every request gets a server-generated `X-Request-ID`,
  bound into all structlog lines and used as the `error_id` in 500 responses
  (previously always `"unknown"`). Prod failures are now correlatable end-to-end.
- **Litestream in the cloud image (opt-in).** `Dockerfile.cloud` now ships litestream
  with a new entrypoint: set `LITESTREAM_REPLICA_URL` and the SQLite database is
  continuously replicated to object storage (S3/R2/Tigris) and auto-restored onto an
  empty volume. Without the env var, behavior is unchanged.
- **Honest upstream error mapping.** Embedding-provider failures during store/recall
  now return `429` (with `Retry-After`) on rate limits and `502` on provider outages,
  via a typed `EmbeddingProviderError` carrying the upstream status — instead of
  collapsing everything into `500 "Failed to store memory"`.

### Fixed
- **Opaque store 500s (intermittent MCP `store_memory` failures).** Root cause:
  unbounded content reached the embedding provider; anything past the model's token
  limit made OpenAI return 400, surfaced as a generic 500. All embedding input is now
  clamped to a provider-safe cap (24K chars) — oversized agent payloads store
  gracefully instead of failing whole.
- **Wrong-prefix API calls no longer return the dashboard.** `GET /v1/...` (the real
  prefix is `/api/v1/...`) and `/metrics` used to fall through the SPA catch-all and
  return **HTTP 200 + index.html**, breaking JSON clients with `Unexpected token '<'`.
  They now return a clean `404 {"detail": "Not found"}`.
- **`rem_` API keys sent as `Authorization: Bearer` now authenticate.** Many HTTP
  clients default to Bearer; a `rem_` key is unmistakably an API key, so it now routes
  to API-key validation instead of failing JWT verification with a confusing 401.
  `X-API-Key` still takes precedence; real JWTs are unaffected.

### Performance
- **Embedding cache actually wired.** Identical single-text embeds (recall queries
  repeat heavily) are served from the in-memory cache, keyed by
  `provider|model|dimensions|text` so provider/model switches can never serve stale
  vectors. Measured on production: repeat recalls **1.1s → 0.13s (~8×)**, and every
  hit is an embedding API call not paid for.

### Added (promoted from Unreleased)
- **Brain layer — themed understanding of your memory (GraphRAG-style).** Remembra now
  clusters the entity graph into **communities (themes)** using a dependency-free,
  deterministic Louvain modularity engine (`remembra/brain/`), then labels and
  summarizes each theme. A new **Brain** tab in the dashboard surfaces the themes
  (with summaries), the most **central entities** ("god nodes"), and **surprising
  cross-theme links**, and the 2D/3D knowledge graph now **colors nodes by theme**.
  New API: `GET /v1/brain/communities`, `GET /v1/brain/insights`, `POST /v1/brain/analyze`.
  Communities recompute automatically in the sleep-time consolidation worker. This is
  the higher-level "what is my memory about" layer the leading systems (Microsoft
  GraphRAG, LightRAG) converge on — built natively, no heavy graph dependency.
- **Knowledge graph "Neural Universe" (3D).** A new immersive 3D view of the memory
  graph — glowing nodes sized by memory count, firing-synapse particles along
  connections, cinematic bloom, a starfield, and a slow orbital drift, so the graph
  feels like floating through your own mind. Toggle **✨ Universe / Flat** at the top
  (defaults to Universe when WebGL is available; the proven 2D graph remains the
  fallback). Both views share the click-to-see-real-memories panel. three.js loads
  lazily, only when the graph tab is opened. (`EntityGraphUniverse.tsx`, `KnowledgeGraph.tsx`)
- **Hosted/remote MCP (connect with a URL, no install).** The MCP server now runs
  as a multi-tenant streamable-HTTP endpoint: every caller authenticates with their
  own `X-API-Key` (no shared server key), an ASGI middleware binds that key per
  request, and the API scopes every operation to it — so one caller can never see
  another's memories. This lets any MCP client (Cursor, Windsurf, Claude Desktop,
  Cline, VS Code, …) connect with just a URL + key, eliminating the stdio-binary +
  PATH friction. See `docs/connect.md` and `docker-compose.mcp.yml`. Covered by
  `tests/test_mcp_remote_auth.py` (per-key isolation, no-key→401, key propagation).

### Fixed (from Unreleased)
- **Recall no longer surfaces superseded facts.** Memories retired by a newer belief
  (via the explicit `supersede()` API or the VERSION conflict strategy) are now marked
  with a queryable `superseded_by` column and **excluded from recall by default** —
  so after "I switched from Stripe to Paddle," recall stops returning Stripe. History
  stays queryable via `include_superseded=true`. Covered by `tests/test_supersession_recall.py`.
- **Recall queries with FTS5 operators no longer crash or mis-match.** Natural-language
  recall containing `AND`/`OR`/`NEAR`, `note:` (column-filter syntax), or punctuation
  like `/` and `(` previously raised `fts5: syntax error` (HTTP 500) or silently matched
  nothing. The keyword arm now tokenizes and quotes the query into a safe MATCH
  expression. Covered by `tests/test_fts_query_sanitizer.py`.

### Security
- **Closed an FTS5 schema-disclosure vector.** A recall query like `note: secret` was
  interpreted as an FTS5 column filter and leaked table column names through the
  database error message. User input is now always quoted as literal search terms.

## [0.15.0] - 2026-06-06

### Removed (BREAKING)
- **Stripe removed entirely — Paddle is the only billing provider.** Per a
  security/sales requirement (prior Stripe breach):
  - Deleted `cloud/billing.py` (Stripe `BillingManager`) and
    `cloud/webhook_email_integration.py`, plus the `scripts/setup_stripe.py` SDK script.
  - Removed `/api/v1/cloud/checkout`, `/api/v1/cloud/portal`, and the unauthenticated
    `/api/v1/cloud/webhook/stripe` endpoints; signup no longer creates a Stripe customer.
  - `api/v1/billing.py` is Paddle-only; `promocodes.py` dropped the Stripe-coupon path.
  - Removed all `stripe_*` settings + vestigial `billing_provider`; added `extra="ignore"`
    so leftover `REMEMBRA_STRIPE_*` env vars in a deployed environment never break boot.
  - Removed the `stripe` dependency. No Stripe SDK import or API call remains.
  - Legacy `stripe_customer_id`/`stripe_subscription_id` DB columns are left inert (no
    destructive migration); they are no longer read or written by active code.

### Fixed
- **Memory graph reveals the actual memories on node click.** Previously the panel
  showed only a "Total Memories: 5" count. It now fetches the entity's real memories
  (`GET /entities/{id}/memories`, project-scoped) and renders each one's content + date
  in a scrollable list with loading/error/empty states and a "showing N of M" indicator.
- Malformed memory IDs return a clean 404 instead of 500 across all id-resolving routes.

## [0.14.0] - 2026-06-05

### Security
- **O(1) API key validation** — Key validation previously loaded *every* active key
  and bcrypt-checked the candidate against each one on a cache miss (O(n)). Because
  invalid keys never cached, this doubled as a CPU-exhaustion vector: spraying random
  `rem_…` keys forced a full bcrypt scan per request. Validation now uses an indexed
  deterministic lookup column (`api_keys.key_lookup` = sha256 of the key) for a single
  indexed read, with bcrypt retained as the at-rest verifier (defense in depth). Legacy
  keys backfill lazily on first use; once migrated, unknown keys are rejected in O(1).
- **Master-key admin endpoints now fail closed** — `require_master_key` previously
  allowed requests through when no master key was configured ("fail open"), leaving
  tenant-signup and promo-admin endpoints unprotected on a misconfigured server. It now
  denies in production (debug may bypass for local dev) and compares the key with
  `hmac.compare_digest` (constant-time, no timing leak).
- **Fixed broken master-key path in key creation** — `POST /api/v1/keys` read a
  non-existent `app.state.settings` / `settings.master_api_key`, so the admin
  key-creation branch errored. It now reads the real `auth_master_key` via
  `get_settings()` with a constant-time comparison.
- **Audio capture endpoints require authentication** — `POST /api/v1/audio/start` and
  `/stop` were unauthenticated. They now require a valid user and bind each capture
  session to its owner; only the owner may stop/transcribe a session.
- **Repaired `get_optional_user`** — the optional-auth dependency never validated a
  supplied key (it fell through to `None`), a latent landmine for any future endpoint
  using it. It now validates the key and returns the authenticated user.
- **Startup posture warnings** — production boot now logs explicit warnings when the
  master key or encryption key are unset (in addition to the existing hard-fail on a
  default/short JWT secret).

### Added
- **Salience-aware memory** — memories can now be marked important or pinned:
  - `POST /api/v1/memories/{id}/pin` / `…/unpin` — pinned memories are never pruned by
    temporal decay or TTL expiration ("never forget this").
  - `PATCH /api/v1/memories/{id}/importance` — set a salience score in `[0,1]`; higher
    importance feeds the decay model so the memory retains relevance longer.
  - New columns `memories.importance` and `memories.pinned` (additive migration;
    existing behavior unchanged when unset). Decay and cleanup honor both.

### Tests
- 19 new tests: `test_api_key_lookup.py` (O(1) validation, lazy backfill, rejection,
  revocation, role/scope normalization), `test_master_key_auth.py` (fail-closed +
  constant-time), `test_salience.py` (pin protects from TTL + decay, importance slows
  decay, user-scoped setters). Full suite: **644 passed, 6 skipped**.

## [0.13.2] - 2026-04-25

### Added
- **Product Surface Refresh** — Reworked the landing page into a live product narrative with memory graph, product dock, API system, and operator-focused proof points.
- **Dashboard Control Plane** — Added a dashboard overview surface that brings live memory health, API posture, graph signals, and operational next steps into one view.
- **Release Notes Page** — Added public landing changelog coverage for the latest product surface and dashboard improvements.

### Changed
- **Truth Scrub** — Updated landing, docs, and generated site copy to remove stale claims and unsupported phrasing.
- **Billing Checkout Safety** — Removed hardcoded Paddle client token usage from tracked frontend files; checkout now initializes from runtime billing config.
- **Security Docs** — Replaced real-looking key examples with environment-variable based examples.
- **Git Safety** — Hardened ignored credential patterns for local agent/tooling files, secrets, keys, and deployment context.

### Verified
- `uv run mkdocs build`
- `npm run build` in `dashboard`
- `npm run build` in `landing`
- Staged publish/security scans for blocked files, live-looking tokens, production IPs, and whitespace.

## [0.13.1] - 2026-03-30

### Added
- **Cold Archive Tier** — Separate queryable storage for decayed memories
  - `archived_memories` table with full memory schema + archive metadata
  - `archive_memory()` moves memories to cold storage with final relevance score
  - `restore_memory()` brings memories back to active storage with re-indexing
  - Keyword search in archive (semantic search requires restore)
  - Archive statistics and breakdown by archive reason
  - Cleanup job now uses real cold storage instead of soft-archive flags

- **Adaptive Thresholds** — Dynamic pruning based on session context
  - Session modes: `exploratory` (0.5x), `operational` (1.5x), `balanced` (auto)
  - Warm-up phase: first 10 queries use conservative 0.05 threshold
  - Quality-aware: high result quality → higher threshold (more selective)
  - Density-aware: more memories → slightly higher threshold
  - Session persistence to `adaptive_thresholds` table
  - Cleanup job integration with `use_adaptive_thresholds` flag

- **New API Endpoints**
  - `GET /api/v1/temporal/archive` — List archived memories
  - `GET /api/v1/temporal/archive/stats` — Archive statistics
  - `GET /api/v1/temporal/archive/{id}` — Get specific archived memory
  - `POST /api/v1/temporal/archive/{id}/restore` — Restore to active storage
  - `GET /api/v1/temporal/archive/search` — Search archive by keyword
  - `GET /api/v1/temporal/adaptive/threshold` — Current adaptive threshold info
  - `POST /api/v1/temporal/adaptive/mode` — Set session mode
  - `POST /api/v1/temporal/adaptive/reset` — Reset calibration

### Changed
- `TemporalCleanupJob` now accepts `adaptive_manager` parameter
- Archive moves memory completely (removes from Qdrant + FTS), not just metadata flag
- Decay cleanup uses adaptive threshold when available

### Technical
- New `adaptive.py` module with `AdaptiveThresholdManager`, `SessionContext`, `SessionMode`
- Database schema additions: `archived_memories`, `adaptive_thresholds` tables
- 12 new unit tests for adaptive threshold behavior
- All 109 temporal tests passing

---

## [0.13.0] - 2026-03-27

### Added

#### Dashboard v2.0
- **Admin Dashboard** — Full user management panel
  - View all users with plan, memory count, API keys, status
  - Delete, deactivate, or reset user passwords
  - Change user plans (Free/Pro/Team/Enterprise)
  - Search and filter users

- **Two-Factor Authentication** — TOTP-based 2FA
  - Enable via Settings > Security
  - Works with any authenticator app (Google, Authy, 1Password)
  - Backup codes for account recovery

- **Activity Log** — Security audit trail
  - Track account and API activity
  - Color-coded by event type
  - JSON export for compliance

- **Team Collaboration** — Shared memory spaces
  - Create teams with role-based access (Viewer/Member/Admin)
  - Invite members with role picker
  - Link projects to teams
  - Shared memory across team members

- **Entity Browser** — Visual entity exploration
  - Browse extracted people, organizations, places, concepts
  - Click to see related memories
  - Entity counts and distribution

- **Timeline Timezone Fix** — Proper local time display
  - Dates now show in user's local timezone
  - "Today" header for current day
  - Times in 12-hour format (e.g., 09:57 AM)

- **Knowledge Graph** — Visualize entity relationships
  - Interactive graph view
  - Bi-temporal relationship queries

- **Settings Rebuild** — Complete settings overhaul
  - Profile, Password, Security, Workspace, Retrieval, Diagnostics, Account tabs
  - Calibration API for retrieval tuning
  - Diagnostics for system health

#### TypeScript SDK (npm)
- **npm package** — `npm install remembra`
  - Full TypeScript support with types
  - Browser and Node.js compatible
  - Async/await API

### Fixed
- **RBAC Enforcement** — Viewer role properly restricted from store/delete
- **SSRF Protection** — Webhooks block private IP ranges
- **Error Sanitization** — No Python exceptions leaked to clients
- **Bcrypt Performance** — SHA256 cache mitigates O(n) timing

---

## [0.12.1] - 2026-03-23

### Fixed
- **Documentation** — Updated PyPI README to reflect v0.12.0 features
- **CI/CD** — Added automated PyPI publishing to release workflow

## [0.12.0] - 2026-03-22

### Added
- **User Profiles API** — Aggregated user intelligence endpoint
  - `GET /api/v1/users/{user_id}/profile` returns facts, activity metrics, top topics
  - Memory count, entity breakdown, last active timestamp
  - Aggregated facts summary for quick user context
  - Perfect for personalization and user insights dashboards

- **Smart Auto-Forgetting** — 35+ temporal patterns automatically set TTL
  - `"meeting tomorrow"` → 36h TTL
  - `"call next week"` → 8 days TTL  
  - `"deadline in 2 hours"` → 3h TTL
  - Supports relative dates, specific times, and duration phrases
  - Zero configuration — just store memories naturally

- **Strict Mode 410 GONE** — Opt-in explicit expiry awareness
  - Enable via `REMEMBRA_STRICT_MODE=true` or config
  - Expired memory requests return `410 GONE` instead of silent accept
  - Allows agents to handle expiration explicitly
  - Prevents stale data from being silently used

- **Event-Driven Expiry** — Explicit timestamp control
  - New `expires_at` parameter on store endpoint
  - ISO 8601 format: `"2026-03-25T14:00:00Z"`
  - Takes precedence over TTL when both specified
  - Perfect for event-driven workflows

- **Shadow TTLs Client-Side** — SDK performance optimization
  - SDK maintains local expiry cache
  - Skip recall for known-expired memories
  - Reduces API calls by up to 40%
  - Automatic cache invalidation on store

### Changed
- Store endpoint now accepts `expires_at` parameter alongside `ttl`
- SDK client caches expiry metadata for performance
- API responses include `expires_at` in memory objects when set

### Documentation
- Added User Profiles API to REST API guide
- Added strict_mode configuration documentation
- Updated store endpoint with expires_at parameter

---

## [0.10.2] - 2026-03-16

### Deployment Marker
- **Forced backend rebuild** to ensure production matches `main`
- **Live smoke suite identified deploy drift** on scoped API keys and memory listing

### Fixed
- **Project-scoped API keys** — response models include `project_ids`, auth chain carries project restrictions, and restricted keys enforce project boundaries
- **Memory listing endpoint** — `GET /api/v1/memories` restored for dashboard browse/search surfaces
- **Production verification** — health, analytics, graph, timeline, spaces, and team-space flows rechecked against the deployed API

## [0.10.1] - 2026-03-15

### Production Validated ✅
- **api.remembra.dev** — Live and verified with proper health response
- **Encryption** — AES-256-GCM confirmed working in production
- **Qdrant** — Vector store healthy and operational
- **All agents** — Claude, Codex, Cursor, Gemini, Windsurf integration tested

### Added
- **Centralized Credentials** — `~/.remembra/credentials` with chmod 600
  - API key saved on first install, auto-loaded for future installs
  - Priority: CLI arg > env var > credentials file
- **Slim Recall Mode** — 90% smaller payload for token-constrained agents
  - `recall_memories(query, slim=True)` returns only synthesized context
- **Bridge Lifecycle Management**
  - `remembra-bridge --stop` gracefully stops running bridge
  - `remembra-bridge --status` checks if bridge is running and healthy
  - Port-in-use detection with clear error messages
  - Health check after startup

### Fixed
- **Encryption Key Format** — Production deployment now requires `base64:` prefix for `REMEMBRA_ENCRYPTION_KEY`

---

## [0.10.0] - 2026-03-15

### Added
- **Universal Agent Installer** — One command to configure supported AI tools
  - `remembra-install --all` auto-detects and configures installed agents
  - `remembra-install --agent <name>` for specific agent setup
  - `remembra-install --detect` lists installed agents
  - Supports: Claude Desktop, Claude Code, Codex CLI, Gemini, Cursor, Windsurf
  - Safe config merging — preserves existing MCP configurations
  - **Centralized credentials** in `~/.remembra/credentials` (chmod 600)
    - API key saved on first install, auto-loaded for future installs
    - No need to pass `--api-key` every time after first setup

- **Setup Diagnostics** — `remembra-doctor` command for troubleshooting
  - `remembra-doctor all` scans all detected agents
  - `remembra-doctor <agent>` diagnoses specific agent
  - Checks: config loading, command resolution, health probe, recall test
  - Clear failure labels: `dns_failure`, `sandbox_blocked`, `auth_failure`, `timeout`

- **Slim Recall Mode** — 90% smaller payload for token-constrained agents
  - `recall_memories(query, slim=True)` returns only synthesized context
  - Full mode still available with `slim=False` (default)
  - Reduces recall response from ~2KB to ~200 bytes

- **Local Bridge** — Proxy for sandboxed agents (Codex CLI)
  - `remembra-bridge` runs local HTTP proxy on 127.0.0.1:9819
  - Forwards requests to remote Remembra API
  - Auto-configured by installer for sandboxed environments
  - `remembra-bridge --stop` gracefully stops running bridge
  - `remembra-bridge --status` checks if bridge is running and healthy
  - Port-in-use detection with clear error messages
  - Health check after startup (fails fast if bridge unhealthy)
  - PID file management for clean process lifecycle

- **Security Hardening**
  - RBAC permissions enforced on all memory endpoints
  - Generic exception handler sanitizes error responses
  - API key caching for reduced latency
  - Webhook SSRF protection
  - 2FA/MFA settings UI in dashboard

### Fixed
- Rate limit removed from `/health` endpoint (was blocking monitoring)
- RBAC inline permission checks instead of Depends()

### Documentation
- New Agent Setup guide at docs.remembra.dev/getting-started/agent-setup
- Updated quickstart with universal installer
- Landing page comparison table updated with multi-agent features

---

## [0.9.0] - 2026-03-09

### Added
- **Temporal Knowledge Graph** — Bi-temporal relationship model
  - Relationships now track `valid_from`, `valid_to`, and `superseded_by`
  - Enables point-in-time queries: "Where did Alice work in January?"
  - Contradiction detection: new relationships auto-supersede old ones
  - Foundation for full temporal knowledge graph (ahead of Zep/Graphiti)

- **6 New MCP Tools** — MCP server goes from 5 tools → 11 tools
  - `update_memory` — Update content without delete+recreate, re-extracts facts and entities
  - `search_entities` — Search the entity graph by name, type, or alias
  - `list_memories` — Browse stored memories without a search query
  - `share_memory` — Cross-agent memory sharing via Spaces
  - `timeline` — Temporal browsing filtered by entity and date range
  - `relationships_at` — Query entity relationships at a specific point in time

- **SDK Client Methods** — Python SDK expanded
  - `memory.update(memory_id, content, metadata)` — Calls PATCH endpoint
  - `memory.list_entities(entity_type, limit)` — Calls entity list endpoint

- **Entity Graph Visualization** — Interactive force-directed graph with react-force-graph
  - Flowing particle effects on relationship edges
  - Entity nodes colored by type
  - Click-to-explore entity neighborhoods

### Changed
- MCP server instructions updated to reflect 11 available tools
- Entity graph retrieval now supports temporal filtering

## [0.8.3] - 2026-03-08

### Fixed
- **Security: Server IP Removed** — Removed hardcoded production server IP from tracked files
- **Security: JWT Secret Blocked** — Added quickstart JWT secret to blocked list
- **Dashboard: Light Mode CSS** — Fixed styling issues in light mode theme
- **Dashboard: Auth Flow** — Fixed userId not being set on token verify, added fallback to user object
- **Dashboard: Auth Check** — Use `isAuthenticated()` for proper JWT support
- **Dashboard: Null project_id** — Handle null project_id in API calls

### Security
- Comprehensive repository audit: no real API keys ever committed
- Removed local filesystem paths from public-facing documentation
- Hardened quickstart defaults

## [0.8.2] - 2026-03-07

### Added
- **AES-256-GCM Field Encryption** — Encrypt memory content at rest
  - PBKDF2-HMAC-SHA256 key derivation with 480,000 iterations (OWASP 2023)
  - Transparent encrypt/decrypt for memory content and metadata
  - Passthrough mode for zero-config development
  - Set `REMEMBRA_ENCRYPTION_KEY` to enable in production
- **Encryption Test Suite** — Comprehensive tests for encryption module
- **Security Features Documentation** — Full guide for encryption, PII detection, anomaly detection

### Changed
- Unified security features for enterprise deployments
- Enhanced documentation for self-hosters

## [0.8.1] - 2026-03-06

### Added
- **MCP Registry Published** — Now discoverable as `io.github.remembra-ai/remembra` in Claude Desktop and other MCP clients
- **TypeScript SDK v0.8.1** — Synced with Python SDK features
- **Encryption Documentation** — Added encryption guide to docs

### Fixed
- Synced `__version__` across all modules
- Standardized GitHub URLs throughout documentation
- Fixed stale version references
- Corrected license to MIT in all files

## [0.8.0] - 2026-03-07

### Added
- **One-Command Quick Start** — `curl -sSL https://raw.githubusercontent.com/remembra-ai/remembra/main/quickstart.sh | bash` sets up Remembra + Qdrant + Ollama with zero API keys required
- **Multi-Provider Entity Extraction** — Entity extraction now works with Anthropic Claude and Ollama, not just OpenAI. New `create_entity_extractor()` factory dispatches based on `REMEMBRA_LLM_PROVIDER`
- **Usage Warning Banners** — API responses include usage percentage headers (`X-Remembra-Usage-Percent`, `X-Remembra-Plan`) and `usage_warning` field at 60/80/95% thresholds
- **Docker Compose Quickstart** — New `docker-compose.quickstart.yml` with 3 services (Qdrant, Ollama, Remembra), health checks, zero config
- **125 New Tests** — Test coverage for embeddings (6 providers), entity extraction (3 providers), conflict resolution, memory spaces (RBAC), and plugin system (pipeline dispatch)
- **Shared Test Fixtures** — New `tests/conftest.py` with reusable fixtures for all test files

### Changed
- **httpx Connection Reuse** — All 6 embedding providers, webhook delivery, Python SDK client, and MCP server now use persistent HTTP clients. Reduces latency by 100-300ms per operation
- **MCP Server Ingestion** — `ingest_conversation` refactored to use SDK's `Memory.ingest_conversation()`
- **Python SDK** — `Memory` client now supports context manager and has explicit `close()` method
- **App Lifespan Cleanup** — Proper shutdown of all persistent HTTP clients on server stop

### Fixed
- **Connection Churn** — Eliminated 13 locations creating new TCP+TLS connections per request

## [0.7.2] - 2026-03-06

### Fixed
- **Dashboard: EntityGraph Performance** — Changed from N+1 API calls to single `/debug/entities/graph` endpoint
- **Dashboard: Error Display** — Fixed `[object Object]` showing instead of actual error messages
- **Dashboard: TypeScript** — Resolved strict mode compilation errors
- **API: Project Filtering** — Fixed recall defaulting to wrong project_id

### Added
- **Admin: rebuild-vectors endpoint** — `POST /admin/rebuild-vectors` to fix memories missing from Qdrant
- **Docs: Troubleshooting Guide** — Comprehensive diagnosis and fix guide for common issues
- **Docs: Setup Checklist** — 10-step verification checklist for self-hosters

## [0.7.1] - 2026-03-03

### Fixed
- **Security: CORS Configuration** — Removed `allow_origins=["*"]`, now configurable via `REMEMBRA_CORS_ORIGINS`
- **API: PATCH /memories/{id}** — Full implementation (was returning 501)
- **API: Batch Operations** — `/store/batch` and `/recall/batch` now functional
- **Streaming: SSE Endpoint** — `/ingest/stream` for conversation ingestion
- **Observability: OpenTelemetry** — Tracing module fully implemented
- **Production: CORS Origins** — Added `app.remembra.dev` and `remembra.dev` to allowed origins
- **Stripe: Environment Variables** — Accept both prefixed and non-prefixed Stripe env vars

### Changed
- Stub endpoints now return 503 Service Unavailable with helpful messages (was 501)
- Improved error messages throughout API

### Documentation
- Added QA Remediation Results report
- Updated MCP Server documentation for v0.7.0
- Added feature comparison chart
- Added Discord and Twitter links

## [0.7.0] - 2026-03-02

### Added
- **Enterprise Features**
  - **Webhook System** - Event-driven integrations
    - HMAC-SHA256 request signing for security
    - Automatic retry delivery with exponential backoff
    - Events: `memory.created`, `memory.updated`, `memory.deleted`, `entity.created`
    - Webhook management API: create, list, delete, test
  - **RBAC (Role-Based Access Control)**
    - Three roles: `admin`, `editor`, `viewer`
    - 12 granular permissions across memories, entities, webhooks, admin
    - Scoped API keys with role assignment
    - Permission middleware for all protected routes
  - **Memory Conflict Detection**
    - Detect contradictions in stored memories
    - Configurable strategies: `update`, `version`, `flag`
    - Conflict resolution API endpoints
  - **Audit Logging**
    - Complete audit trail of all operations
    - Export to JSON or CSV format
    - Role-protected admin endpoints

- **Import/Export System**
  - **Import from**:
    - ChatGPT conversation exports
    - Claude conversation exports
    - Plain text files
    - JSON, JSONL, CSV formats
  - **Export to**:
    - JSON (full fidelity)
    - JSONL (streaming-friendly)
    - CSV (spreadsheet-compatible)
  - Bulk import API with progress tracking

- **Cloud & Revenue (Phase 2)**
  - **Stripe Billing Integration**
    - Subscription management
    - Usage-based metering
    - Customer portal integration
    - Webhook handlers for billing events
  - **Plan Limits**
    - Configurable limits per plan (memories, API calls, storage)
    - Automatic enforcement with graceful degradation
    - Usage dashboards and alerts
  - **Spaces (Multi-tenancy)**
    - Isolated memory spaces per organization
    - Space-level settings and quotas
    - Cross-space queries for admins

- **Plugin System**
  - Extensible plugin architecture
  - Built-in plugins:
    - `auto_tagger` - Automatic memory tagging
    - `recall_logger` - Query analytics
    - `slack_notifier` - Slack integration for events
  - Custom plugin development guide

- **API Expansion**
  - 52 total API routes across 11 route groups
  - New endpoints: `/admin/*`, `/webhooks/*`, `/transfer/*`, `/conflicts/*`
  - OpenAPI schema updated

### Changed
- Embeddings API refactored for multi-provider support
- Memory service expanded with conflict detection
- Config updated with cloud/billing settings

### Fixed
- TypeScript type reference in dashboard api.ts

## [0.6.3] - 2026-03-01

### Added
- **Docker Support** (Week 11)
  - Production-ready `Dockerfile` with multi-stage build
  - `docker-compose.yml` for complete stack (API + Qdrant)
  - `.env.example` with all configuration options
  - `DOCKER.md` deployment guide
  - Static file serving for dashboard UI
  - Health checks for container orchestration
- **Configuration**
  - `REMEMBRA_STATIC_DIR` for serving dashboard

### Changed
- Dashboard UI now served by API server when `static_dir` is set

## [0.6.2] - 2026-03-01

### Added
- **Entity API Endpoints** (Week 10)
  - `GET /api/v1/entities` - List all entities with type counts
  - `GET /api/v1/entities/{id}` - Get entity by ID
  - `GET /api/v1/entities/{id}/relationships` - Get entity relationships
  - `GET /api/v1/entities/{id}/memories` - Get memories linked to entity
- **Dashboard Improvements**
  - Entity graph visualization (force-directed layout)
  - Memory editing support
  - Graph tab with interactive canvas
  - Entity detail modal with relationships and memories

### Fixed
- TypeScript strict mode compatibility in dashboard components

## [0.6.1] - 2026-03-01

### Added
- **Temporal API Endpoints** - REST API for decay management
  - `GET /api/v1/temporal/decay/report` - View memory health and decay scores
  - `POST /api/v1/temporal/cleanup` - Run cleanup with dry-run support
  - `GET /api/v1/temporal/memory/{id}/decay` - Single memory decay info
- **Decay Module** (`remembra.temporal.decay`)
  - Ebbinghaus forgetting curve implementation
  - Configurable decay parameters (DecayConfig)
  - `calculate_relevance_score()`, `should_prune()` functions
- **TTL Module** (`remembra.temporal.ttl`)
  - Parse TTL strings: "30d", "1y", "2w", "24h"
  - TTL presets: session, conversation, short_term, long_term, permanent
- **Cleanup Job** (`remembra.temporal.cleanup`)
  - Background cleanup for expired/decayed memories
  - Archive mode (soft delete) vs hard delete

### Fixed
- Temporal module properly exported from package

## [0.6.0] - 2026-03-01

### Added
- **Temporal Features (Week 8)** - Time-aware memory operations
  - **TTL (Time-to-Live)** - Memories can now expire automatically
    - Set TTL on store: `memory.store("...", ttl="30d")` (supports d/w/m/y)
    - `cleanup_expired()` method and `/cleanup-expired` endpoint
    - Server-side default TTL configurable via `REMEMBRA_DEFAULT_TTL_DAYS`
  
  - **Memory Decay Algorithm** - Older/unused memories rank lower
    - Exponential time decay with configurable half-life
    - Access count boost (frequently accessed = higher score)
    - Recency of access boost (recently accessed = higher score)
    - `get_memories_with_decay()` for decay score visibility
  
  - **Historical Queries (as_of)** - Time-travel memory recall
    - `recall_as_of()` method to see memories at a point in time
    - Useful for auditing, debugging, historical analysis
    - Respects both creation time and expiration time
  
- **Changelog Ingestion** - Auto-import project history
  - New endpoint: `POST /api/v1/ingest/changelog`
  - Parses Keep a Changelog format (and similar markdown formats)
  - Each release becomes a searchable memory with version/date metadata
  - SDK method: `memory.ingest_changelog(content_or_path, project_name="...")`
  - Supports both raw content and file path input
  
- Database temporal query methods:
  - `get_expired_memories()` - Find memories past their TTL
  - `get_memories_as_of()` - Query historical memory state
  - `get_memories_with_decay_info()` - Get access/decay metadata
  - `cleanup_expired_memories()` - Batch delete expired memories
  - `migrate_memory_relationships()` - Preserve links during UPDATE

### Fixed
- **Critical: FK Constraint Bug in Consolidation** - Memory UPDATE/DELETE operations
  no longer fail with foreign key constraint errors. The fix:
  - Relationships are now properly migrated to new memory before old is deleted
  - `delete_memory()` cleans up relationships and entity links first
  - Entity links preserved during consolidation merges
  
- Fixed duplicate `max_tokens` field in `RecallRequest` model

### Changed
- Memory deletion now explicitly handles FK constraints (relationships, entity links)
- Consolidation UPDATE path migrates relationships to preserve entity graph integrity
- `RecallRequest` now includes `as_of` and `include_decay_score` parameters

### Configuration
- `REMEMBRA_DEFAULT_TTL_DAYS` - Default TTL for all memories (optional)

## [0.5.0] - 2026-03-01

### Added
- **API Key Authentication** - Secure access control for all memory operations
  - Generate API keys with `rem_` prefix and 256-bit entropy
  - Keys hashed with bcrypt before storage (never stored in plaintext)
  - Per-user memory isolation enforced via API key
  - Master key support for admin operations
  - Key management endpoints: POST/GET/DELETE /api/v1/keys
  
- **Rate Limiting** - Protection against abuse and DoS
  - Per-endpoint limits (store: 30/min, recall: 60/min, forget: 10/min)
  - Rate limit by API key (not just IP)
  - Uses `slowapi` with in-memory or Redis backend
  - Configurable via environment variables
  
- **Memory Protection Layer** - Defense against prompt injection (MINJA)
  - Input sanitization before storage
  - Trust scoring based on suspicious pattern detection
  - Patterns detected: instruction override, role manipulation, delimiter injection
  - SHA-256 checksums for integrity verification
  - Content provenance tracking (source, trust_score, checksum)
  
- **Audit Logging** - Security monitoring and compliance
  - Logs all memory operations (store, recall, forget)
  - Logs authentication events (key created, revoked, failed attempts)
  - Includes: timestamp, user_id, key_id, action, resource_id, IP, success
  - Never logs actual memory content or full API keys
  
- New `auth/` module with:
  - `keys.py` - API key generation, hashing, validation
  - `middleware.py` - FastAPI dependencies for authentication
  
- New `security/` module with:
  - `sanitizer.py` - Content sanitization and trust scoring
  - `audit.py` - Security audit logging
  
- Database schema updates:
  - `api_keys` table for key storage
  - `audit_log` table for security events
  - Memory provenance columns: source, trust_score, checksum

### Configuration
- `REMEMBRA_AUTH_ENABLED` - Enable API key authentication (default: true)
- `REMEMBRA_AUTH_MASTER_KEY` - Master key for admin operations
- `REMEMBRA_RATE_LIMIT_ENABLED` - Enable rate limiting (default: true)
- `REMEMBRA_RATE_LIMIT_STORAGE` - Rate limit backend: "memory" or "redis://..."
- `REMEMBRA_SANITIZATION_ENABLED` - Enable input sanitization (default: true)
- `REMEMBRA_TRUST_SCORE_THRESHOLD` - Suspicious content threshold (default: 0.5)

### Security
- OWASP API Security Top 10 addressed
- Defense-in-depth against memory injection attacks (MINJA - 95% success rate in research)
- Cross-user memory access blocked via API key scoping
- user_id in requests overridden by authenticated user (prevents spoofing)

### Dependencies
- Added `bcrypt>=4.0.0` for key hashing
- Added `slowapi>=0.1.9` for rate limiting

## [0.4.0] - 2026-03-01

### Added
- **Hybrid Search** - Combines semantic (vector) and keyword (BM25) matching
  - **SQLite FTS5** integration for persistent full-text indexing
  - In-memory BM25 fallback when FTS5 unavailable
  - Score normalization with min-max scaling
  - Configurable alpha weight for keyword/semantic balance
  - Reciprocal Rank Fusion (RRF) option for rank-based fusion
  
- **CrossEncoder Reranking** - Optional post-retrieval reranking (NEW)
  - Uses `sentence-transformers` CrossEncoder models
  - Reduces hallucinations by ~35% (per Databricks research)
  - Default model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (local, free)
  - Graceful degradation when model unavailable
  - Blends rerank scores with original scores
  
- **Graph-Aware Retrieval** - Uses entity relationships for smarter recall
  - Traverses entity graph to find related memories
  - Alias matching ("Mr. Kim" → "David Kim")
  - Configurable traversal depth (default: 2 hops)
  - Entity neighborhood expansion
  
- **Context Window Optimization** - Smart truncation for LLM context limits
  - **tiktoken integration** for accurate token counting (NEW)
  - Character-based fallback estimation
  - `max_tokens` parameter on `recall()` endpoint
  - Relevance-aware truncation at sentence boundaries
  
- **Advanced Relevance Ranking** - Multi-signal scoring
  - Recency boost (newer memories score higher)
  - Entity match boost (entities in query)
  - Keyword match boost (from BM25)
  - Diversity-aware reranking (MMR) to reduce redundancy
  - Configurable weights via environment variables
  
- New `retrieval/` module with:
  - `hybrid.py` - BM25Index, HybridSearcher
  - `graph.py` - GraphRetriever for entity traversal
  - `context.py` - ContextOptimizer with tiktoken
  - `ranking.py` - RelevanceRanker with configurable boosts
  - `reranker.py` - CrossEncoderReranker for quality improvement (NEW)
  
- FTS5 full-text search table in SQLite (`memories_fts`)
- Comprehensive tests for all retrieval features

### Configuration
- `REMEMBRA_HYBRID_SEARCH_ENABLED` - Toggle hybrid search (default: true)
- `REMEMBRA_HYBRID_ALPHA` - Keyword weight 0-1 (default: 0.4)
- `REMEMBRA_RERANK_ENABLED` - Toggle CrossEncoder reranking (default: false)
- `REMEMBRA_RERANK_MODEL` - CrossEncoder model name
- `REMEMBRA_DEFAULT_MAX_TOKENS` - Max context tokens (default: 4000)
- `REMEMBRA_GRAPH_RETRIEVAL_ENABLED` - Toggle graph traversal (default: true)
- `REMEMBRA_GRAPH_TRAVERSAL_DEPTH` - Entity graph depth (default: 2)
- `REMEMBRA_RANKING_SEMANTIC_WEIGHT` - Ranking semantic weight (default: 0.6)
- `REMEMBRA_RANKING_RECENCY_WEIGHT` - Ranking recency weight (default: 0.15)
- `REMEMBRA_RANKING_ENTITY_WEIGHT` - Ranking entity weight (default: 0.15)
- `REMEMBRA_RANKING_KEYWORD_WEIGHT` - Ranking keyword weight (default: 0.1)
- `REMEMBRA_RANKING_RECENCY_DECAY_DAYS` - Recency half-life (default: 30)

### Changed
- `recall()` now uses advanced retrieval pipeline by default
- `RecallRequest` accepts `max_tokens`, `enable_hybrid`, `enable_rerank` params
- `store()` now indexes memories in FTS5 for keyword search
- Improved relevance scoring considers multiple signals
- Context output optimized for LLM consumption

### Dependencies
- Added `tiktoken>=0.7.0` to server extras
- Added `sentence-transformers>=2.5.0` as optional `rerank` extra

## [0.3.0] - 2026-03-01

### Added
- **Entity Extraction** - LLM extracts PERSON, ORG, LOCATION entities from memories
- **Entity Matching** - Resolves aliases ("Mr. Kim" → "David Kim", "NYC" → "New York City")
- **Alias Management** - Automatic alias tracking and resolution
- **Relationship Storage** - Stores entity relationships (WORKS_AT, SPOUSE_OF, KNOWS, etc.)
- **Memory-Entity Links** - Bidirectional links between memories and entities
- **Entity-Aware Recall** - Find memories via entity graph traversal
- New `entities.py` module for entity extraction
- New `matcher.py` module for entity resolution
- Entity resolution documentation

### Changed
- Memory storage now extracts and links entities automatically
- Recall considers entity relationships for improved relevance

## [0.2.0] - 2026-03-01

### Added
- **LLM-powered fact extraction** - Transforms messy text into clean atomic facts
- **Memory consolidation** - ADD/UPDATE/DELETE/NOOP logic prevents duplicates
- **Smart merging** - Updates preserve history (e.g., "VP of Sales (promoted from Director)")
- New extraction module with configurable LLM backend
- New consolidation module for memory conflict resolution

### Changed
- `store()` now uses intelligent extraction by default
- Improved recall relevance with semantic understanding
- Default threshold lowered to 0.40 for better recall

### Configuration
- `REMEMBRA_SMART_EXTRACTION_ENABLED` - Toggle LLM extraction (default: true)
- `REMEMBRA_EXTRACTION_MODEL` - Model for extraction (default: gpt-4o-mini)
- `REMEMBRA_CONSOLIDATION_THRESHOLD` - Similarity threshold for consolidation

## [0.1.0] - 2026-03-01

### Added
- Initial release of Remembra
- Python SDK with `Memory` client class
- REST API with FastAPI
- `store()` - Store memories with automatic fact extraction
- `recall()` - Semantic search across memories
- `forget()` - GDPR-compliant deletion
- Qdrant vector store integration
- SQLite metadata storage
- Embedding support for OpenAI, Ollama, and Cohere
- Docker and docker-compose setup
- Comprehensive test suite

### Notes
- This is an alpha release - API may change
- Entity resolution coming in v0.2.0
- LLM-powered extraction coming in v0.2.0

## [0.4.1] - 2026-03-01

### Fixed
- API recall endpoint signature (removed duplicate max_tokens argument)
- Hybrid search fallback path (correct method signature for fusion)
- Test compatibility with HybridSearchConfig API

### Added
- RELEASE-CHECKLIST.md - mandatory pre-deploy verification
n��ti�|o]�y�4߯m�<y�y���Ӎ�
