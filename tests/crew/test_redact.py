"""The outbound redaction choke point (§11) against the WP-0a corpus and the crew rules."""

from __future__ import annotations

import copy
import json
import re

import pytest

from remembra.crew.redact import PAYLOAD_TYPES, command_verb, outbound, redact_text
from remembra.security.secrets import redact_secrets
from tests.crew.vectors.loader import load, run_redaction_corpus


def test_corpus_passes_for_every_payload_type():
    assert run_redaction_corpus(outbound) == []


def test_the_corpus_fake_stripe_key_is_caught_by_the_stripe_rule():
    """The corpus fake is low-entropy and short of push protection's 24+ characters; the product's
    stripe_key rule (16+) must still name it on its own, not only behind a STRIPE_SECRET_KEY= label."""
    fakes = set(re.findall(r"(?:sk|rk)_live_[A-Za-z0-9]+", json.dumps(load("redaction/corpus.json"))))
    assert fakes == {"sk_live_0000TESTONLY0000FAKE"}
    for fake in fakes:
        for text, want in ((fake, "[REDACTED:stripe_key]"), (f"key {fake} end", "key [REDACTED:stripe_key] end")):
            res = redact_secrets(text, fallback=False)  # the named rules only, no high-entropy fallback
            assert (res.text, res.counts) == (want, {"stripe_key": 1}), (text, res)


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
