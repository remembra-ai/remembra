# MCP tool map (§8.2)

Reference: `remembra.crew.schemas.classify_mcp_tool(name, tool_input, zone_rules)`.
Vectors: `tests/crew/vectors/mcp/tool_map.json`. Runner: `run_mcp_tool_map`.

Tool names are `mcp__<server>__<tool>`; other names are built-in tools. The tool part is split
into lowercase words (`_`, `-` and camelCase boundaries).

Result `{kind, paths, services, zone_slug, github_repo}` where `kind` is:

| Order | Match | kind | Result |
|---|---|---|---|
| 0 | one of Remembra's own crew tools (`crew_status crew_claim crew_guard crew_task crew_say crew_checkpoint crew_report`, `CREW_MCP_TOOL_NAMES`) on the Remembra server (`remembra`, or a server name that starts or ends with that word; case and `-`/`_` ignored) | `other` | allowed whatever its arguments: `paths` names what to claim, release, adopt or check, not files written, and the server answers the call. The same tool name on another server falls through to the rows below |
| 1 | a read word (`get list search read recall query find fetch describe show view count lookup`) and no write word | `read` | fast exit |
| 2 | a zones.yml `mcp_tools` rule `{tool: <pattern with * only>, service?}` | `services` / `zone` | the service, or the zone itself |
| 3 | `…apply_migration…` | `services` | `supabase:migrations`, `schema:main` |
| 4 | `…execute_sql…` whose `query`/`sql` contains DDL (`create alter drop truncate rename grant revoke comment on`) | `services` | `schema:main` (no DDL → `other`) |
| 5 | words `deploy`, `promote`, `rollback`, or `create` + `deployment` | `services` | `deploy:<server>` (`deploy:default` without a server) |
| 6 | `write_file`, `edit_file`, `create_directory` (`path`); `move_file` (`source`, `destination`) | `paths` | those fields |
| 7 | GitHub `create_or_update_file`, `push_files`, `delete_file` | `paths` | `path` and `files[].path`; `github_repo = owner/repo` (gated only when it is the crew's repo) |
| 8 | any input field in `MCP_PATH_FIELDS` with a string or list value | `paths` | those values |
| 9 | anything else | `other` | allowed |

Write words: `MCP_WRITE_WORDS`. Tool-name patterns use `star_match` (only `*`, linear, no regex).
`<db>` is `main` unless a zone rule names another service.
