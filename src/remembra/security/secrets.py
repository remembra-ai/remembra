"""Credential detection and redaction for memory content (SEC-23).

Memories must never persist or return live credentials. ``redact_secrets``
replaces anything that looks like an API key, token, private key, or password
with a typed placeholder such as ``[REDACTED:resend_key]`` — the value itself
is discarded, never stored or logged.

Detection combines provider-specific patterns (Remembra, Resend, Stripe,
OpenAI, Anthropic, GitHub, Slack, AWS, Google, Paddle, SendGrid, npm, …),
structural secrets (PEM private keys, JWTs, credentialed URLs, bearer tokens,
``password=…`` assignments) and a conservative high-entropy fallback for
unlabelled random tokens. The fallback deliberately ignores hex digests, UUIDs
and plain words so commit SHAs, memory ids and prose survive.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER = "[REDACTED:{kind}]"

_PLACEHOLDER_RE = re.compile(r"\[REDACTED:[a-z0-9_]+\]")


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0  # 0 = whole match; N = only that capture group is secret
    check: Callable[[str], bool] | None = None  # extra plausibility check on the secret part


def _random(value: str) -> bool:
    return _looks_random(value, min_len=8, min_entropy=2.5)


def _password_like(value: str) -> bool:
    # "password is required" / "password: reset" are prose, not credentials.
    return not (value.isalpha() and value.islower())


def _r(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


# CLI-01: credentials typed on a command line ("mysql -pX", "curl -u user:X",
# "docker login -p X", "--password X", "PGPASSWORD=X"). The value is one shell
# word: quoted, or unquoted up to whitespace or a shell operator. A reference
# ("$TOKEN", "$(gh auth token)"), a placeholder ("<pw>") or another flag is not
# a credential, and neither is a word that only names the option in prose.
_CLI_VALUE = r"""("[^"\n]*"|'[^'\n]*'|[^\s"'`;&|<>()]+)"""
# The rest of one command, up to the flag: bounded, so a long line of repeated
# command words cannot make the scan quadratic. A backslash-newline continues
# the command on the next line (a multi-line "curl \\ -u ..." is one command).
_CLI_ARGS = r"(?:[^\n|;&]|\\\n){0,256}?"
_CLI_NOT_SECRET = frozenset(
    {
        "flag",
        "option",
        "argument",
        "arg",
        "value",
        "parameter",
        "param",
        "prompt",
        "stdin",
        "none",
        "null",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "required",
        "optional",
        "here",
        "basic",
        "bearer",
        "digest",
    }
)
_CLI_SPEC_RE = re.compile(r"^(?:id|src|source|env|type)=", re.IGNORECASE)  # docker build --secret id=x,src=y


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


_MASK_RE = re.compile(r"[*xX•._\-]+")  # "****", "xxxx", "..." already hide the value


def _cli_reference(bare: str) -> bool:
    """A variable, substitution, placeholder, another flag, an elided or masked value, or a redaction."""
    return (
        not bare
        or bare.startswith(("$", "%", "<", "-", "@", "`"))
        or "..." in bare
        or "…" in bare
        or bool(_MASK_RE.fullmatch(bare))
        or bool(_PLACEHOLDER_RE.search(bare))
    )


def _cli_password(value: str) -> bool:
    """A password given to a password option: anything but a reference or a prose word."""
    bare = _unquote(value)
    return len(bare) >= 3 and not _cli_reference(bare) and bare.lower() not in _CLI_NOT_SECRET


def _cli_token(value: str) -> bool:
    """A token/key given to a token option: also not a short number (``--max-token 4096``) or a build spec."""
    bare = _unquote(value)
    if not _cli_password(value) or len(bare) < 4 or _CLI_SPEC_RE.match(bare) or _looks_like_path(bare):
        return False
    if any(c.isspace() for c in bare) or not _password_like(bare):  # a sentence or a plain word ("--api-key puts ...")
        return False
    return not (bare.isdigit() and len(bare) < 8)


