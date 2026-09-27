"""SEC-23: credentials are detected and redacted; ordinary text is left alone.

All keys below are synthetic and assembled at runtime so no real-looking
credential literal is committed.
"""

from __future__ import annotations

import base64
import secrets
import string
import uuid

import pytest

from remembra.security.secrets import redact_secrets


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    # Guarantee mixed classes so the synthetic key looks like a real one.
    body = "".join(secrets.choice(alphabet) for _ in range(n - 3))
    return body + "aZ7"


FAKE = {
    "remembra_key": "rem_" + secrets.token_urlsafe(32).replace("-", "a") + "Z9",
    "resend_key": "re_" + _rand(8) + "_" + _rand(24),
    "stripe_key": "sk_" + "live_" + _rand(24),
    "stripe_webhook_secret": "whsec_" + _rand(32),
    "anthropic_key": "sk-" + "ant-api03-" + _rand(40),
    "openai_key": "sk-" + "proj-" + _rand(40),
    "github_token": "gh" + "p_" + _rand(36),
    "slack_token": "xo" + "xb-" + "1234567890-" + _rand(24),
    "aws_access_key": "AK" + "IA" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
    "google_api_key": "AI" + "za" + _rand(35),
    "jwt": "eyJ" + _rand(20) + ".eyJ" + _rand(30) + "." + _rand(30),
}


@pytest.mark.parametrize("kind", sorted(FAKE))
def test_provider_keys_redacted(kind):
    value = FAKE[kind]
    text = f"Deploy note: the key is {value} (rotate quarterly)."
    result = redact_secrets(text)
    assert value not in result.text
    assert f"[REDACTED:{kind}]" in result.text, result.text
    assert result.text.startswith("Deploy note: the key is ")
    assert result.text.endswith(" (rotate quarterly).")


def test_structural_secrets_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + _rand(60) + "\n-----END RSA PRIVATE KEY-----"
    db_pw = "S3cr3t" + _rand(10)
    text = (
        f"key:\n{pem}\n"
        f"db: postgres://app:{db_pw}@db.internal:5432/main\n"
        "password: hunter22x\n"
        f"api_key = '{_rand(24)}'\n"
        f"Authorization: Bearer {_rand(40)}"
    )
    result = redact_secrets(text)
    assert "MIIEow" not in result.text
    assert db_pw not in result.text and "postgres://app:[REDACTED:url_credentials]@db.internal" in result.text
    assert "hunter22x" not in result.text
    assert "Bearer [REDACTED:bearer_token]" in result.text
    assert {"private_key", "url_credentials", "password", "secret", "bearer_token"} <= set(result.counts)


def test_unlabelled_high_entropy_token_redacted():
    token = _rand(48)
    assert token not in redact_secrets(f"use {token} for the cron job").text


@pytest.mark.parametrize(
    "text",
    [
        "Baseline commit f92e34d48005db6e70d450f9c37af320ecf5d622 fixed the auth bug.",
        "Memory id 3f2b8c1e-9a4d-4e2b-8f1a-2c3d4e5f6a7b was superseded.",
        "The password is required for login and the password reset link expires in 24h.",
        "Access token: refreshed hourly by the scheduler.",
        "We re_run the migration; call re_index_documents after deploy.",
        "Path /Users/dolphy/Projects/remembra/src/remembra/security/secrets.py was edited.",
        "sk-learn and scikit are libraries; rem_embedding_dimension_setting is a variable name.",
        "[REDACTED:resend_key] was already redacted.",
    ],
)
def test_ordinary_text_untouched(text):
    result = redact_secrets(text)
    assert result.text == text, result.text
    assert not result.redacted


