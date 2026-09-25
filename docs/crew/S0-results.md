# Crew mode spike S0: results (WP-0b)

**Date:** 2026-09-25. **Target:** the installed Claude Code **2.1.168** (`/Users/dolphy/.npm-global/bin/claude`).
**Scope:** spec §8.2 "Verification status and spike S0", decisions D13 and D14 (§14 Wave 0, WP-0b).
**Evidence:** `tests/crew/fixtures/captures/claude-code-2.1.168/<mode>/<scenario>/` holds the raw hook stdin payloads
(`NN-<Event>.json`, in firing order) and a `summary.json` per run (verdict, what the model received, hook events).
`tests/crew/fixtures/captures/test_s0_captures.py` pins every claim in this note to those files.

## Summary

| Feature | Verdict | Level reached |
|---|---|---|
| PreToolUse `permissionDecision:"deny"` blocks the write | **GO** | V-live, `-p` and interactive TUI |
| Deny reason reaches the model | **GO** | V-live: verbatim in the `tool_result` sent to the API, `is_error: true` |
| PreToolUse `additionalContext` on allow, no decision | **GO** (D13 primary path) | V-live, `-p`: delivered in the same user turn as the tool result |
| SessionStart / UserPromptSubmit `additionalContext` | **GO** | V-live, `-p`: both in the first API request |
| `$CLAUDE_ENV_FILE` from SessionStart | **GO** | V-live, `-p`: exported var visible to the Bash tool |
| async PostToolUse under `-p` | **CONDITIONAL GO** | V-live: runs, but a hook still running when `-p` exits is dropped **and SessionEnd is skipped** |
| async PostToolUse, interactive | **GO** | V-live: completes, SessionEnd fires |
| `asyncRewake` wakes the model | **GO for L0** | V-live: mid-turn (`-p`), after the turn ends (`-p`), and while the TUI is idle |
| StopFailure fires with the right `error` | **GO** | V-live against the real client with scripted API errors; one real-API capture (401) |
| StopFailure on real credit exhaustion / real subscription usage limit | **NOT CAPTURED** (by design) | Inferred from the binary; not triggered (would require exhausting credits) |
| StopFailure hook survives `-p` exit | **GO** | V-live: a 3 s hook finished after the process exited |

D13 and D14 are fixed at the end of this note.

## Method

`tests/crew/fixtures/captures/s0_harness.py` drives the real `claude` binary. Nothing touched `~/.claude`:

- Each run gets a fresh throwaway git repo with a **project-level** `.claude/settings.json`. Every hook is
  `s0_hook.py`, which records its raw stdin, then answers with the action under test (record, deny, ctx, envfile,
  sleep-then-ctx, rewake).
- `--setting-sources project` keeps the user's own hooks out (the user-level SessionStart hook never ran), and
  `--strict-mcp-config` keeps every MCP server out.
