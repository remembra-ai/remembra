# Crew mode contracts (WP-0a)

These pages fix the interfaces every other Crew-mode work package builds against. They are
written for implementers. The build specification is the authority for behaviour; these
contracts pin down the exact shapes, orders and texts so that independently built pieces
(server, gate, crewd, dashboard, MCP) agree.

## Where each contract lives

| Contract | Machine-readable source | Human spec | Vectors (`tests/crew/vectors/`) | Conformance runner (`tests.crew.vectors.loader`) | Implemented by |
|---|---|---|---|---|---|
| Event envelope, closed set, payload schemas, client whitelist, moments, hash chain | `remembra.crew.schemas` | [events.md](events.md) | `events/samples.json` | `validate_envelope`, `validate_client_event` (reference) | WP-2 (emit), WP-4..8 (payloads) |
| Snapshot (server and local), WebSocket frames, tree | `schemas.SNAPSHOT`, `LOCAL_SNAPSHOT`, `WS_FRAMES` | [snapshot.md](snapshot.md) | `guard/concrete.json` (local), reducer inputs | `validate_ws_frame` | WP-2, WP-8, WP-9 |
| Shared reducer | `remembra.crew.reducer` (Python reference) | [reducer.md](reducer.md) | `reducer/*.json` | `run_reducer_vectors(reduce_fn)` | WP-9 (Python), WP-12 (TypeScript) |
| Guard decision table (§5.2) | `schemas.GUARD_RULES`, `guard_decide` | [guard.md](guard.md) | `guard/table.json`, `guard/concrete.json` | `run_guard_table`, `run_guard_concrete` | WP-3 (gatecore), WP-5 (server guard) |
| Bash parser | `schemas.BASH_*` vocabulary | [bash-parser.md](bash-parser.md) | `bash/corpus.json` (321 commands) | `run_bash_corpus(parse_fn)` | WP-3 |
| MCP tool map | `schemas.classify_mcp_tool` | [mcp-tool-map.md](mcp-tool-map.md) | `mcp/tool_map.json` | `run_mcp_tool_map` | WP-3 |
| Zone command grammar (D38) | `schemas.validate_command_pattern`, `command_pattern_matches` | [command-grammar.md](command-grammar.md) | `grammar/command_patterns.json` | `run_command_grammar` | WP-3 (trie), WP-5 and WP-9 (validation) |
| REST API stubs (§6) | `schemas.ROUTES`, `REQUEST_SHAPES` | [rest-api.md](rest-api.md), [openapi.json](openapi.json) | — | route-table tests | WP-4..8, WP-14 |
| MCP tool signatures and instructions (§7) | `schemas.MCP_TOOLS`, `MCP_INSTRUCTIONS` | [mcp-tools.md](mcp-tools.md) | — | `validate_mcp_call` | WP-11 |
| Hook stdout and agent-facing text | `schemas.hook_*`, `validate_hook_stdout`, `check_agent_text` | [hook-contracts.md](hook-contracts.md) | `hooks/stdout.json`, `hooks/agent_text.json` | `run_hook_stdout`, `run_agent_text` | WP-9, WP-10, WP-3 (templates) |
| Redaction corpus (§11) | — | [redaction.md](redaction.md) | `redaction/corpus.json` | `run_redaction_corpus(outbound_fn)` | owner of `crew/redact.py` (see below) |
| Console scripts | `schemas.CONSOLE_SCRIPTS`, `pyproject.toml` | this page | — | pyproject test | WP-9 fills the modules |

Spike S0 (WP-0b owns it) records the live Claude Code hook proofs and the D13/D14 go/no-go
decisions in [S0-results.md](S0-results.md), with the captured payloads under
`tests/crew/fixtures/captures/`. WP-9 and WP-10 read it before building hooks.

`remembra.crew.schemas` and `remembra.crew.reducer` import only the standard library (a test runs
them under `python -I` with every non-stdlib import blocked), so the vendored gate
(`crew-gate.py`) and the CLI can use or vendor them without the server extras.

## Using the vectors from another work package

```python
from remembra.crew import gatecore                      # your module
from tests.crew.vectors.loader import run_bash_corpus

def test_bash_parser_conforms():
    assert run_bash_corpus(gatecore.parse_bash) == []
```

Each runner returns a list of readable failures; an empty list means conformant. The TypeScript
reducer (WP-12) loads the same JSON files and applies the assertion language in
[reducer.md](reducer.md).

## Changing a contract

1. Edit the source (`schemas.py`, `reducer.py`, or the literal expectations in
   `tests/crew/vectors/build.py` / `build_corpora.py`).
2. Regenerate: `PYTHONPATH=src python -m tests.crew.vectors.build` and, for REST changes,
   `PYTHONPATH=src python -c "from remembra.crew.schemas import render_openapi; open('docs/crew/openapi.json','w').write(render_openapi())"`.
3. `tests/crew/vectors/test_vectors_in_sync.py` and the OpenAPI test fail on drift.

A new event type, a new payload key or a change in rule order is a spec change (§4.2): bump
`CONTRACT_VERSION` and tell every consuming work package.

## Console scripts

`pyproject.toml` declares, once, for the whole build (§14 interface file):

```toml
remembra-crew = "remembra.relay.crew.cli:entrypoint"
remembra-crewd = "remembra.relay.crew.crewd:main"
```

