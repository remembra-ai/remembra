# Outbound redaction (§11)

One choke point for everything that leaves a host or goes to memory:

```python
def outbound(payload_type: str, payload: Any, *, repo_root: str | None = None, home: str | None = None) -> Any
```

`payload_type` ∈ `event, presence, heartbeat, snapshot, checkpoint, report, stall, promotion`.
It returns a new payload (JSON value); the input is not mutated. It applies `redact_secrets`
(`security/secrets.py`) and the PII scrubber, plus the crew rules:

* **Command metadata only.** Raw command strings are dropped (a `command` value becomes verb +
  classified targets); test-runner fingerprints needed for criteria matching are kept, redacted.
* **Env assignments.** `NAME=value` where NAME matches `KEY|TOKEN|SECRET|PASS|PWD|DSN|URL|AUTH`,
  and `export X=<≥32 high-entropy chars>`, lose the value.
* **Headers and credentials.** `-H 'Authorization: …'`, `--header …`, `-u user:pass`, URL
  credentials (`https://user:token@host`), DSNs (`postgres://user:pass@…`).
* **No stdout/stderr** of commands that read `.env*`, key or credential files.
* **Paths.** Absolute paths under `repo_root` become repo-relative; other paths under `home` are
  dropped or reduced so the home directory never leaves the host. Raw hostnames never leave
  (`member_key` uses a salted host label).

Corpus: `tests/crew/vectors/redaction/corpus.json`, 20 cases over all eight payload types
(bearer headers, DSNs, env exports, vendor keys, `.env` dumps, private keys, basic auth, URL
credentials, absolute home paths, hostnames). For each case every `must_not_contain` string must be
absent from the JSON of the output and every `must_contain` string present. The secrets are
deterministic fakes generated in `build_corpora.py`, not real credentials.

Runner: `run_redaction_corpus(outbound)`. The spec names this function `crew.redact.outbound()`
but §14 assigns `crew/redact.py` to no work package; it needs an owner before WP-2/4/6/9 emit
payloads.
