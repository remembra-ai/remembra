# Relay hook payload fixtures

Each directory holds the stdin JSON an agent's session hooks receive, and
for Codex its rollout transcript. tests/test_relay_adapter_fixtures.py replays
them through each adapter.

| Directory | Source | How it was made |
| --- | --- | --- |
| `codex/` | codex-cli 0.155.0-alpha.16.4 (`RECORDED.json`) | **Recorded** by tests/test_relay_codex_live.py (`REMEMBRA_RELAY_LIVE=1 REMEMBRA_RECORD_FIXTURES=1`): real hooks, real rollouts. Temp paths are replaced with `/home/dev/...` and Codex's built-in instructions are cut; nothing else is edited. |
| `gemini/` | https://geminicli.com/docs/hooks/reference/ (accessed 2026-09-25) | **Written from the docs**: the documented common fields plus `source` / `reason`. Not recorded; Gemini CLI was not available. |
| `qwen/` | https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/ (accessed 2026-09-25) | **Written from the docs**, as above. Not recorded. |
| `cursor/` | cursor-agent 2026.09.26-dd393fe | **Recorded from its own hook runner**: the CLI's hook config loader and executor, loaded from the shipped bundle and driven by a harness exactly as its run loop calls them (a logged-in session could not be run). `session_id` equals `conversation_id`, there is no `cwd`, `transcript_path` is null. Paths replaced with `/home/dev/...`. |
| `hosts/` | one file per agent that runs other agents' hooks | The payload and environment each agent gives a relay hook it runs (C1 routing, tests/test_relay_hosts.py). Cursor, Kimi Code, Gemini CLI (SessionStart) and Qwen Code are **recorded**; Grok Build and Continue are **built from their source**, Devin **from its docs**. Each file's `how` says which. |

The ids, versions and paths in the doc-derived files are made up; only the
field names and value sets come from the docs. Replace a directory with a
recorded capture when the agent has been run, and only then mark its adapter
verified.
