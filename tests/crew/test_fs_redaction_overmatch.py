"""F29: redaction must not eat ordinary engineering text.

Four over-matching rules destroyed the facts a handoff exists to carry:

* the crew env rule matched ``PASS`` anywhere in a name, so the server's own
  ``Test: <cmd> passed=41 failed=0`` line lost its pass count;
* any bare 8-17 digit run (a CI run id, a migration number, an id in a URL)
  became ``[REDACTED_BANK_ACCOUNT]``, a 10-digit one ``[REDACTED_PHONE_US]``;
* overlapping PII matches were spliced into each other
  (``[REDACTED_BANK_ACCOUNT]PHONE_US]``);
* a URL tail or a ``NAME=/abs/path`` word was scored as one high-entropy token.

Every negative here is paired with a positive: real credentials and labelled
account numbers are still redacted.
"""

from __future__ import annotations

import json
import re

import pytest

from remembra.crew.checkpoints import CheckpointService
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS
from remembra.crew.redact import outbound, redact_text
from remembra.crew.tasks import Caller
from remembra.security.pii_detector import PIIDetector, redact_pii, scan_for_pii
from remembra.security.secrets import redact_secrets
from tests.crew.wp6_support import CREW, event_log, open_db, seed_crew, seed_session

# ---------------------------------------------------------------------------
# 1. The crew env-assignment rule
# ---------------------------------------------------------------------------

NOT_SECRET_ASSIGNMENTS = [
    "Test: node --test tests/*.test.mjs passed=41 failed=0",
    "Test: pytest -q passed=None failed=None",
    "remote square-payments-tmp/green1.tap passed=128 failed=11",
    "tests_passed=41 tests_failed=0",
    "PASSED=41 FAILED=0",
    "CI summary: PASS=120 FAIL=0 SKIP=3",
    "TESTS_PASS=128 TESTS_FAIL=2",
    "bypass=true compass=north",
    "author=mani authored=yesterday",
    "curl=/usr/bin/curl",
    "monkey=patch keyboard=us hotkey=ctrl-k",
    "max_tokens=4096 tokens=512",
    "tokenizer=bpe keys=3",
]


@pytest.mark.parametrize("text", NOT_SECRET_ASSIGNMENTS)
def test_ordinary_assignments_keep_their_value(text):
    assert redact_text(text) == text
    assert outbound("promotion", text) == text


SECRET_ASSIGNMENTS = [
    ("DB_PASS=hunter22x", "hunter22x"),
    ("PGPASSWORD=hunter22x psql", "hunter22x"),
    ("DATABASE_URL=postgres://db.internal:5432/app", "db.internal"),
    ("REDISURL=redis://cache.internal:6379/0", "cache.internal"),
    ("dbUrl=postgres://db.internal:5432/app", "db.internal"),
    ("OPENAI_API_KEY=abc123def", "abc123def"),
    ("API_KEY=1234567", "1234567"),
    ("MYAPIKEY=abc123def", "abc123def"),
    ("GITHUB_TOKEN=abc123def", "abc123def"),
    ("authToken=abc123def", "abc123def"),
    ("clientSecret=abc123def", "abc123def"),
    ("SENTRY_DSN=https://o1.ingest.example/7", "ingest.example"),
    ("encryptionKey=abc123def signingKey=zyx987wvu", "abc123def"),
    ("encryptionKey=abc123def signingKey=zyx987wvu", "zyx987wvu"),
    ("KEYS=abc123def,zyx987wvu", "abc123def"),
    ("passwords=hunter22x,hunter33y", "hunter22x"),
    ("MYSQL_PWD=hunter22x", "hunter22x"),
    ("BASIC_AUTH=svc:hunter22x", "hunter22x"),
    ("SECRET_KEY_BASE=abc123def", "abc123def"),
    ("DBPASS=hunter22x", "hunter22x"),
    ("TOKENS=abc123def", "abc123def"),
    ("PASS=hunter22x", "hunter22x"),
]


