"""The outbound redaction choke point (§11) against the WP-0a corpus and the crew rules."""

from __future__ import annotations

import copy
import json

import pytest

from remembra.crew.redact import PAYLOAD_TYPES, command_verb, outbound, redact_text
from tests.crew.vectors.loader import run_redaction_corpus


def test_corpus_passes_for_every_payload_type():
    assert run_redaction_corpus(outbound) == []


def test_input_is_not_mutated_and_output_is_new():
    payload = {"last_action": {"tool": "Bash", "command": "curl -H 'Authorization: Bearer abc.def.ghi' x"}, "n": 3}
    before = copy.deepcopy(payload)
    out = outbound("presence", payload)
    assert payload == before
    assert out == {"last_action": {"tool": "Bash", "verb": "curl"}, "n": 3}


def test_commands_become_verbs_except_test_fingerprints():
    facts = {
        "commands": [
            {"cmd": "npx prettier --write src/app/pos", "exit_code": 0},
            {"fingerprint": "npm test -- pos", "exit_code": 0},
        ],
        "tests": [{"command": "API_TOKEN=abcdEFGH1234abcdEFGH1234abcdEFGH1234 npm test -- pos", "passed": 3, "failed": 0}],
    }
    out = outbound("checkpoint", facts)
    assert out["commands"][0] == {"verb": "prettier", "exit_code": 0}  # package runners are unwrapped
    assert out["commands"][1] == {"fingerprint": "npm test -- pos", "exit_code": 0}
    assert out["tests"][0]["command"].endswith("npm test -- pos")
    assert "abcdEFGH1234" not in json.dumps(out)


def test_output_keys_and_hostnames_are_dropped():
    out = outbound("heartbeat", {"stdout": "DB_PASSWORD=x", "stderr": "boom", "hostname": "Manis-MBP.local", "ok": True})
    assert out == {"ok": True}


def test_paths_are_made_relative_or_home_reduced():
    text = "edited /Users/mani/code/yb/src/a.ts and /Users/mani/.aws/credentials in /Users/mani/code/yb"
    out = redact_text(text, repo_root="/Users/mani/code/yb", home="/Users/mani")
    assert out == "edited src/a.ts and ~/.aws/credentials in ."


def test_env_header_basic_auth_and_url_credentials():
    text = (
        "DATABASE_URL=postgres://app:s3cretpass@db:5432/x NODE_ENV=production "
        "curl -u admin:hunter22 --header 'X-Api-Key: zzz' https://bob:pw12345@example.com/"
    )
    out = redact_text(text)
    for secret in ("s3cretpass", "hunter22", "zzz", "pw12345"):
        assert secret not in out
    assert "NODE_ENV=production" in out


def test_pii_scrubber_is_applied_last():
    out = outbound("report", {"summary": "call 555-0100"}, pii=lambda s: s.replace("555-0100", "[REDACTED:phone]"))
    assert out == {"summary": "call [REDACTED:phone]"}


def test_unknown_payload_type_is_refused():
    assert "promotion" in PAYLOAD_TYPES
    with pytest.raises(ValueError):
        outbound("email", {})


@pytest.mark.parametrize(
    ("command", "verb"),
    [
        ("export ANTHROPIC_API_KEY=x && claude -p hi", "claude"),
        ("FOO=1 sudo npm run build", "npm"),
        ("/usr/local/bin/pytest -q", "pytest"),
        ("", "command"),
    ],
)
def test_command_verb(command, verb):
    assert command_verb(command) == verb