def _hex(n: int) -> str:
    return secrets.token_hex(n // 2)


# A vendor-prefixed key name in "NAME: value" form (docker-compose / k8s env YAML, a JSON config) whose value
# is hex or a UUID: the high-entropy fallback skips hex and UUIDs (commit SHAs, ids), so only the label can
# catch these. The label must match after "_", "-" or a camelCase boundary, not only at a word start.
@pytest.mark.parametrize(
    "template",
    [
        "datadog_api_key: {hex}",
        "HEROKU_API_KEY: {uuid}",
        '"datadogApiKey": "{hex}"',
        "sentry-auth-token: {hex}",
        "  MAILCHIMP_API_KEY: {hex}-us21",
        "POSTGRES_PASSWORD: {hex}",
        "stripeSecretKey={hex}",
        "github_access_token = {hex}",
    ],
)
def test_prefixed_labels_redact_hex_and_uuid_values(template: str) -> None:
    value_hex, value_uuid = _hex(32), str(uuid.uuid4())
    text = "env:\n" + template.format(hex=value_hex, uuid=value_uuid) + "\nnext: ok"
    out = redact_secrets(text).text
    assert value_hex not in out and value_uuid not in out, out
    assert "next: ok" in out


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens: 4096 and num_tokens: 12",
        "api_key_id: key_1234 names the key, not its value",
        "Set sort_key: created_at and page_token: null.",
        "my_secretary: Jane Doe",
        "The deploy_token is rotated weekly.",
        "commit_sha: f92e34d48005db6e70d450f9c37af320ecf5d622",
        "session_id: 3f2b8c1e-9a4d-4e2b-8f1a-2c3d4e5f6a7b",
    ],
)
def test_prefixed_label_rule_leaves_names_and_ids_alone(text: str) -> None:
    assert redact_secrets(text).text == text


def test_redaction_is_idempotent():
    once = redact_secrets(f"key {FAKE['resend_key']}").text
    assert redact_secrets(once).text == once


# Live 2026-09-25 dry run: most "high_entropy_token" hits were file paths with
# digits (/Volumes/T7/..., /Users/.../v2). Paths must survive; slash-bearing
# base64 secrets must not.
@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/T7/Projects/ChairTime2026/build42/ios/Runner",
        "/Users/dolphy/Projects/remembra-wt/ret/src/remembra/services",
        "~/Developer/CheckTheFridge/App/Sources/Views2",
        "clawd/projects/trademind/pxexec_practice/runs2026",
    ],
)
def test_file_paths_are_not_redacted(path: str) -> None:
    assert redact_secrets(f"repo lives at {path} now").text == f"repo lives at {path} now"


def test_slash_bearing_random_secret_is_still_redacted() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzk9sQ2Lm8Ew"
    assert secret not in redact_secrets(f"aws secret {secret}").text


# ---------------------------------------------------------------------------
# CLI-01: credentials typed on a command line (relay handoffs store failed
# commands, test commands and error lines). Every value below is synthetic and
# assembled at runtime.
# ---------------------------------------------------------------------------


def _hex(n: int) -> str:
    return "".join(secrets.choice("0123456789abcdef") for _ in range(n))