@pytest.mark.parametrize(("text", "secret"), SECRET_ASSIGNMENTS)
def test_secret_assignments_still_lose_their_value(text, secret):
    assert secret not in redact_text(text)


async def test_promoted_checkpoint_keeps_its_test_counts(tmp_path):
    """The server writes ``passed=<n> failed=<n>`` itself; its own redaction must not erase the count."""
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)

        async def resolver(owner):
            return SELF_HOSTED_CREW_LIMITS

        svc = CheckpointService(event_log(db), limits_resolver=resolver, pii=PIIDetector(enabled=True, mode="redact").redact)
        s = await seed_session(db)
        facts = {
            "branch": "main",
            "head": "a1b2c3d4e5f6",
            "tests": [
                {"command": "node --test tests/*.test.mjs", "passed": 41, "failed": 0},
                {"command": "pytest -q"},
            ],
        }
        res = await svc.ingest(CREW, Caller.for_session(s), {"session_id": s["id"], "trigger": "test", "facts": facts})
        assert res.promotion == "promoted"
        rows = await db.fetchall("SELECT payload FROM crew_outbox WHERE kind = 'memory_promotion'")
        content = json.loads(rows[0]["payload"])["content"]
        assert "Test: node --test tests/*.test.mjs passed=41 failed=0" in content
        assert "Test: pytest -q passed=None failed=None" in content
        assert "[REDACTED" not in content
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 2. Numbers that are developer identifiers, not accounts or phones
# ---------------------------------------------------------------------------

DEVELOPER_NUMBERS = [
    "gh run view 42664995945",
    "Release run 40233615394 is waiting",
    "kill-9 soak job passed in CI run 40233615394",
    "TODO: Banking redesign UI B1-B8 + PR (migrations 590000000-599999)",
    "www.facebook.com/100063512345678 (redirects heartandsoledancestudio)",
    "https://github.com/acme/widget/actions/runs/42664995945/job/71234567890",
    "App Store Connect app ID 1234567890, bundle com.example.app",
    "app_id=6810035908 team id 1234567890",
    "epoch 1762735701",
    "workflow run 4266499594512345 finished",
    "build 1234567890 deployed to staging",
    "see artifacts/20261110/1234567890123/report.html",
]


@pytest.mark.parametrize("text", DEVELOPER_NUMBERS)
def test_developer_identifiers_are_not_pii(text):
    assert redact_pii(text) == text
    assert scan_for_pii(text).matches == []


STILL_PII = [
    ("acct 4000123456789 on file", "4000123456789", "[REDACTED_BANK_ACCOUNT]"),
    ("wire to 9876543210123-", "9876543210123", "[REDACTED_BANK_ACCOUNT]"),
    ("account for run 42664995945", "42664995945", "[REDACTED_BANK_ACCOUNT]"),
    ("account https://bank.example/42664995945", "42664995945", "[REDACTED_BANK_ACCOUNT]"),
    ("Tax ID 123456789", "123456789", "[REDACTED_BANK_ACCOUNT]"),
    ("check # 12345678", "12345678", "[REDACTED_BANK_ACCOUNT]"),
    ("call me on 2035550123 tomorrow", "2035550123", "[REDACTED_"),
    ("Cash app 2035550123", "2035550123", "[REDACTED_"),
    ("ask the job site foreman 2035550123", "2035550123", "[REDACTED_"),
    ("call 203-555-0123 about the build", "203-555-0123", "[REDACTED_PHONE_US]"),
    ("job contact: +1 203 555 0123", "203 555 0123", "[REDACTED_PHONE_US]"),
    ("card 4111111111111111 declined", "4111111111111111", "[REDACTED_"),
    ("card 4111 1111 1111 1111 for the build", "4111 1111 1111 1111", "[REDACTED_CREDIT_CARD]"),
    ("ssn 123-45-6789 in the run log", "123-45-6789", "[REDACTED_SSN]"),
]


@pytest.mark.parametrize(("text", "number", "placeholder"), STILL_PII)
def test_personal_numbers_are_still_redacted(text, number, placeholder):
    redacted = redact_pii(text)
    assert number not in redacted and placeholder in redacted