- **mock** runs (`--mode mock`, the default) and **interactive** runs (`--mode interactive`) point Claude Code at a
  local mock of the Messages API (`ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`) with a dummy `ANTHROPIC_API_KEY` and a
  temp `CLAUDE_CONFIG_DIR`. The client code path is the real one: request building, SSE parsing, hook dispatch, the
  permission system, error classification and exit handling. The mock scripts the model (for example "call Write
  once, then answer") and logs every request body, which proves byte-for-byte what reaches the model. No real
  credential leaves the machine and no credits are spent.
- **interactive** runs start the TUI on a pseudo-terminal with the prompt as the first message, leave it idle for 15 s,
  then type `/exit`. First-run dialogs are pre-accepted in the temp config dir only.
- **real** runs use the normal login against the real API with `--no-session-persistence`.

Fixtures are sanitised: temp paths become `<S0_TMP>`, the home directory `<HOME>`, and keys `<API_KEY>`. The test
suite fails if an absolute home path or `sk-ant-` string appears in a fixture.

Reproduce (about 2 minutes, no credits):

```bash
cd tests/crew/fixtures/captures
python3 s0_harness.py --workdir /tmp/s0 --out claude-code-2.1.168                     # 16 mock scenarios
python3 s0_harness.py --workdir /tmp/s0 --out claude-code-2.1.168 --mode interactive  # 2 TUI scenarios
python3 s0_harness.py --workdir /tmp/s0 --out claude-code-2.1.168 --mode real --only real_stopfailure_model
REMEMBRA_S0_LIVE=1 pytest tests/crew/fixtures/captures -q -p no:cacheprovider          # live re-check
```

## Results per scenario

### PreToolUse deny: `deny` (mock) and `interactive_deny_rewake` (TUI)

Hook output: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"S0-DENY-…"}}`, exit 0.

- The file was **not written** in either mode, even under `--permission-mode acceptEdits`.
- The next API request carries `{"type":"tool_result","is_error":true,"content":"S0-DENY-…: Crew: src/pos is in zone pos held by codex-1. Do not modify it."}`.
  The reason is passed **verbatim**, with no prefix added by Claude Code.
- PostToolUse and PostToolUseFailure do **not** fire for a denied call. Stop fires normally afterwards.
- Firing order: `SessionStart, UserPromptSubmit, PreToolUse, Stop, SessionEnd`.

**Verified:** the deny blocks the write, and the reason is what the model receives.
**Not verified:** how a real model reacts to the reason, meaning whether it obeys, retries or works around it. The
real-model run `real_deny_ctx` is in the harness but could not run, because the CLI's stored OAuth token is invalid
outside the desktop app (see "Not done").

### additionalContext without a decision: `context`

SessionStart, UserPromptSubmit and PreToolUse (matcher `Edit|Write|…`) each emitted only
`hookSpecificOutput.additionalContext`, with no `permissionDecision`.

- The Write **still ran** (the file exists). An allow with context stays an allow.
- The first request's user turn contains `<system-reminder>\nSessionStart hook additional context: S0-SESSIONSTART-CTX-…</system-reminder>`
  and `<system-reminder>\nUserPromptSubmit hook additional context: S0-PROMPT-CTX-…</system-reminder>`.
- The second request's user turn contains the `tool_result` **followed by** `<system-reminder>\nPreToolUse:Write hook additional context: S0-PRETOOL-CTX-…</system-reminder>`.
  The model sees it immediately after the tool, before it chooses its next action. This is the mid-turn channel D13
  needs.

### `$CLAUDE_ENV_FILE`: `envfile`

The SessionStart hook appended `export S0_CREW_ENV=…` to `$CLAUDE_ENV_FILE`. A later `printenv S0_CREW_ENV` Bash
call returned the value. `CLAUDE_ENV_FILE` was set only for SessionStart, not for PreToolUse. This path is GO for
§8.2 step 6 (`REMEMBRA_PROJECT_ID`, `REMEMBRA_MEMBER`).

### async PostToolUse: `async_post`, `async_post_fast`, `async_post_settled`, `interactive_async_post`

Hook: `{"type":"command","async":true,…}` that sleeps, writes a marker, then prints `additionalContext`.

| Scenario | Hook | Result |
|---|---|---|
| `async_post` (`-p`) | 4 s | Started (payload captured), **never finished** (no marker even 6 s after exit). **SessionEnd did not fire.** The run took 0.44 s, so the hook did not block. |
| `async_post_fast` (`-p`) | 0.2 s | Same: never finished, **SessionEnd did not fire**. The final answer arrived 0.03 s after the tool. |
| `async_post_settled` (`-p`) | 0.2 s, final answer delayed 2.5 s | Finished, and SessionEnd fired. |
| `interactive_async_post` (TUI) | 4 s | Finished while idle. SessionEnd (`reason: prompt_input_exit`) fired. |

In none of these runs did an async hook's `additionalContext` reach the model. In the settled case the model call had
already been sent. Treat async output as **not delivered**.

**Conclusion:** under `-p`, any async hook still running when the session ends is dropped, **and the session's
SessionEnd hooks are skipped**. This happened in every such run we made (8 of 8, counting development runs). With a real
model the final answer after a tool takes seconds, so a PostToolUse that exits in under about 100 ms is practically
never still in flight. But nothing guarantees that.

### asyncRewake: `async_rewake`, `async_rewake_quiet`, `async_rewake_idle`, `interactive_deny_rewake`

Hook: `{"type":"command","asyncRewake":true,…}`. On exit 2 its stderr is queued for the model. On exit 0 its
stdout JSON is handled like any hook output.

- **Mid-turn, `-p` (`async_rewake`, PostToolUse):** the next API call was **held about 3.1 s** until the hook exited.
  That request then carried the tool result plus
  `<system-reminder>PostToolUse:Write hook blocking error from command: "<full command line>": [<full command line>]: S0-REWAKE-…</system-reminder>`.
  So under `-p`, asyncRewake hooks are effectively awaited before the next model call.
- **Exit 0 (`async_rewake_quiet`):** same 3.1 s hold, and the hook's `additionalContext` **did** reach the model. Only
  `asyncRewake` hooks deliver stdout context. Plain `async` hooks do not.
- **After the turn ended, `-p` (`async_rewake_idle`, on Stop):** the model had answered and Stop had fired. About
  3 s later the hook exited 2, and Claude Code sent a new request whose user turn was
  `Stop hook feedback:\n[<full command line>]: S0-REWAKE-…`. The model answered, and Stop fired a second time (the hook
  must be idempotent; ours is `rewake_once`). No UserPromptSubmit fired for the rewake under `-p`. The binary caps
  this end-of-run wait at `ASYNC_REWAKE_FLUSH_TIMEOUT_MS = 30000` (inferred from the code, not timed).
- **Idle TUI (`interactive_deny_rewake`, on Stop):** the TUI finished its turn and sat at the prompt. 3.1 s later it
  woke by itself and sent a request whose user turn was
  `<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\nStop hook blocking error from command "Stop": S0-REWAKE-…</system-reminder>`.
  The model answered. **UserPromptSubmit fired for that synthetic prompt** (its `prompt` field is the text above).

**Conclusion:** asyncRewake can wake an idle agent. That is the mechanism for "your queued claim was granted".

### StopFailure: `stopfailure_billing`, `stopfailure_ratelimit`, `stopfailure_overloaded`, `stopfailure_auth`, `stopfailure_model`, `stopfailure_network`, `stopfailure_slow_hook`, `real_stopfailure_model`

The mock answered `/v1/messages` with the HTTP error shown below. Retries were off (`CLAUDE_CODE_MAX_RETRIES=0`).

| Scenario | API response | `error` | `last_assistant_message` |
|---|---|---|---|
| `stopfailure_billing` | 400 `invalid_request_error` "Your credit balance is too low …" | `billing_error` | `Credit balance is too low` |
| `stopfailure_ratelimit` | 429 `rate_limit_error` (+ unified rate-limit headers) | `rate_limit` | `API Error: Request rejected (429) · This request would exceed your rate limit.` |
| `stopfailure_overloaded` | 529 `overloaded_error` | `server_error` (not `overloaded`) | `API Error: 529 Overloaded. …` |
| `stopfailure_auth` | 401 `authentication_error` | `authentication_failed` | `Invalid API key · Fix external API key` |
| `stopfailure_model` | 404 `not_found_error` | `model_not_found` | `There's an issue with the selected model …` |
| `stopfailure_network` | connection refused | `unknown` | `API Error: Unable to connect to API (ConnectionRefused)` |
| `real_stopfailure_model` (**real API**) | real 401: the CLI's stored OAuth token is invalid | `authentication_failed` | `Failed to authenticate. API Error: 401 OAuth access token is invalid.` |

