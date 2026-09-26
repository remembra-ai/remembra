# Claude Code hook payloads

Hook stdin payloads recorded from the real Claude Code client (2.1.168) by the Crew S0 spike
(`docs/crew/S0-results.md` on `feat/crew`). The client ran against a mock Messages API that answered
with the real error bodies; `stopfailure-authentication_failed-real-api.json` came from the real API.
Temporary paths are replaced with `<S0_TMP>` / `<HOME>`: tests substitute their own `cwd`,
`session_id` and `transcript_path` and keep every other field as recorded.

Not captured yet: a StopFailure from a real subscription usage limit (5-hour or weekly) and a
PreCompact payload. The tests build PreCompact from the hooks reference (`trigger`,
`custom_instructions`), and read both `error` (what 2.1.168 sends) and `error_type` (the name in the
hooks reference).
