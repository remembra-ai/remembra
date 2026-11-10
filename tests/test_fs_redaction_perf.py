"""F29 follow-up: redaction runs in time linear in the text, and redacts what it did before.

The F29 fixes (tests/crew/test_fs_redaction_overmatch.py) looked around every match with a scan
of its own, so the work grew with the square of the text. Redaction runs inline in the
single-process async API server, on bodies of up to 50,000 characters:

* ``pii_detector``: the developer-number check re-read the whole whitespace-free word around
  every bare digit run. Compact JSON with ten-to-sixteen digit numbers took about 4 s.
* ``secrets``: the URL-head search ran over the 2,048 characters before every long token. Long
  tokens joined by dots took about 10 s.
* ``crew.redact``: every ``*_PASS=<n>`` searched the whole text for its ``*_FAIL=``. About 1.5 s.

Three older patterns had the same shape and are covered too: the e-mail pattern and the two
``scheme://user:password@`` patterns were tried at every word boundary of a long dotted run
(1.6 s to 3.6 s each), the identifier check re-read a dotted run of digits for every number in
it (16 s), and each placeholder was spliced into the text with a copy of all of it.

Each worst case is built at the 50,000-character cap and must finish well inside a bound that is
many times the measured time (0.01 s to 0.1 s on the machine the fix was written on), and its
output must be the one the code produced before the rewrite. ``EXPECTED`` holds those outputs,
captured from the unoptimised branch code: whole for the short representative inputs, as a
SHA-256 prefix for the 50,000-character ones. The ``*_matches_the_pattern`` tests check each
rewritten search against the regular expression it replaces, on seeded random text.
"""

from __future__ import annotations

import hashlib
import random
import re
import time

import pytest

from remembra.crew.redact import _env_count_name, redact_text
from remembra.security import secrets
from remembra.security.pii_detector import (
    _ACCOUNT_LABEL,
    _LABEL_WINDOW,
    PII_PATTERNS,
    PIIDetector,
    _email_matches,
    _ScanContext,
)
from remembra.security.secrets import _UrlHeads, redact_secrets, url_password_spans

# --- inputs (no imports from remembra: the expected outputs are captured by running this part
# --- against the code as it was before the rewrite) ---------------------------------------------

CAP = 50_000  # the largest text the API accepts
# Seconds. The slowest case measures 0.1 s after the fix and 1.5 s to 28 s before it.
BOUND = 1.5

TOKEN = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3dEf6h"


def _fill(unit: str, size: int = CAP) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def _compact_json() -> str:
    numbers = ",".join(f'"id{i}":{10**11 + i * 7919}' for i in range(4000))
    return ("{" + numbers + "}")[:CAP]


def _pass_assignments() -> str:
    return " ".join(f"SUITE{i}_PASS={i % 900}" for i in range(4000))[:CAP]


def _pass_fail_pairs() -> str:
    return "".join(f"S{i}_PASS={i % 900};S{i}_FAIL=0;" for i in range(3000))[:CAP]


