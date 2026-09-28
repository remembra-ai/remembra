"""Redaction corpus (§11, §13.1) for the single outbound choke point ``crew.redact.outbound``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.crew.vectors import build_corpora
from tests.crew.vectors.loader import load, run_redaction_corpus

CORPUS = load("redaction/corpus.json")
# The Stripe secret-key shape GitHub push protection blocks: 24+ alphanumerics after the prefix.
PUSH_PROTECTION_STRIPE = re.compile(r"(?:sk|rk)_live_[0-9A-Za-z]{24,}")


def test_corpus_covers_every_outbound_payload_type() -> None:
    types = {c["payload_type"] for c in CORPUS["cases"]}
    assert types == set(CORPUS["payload_types"])
    assert set(CORPUS["payload_types"]) == {
        "event",
        "presence",
        "heartbeat",
        "snapshot",
        "checkpoint",
        "report",
        "stall",
        "promotion",
    }
    for c in CORPUS["cases"]:
        assert c["must_not_contain"], c["id"]
        text = json.dumps(c["payload"], ensure_ascii=False)
        for secret in c["must_not_contain"]:
            assert secret in text, (c["id"], "corpus secret must be present in the input")
        for keep in c["must_contain"]:
            assert keep in text, (c["id"], "kept value must be present in the input")


def test_corpus_covers_the_required_secret_forms() -> None:
    blob = json.dumps(CORPUS, ensure_ascii=False)
    for form in (
        "Authorization: Bearer",
        "--header 'Authorization",
        "postgres://app:",
        "export ANTHROPIC_API_KEY=",
        "OPENAI_API_KEY=",
        "sk-ant-api03-",
        "ghp_",
        "AKIA",
        "whsec_",
        "-u admin:",
        "https://mani:",
        "PRIVATE KEY-----",
        CORPUS["context"]["home"],
        "hostname",
        "datadog_api_key: ",  # vendor-prefixed labels with hex / UUID values
        "HEROKU_API_KEY: ",
        "datadogApiKey",
    ):
        assert form in blob, form


def test_no_corpus_value_has_the_stripe_shape_push_protection_blocks() -> None:
    """A committed value of that shape blocks every push of the corpus; the fake stays short of it
    (tests/crew/test_redact.py proves the product's stripe_key rule still redacts it)."""
    blob = json.dumps(CORPUS, ensure_ascii=False)
    assert "sk_live_" in blob  # the Stripe case is still in the corpus
    assert not PUSH_PROTECTION_STRIPE.search(blob)
    assert not PUSH_PROTECTION_STRIPE.search(Path(build_corpora.__file__).read_text(encoding="utf-8"))


def test_runner_reports_leaks_for_an_identity_redactor() -> None:
    failures = run_redaction_corpus(lambda kind, payload, **ctx: payload)
    leaked_cases = {f.split()[0] for f in failures if "leaked" in f}
    assert leaked_cases == {c["id"] for c in CORPUS["cases"]}


def test_runner_reports_losses_for_a_redactor_that_drops_everything() -> None:
    failures = run_redaction_corpus(lambda kind, payload, **ctx: {})
    lost = {f.split()[0] for f in failures if "lost" in f}
    assert lost == {c["id"] for c in CORPUS["cases"] if c["must_contain"]}
    assert not any("leaked" in f for f in failures)


def test_runner_passes_a_redactor_that_removes_exactly_the_listed_values() -> None:
    by_payload = {json.dumps(c["payload"], sort_keys=True): c for c in CORPUS["cases"]}

    def targeted(kind: str, payload: Any, *, repo_root: str, home: str) -> Any:
        case = by_payload[json.dumps(payload, sort_keys=True)]
        text = json.dumps(payload, ensure_ascii=False)
        text = text.replace(repo_root + "/", "")
        for secret in case["must_not_contain"]:
            text = text.replace(secret, "[REDACTED]")
        return json.loads(text)

    assert run_redaction_corpus(targeted) == []
