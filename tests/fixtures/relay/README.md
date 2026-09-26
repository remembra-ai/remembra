# Relay hook payload fixtures

Each directory holds the stdin JSON an agent's session hooks receive, and
for Codex its rollout transcript. tests/test_relay_adapter_fixtures.py replays
them through each adapter.

| Directory | Source | How it was made |
| --- | --- | --- |
| `codex/` | codex-cli 0.155.0-alpha.16.4 (`RECORDED.json`) | **Recorded** by tests/test_relay_codex_live.py (`REMEMBRA_RELAY_LIVE=1 REMEMBRA_RECORD_FIXTURES=1`): real hooks, real rollouts. Temp paths are replaced with `/home/dev/...` and Codex's built-in instructions are cut; nothing else is edited. |
| `gemini/` | Gemini CLI 0.61.0 (`RECORDED.json`) | **Recorded** by tests/test_relay_gemini_live.py (`REMEMBRA_RELAY_LIVE=1 REMEMBRA_RECORD_FIXTURES=1`): headless `gemini -p` (SessionStart, BeforeAgent, SessionEnd) and a pseudo-terminal session (`/clear`, `/quit`), against a local stand-in for the Gemini API. The BeforeAgent `prompt` holds the brief Gemini put in front of it. Paths replaced with `/home/dev/...`. |
| `qwen/` | Qwen Code 0.24.6 (`RECORDED.json`) | **Recorded** by tests/test_relay_qwen_live.py: a pseudo-terminal session (`/compress`, `/clear`, a 402 quota error, `/quit`) and a `--continue` resume, against a local OpenAI-compatible stand-in. Paths replaced, the project-directory slug too. |
| `kimi/` | Kimi Code 2.1.1 (`RECORDED.json`) | **Recorded** by tests/test_relay_kimi_live.py: the TUI in a pseudo-terminal (UserPromptSubmit, SessionEnd on Ctrl-D), against a local OpenAI-compatible stand-in. Kimi sends no transcript path. Paths replaced. |
| `cursor/` | cursor-agent 2026.09.26-dd393fe | **Recorded from its own hook runner**: the CLI's hook config loader and executor, loaded from the shipped bundle and driven by a harness exactly as its run loop calls them (a logged-in session could not be run). `session_id` equals `conversation_id`, there is no `cwd`, `transcript_path` is null. Paths replaced with `/home/dev/...`. |
| `hosts/` | one file per agent that runs other agents' hooks | The payload and environment each agent gives a relay hook it runs (C1 routing, tests/test_relay_hosts.py). Cursor, Kimi Code, Gemini CLI (SessionStart) and Qwen Code are **recorded**; Grok Build and Continue are **built from their source**, Devin **from its docs**. Each file's `how` says which. |

Only recorded directories back a verified adapter: the adapter is marked
verified in the same change that commits its `RECORDED.json`, and the notes
name the version that was run.