In every case:

- StopFailure fired **instead of** Stop (Stop never fired), and `claude -p` exited with code 1.
- `error_details` was **absent**.
- The payload has `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `error`, `last_assistant_message`, and on
  the real run also `effort`.
- **Order against SessionEnd is not stable.** StopFailure is fire-and-forget, and in 3 of the 8 recorded runs
  (`stopfailure_billing`, `stopfailure_network`, `real_stopfailure_model`) SessionEnd was recorded first.
- `stopfailure_slow_hook`: a StopFailure hook needing 3 s was **not killed** when `-p` exited after 0.39 s. It
  finished afterwards. `remembra-crew stall` (20 s budget) will therefore run to completion in headless loops.

**Inferred from the binary, not live-verified:**

- **Real credit exhaustion (API key, prepaid).** The client maps any error whose message contains "credit balance is
  too low" to `billing_error`, and the mock used the real API wording. What is unverified is only that the production
  API returns exactly that text.
- **Subscription usage limit (5-hour or weekly).** Every 429 path returns `error: "rate_limit"`. For a claude.ai
  subscriber carrying the `anthropic-ratelimit-unified-*` headers, `last_assistant_message` is built from these
  templates: `You've hit your session limit · resets …`, `You've hit your weekly limit …`,
  `You've hit your Opus limit …`, `You're out of usage credits …`, `Your org is out of usage · …`,
  `You've hit your monthly spend limit.` (prefix list `_Q5` in the binary). A transient 429 reads
  `Server is temporarily limiting requests (not your usage limit)` or `Request rejected (429)`. **So `rate_limit`
  alone does not mean quota.** The subscriber path could not be reproduced: it requires an OAuth subscriber session,
  and the dummy-key mock takes the API-key branch.