# name -> (the function under test, the text). The first three are the cases of the review.
LARGE = {
    "compact_json_numbers": ("pii", _compact_json),
    "dot_joined_tokens": ("secrets", lambda: _fill(TOKEN + ".")),
    "pass_assignments": ("crew", _pass_assignments),
    # Other shapes that were quadratic in one of the three, or could have been.
    "pass_fail_pairs": ("crew", _pass_fail_pairs),
    "camel_pass_assignments": ("crew", lambda: " ".join(f"suite{i}Pass={i % 900}" for i in range(4000))[:CAP]),
    "numbers_joined_by_slashes": ("pii", lambda: _fill("12345678901/")),
    "numbers_joined_by_dots": ("pii", lambda: _fill("12345678901.")),
    "numbers_joined_by_equals": ("pii", lambda: _fill("12345678901=")),
    "numbers_joined_by_commas": ("pii", lambda: _fill("1234567890123456,")),
    "numbers_joined_by_dashes": ("pii", lambda: _fill("123456789-")),
    "numbers_in_quotes": ("pii", lambda: _fill('"12345678901"')),
    "numbers_after_account_labels": ("pii", lambda: _fill("acct:12345678901,")),
    "numbers_in_one_url_path": ("pii", lambda: "https://example.com/" + _fill("12345678901/", CAP - 20)),
    "numbers_in_nested_urls": ("pii", lambda: _fill("https://a.example/r/12345678901?u=www.b.example/4155552671/")),
    "numbers_in_a_named_path": ("pii", lambda: _fill("ab/12345678901/cd/")),
    "host_names_after_numbers": ("pii", lambda: _fill("123456789-a.")),
    "dotted_run_without_at": ("pii", lambda: _fill("a.b+c-d.")),
    "dotted_run_before_one_address": ("pii", lambda: _fill("ab.cd+ef-")[:-6] + "@x.com"),
    "tokens_in_one_url_path": ("secrets", lambda: "https://example.com/" + _fill(TOKEN + "/", CAP - 20)),
    "tokens_in_nested_urls": ("secrets", lambda: _fill("https://a.example/r?u=http://b.example/" + TOKEN + "&v=")),
    "tokens_after_www": ("secrets", lambda: _fill("www." + TOKEN + ".")),
    "tokens_after_scheme_characters": ("secrets", lambda: _fill("a.b+c-d." + TOKEN + ".")),
    "tokens_joined_by_equals": ("secrets", lambda: _fill(TOKEN + "=")),
    "scheme_characters_run": ("secrets", lambda: _fill("a.b+c-d.")),
    "scheme_characters_run_crew": ("crew", lambda: _fill("a.b+c-d.")),
    "www_run": ("secrets", lambda: _fill("www.")),
    "www_run_crew": ("crew", lambda: _fill("www.")),
    "url_credentials": ("crew", lambda: _fill("a.b+c://user:pw12345@")),
}
for _name, _unit in (("digits", "1"), ("dots", "."), ("slashes", "/"), ("equals", "=")):
    for _function in ("pii", "secrets", "crew"):
        LARGE[f"{_name}_{_function}"] = (_function, lambda unit=_unit: unit * CAP)