WP-9 owns those target modules (`remembra/relay/crew/cli.py` and `crewd.py`).
`tests/crew/test_crew_commands_exist.py` runs `remembra-crew <subcommand> --help` for every
command line the agent-facing texts show, and checks every MCP tool they name is registered.

## Interpretations fixed by these contracts

The spec leaves a few points open. The contracts settle them as follows (each has vectors):

* **Guard row 17 in observe** ("same"): auto-claim still runs; a 409 or cap becomes `warn`, not
  `deny`. Only rows 1–5 deny in observe.
* **Guard row 17 with auto-claim disabled or over the cap**: `deny` variant `claim_required` /
  `claim_cap` in enforce, `warn` in observe.
* **Rule 0** is the fast exit before the table (read-only Bash, read-like MCP tool, a path outside
  every checkout that is not crew-policy, or opaque Bash marked for the post-tool check).
* **`REMEMBRA_BYPASS`**: only the literal code form `RCB-XXXXX-XXXXX` (Crockford base32) as an
  inline prefix of a `git` command is not tamper; `export REMEMBRA_BYPASS=…` always is.
* **`git config core.hooksPath`** with no value (a read) is read-only; setting, unsetting or
  adding it is tamper.
* **`--no-verify` on `merge`, `rebase` and `am`** is tamper, like commit and push.
* **`chmod`/`chown`** are writers (a `chmod -x` of a hook is `crew_files_removed`).
* **Bash `sh -c '<literal>'`** is opaque (not recursively parsed); tamper markers are found by a
  raw scan of opaque segments instead, so `bash -c "git commit --no-verify"` is still denied.
* **Tree writers with a glob or variable argument** get scope `["."]` (whole checkout), which is
  stricter than "unparseable → allow".
* **`git checkout <arg>` without `--`** is a path when the last component has an extension or the
  argument is `.`; otherwise it is a branch switch (tree-wide op).
* **Baton refs** for claims with no task use the session id: `refs/remembra/baton/cs_…/<seq>`.
* **Snapshot footprints**: the server and local snapshots carry live dirty/committed footprints
  (`footprints[]`), which guard rows 5 and 16 need.
* **Redaction choke point**: §11 names `crew.redact.outbound()` but §14 gives it no owner. The
  signature and corpus are fixed here; the module still needs an owner (flagged to the lead).

## Sub-agents and the continuity riders

A sub-agent is its own crew session, not part of the session that started it
(owner decision on the continuity gap analysis, open question 1). It joins with
`parent_session_id` naming a live session of the same account in the same crew
(else 422 `cross_crew_reference` / `parent_session_mismatch`, or 409
`parent_session_ended` / `parent_session_not_live` for a lost parent), and
optionally `sub_agent_id`. The link must be proven: the join carries the
parent's current session token in `X-Remembra-Crew-Session`, or is made with
the parent's own verified agent key (else 403 `parent_session_unproven`). It gets its own
callsign, claims and checkpoints; the session view carries `parent_session_id`,
`sub_agent_id` and `provider`.

The crew.db v1 schema also carries the continuity riders as nullable columns,
added before crew.db first deployed: `crew_sessions` (provider,
parent_session_id, sub_agent_id, run_id, capabilities, context_window,
env_fp_id), `crew_checkpoints` (run_id, state_before_ref, decision_ids,
quality, confidence, continuity_seq), `crew_decisions` (evidence,
proposed_by_verified, decided_by_verified, intent_version) and
`crew_footprints` (content_hash, artifact_id).

Several riders are optional request fields: `provider`, `capabilities`,
`sub_agent_id`, `run_id` and `context_window` on Join; `decisions`,
`state_before` and `run_id` on Checkpoint (`run_id` defaults to the session's,
and the server's own checkpoints carry the session's); `content_hash` (sha256)
on a heartbeat footprint; `evidence` on a decision. The decision service records
whether the proposer and the decider were verified. `env_fp_id`, `quality`,
`confidence`, `continuity_seq`, `intent_version` and `artifact_id` have no
writer yet. `failure.recorded`, `failure.resolved` and `artifact.recorded` are
reserved L1 event names with no producer yet. A NULL rider changes no behaviour.

The parent stays accountable for its sub-agent: the actor of every event a
sub-agent causes carries `parent_session_id` (absent for every other actor, so
their envelopes and hashes are unchanged); the parent may release the
sub-agent's claims and block, unblock or release its tasks; a parent that ends
takes its live sub-agents with it (`end_reason` `parent_ended`, their work
released or reserved as their own leave would); snapshots list a sub-agent
right after its parent; the YOU line shows the claims the caller answers for
(`zone pos (via sub-agent cc-2)`) and DO NOT TOUCH names the parent
(`zone pos → cc-2 (sub-agent of cc-1)`).

## Account erasure and the hash chain

`remembra.crew.erasure` holds the crew.db erasure rules (`CREW_ERASURE_RULES`)
and, per table, what it holds for a user and which rows are the user's own and
which are shared crew history (`CREW_TABLE_HOLDINGS`). In a crew the erased
account does not own, every event carrying its identity is tombstoned
(`remembra.crew.events.tombstone_events`): its actor, refs, summary and payload
become the fixed tombstone, and `crew_event_tombstones` records the prev_hash
and hash it had. `verify_crew_chain` accepts a tombstoned event only when its
content is exactly the tombstone and its links equal the recorded ones. Both
reducers skip a tombstoned event's handler (it only advances `last_seq`; a
moment keeps its place). Retention deletes a tombstone with its event.