- Retries: the default is 10 (`CLAUDE_CODE_MAX_RETRIES`). 5xx/529 are retried with backoff before StopFailure fires.
  A 429 is retried only for **non-subscribers** (and enterprise). For a claude.ai Pro/Max subscriber, a usage-limit
  429 ends the turn at once, so StopFailure fires without delay. Only the 0-retry case was run.

### Other observations useful to WP-9 / WP-10

- **Payload fields seen** (all events: `session_id`, `transcript_path`, `cwd`, `hook_event_name`):
  - `PreToolUse` / `PostToolUse`: `tool_name`, `tool_input`, `tool_use_id`, `permission_mode`.
  - `PostToolUse` also has `tool_response`, `duration_ms`. For Write, `tool_response` is `{type:"create", filePath, content, structuredPatch, originalFile, userModified}`.
  - `SessionStart`: `source`, plus `model` in the TUI.
  - `UserPromptSubmit`: `prompt`, `permission_mode`.
  - `Stop`: `stop_hook_active`, `last_assistant_message`, `background_tasks`, `session_crons`.
  - `SessionEnd`: `reason`, which was `other` under `-p` and `prompt_input_exit` after `/exit`.
- **The full hook command line is shown to the model** in `-p` asyncRewake messages. Command strings must never carry
  secrets. The spec already forbids that; this confirms why it matters.
- **SessionEnd timeout** (inferred): default `1500` ms, raised to the largest configured SessionEnd `timeout`, capped
  at `60000` ms, or overridden by `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`. The §8.2 `timeout: 20` entry therefore
  gets 20 s.
- The TUI makes one small side request (`claude-haiku-4-5`) before the main loop. `-p` does not.

## Decisions

### D13 (mid-turn delivery): **GO on the primary path**

- Urgent **server-generated or human-authored** items are delivered as synchronous PreToolUse `additionalContext`
  with no `permissionDecision`. This is V-live. The text lands right after the tool result in the same turn, prefixed
  `PreToolUse:<Tool> hook additional context:`. The §8.2 contract stands: allow means empty stdout, and an urgent item
  on allow means `additionalContext` only (≤300 chars, in the data block where it quotes agent text). The deny-on-next-
  write fallback is **not needed**. Keep it only as the degraded path when the gate cannot reach the snapshot.