REPRESENTATIVE = {
    "pii": [
        "CI run 40233615394 passed",
        "gh run view 42664995945 --log",
        "migrations 590000000-599999",
        "epoch 1762735701",
        "App Store Connect app ID 1234567890",
        "build 12345678 deploy 123456789 job id: 9876543210",
        "call me at 4155552671",
        "my account 12345678901",
        "acct no 123456789012",
        "Tax ID 123456789",
        "check # 12345678",
        "run card 4155552671",
        "https://github.com/acme/app/actions/runs/42664995945/job/7",
        "www.facebook.com/100063512345678",
        "artifacts/20261110/1234567890123/report.html",
        "12345678901/021000021",
        "2035551234/ext",
        "tel://4155552671",
        "https://wa.me/14155552671",
        "https://pay.example.com/charge/4111111111111111",
        "https://x.io/id/1234567890123456",
        "https://x.io/id/4111111111111111",
        "(see https://x.io/runs/12345678901).",
        "https://x.io/a?run=12345678901&job=98765432101",
        "account12345678901/12345678901/12345678901",
        "x/y/account/12345678901 and ab/cd/12345678901",
        "acct https://x.io/runs/12345678901/98765432101",
        "acct:12345678901,98765432101",
        '{"id":12345678901,"run_id":42664995945,"phone":"4155552671","acct":"12345678901"}',
        "card 4111 1111 1111 1111",
        "ssn 123-45-6789",
        "+1 415-555-2671 or (415) 555-2671",
        "629134551556-8v3kq0abc.apps.googleusercontent.com",
        "20260926123456-a1b2c3",
        "12345678-1234-5678-1234-567812345678",
        "account 12345678-checking",
        "123456789-a.123456789-a.123456789-a.b",
        "mail bob.smith+tag@example.co.uk now",
        "a@b.co@d.ef",
        "x.y.z@ and @example.com and bob@example",
        "user_12345678901@example.com",
        "first.last@mail.example.org,second@x.io;third%x@y.dev",
        "password: hunter2secret",
        "nothing to see here",
    ],
    "secrets": [
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuvwx",
        "https://example.com/reset/aB3xK9mQ2pL7vR4tZ8wN1c",
        "https://example.com/cb?code=aB3xK9mQ2pL7vR4tZ8wN1cYq&state=xyz",
        "https://example.com/cb#access=aB3xK9mQ2pL7vR4tZ8wN1cYq",
        "www.example.com/files/" + TOKEN,
        "WWW.Example.com/files/" + TOKEN,
        "xwww.example.com/files/" + TOKEN,
        "token " + TOKEN + " here",
        "https://e.co/user/" + TOKEN + "/x",
        "https://e.co/files/" + TOKEN + "/" + TOKEN,
        "https://e.co/a?next=/docs/setup/" + TOKEN,
        "https://e.co/\n" + TOKEN,
        'see "https://e.co/a" ' + TOKEN,
        "<https://e.co/a>" + TOKEN,
        "ftp://" + TOKEN + "." + TOKEN,
        "1://" + TOKEN + "." + TOKEN,
        "a.b+c-d." + TOKEN + ".x://y/" + TOKEN,
        TOKEN + "." + TOKEN + "." + TOKEN,
        "postgres://admin:s3cretPass@db.internal:5432/app",
        "x.y+https://u:pw3@h/ and \u00e9https://u:pw3@h and _https://u:pw3@h",
        "https://u:p@h and https://u:pw@h and https://user@h and https://h:8080/x@y",
        "a://b:c:d:e@f g://h:ijk@l://m:nop@q",
        "TMPDIR=/Volumes/T7/Claude-Worktrees/fault-survey-20261110/logs",
        "feature/Add-OAuth2-Login-For-Enterprise-SSO",
        "sha 0123456789abcdef0123456789abcdef01234567",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "nothing to see here",
    ],
    "crew": [
        "PASS=120 FAIL=0",
        "TESTS_PASS=128",
        "Test: pytest -q passed=41 failed=0",
        "DB_PASS=123456",
        "SSHPASS=123456",
        "SUITE_PASS=12 SUITE_FAIL=0",
        "SUITE_PASS=12 SUITE_FAILED=1",
        "SUITE_PASS=12 SUITE_FAILURE=1 UNIT_PASS=3 UNIT_FAILURES=0 E2E_PASS=4 e2e_fails=0",
        "suitePass=12 suiteFailures=2",
        "X_PASS=12 Y_FAIL=0",
        "A_PASS=7 xA_FAIL=1",
        "A_PASS=7;A_FAIL=",
        "A_PASS=7 A_FAIL =1",
        "PASS_RATE_PASS=3 PASS_RATE_FAIL=1",
        "S1_PASS=1 S2_PASS=2 S2_FAIL=0 S3_PASS=None S3_FAILED=0",
        "PASSWORD=12 FAILWORD=0",
        "max_tokens=4096 tokens=512 secrets=2 passwords=1 urls=4 keys=3",
        "author=jane bypass=1",
        "PGPASSWORD=None",
        "API_KEY=abc123",
        "export FOO=" + TOKEN,
        "curl -u bob:hunter2 https://x.io",
        "Authorization: Bearer abc",
        "git clone https://bob:hunter2@github.com/acme/app.git",
        "x.y+ssh://u:p@h and \u00e9http://u:p@h and _http://u:p@h and http://u@h",
        "a://b:c:d:e@f g://h:i@l://m:n@q",
        "DB_URL=postgres://u:p@h/db",
        "/Users/someone/Projects/app/file.py",
        "nothing to see here",
    ],
}

# --- end inputs ---------------------------------------------------------------------------------