def _cli_cases() -> list[tuple[str, str, str]]:
    """``(name, text, secret)``: the secret must not survive redaction."""
    pw = "Pw" + _rand(12)  # machine-style password
    weak = "hunter" + str(secrets.randbelow(90) + 10)  # human-style password: lower case + digits
    basic = base64.b64encode(f"admin:{_rand(12)}".encode()).decode()
    token = _rand(24)
    do_token = "dop" + "_v1_" + _hex(64)
    mailgun = "key" + "-" + _hex(32)
    databricks = "dapi" + _hex(32)
    return [
        ("mysql_attached_p", f"mysql -u root -p{pw} appdb", pw),
        ("mysql_attached_weak", f"mysql -h db.internal -u app -p{weak} -e 'select 1'", weak),
        ("mysqldump_attached_p", f"mysqldump -p{pw} --all-databases > dump.sql", pw),
        ("mariadb_attached_quoted", f"mariadb -uroot -p'{weak}' shop", weak),
        ("curl_basic_u", f"curl -u admin:{pw} https://api.example.com/x", pw),
        ("curl_basic_user_quoted", f"curl -s --user 'api:{pw}' https://api.example.com/v3", pw),
        ("curl_basic_header", f"curl -H 'Authorization: Basic {basic}' https://x.example", basic),
        ("git_extraheader_basic", f'git -c http.extraHeader="Authorization: Basic {basic}" fetch', basic),
        ("docker_login_p", f"docker login -u me -p {pw} registry.example.com", pw),
        ("podman_login_p", f"podman login quay.io -u me -p {weak}", weak),
        ("password_space", f"./deploy.sh --password {pw}", pw),
        ("password_space_weak", f"./deploy.sh --env prod --password {weak} --yes", weak),
        ("password_equals", f"./deploy.sh --password={weak}", weak),
        ("db_password_flag", f"psql-migrate --db-password '{pw}' up", pw),
        ("vercel_token", f"vercel deploy --prod --token {token}", token),
        ("api_key_flag", f"stripe listen --api-key {token}", token),
        ("secret_flag", f"supabase secrets set --secret={pw}", pw),
        ("do_token", f"doctl auth init -t {do_token}", do_token),
        ("mailgun_key", f"curl -s --user 'api:{mailgun}' https://api.mailgun.net/v3", mailgun),
        ("mailgun_bare", f"mailgun key is {mailgun} for the sandbox", mailgun),
        ("databricks_token", f"databricks configure --host h {databricks}", databricks),
        ("pgpassword", f"PGPASSWORD={pw} psql -h db -U app", pw),
        ("pgpassword_weak", "PGPASSWORD=postgres psql -h localhost -U postgres", "=postgres "),
        ("export_db_password", f"export DB_PASSWORD={weak}", weak),
        ("mysql_pwd_env", f"MYSQL_PWD={pw} mysql -u root", pw),
        ("github_token_env", f"GITHUB_TOKEN={token} gh pr list", token),
        ("basic_auth_env", f"BASIC_AUTH=admin:{pw} ./smoke.sh", pw),
        ("lowercase_env", f"db_password={weak} ./migrate", weak),
        ("npmrc_auth_token", f"//registry.npmjs.org/:_authToken={pw}", pw),
        ("sshpass", f"sshpass -p {pw} ssh deploy@host", pw),
        ("redis_cli", f"redis-cli -h cache -a {pw} ping", pw),
        ("mongosh", f"mongosh -u admin -p {pw} mongodb://db/app", pw),
        ("openssl_passin", f"openssl pkcs12 -in cert.p12 -passin pass:{pw} -nodes", pw),
        ("keytool_storepass", f"keytool -list -keystore app.jks -storepass {pw}", pw),
        # Still redacted next to the false-positive fixes: numbers and paths are refused only where
        # they read as counts or helpers, and --auth takes a value with a digit.
        ("db_pass_long_number", "DB_PASS=48193027 ./migrate", "48193027"),
        ("pgpassword_number", "PGPASSWORD=271828 psql -h db", "271828"),
        ("smtp_pass_env", f"SMTP_PASS={weak} ./send.sh", weak),
        ("auth_flag_token", f"./cli --auth {token}", token),
        ("askpass_neighbour", f"SSH_ASKPASS=/usr/bin/true DB_PASSWORD={pw} ./x", pw),
        # CLI-01 review residuals: an email-shaped user, vercel's short -t, an attached docker -pX,
        # and backslash-continued (multi-line) commands.
        ("curl_basic_email_user", f"curl -s -u dev@example.com:{pw} https://acme.atlassian.net/rest/api/3/myself", pw),
        ("curl_basic_email_user_quoted", f"curl --user 'dev@example.com:{pw}' https://api.example.com", pw),
        ("curl_basic_email_user_weak", f"curl -u ops.team+ci@example.co.uk:{weak} https://x.example", weak),
        ("wget_basic_email_user", f"wget --user=dev@example.com:{pw} https://x.example/f", pw),
        ("vercel_short_t", f"vercel deploy --prod -t {token}", token),
        ("vercel_short_t_equals", f"vercel ls -t={token}", token),
        ("docker_login_attached_p", f"docker login -u me -p{pw} ghcr.io", pw),
        ("podman_login_attached_p_weak", f"podman login -u me -p{weak} quay.io", weak),
        ("curl_multiline_u", f"curl -fsS \\\n  -X POST \\\n  -u admin:{pw} \\\n  https://api.example.com/x", pw),
        ("docker_login_multiline_p", f"docker login \\\n  -u me \\\n  -p {pw} \\\n  registry.example.com", pw),
        ("mysql_multiline_p", f"mysql \\\n  -h db.example.com \\\n  -u root \\\n  -p{weak} appdb", weak),
    ]