# ---------------------------------------------------------------------------
# 3. Overlapping PII matches give one whole placeholder
# ---------------------------------------------------------------------------

_PLACEHOLDER_SHAPE = r"\[(?:REDACTED|HASH)_[A-Z_]+(?::[0-9a-f]{12})?\]"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("acct 1234567890", "acct "),
        ("ids 59000000-59999999", "ids "),
        ("wire 4111111111111111 today", "wire "),
        ("my number is 2035550123.", "my number is "),
    ],
)
def test_overlapping_matches_are_not_spliced(text, kept):
    detector = PIIDetector(enabled=True, mode="redact")
    for out in (redact_pii(text), detector.scan(text).redacted_content, detector.hash_pii(text)):
        assert out is not None and out.startswith(kept)
        leftover = re.sub(_PLACEHOLDER_SHAPE, "", out)
        assert "[" not in leftover and "]" not in leftover, out
        assert not re.search(r"\d{4}", leftover), out
        assert len(re.findall(_PLACEHOLDER_SHAPE, out)) == 1, out


def test_overlap_keeps_the_most_severe_type_and_separate_matches_stay_separate():
    assert redact_pii("wire 4111111111111111 today") == "wire [REDACTED_CREDIT_CARD] today"
    out = redact_pii("mail a@example.com or acct 4000123456789")
    assert out == "mail [REDACTED_EMAIL] or acct [REDACTED_BANK_ACCOUNT]"


# ---------------------------------------------------------------------------
# 4. The unlabelled high-entropy fallback
# ---------------------------------------------------------------------------

NOT_TOKENS = [
    "NODE_OPTIONS=--preserve-symlinks TMPDIR=/Volumes/T7/Codex-Worktrees/bth-ai-setup-20261107 node --test",
    "pytest --output=/Volumes/T7/Codex-Worktrees/bth-ai-setup-20261107/report.xml",
    "cd T7/Codex-Worktrees/bth-ai-setup-20261107 && npm test",
    "branch feature/Add-OAuth2-Login-For-Enterprise-SSO",
    "node --test --test-name-pattern=Checkout2Flow_handlesRetries_3x",
    "YouTube https://www.youtube.com/channel/UCa1B2c3D4e5F6g7H8i9J0kL",
    "https://github.com/acme/Widget-Service/actions/runs/42664995945/job/7Ab",
    "see www.example.com/docs/Getting-Started/Install-Guide-v2 for setup",
]


@pytest.mark.parametrize("text", NOT_TOKENS)
def test_paths_urls_and_names_are_not_high_entropy_tokens(text):
    result = redact_secrets(text)
    assert result.text == text and not result.redacted
    assert redact_text(text, home="/Users/nobody") == text


_RANDOM_40 = "q7Lm2Xv9Tb4Rk8Zs1Wd6Yh3Nc5Jf0Pg7Ua2Ee9Io"
_RANDOM_B64 = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzQ3vT9kLm2X"

STILL_TOKENS = [
    f"the value is {_RANDOM_40} keep it safe",
    f"blob {_RANDOM_B64} was pasted",
    f"TMPDIR={_RANDOM_40} node --test",
    f"--output={_RANDOM_40}",
    f"https://x.test/cb?token={_RANDOM_40}",
    f"https://x.test/cb?state={_RANDOM_40}",
    f"https://hooks.example.com/services/{_RANDOM_40}",
    f"open www.example.com/reset/{_RANDOM_40} now",
]


@pytest.mark.parametrize("text", STILL_TOKENS)
def test_real_tokens_are_still_redacted(text):
    out = redact_secrets(text).text
    assert _RANDOM_40 not in out and _RANDOM_B64 not in out, out
    assert "[REDACTED:" in out


def test_a_secret_in_a_url_is_cut_out_without_eating_the_rest_of_the_url():
    out = redact_secrets(f"https://hooks.example.com/services/{_RANDOM_40}/status").text
    assert out == "https://hooks.example.com/services/[REDACTED:high_entropy_token]/status"