# --- expected (captured from the code before the rewrite) ---------------------------------------
EXPECTED: dict = {
    "large": {
        "camel_pass_assignments": "8e60406485d5a713",
        "compact_json_numbers": "0e160d80f0129825",
        "digits_crew": "5fc6a3936f98e0c0",
        "digits_pii": "5fc6a3936f98e0c0",
        "digits_secrets": "5fc6a3936f98e0c0",
        "dot_joined_tokens": "60d9f3a7fd2d1a2b",
        "dots_crew": "33a2162f9179fd1b",
        "dots_pii": "33a2162f9179fd1b",
        "dots_secrets": "33a2162f9179fd1b",
        "dotted_run_before_one_address": "2a40c9e2e9071d9a",
        "dotted_run_without_at": "920534d87f08c964",
        "equals_crew": "27e8d8ede52e1f9f",
        "equals_pii": "27e8d8ede52e1f9f",
        "equals_secrets": "27e8d8ede52e1f9f",
        "host_names_after_numbers": "7017d5a598d4a5c3",
        "numbers_after_account_labels": "39c4f77090fc01e5",
        "numbers_in_a_named_path": "9a5e24f50f9711ca",
        "numbers_in_nested_urls": "dbed154471a8fa95",
        "numbers_in_one_url_path": "df505090d709c9a9",
        "numbers_in_quotes": "b93c5bbb41e7df5c",
        "numbers_joined_by_commas": "83ddaa293f2445fd",
        "numbers_joined_by_dashes": "fd4665803be2a0cb",
        "numbers_joined_by_dots": "46cdf2cef7b4f004",
        "numbers_joined_by_equals": "62937191a3cf23c3",
        "numbers_joined_by_slashes": "78ac3fd99f964cda",
        "pass_assignments": "3f0ff615eb38c095",
        "pass_fail_pairs": "7ed7cf7810b40857",
        "scheme_characters_run": "920534d87f08c964",
        "scheme_characters_run_crew": "920534d87f08c964",
        "slashes_crew": "92d21b777efa94be",
        "slashes_pii": "92d21b777efa94be",
        "slashes_secrets": "92d21b777efa94be",
        "tokens_after_scheme_characters": "25f203824c32e726",
        "tokens_after_www": "a61adaeec710fe4b",
        "tokens_in_nested_urls": "7cb18429f72ac181",
        "tokens_in_one_url_path": "e67e9a849b131323",
        "tokens_joined_by_equals": "a0fe45f4aa1a367e",
        "url_credentials": "ead0ff8d2182d72b",
        "www_run": "10867bed183a3657",
        "www_run_crew": "10867bed183a3657",
    },
    "pii": [
        "CI run 40233615394 passed",
        "gh run view 42664995945 --log",
        "migrations 590000000-599999",
        "epoch 1762735701",
        "App Store Connect app ID 1234567890",
        "build 12345678 deploy 123456789 job id: 9876543210",
        "call me at [REDACTED_PHONE_US]",
        "my account [REDACTED_BANK_ACCOUNT]",
        "acct no [REDACTED_BANK_ACCOUNT]",
        "Tax ID [REDACTED_BANK_ACCOUNT]",
        "check # [REDACTED_BANK_ACCOUNT]",
        "run card [REDACTED_PHONE_US]",
        "https://github.com/acme/app/actions/runs/42664995945/job/7",
        "www.facebook.com/100063512345678",
        "artifacts/20261110/1234567890123/report.html",
        "[REDACTED_BANK_ACCOUNT]/[REDACTED_BANK_ACCOUNT]",
        "[REDACTED_PHONE_US]/ext",
        "tel://[REDACTED_PHONE_US]",
        "https://wa.me/[REDACTED_BANK_ACCOUNT]",
        "https://pay.example.com/charge/[REDACTED_CREDIT_CARD]",
        "https://x.io/id/1234567890123456",
        "https://x.io/id/[REDACTED_CREDIT_CARD]",
        "(see https://x.io/runs/12345678901).",
        "https://x.io/a?run=12345678901&job=98765432101",
        "account12345678901/[REDACTED_BANK_ACCOUNT]/[REDACTED_BANK_ACCOUNT]",
        "x/y/account/[REDACTED_BANK_ACCOUNT] and ab/cd/[REDACTED_BANK_ACCOUNT]",
        "acct https://x.io/runs/[REDACTED_BANK_ACCOUNT]/[REDACTED_BANK_ACCOUNT]",
        "acct:[REDACTED_BANK_ACCOUNT],[REDACTED_BANK_ACCOUNT]",
        (
            '{"id":[REDACTED_BANK_ACCOUNT],"run_id":[REDACTED_BANK_ACCOUNT],"phone":"[REDACTE'
            'D_PHONE_US]","acct":"[REDACTED_BANK_ACCOUNT]"}'
        ),
        "card [REDACTED_CREDIT_CARD]",
        "ssn [REDACTED_SSN]",
        "+1 [REDACTED_PHONE_US] or ([REDACTED_PHONE_US]",
        "629134551556-8v3kq0abc.apps.googleusercontent.com",
        "20260926123456-a1b2c3",
        "[REDACTED_CREDIT_CARD]-[REDACTED_CREDIT_CARD]",
        "account [REDACTED_BANK_ACCOUNT]-checking",
        "123456789-a.123456789-a.[REDACTED_BANK_ACCOUNT]-a.b",
        "mail [REDACTED_EMAIL] now",
        "[REDACTED_EMAIL]@d.ef",
        "x.y.z@ and @example.com and bob@example",
        "[REDACTED_EMAIL]",
        "[REDACTED_EMAIL],[REDACTED_EMAIL];[REDACTED_EMAIL]",
        "password: [REDACTED_PASSWORD]",
        "nothing to see here",
    ],
    "pii_hashed": [
        "CI run 40233615394 passed",
        "gh run view 42664995945 --log",
        "migrations 590000000-599999",
        "epoch 1762735701",
        "App Store Connect app ID 1234567890",
        "build 12345678 deploy 123456789 job id: 9876543210",
        "call me at [HASH_PHONE_US:e732f608bbbf]",
        "my account [HASH_BANK_ACCOUNT:254aa248acb4]",
        "acct no [HASH_BANK_ACCOUNT:2a33349e7e60]",
        "Tax ID [HASH_BANK_ACCOUNT:15e2b0d3c338]",
        "check # [HASH_BANK_ACCOUNT:ef797c8118f0]",
        "run card [HASH_PHONE_US:e732f608bbbf]",
        "https://github.com/acme/app/actions/runs/42664995945/job/7",
        "www.facebook.com/100063512345678",
        "artifacts/20261110/1234567890123/report.html",
        "[HASH_BANK_ACCOUNT:254aa248acb4]/[HASH_BANK_ACCOUNT:1f873cdde0ca]",
        "[HASH_PHONE_US:b0698ba8dc6b]/ext",
        "tel://[HASH_PHONE_US:e732f608bbbf]",
        "https://wa.me/[HASH_BANK_ACCOUNT:758fbf68945f]",
        "https://pay.example.com/charge/[HASH_CREDIT_CARD:9bbef1947662]",
        "https://x.io/id/1234567890123456",
        "https://x.io/id/[HASH_CREDIT_CARD:9bbef1947662]",
        "(see https://x.io/runs/12345678901).",
        "https://x.io/a?run=12345678901&job=98765432101",
        "account12345678901/[HASH_BANK_ACCOUNT:254aa248acb4]/[HASH_BANK_ACCOUNT:254aa248acb4]",
        "x/y/account/[HASH_BANK_ACCOUNT:254aa248acb4] and ab/cd/[HASH_BANK_ACCOUNT:254aa248acb4]",
        "acct https://x.io/runs/[HASH_BANK_ACCOUNT:254aa248acb4]/[HASH_BANK_ACCOUNT:eaf44040fbc9]",
        "acct:[HASH_BANK_ACCOUNT:254aa248acb4],[HASH_BANK_ACCOUNT:eaf44040fbc9]",
        (
            '{"id":[HASH_BANK_ACCOUNT:254aa248acb4],"run_id":[HASH_BANK_ACCOUNT:5686a0545558]'
            ',"phone":"[HASH_PHONE_US:e732f608bbbf]","acct":"[HASH_BANK_ACCOUNT:254aa248acb4]'
            '"}'
        ),
        "card [HASH_CREDIT_CARD:6a7e0e79b018]",
        "ssn [HASH_SSN:01a54629efb9]",
        "+1 [HASH_PHONE_US:48c1cea5f658] or ([HASH_PHONE_US:d2782530e3f3]",
        "629134551556-8v3kq0abc.apps.googleusercontent.com",
        "20260926123456-a1b2c3",
        "[HASH_CREDIT_CARD:d8764af8f091]-[HASH_CREDIT_CARD:7a072e7883fd]",
        "account [HASH_BANK_ACCOUNT:ef797c8118f0]-checking",
        "123456789-a.123456789-a.[HASH_BANK_ACCOUNT:15e2b0d3c338]-a.b",
        "mail [HASH_EMAIL:908065bdd531] now",
        "[HASH_EMAIL:80305c9bb1bb]@d.ef",
        "x.y.z@ and @example.com and bob@example",
        "[HASH_EMAIL:868de725e181]",
        "[HASH_EMAIL:11fd2ca24e5c],[HASH_EMAIL:a39ae38db0e6];[HASH_EMAIL:946d96414ea6]",
        "password: [HASH_PASSWORD:5ccefdc77432]",
        "nothing to see here",
    ],
    "secrets": [
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuvwx",
        "https://example.com/reset/[REDACTED:high_entropy_token]",
        "https://example.com/cb?code=aB3xK9mQ2pL7vR4tZ8wN1cYq&state=xyz",
        "https://example.com/cb#access=aB3xK9mQ2pL7vR4tZ8wN1cYq",
        "www.example.com/files/[REDACTED:high_entropy_token]",
        "WWW.Example.com/files/[REDACTED:high_entropy_token]",
        "xwww.example.[REDACTED:high_entropy_token]",
        "token [REDACTED:high_entropy_token] here",
        "https://e.co/user/[REDACTED:high_entropy_token]/x",
        "https://e.co/files/[REDACTED:high_entropy_token]/[REDACTED:high_entropy_token]",
        "https://e.co/a?next=/docs/setup/[REDACTED:high_entropy_token]",
        "https://e.co/\n[REDACTED:high_entropy_token]",
        'see "https://e.co/a" [REDACTED:high_entropy_token]',
        "<https://e.co/a>[REDACTED:high_entropy_token]",
        "ftp://Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3dEf6h.[REDACTED:high_entropy_token]",
        "1://Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3dEf6h.[REDACTED:high_entropy_token]",
        "a.b+c-d.[REDACTED:high_entropy_token].x://y/Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3dEf6h",
        "[REDACTED:high_entropy_token].[REDACTED:high_entropy_token].[REDACTED:high_entropy_token]",
        "postgres://admin:[REDACTED:url_credentials]@db.internal:5432/app",
        "x.y+https://u:[REDACTED:url_credentials]@h/ and éhttps://u:pw3@h and _https://u:pw3@h",
        "https://u:p@h and https://u:pw@h and https://user@h and https://h:8080/x@y",
        "a://b:[REDACTED:url_credentials]@f g://h:[REDACTED:url_credentials]@l://m:[REDACTED:url_credentials]@q",
        "TMPDIR=/Volumes/T7/Claude-Worktrees/fault-survey-20261110/logs",
        "feature/Add-OAuth2-Login-For-Enterprise-SSO",
        "sha 0123456789abcdef0123456789abcdef01234567",
        "Authorization: Bearer [REDACTED:bearer_token]",
        "nothing to see here",
    ],
    "crew": [
        "PASS=120 FAIL=0",
        "TESTS_PASS=128",
        "Test: pytest -q passed=41 failed=0",
        "DB_PASS=[REDACTED:env]",
        "SSHPASS=[REDACTED:env]",
        "SUITE_PASS=12 SUITE_FAIL=0",
        "SUITE_PASS=12 SUITE_FAILED=1",
        "SUITE_PASS=12 SUITE_FAILURE=1 UNIT_PASS=3 UNIT_FAILURES=0 E2E_PASS=4 e2e_fails=0",
        "suitePass=12 suiteFailures=2",
        "X_PASS=[REDACTED:env] Y_FAIL=0",
        "A_PASS=[REDACTED:env] xA_FAIL=1",
        "A_PASS=7;A_FAIL=",
        "A_PASS=[REDACTED:env] A_FAIL =1",
        "PASS_RATE_PASS=[REDACTED:env] PASS_RATE_FAIL=[REDACTED:env]",
        "S1_PASS=[REDACTED:env] S2_PASS=2 S2_FAIL=0 S3_PASS=None S3_FAILED=0",
        "PASSWORD=[REDACTED:env] FAILWORD=0",
        "max_tokens=4096 tokens=512 secrets=2 passwords=1 urls=4 keys=3",
        "author=jane bypass=1",
        "PGPASSWORD=[REDACTED:env]",
        "API_KEY=[REDACTED:env]",
        "export FOO=[REDACTED:env]",
        "curl -u bob:[REDACTED:password] https://x.io",
        "Authorization: [REDACTED:header]",
        "git clone https://bob:[REDACTED:password]@github.com/acme/app.git",
        "x.y+ssh://u:[REDACTED:password]@h and éhttp://u:p@h and _http://u:p@h and http://u@h",
        "a://b:[REDACTED:password]@f g://h:[REDACTED:password]@l://m:[REDACTED:password]@q",
        "DB_URL=[REDACTED:env]",
        "~/Projects/app/file.py",
        "nothing to see here",
    ],
}
# --- end expected -------------------------------------------------------------------------------