def _attached_password(value: str) -> bool:
    """``mysql -pX`` / ``sshpass -pX`` / ``pass:X``: any non-reference value is the password."""
    return not _cli_reference(_unquote(value))


def _env_password(value: str) -> bool:
    """``PGPASSWORD=X``: any word but a reference, a prose word or a path (``HOME_PWD=/src``)."""
    bare = _unquote(value)
    return len(bare) >= 3 and not _cli_reference(bare) and bare.lower() not in _CLI_NOT_SECRET and not _looks_like_path(bare)


def _env_pass(value: str) -> bool:
    """A bare ``*PASS=`` is also a test count (``PASS=120``, ``TESTS_PASS=128``): a short number is not a password."""
    bare = _unquote(value)
    return _env_password(value) and not (bare.isdigit() and len(bare) < 8)


def _cli_auth(value: str) -> bool:
    """``--auth X`` also names a method (``--auth sso-google``): a credential has a digit or is ``user:password``."""
    bare = _unquote(value)
    return _cli_token(value) and (any(c.isdigit() for c in bare) or ":" in bare)


# A name in code: "body.password,", "{inviteToken}", "self.api_key)", "get_token(".
_CODE_NAME_RE = re.compile(r"[{(\[]?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\)?|\[\]?)?[}\]),;:]*")


def _lower_env_secret(value: str) -> bool:
    # "db_password=hunter22" yes; "bypass=true", "token=refresh" (a word) and keyword arguments in
    # code ("password=body.password,", "onToken={setToken}": a name without a digit) no.
    bare = _unquote(value)
    if not (_cli_token(value) and _password_like(bare)):
        return False
    return not (_CODE_NAME_RE.fullmatch(bare) and not any(c.isdigit() for c in bare))