@pytest.mark.parametrize("case", _cli_cases(), ids=lambda c: c[0])
def test_cli_credentials_are_redacted(case):
    name, text, secret = case
    result = redact_secrets(text)
    assert secret not in result.text, (name, result.text)
    assert "[REDACTED:" in result.text
    assert redact_secrets(result.text).text == result.text  # idempotent


@pytest.mark.parametrize(
    "text",
    [
        "mkdir -p build/out && docker run -p 8080:80 nginx",
        "ssh -p 2222 deploy@host 'uptime'",
        "git add -p && git commit -m 'fix: password reset flow'",
        "mysql -u root -p appdb",  # a spaced -p prompts for the password
        "mysql -h db -P 3306 -u app appdb",  # -P is the port
        "docker login -u me --password-stdin registry.example.com",
        "vercel deploy --token $VERCEL_TOKEN",
        'curl -u "$API_USER:$API_PASS" https://api.example.com',
        'curl -H "Authorization: Bearer $TOKEN" https://api.example.com',
        "REMEMBRA_AUTH_ENABLED=false REMEMBRA_RATE_LIMIT_ENABLED=false pytest -q -p no:cacheprovider",
        "SECRET_REDACTION_ENABLED=true PASSWORD_MIN_LENGTH=12 TOKEN_TTL=3600 make test",
        "env -u TYPESAFE_API_KEY python -m pytest -q --no-cov",
        "export GITHUB_TOKEN=$(gh auth token)",
        "docker build --secret id=npmrc,src=.npmrc -t app .",
        "Use the --token flag or --password prompt to authenticate.",
        "pg_dump --no-password -h db mydb > out.sql",
        "llm --max-tokens 4096 --temperature 0.2",
        "docker run -u 1000:1000 -e URL=https://example.com app",
        # CLI-01 review false positives: test counts, askpass helpers, a named auth method. The
        # redaction is irreversible (a memory's content is scrubbed when it is stored).
        "CI summary: PASS=120 FAIL=0 SKIP=3",
        "TESTS_PASS=128 TESTS_FAIL=2",
        "pass=42 fail=0",
        "export GIT_ASKPASS=/usr/bin/true",
        "SSH_ASKPASS=/usr/lib/ssh/x11-ssh-askpass ssh-add",
        "SSH_ASKPASS=ssh-askpass SUDO_ASKPASS=ksshaskpass sudo -A true",
        "git -c core.askpass=/usr/bin/true fetch",
        "the flag is --auth sso-google",
        "gcloud auth login --auth saml-okta",
        "export PASS_THRESHOLD=/opt/ci/thresholds.json",
        "HOME_PWD=/home/app/src make",
        # CLI-01 review: shapes next to the residual rules that carry no credential.
        "curl -u dev@example.com https://api.example.com",  # no password: curl prompts
        "curl -u admin https://host.example.com:8443/x",
        "docker login -u me -p",
        "vercel deploy --prod --target preview -t $VERCEL_TOKEN",
    ],
)
def test_cli_commands_without_credentials_are_untouched(text):
    result = redact_secrets(text)
    assert result.text == text, result.text
    assert not result.redacted


def test_cli_rules_stay_linear_on_repeated_command_words():
    """The command-line rules scan a bounded window after each command word, so a
    50,000-character memory (the store limit) of repeated words stays fast."""
    import time

    size = 50_000
    for unit in (
        "curl -x ",
        "mysql a ",
        "docker login ",
        "sshpass x ",
        "A",
        "PASS=",
        '--token "',
        "curl -u a",
        "vercel -t ",
        "curl \\\n ",
        "docker login -p",
        "ASKPASS=",
        "--auth ",
    ):
        text = unit * (size // len(unit))
        started = time.perf_counter()
        redact_secrets(text)
        assert time.perf_counter() - started < 2.0, unit