def _pii(text: str) -> str:
    return PIIDetector(mode="redact").redact(text)


def _secrets(text: str) -> str:
    return redact_secrets(text).text


FUNCTIONS = {"pii": _pii, "secrets": _secrets, "crew": redact_text}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.mark.parametrize("name", sorted(LARGE))
def test_worst_case_at_the_size_cap_is_fast_and_unchanged(name):
    function, build = LARGE[name]
    text = build()
    assert len(text) == CAP

    started = time.perf_counter()
    out = FUNCTIONS[function](text)
    elapsed = time.perf_counter() - started

    assert elapsed < BOUND, f"{function} took {elapsed:.2f}s on {name}"
    assert _digest(out) == EXPECTED["large"][name]


@pytest.mark.parametrize("function", sorted(REPRESENTATIVE))
def test_representative_inputs_redact_as_before(function):
    got = [FUNCTIONS[function](text) for text in REPRESENTATIVE[function]]
    assert got == EXPECTED[function]
    # The set is not vacuous: most inputs are changed by redaction, and some are left alone.
    changed = sum(1 for text, out in zip(REPRESENTATIVE[function], got, strict=True) if text != out)
    assert 0 < changed < len(got)


def test_pii_hashing_is_unchanged():
    detector = PIIDetector(mode="detect")
    assert [detector.hash_pii(text) for text in REPRESENTATIVE["pii"]] == EXPECTED["pii_hashed"]