# Order matters: more specific prefixes first (e.g. sk-ant- before sk-).
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "private_key",
        _r(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"),
    ),
    _Rule("remembra_key", _r(r"(?<![A-Za-z0-9_])rem_[A-Za-z0-9_\-]{20,}"), check=_random),
    _Rule("resend_key", _r(r"(?<![A-Za-z0-9_])re_[A-Za-z0-9]{6,}_[A-Za-z0-9]{12,}"), check=_random),
    _Rule("resend_key", _r(r"(?<![A-Za-z0-9_])re_[A-Za-z0-9_]{24,}"), check=_random),
    _Rule("stripe_key", _r(r"(?<![A-Za-z0-9_])(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    _Rule("stripe_webhook_secret", _r(r"(?<![A-Za-z0-9_])whsec_[A-Za-z0-9+/=]{16,}")),
    _Rule("anthropic_key", _r(r"(?<![A-Za-z0-9_])sk-ant-[A-Za-z0-9_\-]{20,}")),
    _Rule("openai_key", _r(r"(?<![A-Za-z0-9_])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), check=_random),
    _Rule("github_token", _r(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    _Rule("gitlab_token", _r(r"(?<![A-Za-z0-9_])glpat-[A-Za-z0-9_\-]{20,}")),
    _Rule("slack_token", _r(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,}")),
    _Rule("slack_webhook", _r(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    _Rule("aws_access_key", _r(r"(?<![A-Z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])")),
    _Rule(
        "aws_secret_key",
        _r(r"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})"),
        group=1,
    ),
    _Rule("google_api_key", _r(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{35}")),
    _Rule("paddle_key", _r(r"(?<![A-Za-z0-9_])pdl_(?:live|sdbx)_[A-Za-z0-9_]{16,}")),
    _Rule("sendgrid_key", _r(r"(?<![A-Za-z0-9_])SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    _Rule("npm_token", _r(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{30,}")),
    _Rule("huggingface_token", _r(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{30,}")),
    _Rule("twilio_key", _r(r"(?<![A-Za-z0-9_])SK[0-9a-f]{32}(?![0-9a-f])")),
    _Rule("jwt", _r(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    _Rule(
        "url_credentials",
        _r(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s@/]{3,})@"),
        group=1,
    ),
    _Rule("bearer_token", _r(r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]{20,}=*)"), group=1),
    _Rule(
        "password",
        _r(r"(?i)\b(?:password|passwd|pwd|passphrase)\b[\"']?\s*(?:[:=]|\bis\b)\s*[\"']?([^\s\"',;]{6,})"),
        group=1,
        check=_password_like,
    ),
    _Rule(
        "secret",
        _r(
            r"(?i)\b(?:api[_\-]?key|apikey|secret(?:[_\-]?key)?|client[_\-]?secret|access[_\-]?token|auth[_\-]?token"
            r"|refresh[_\-]?token|private[_\-]?key|token)\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
        ),
        group=1,
        check=_random,
    ),
    # --- CLI-01: credentials on a command line -------------------------------
    _Rule("digitalocean_token", _r(r"(?<![A-Za-z0-9_])do[opr]_v1_[0-9a-f]{64}(?![0-9a-f])")),
    _Rule("mailgun_key", _r(r"(?<![A-Za-z0-9_\-])key-[0-9a-f]{32}(?![0-9a-f])")),
    _Rule("databricks_token", _r(r"(?<![A-Za-z0-9_])dapi[0-9a-f]{32}(?:-\d+)?(?![0-9a-f])")),
    # "Authorization: Basic <base64>" / "Authorization: token <t>" (a curl -H, a git extraHeader).
    _Rule("basic_auth", _r(r"(?i)\bauthorization\s*:\s*(?:basic|token)\s+([A-Za-z0-9+/=._~\-]{6,})"), group=1),
    # "curl -u user:pw", "curl --user 'api:pw'", "http -a user:pw": the part after the colon. The user
    # may be an email ("-u dev@example.com:pw", the usual form for Atlassian/Jira/Zendesk API tokens).
    _Rule(
        "basic_auth",
        _r(
            rf"(?<![\w-])(?i:curl|xh|httpie|wget|https?(?=[ \t]))\b{_CLI_ARGS}[ \t](?:-u|--user|-a|--auth)(?:[ \t]*|=)"
            r"[\"']?[^\s:\"']*:([^\s\"']+)"
        ),
        group=1,
        check=_attached_password,
    ),
    # "--password X", "--db-password=X", "--pass X" (not --password-stdin / --no-password).
    _Rule(
        "password",
        _r(rf"(?i)(?<![\w-])--(?!no-)(?:[a-z0-9]+-)*(?:password|passwd|passphrase|pass|pwd)(?:=|[ \t]+){_CLI_VALUE}"),
        group=1,
        check=_cli_password,
    ),
    # "--token X", "--api-key=X", "--client-secret X", "--basic-auth X" (not --token-file / --max-tokens).
    _Rule(
        "secret",
        _r(
            r"(?i)(?<![\w-])--(?!no-)(?!auth(?:=|[ \t]))(?:[a-z0-9]+-)*"
            r"(?:token|secret|api-?key|apikey|access-?key|secret-?key|master-?key|auth)"
            rf"(?:=|[ \t]+){_CLI_VALUE}"
        ),
        group=1,
        check=_cli_token,
    ),
    # A bare "--auth X" is often a method name ("--auth sso-google"), so the value must look like a credential.
    _Rule("secret", _r(rf"(?i)(?<![\w-])--auth(?:=|[ \t]+){_CLI_VALUE}"), group=1, check=_cli_auth),
    # mysql family: an attached "-pX" is the password (a bare "-p" prompts; "-P" is the port).
    _Rule(
        "password",
        _r(
            r"(?<![\w-])(?i:mysql|mysqldump|mysqladmin|mysqlimport|mysqlshow|mysqlcheck|mysqlpump|mysqlsh"
            rf"|mariadb-dump|mariadb-admin|mariadb)\b{_CLI_ARGS}[ \t]-p{_CLI_VALUE}"
        ),
        group=1,
        check=_attached_password,
    ),
    # "docker login -p X", "-p=X" or an attached "-pX" (podman, nerdctl, helm registry login, ...).
    _Rule(
        "password",
        _r(
            r"(?<![\w-])(?i:docker|podman|nerdctl|buildah|skopeo|helm|oras|crane|finch)(?:[ \t]+registry)?[ \t]+login\b"
            rf"{_CLI_ARGS}[ \t]-p(?:=|[ \t]+)?{_CLI_VALUE}"
        ),
        group=1,
        check=_cli_password,
    ),
    # vercel's short token flag: "vercel deploy -t X" / "-t=X" (the long --token is the rule above).
    _Rule(
        "secret",
        _r(rf"(?<![\w-])(?i:vercel)\b{_CLI_ARGS}[ \t]-t(?:=|[ \t]+){_CLI_VALUE}"),
        group=1,
        check=_cli_token,
    ),
    _Rule("password", _r(rf"(?<![\w-])(?i:sshpass)\b{_CLI_ARGS}[ \t]-p[ \t]*{_CLI_VALUE}"), group=1, check=_attached_password),
    _Rule(
        "password",
        _r(rf"(?<![\w-])(?i:redis-cli|valkey-cli|keydb-cli)\b{_CLI_ARGS}[ \t]-a[ \t]+{_CLI_VALUE}"),
        group=1,
        check=_cli_password,
    ),
    _Rule(
        "password",
        _r(
            r"(?<![\w-])(?i:mongosh|mongodump|mongorestore|mongoexport|mongoimport|mongostat|mongotop|mongo)\b"
            rf"{_CLI_ARGS}[ \t]-p[ \t]+{_CLI_VALUE}"
        ),
        group=1,
        check=_cli_password,
    ),
    # openssl "-passin pass:X" and keytool "-storepass X".
    _Rule("password", _r(r"(?<![\w-])-(?:pass|passin|passout)[ \t]+pass:([^\s\"'`;&|<>()]+)"), group=1, check=_attached_password),
    _Rule(
        "password",
        _r(rf"(?<![\w-])-(?:store|key|srcstore|deststore|srckey|destkey)pass[ \t]+{_CLI_VALUE}"),
        group=1,
        check=_cli_password,
    ),
    # Env-style assignments, no word boundary before the keyword: "PGPASSWORD=X", "export DB_PASSWORD=X",
    # "MYSQL_PWD=X", "DB_PASS=X", "GITHUB_TOKEN=X", "BASIC_AUTH=u:X" (the shell's own PWD/OLDPWD are paths,
    # and GIT_ASKPASS / SSH_ASKPASS / SUDO_ASKPASS name a helper program, never a password).
    _Rule(
        "password",
        _r(
            r"(?<![A-Za-z0-9_])(?!(?:OLD)?PWD=)(?![A-Z0-9_]*ASKPASS=)[A-Z0-9_]*(?:PASSWORD|PASSWD|PASSPHRASE|PWD)"
            rf"=(?!=){_CLI_VALUE}"
        ),
        group=1,
        check=_env_password,
    ),
    _Rule(
        "password",
        _r(rf"(?<![A-Za-z0-9_])(?![A-Z0-9_]*ASKPASS=)[A-Z0-9_]*PASS=(?!=){_CLI_VALUE}"),
        group=1,
        check=_env_pass,
    ),
    _Rule(
        "secret",
        _r(
            r"(?<![A-Za-z0-9_])[A-Z0-9_]*(?:TOKEN|SECRET|SECRET_KEY|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|AUTH)"
            rf"=(?!=){_CLI_VALUE}"
        ),
        group=1,
        check=_cli_token,
    ),
    # The same in lower case ("db_password=X", npmrc "_authToken=X", "?token=X"), with a stricter value check.
    _Rule(
        "secret",
        _r(
            r"(?i)(?<![A-Za-z0-9_])(?!(?:old)?pwd=)(?![a-z0-9_]*askpass=)"
            r"[a-z0-9_]*(?:password|passwd|passphrase|pass|pwd|token|secret|api_?key|auth)"
            rf"=(?!=){_CLI_VALUE}"
        ),
        group=1,
        check=_lower_env_secret,
    ),
)

# Unlabelled high-entropy tokens (fallback).
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_+/=\-])[A-Za-z0-9_+/=\-]{32,}(?![A-Za-z0-9_+/=\-])")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _entropy(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _looks_random(value: str, min_len: int = 16, min_entropy: float = 3.0) -> bool:
    """True for machine-generated tokens: long, mixed character classes, high entropy."""
    if len(value) < min_len or _PLACEHOLDER_RE.fullmatch(value):
        return False
    classes = sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
        )
    )
    return classes >= 2 and any(c.isdigit() for c in value) and _entropy(value) >= min_entropy


_PATH_WORD_RE = re.compile(r"^(?:[A-Z]?[a-z0-9_-]*|[A-Z0-9_-]+)$")


def _looks_like_path(token: str) -> bool:
    """Filesystem paths contain digits often enough (T7, v2, 2026-09) to pass
    the entropy check, but their segments read like words. Base64-style
    secrets that contain '/' have random mixed-case segments and stay flagged."""
    if token.startswith(("/", "~/", "./")):
        return True
    parts = [p for p in token.split("/") if p]
    if len(parts) < 2:
        return False
    return all(_PATH_WORD_RE.fullmatch(p) for p in parts)


def _is_high_entropy_token(token: str) -> bool:
    if _HEX_RE.fullmatch(token) or _UUID_RE.fullmatch(token):
        return False  # commit SHAs, digests, ids
    if _looks_like_path(token):
        return False  # file paths (/Volumes/T7/..., ~/Projects/...) are not credentials
    stripped = token.strip("=")
    classes = sum(
        (
            any(c.islower() for c in stripped),
            any(c.isupper() for c in stripped),
            any(c.isdigit() for c in stripped),
        )
    )
    return classes == 3 and _entropy(stripped) >= 4.2


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.counts)


def _spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            start, end = match.span(rule.group)
            if start < 0 or end <= start:
                continue
            value = text[start:end]
            if _PLACEHOLDER_RE.fullmatch(value):
                continue
            if rule.check is not None and not rule.check(value):
                continue
            spans.append((start, end, rule.kind))
    for match in _TOKEN_RE.finditer(text):
        if _is_high_entropy_token(match.group()):
            spans.append((match.start(), match.end(), "high_entropy_token"))
    return spans


def redact_secrets(text: str) -> RedactionResult:
    """Replace every detected credential in ``text`` with ``[REDACTED:<kind>]``."""
    if not text:
        return RedactionResult(text=text)
    spans = _spans(text)
    if not spans:
        return RedactionResult(text=text)

    # Resolve overlaps: earliest start wins, then the longest span.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in spans:
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_kind = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_kind)
            continue
        merged.append((start, end, kind))

    counts: dict[str, int] = {}
    out: list[str] = []
    cursor = 0
    for start, end, kind in merged:
        out.append(text[cursor:start])
        out.append(PLACEHOLDER.format(kind=kind))
        counts[kind] = counts.get(kind, 0) + 1
        cursor = end
    out.append(text[cursor:])
    return RedactionResult(text="".join(out), counts=counts)


def redaction_enabled() -> bool:
    try:
        from remembra.config import get_settings

        return bool(getattr(get_settings(), "secret_redaction_enabled", True))
    except Exception:
        return True  # fail safe: redact


def scrub(text: str) -> str:
    """Redact credentials if redaction is enabled (default). Safe on any string."""
    if not text or not isinstance(text, str) or not redaction_enabled():
        return text
    return redact_secrets(text).text


def scrub_memory_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a raw memory row/dict with credentials redacted from its text fields."""
    if not redaction_enabled():
        return record
    cleaned = dict(record)
    if isinstance(cleaned.get("content"), str):
        cleaned["content"] = scrub(cleaned["content"])
    facts = cleaned.get("extracted_facts")
    if isinstance(facts, list):
        cleaned["extracted_facts"] = [scrub(f) if isinstance(f, str) else f for f in facts]
    elif isinstance(facts, str):
        cleaned["extracted_facts"] = scrub(facts)
    return cleaned
