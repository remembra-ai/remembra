# Claude Code hook stdout and agent-facing text

Source: `remembra.crew.schemas` (`hook_*` builders, `validate_hook_stdout`, `check_agent_text`,
`clip_item`, `split_data_blocks`, `TEXT_CAPS`). Vectors: `tests/crew/vectors/hooks/stdout.json`,
`hooks/agent_text.json`. Spec: §8.2 "Stdout contracts", §5.2 templates, §5.3, §11, D13, D16, D34.

## Stdout per hook

All hooks exit 0 (also on internal errors, diagnostics on stderr). Exit code 2 is never used in
PreToolUse. Output is at most one JSON line (a trailing newline is fine).

| Hook | No-op | Action output |
|---|---|---|
| SessionStart | never (empty stdout is invalid) | `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":…}}` ≤6,000 chars (brief ≤4,500 + crew block ≤1,500), up to two data blocks |
| UserPromptSubmit | empty | `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":…}}` ≤600 |
| PreToolUse | empty (= allow) | deny: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":…}}` ≤450; ask: same with `"ask"`; urgent server/human item on allow: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":…}}` ≤300 (S0-gated) |
| Stop | empty | `{"decision":"block","reason":…}` ≤400, **no data block** |
| PostToolUse, StopFailure, PreCompact, SessionEnd | empty | nothing, ever |

**Never** `"permissionDecision":"allow"`, never `updatedInput`, never any other key. The builders
produce these strings exactly (key order as above, compact separators, UTF-8 unescaped) and raise
`ValueError` when the text breaks a rule below.

## Agent-facing text rules (`check_agent_text(text, channel)`)

Channels and caps (`TEXT_CAPS`): `session_start` 6,000 · `brief` 4,500 · `crew_block` 1,500 ·
`turn` 600 · `turn_compact` 200 · `pretool_context` 300 · `deny` 450 · `stop` 400 ·
`piggyback` 300 · `mcp_instructions` 1,300.

1. **Data block.** All agent- or repo-authored text (task/zone titles, messages, handoff items,
   `frozen_note`, …) sits inside `<remembra-data untrusted="true">` … `</remembra-data>` (the
   existing relay wrapper). Blocks are well formed and not nested; tag-like text inside is
   neutralised (`clip_item`/`neutralize_data`, the same rule as `relay/handoff.py`). At most one
   block per channel, two for SessionStart, none for Stop.
2. **Clipping.** Each agent-authored item is newline- and control-character-stripped and clipped
   to 140 characters (`clip_item`).
3. **No destructive commands** outside the data block: `git checkout [<ref>] --`, `git checkout .`,
   `git reset --hard`, `git clean -f…`, `git restore`, `git stash`, `git push --force/-f`,
   `git branch -D`, `rm -r…` (`DESTRUCTIVE_COMMAND_RES`). Deny texts for tree-wide ops name the
   kind ("a tree-wide git operation (stash)"), not the command line.
4. **No bypass mechanism** outside the data block: `REMEMBRA_BYPASS`, `REMEMBRA_CREW=`,
   `--no-verify`, "bypass code", `remembra-crew bypass` (D34: humans only).
5. **No control characters** except newline and tab.
6. Server template lines use ids, slugs and callsigns only; the deny reason's reserved variant
   shows the adopt command only when the caller holds an offer (guard variant
   `reserved_offered`).

The §5.2 and §8.2 templates (deny, reserved-offered, Stop report-missing, Stop breach, crew block,
turn delta) are in the vectors as valid texts, so WP-3's template functions can be checked with
`run_agent_text` and `validate_hook_stdout`.