def test_scrub_is_the_same_redaction(monkeypatch):
    monkeypatch.setattr(secrets, "redaction_enabled", lambda: True)
    text = LARGE["dot_joined_tokens"][1]()
    started = time.perf_counter()
    out = secrets.scrub(text)
    assert time.perf_counter() - started < BOUND
    assert _digest(out) == EXPECTED["large"]["dot_joined_tokens"]


def test_time_grows_with_the_text_not_its_square():
    """Four times the text in well under sixteen times the time (quadratic work would take ~16x)."""
    for name in ("compact_json_numbers", "dot_joined_tokens", "pass_assignments"):
        function, build = LARGE[name]
        text = build()

        def best(value, run=FUNCTIONS[function]):
            times = []
            for _ in range(3):
                started = time.perf_counter()
                run(value)
                times.append(time.perf_counter() - started)
            return min(times)

        small, large = best(text), best(text * 4)
        assert large < BOUND, f"{function} took {large:.2f}s on four times {name}"
        assert large < 10 * small + 0.05, f"{function}: {small:.3f}s for {CAP} characters, {large:.3f}s for four times that"


# --- each rewritten search against the regular expression it replaces ----------------------------

# fmt: off
_URL_ATOMS = (
    TOKEN, "aB3xK9mQ2pL7vR4tZ8wN1c", "https://", "http://", "HTTPS://", "www.", "WWW.", "ftp://", "://", ":", "/", "/",
    ".", ".", "+", "-", "a", "b", "x1", "9", "com", "example", "user/", "?", "#", "=", "&", "?t=", " ", " ", "\n", "\t",
    '"', "'", "<", ">", "@", ":pw@", "user:secret123@", "u:p@", "u:pw3@", "_", "\u00e9", "\u212a", "\u017f", "(", ",",
)
_EMAIL_ATOMS = (
    "a", "bob", "x1", "b.c", "+", "-", "%", "_", ".", "@", "@", "example", ".com", ".io", " ", ",", "\u00e9", "a@b.co",
)
_NUMBER_ATOMS = (
    "12345678", "42664995945", "1234567890123456", "4155552671", "acct", "account", "Account", "a/c", "iban", "acc",
    "no", "num", "number", "#", "_", "_", " ", " ", "\n", "/", "/", "x", "-", ".", "https://", "www.", "run", "id", ",",
)
_ASSIGNMENT_ATOMS = (
    "S1_", "S1_", "suiteA", "PASS", "FAIL", "fail", "Fails", "FAILED", "failure", "FAILURES",
    "fa\u0131l", "FA\u0130L", "fai\u017f", "_x", "=", "=", "=1 ", "=0;", " ", " ", ";", "x", "\u00e9", "-",
    "S1_FAIL=0 ", "suiteAfailed=2;", " S1_fails_x=", "xS1_FAIL=1 ",
)
# fmt: on