- PostToolUse stays `async: true`, with one amendment (proposed for WP-9/WP-10): `gate posttool` must **hand off and
  exit in ≤100 ms**, for example by appending to the outbox or sending one socket datagram to crewd. crewd then runs
  the `git status --porcelain` delta and the attribution window. The §8.2 "5 s budget with an 800 ms git status inline"
  must not run inside the hook process, because an in-flight async hook at `-p` exit is dropped **and suppresses
  SessionEnd**.
- Because SessionEnd can be skipped in headless runs, crewd's orphan path (§8.1 liveness: dead pid with no `ended`
  marker, then baton ref, relay close `end_reason="orphaned"`, `leave`) is **required** for Claude Code too, not only
  as a backup.
- **asyncRewake is promoted to L0.** A granted queued claim, a human hand-over or a pause can wake an idle Claude Code
  session. Constraints for WP-10:
  - The waiter must be idempotent, because Stop fires again after the rewake turn.
  - It must exit 0 immediately when nothing is queued, because under `-p` every pending asyncRewake hook holds the
    process for up to 30 s at exit and holds each next model call mid-turn.
  - The rewake text arrives framed as "hook blocking error" / "Stop hook feedback". Use the §5.2 server templates
    (ids only), because the frame already reads as an instruction.
  - In the TUI the rewake also fires UserPromptSubmit with a `<task-notification>` prompt. `gate turn` should treat
    that prompt as a wake-up, not as user input.

### D14 (quota detection): **StopFailure is the primary signal, with a narrower quota test; the transcript detector stays on for Claude Code**

- Install the StopFailure hook (§8.2) as specified. It fires on every API-error turn end, fire-and-forget, and survives
  `-p` exit.
- `remembra-crew stall` classifies the error as follows:
  - `billing_error`: quota. Covers exhausted credits.
  - `authentication_failed` and `oauth_org_not_allowed`: blocked. Stall with reason `auth`, not quota.
  - `rate_limit`: quota **only when** `last_assistant_message` starts with one of the usage-limit prefixes (`You've hit your`, `You're out of usage credits`, `Your org is out of usage`, `You've used`). Otherwise it is a transient 429: checkpoint only, no baton, no `quota_blocked`.
  - `server_error`, `overloaded`, `unknown`, `invalid_request`, `model_not_found`, `max_output_tokens`: checkpoint only. This matches §8.2.
- **The transcript-tail detector stays enabled for Claude Code** until the real subscription usage-limit payload is
  captured, as the spec requires ("until StopFailure is V-live for that exact condition"). Reuse the same prefix list
  as its Claude Code limit patterns.
- Do not rely on StopFailure/SessionEnd ordering. The server's `stall` and `leave` handlers must commute (§10.2
  recovery rules already assume this).

## Not done (with reasons)

- **Real-model runs of deny and context** (`real_deny_ctx`, `real_pretool_ctx`). The CLI's stored OAuth login is
  invalid outside the desktop app: the real API returned 401, captured as `real_stopfailure_model`. Refreshing the
  login is Mani's action. After `claude /login`, run
  `python3 s0_harness.py --workdir /tmp/s0 --mode real` (3 short calls, Haiku). The mock runs already prove the
  bytes the model receives. Only the model's behavioural reaction is unverified.
- **Real StopFailure for credit exhaustion and for a subscription usage limit.** Not triggered, by instruction:
  exhausting credits is unsafe. The client-side mapping is verified. The server wording is inferred (see above).
- **Exhausted-credit transcripts for Codex, Cursor and Gemini** (WP-0b scope line in §14). Not attempted, for the same
  reason. The Wave-0 task limited this spike to Claude Code. The transcript detector patterns for those adapters
  remain research-grade (U).
- **SessionEnd timeout** and the `ASYNC_REWAKE_FLUSH_TIMEOUT_MS` cap were read from the binary, not timed.
