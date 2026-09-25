# Guard decision table (§5.2)

Source of truth: `remembra.crew.schemas.GUARD_RULES` and the reference evaluator
`guard_decide(facts, mode, interactive_override=False, permission_mode="default")`.
Vectors: `tests/crew/vectors/guard/table.json` (facts → outcome) and `guard/concrete.json`
(snapshot + tool call → facts → outcome).

The table is split in two layers so the gate and the server agree by construction:

1. **Derive predicates** (gatecore, WP-3; server guard, WP-5): from the tool call, the paths
   (realpath, NFC, case-folded on case-insensitive volumes, mapped to a checkout), the Bash parse,
   the MCP tool map and the snapshot, decide which predicates below are true.
2. **Evaluate the table** first-match, top to bottom: `guard_decide`. Both implementations must
   return the same `{rule, decision, variant, effects}`.

## Rows

| # | Predicates (any true) | enforce | observe | ask-eligible |
|---|---|---|---|---|
| 1 | `session_paused` | deny `paused` | deny | |
| 2 | `crew_policy_target`, `tamper_command` | deny `crew_policy` / `tamper` | deny | |
| 3 | `zone_protected_no_grant`, `zone_frozen` | deny `protected` / `frozen` | deny | |
| 4 | `foreign_checkout` | deny | deny | |
| 5 | `same_worktree_dirty_elsewhere` | deny | deny | |
| 6 | `path_ignored` | allow | allow | |
| 7 | `own_claim_lease_ok` | allow | allow | |
| 8 | `own_claim_lease_passed` | deny `lease_unconfirmed` | warn | |
| 9 | `exclusive_held_by_other` | deny | warn | yes |
| 10 | `reserved_for_other` | deny `reserved_offered` / `reserved_not_offered` | warn | yes |
| 11 | `append_only_edit_existing` | deny | warn | |
| 12 | `commons` | allow + effects | allow + effects | |
| 13 | `tree_writer_other_exclusive` | deny | warn | yes |
| 14 | `service_claimed_by_other` | deny | warn | yes |
| 15 | `tree_git_op_other_live_same_checkout` | deny | warn | yes |
| 16 | `shared_claim_by_others`, `dirty_in_other_checkout` | allow + collision | allow + collision | |
| 17 | `leaf_zone_unclaimed` | auto-claim (below) | auto-claim | |
| 18 | `parent_zone_unclaimed` | deny `task_required` | warn | |
| 19 | `no_zone_match` (or no predicate at all) | `undeclared_policy` (below) | same | |

`warn` = allow, log `would_deny` (effect `would_deny`). Every `deny` except row 2 carries effect
`guard.blocked`; row 2 carries `guard.tamper_blocked`.

**Ask.** With `interactive_override` in enforce mode and a `permission_mode` other than
`bypassPermissions`, rows 9, 10, 13, 14 and 15 return `ask` instead of `deny` (no effects).
Rows 1–5 never ask; observe never asks.

## Modifiers

| Fact | Used by | Meaning |
|---|---|---|
| `holds_offer` | 10 | the caller holds a recorded offer for this baton (D33) → `reserved_offered`; only then may the deny text show the adopt command |
| `commons_kind` | 12 | `plain` (default) · `serialize` · `append_only` |
| `creates_file` | 12 | the write creates a new file |
| `migration` | 12 | the path is a migration (takes a `schema:<db>` claim) |
| `auto_claim_enabled` | 17 | `settings.auto_claim` and `zone.auto_claim` and under the per-session cap (default true) |
| `auto_claim_result` | 17, 19 | the server/crewd answer: `granted · conflict · cap · timeout · rate_limited` |
| `unconfirmed_pending` | 17 | an unconfirmed claim for this zone is already spooled (D11) |
| `undeclared_policy` | 19 | `footprint` (default) · `file_claim` |

Row 12 effects: `notify_watchers` always; `micro_lease` for `serialize`, or `append_only` when
creating a file; `schema_claim` for migrations. Row 16 effects: `collision:same_zone_shared` when a
shared claim by others exists, else `collision:same_file`.

## Row 17 (auto-claim, D11)

| Situation | enforce | observe |
|---|---|---|
| auto-claim disabled / leaf-only rule not met | deny `claim_required` | warn |
| `granted` | allow `auto_claimed`, effect `claim.granted` | same |
| `conflict` (409) | deny `claim_conflict` (the reason names the winner) | warn |
| `cap` (409 `claim_cap`) | deny `claim_cap` | warn |
| `timeout` / `rate_limited`, first write | allow `allowed_unconfirmed`, effects `claim.unconfirmed` (+ `gate.deadline` on timeout) | same |
| `timeout` / `rate_limited` with `unconfirmed_pending` | deny `unconfirmed_pending` | warn |

Row 19 with `undeclared_policy = file_claim` evaluates like row 17 on a file-level claim; its
variants are prefixed `file_` (`file_auto_claimed`, `file_claim_conflict`, …). With `footprint`:
allow `footprint`.

## Rule 0 (fast exit)

Before the table: read-only Bash, a read-like MCP tool, a path outside every known checkout that
is not in crew-policy, and opaque Bash (allowed, marked for the post-tool check). Concrete vectors
show these as `{"rule": 0, "decision": "allow", "variant": "read_only" | "outside_checkouts" |
"post_tool_check"}` with empty facts.

## Concrete vectors (`guard/concrete.json`)

```json
{"home": "/Users/mani", "hmac_key": "…", "snapshot": LocalSnapshot,
 "cases": [{"name", "mode", "now", "caller", "cwd", "tool_name", "tool_input",
            "existing_files": [repo-relative paths that exist], "server": mocked claim response | null,
            "facts": {…}, "expect": {"rule", "decision", "variant"}}]}
```

`run_guard_concrete(evaluate)` calls `evaluate(case, {"snapshot", "home", "hmac_key"})`; if the
result includes `facts`, the listed facts are compared too. The cases cover every row (0–19),
case-insensitive volumes, NFD paths against NFC globs, foreign checkouts (direct and via
`cd ../other &&`), crew-policy files in the repo, in `~/.remembra` and in the common git dir,
settings-hook edits (tamper) vs unrelated settings edits (allowed), MCP writes and migrations,
tree writers, tree-wide git ops in a shared checkout, clobber, commons kinds, auto-claim results,
parent zones, protected and frozen zones.