def _random_texts(seed: int, atoms: tuple[str, ...], count: int = 400) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(atoms) for _ in range(rng.randint(1, rng.choice([4, 10, 25, 60])))) for _ in range(count)]


def test_email_search_matches_the_pattern():
    hits = 0
    for text in _random_texts(1, _EMAIL_ATOMS) + REPRESENTATIVE["pii"]:
        expected = [m.span() for m in PII_PATTERNS["email"].finditer(text)]
        assert [m.span() for m in _email_matches(text)] == expected, text
        hits += len(expected)
    assert hits > 50


def test_url_password_search_matches_the_patterns():
    # crew.redact's pattern (any password) and the secrets rule's (three characters and more).
    patterns = {
        1: re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s:/@]+):([^\s@/]+)@"),
        3: re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s:/@]+):([^\s@/]{3,})@"),
    }
    hits = 0
    for text in _random_texts(2, _URL_ATOMS) + REPRESENTATIVE["secrets"] + REPRESENTATIVE["crew"]:
        for min_chars, pattern in patterns.items():
            expected = [m.span(3) for m in pattern.finditer(text)]
            assert list(url_password_spans(text, min_chars)) == expected, text
            hits += len(expected)
    assert hits > 50


@pytest.mark.parametrize("lookback", [5, 20, 64, 2048])
def test_url_head_search_matches_the_pattern(monkeypatch, lookback):
    monkeypatch.setattr(secrets, "_URL_LOOKBACK", lookback)
    head_re = re.compile(r"(?i)(?:[a-z][a-z0-9+.\-]*://|(?<![A-Za-z0-9.])www\.)[^\s\"'<>]*$")
    hits = 0
    for text in _random_texts(3, _URL_ATOMS, 150) + REPRESENTATIVE["secrets"]:
        heads = _UrlHeads(text)
        for token in range(len(text) + 1):
            window = text[max(0, token - lookback) : token]
            head = head_re.search(window)
            start = heads.start(token)
            if head is None:
                assert start is None, (text, token)
                continue
            hits += 1
            assert start == max(0, token - lookback) + head.start(), (text, token)
            assert heads.in_query(start, token) == ("?" in head.group() or "#" in head.group()), (text, token)
    assert hits > 50


def test_account_label_search_matches_the_pattern():
    hits = 0
    for text in _random_texts(4, _NUMBER_ATOMS):
        context = _ScanContext(text)
        for run in re.finditer(r"\d{8,17}", text):
            start, end = run.span()
            left = start
            while left > 0 and not text[left - 1].isspace():
                left -= 1
            expected = bool(_ACCOUNT_LABEL.search(text[max(0, left - _LABEL_WINDOW) : start]))
            assert context.word(start, end).account_label_before(start) == expected, (text, start)
            hits += expected
    assert hits > 50


def test_fail_sibling_search_matches_the_pattern():
    hits = 0
    for text in _random_texts(5, _ASSIGNMENT_ATOMS):
        # Names whose other words are not counting words: only a ``*_FAIL=`` sibling makes them a count.
        for name, before, after in (("S1_PASS", "S1_", ""), ("suiteAPass", "suiteA", ""), ("S1_PASS_x", "S1_", "_x")):
            sibling = re.escape(before) + "fail(?:s|ed|ures?)?" + re.escape(after)
            expected = bool(re.search(rf"(?i)(?<![A-Za-z0-9_]){sibling}=", text))
            assert _env_count_name(name, text) == expected, (name, text)
            hits += expected
    assert hits > 50
