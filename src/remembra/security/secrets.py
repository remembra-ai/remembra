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

A hex key has the shape of a digest, so hex is redacted only when a
credential word labels it ("datadog key <hex>", "auth token <hex>") or when it
decodes to a credential. A base64 or hex token that decodes to text holding a
credential (a Kubernetes Secret's data field, a hex dump) is redacted with the
kind of the credential it holds.

``redact_value`` / ``scrub_value`` apply the same detection to every string
inside a dict or list (memory and status metadata, handoff and inbox fields).
"""

from __future__ import annotations

import base64
import binascii
import bisect
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator
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
    # The spans of the secret part, for a pattern that ``finditer`` cannot run in linear time.
    finder: Callable[[str], Iterator[tuple[int, int]]] | None = None


def _random(value: str) -> bool:
    # "rem_…wxyz" (tools.keyinput.mask_key) is a key shown masked on purpose, never a usable credential
    return "\u2026" not in value and _looks_random(value, min_len=8, min_entropy=2.5)


def _password_like(value: str) -> bool:
    # "password is required" / "password: reset" are prose, not credentials.
    return not (value.isalpha() and value.islower())


def _r(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_SCHEME_RUN_REVERSED = re.compile(r"(?i)[a-z0-9+.\-]*")
_SCHEME_START = re.compile(r"(?i)\b[a-z]")
# "://user:password@" from the "://" on, for a password of at least 1 and of at least 3 characters.
_URL_USERINFO = {1: re.compile(r"://[^\s:/@]+:([^\s@/]+)@"), 3: re.compile(r"://[^\s:/@]+:([^\s@/]{3,})@")}


def url_password_spans(text: str, min_chars: int = 1) -> Iterator[tuple[int, int]]:
    """The span of each password in a ``scheme://user:password@`` of ``text``.

    The spans group 1 of ``(?i)\\b[a-z][a-z0-9+.\\-]*://[^\\s:/@]+:([^\\s@/]{min_chars,})@`` has
    under ``finditer``, in time linear in the text. That pattern is tried at every word boundary
    and each try reads the run of scheme characters to its end, so a long dotted run costs the
    square of its length. Here each ``://user:password@`` is found from its ``://``, then the
    scheme is looked for once in the run of scheme characters before it.
    """
    floor = 0  # a scheme never reaches back past the "@" that ends the last "://user:password@"
    for match in _URL_USERINFO[min_chars].finditer(text):
        colon = match.start()
        run = _SCHEME_RUN_REVERSED.match(text[floor:colon][::-1])
        if _SCHEME_START.search(text, colon - (run.end() if run else 0), colon):
            yield match.span(1)
        floor = match.end()


# Where a credential label may start: at a word start, after "_" or "-" (a vendor-prefixed name such as
# datadog_api_key or HEROKU_API_KEY), or at a camelCase boundary (datadogApiKey). A plain \b misses the
# prefixed names, and their hex or UUID values are skipped by the high-entropy fallback (ids, SHAs).
_LABEL_START = r"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z])(?=[A-Z])))"


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
        finder=lambda text: url_password_spans(text, 3),
    ),
    _Rule("bearer_token", _r(r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]{20,}=*)"), group=1),
    _Rule(
        "password",
        # "pwd" only as a whole word: HOME_PWD / OLD_PWD name directories, not passwords
        _r(
            rf"(?i)(?:{_LABEL_START}(?:password|passwd|passphrase)|\bpwd)\b"
            r"[\"']?\s*(?:[:=]|\bis\b)\s*[\"']?([^\s\"',;]{6,})"
        ),
        group=1,
        check=_password_like,
    ),
    _Rule(
        "secret",
        _r(
            rf"(?i){_LABEL_START}(?:api[_\-]?key|apikey|secret(?:[_\-]?key)?|client[_\-]?secret|access[_\-]?token"
            r"|auth[_\-]?token|refresh[_\-]?token|private[_\-]?key|token)\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
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


_MIN_TOKEN_CHARS = 32
# A URL up to the token: "https://www.youtube.com/channel/", "www.facebook.com/". It starts at a
# scheme or a "www." and runs to the token with no white space, quote or angle bracket, and it is
# looked for in the _URL_LOOKBACK characters before the token: the pattern
# (?i)(?:[a-z][a-z0-9+.\-]*://|(?<![A-Za-z0-9.])www\.)[^\s"'<>]*$ searched in that stretch. Run for
# every token that search costs the square of the text, so _UrlHeads finds the pieces in one pass.
_URL_BREAK_RE = re.compile(r"[\s\"'<>]")
_URL_SCHEME_RUN_RE = re.compile(r"(?i)[a-z0-9+.\-]+")
_URL_SCHEME_LETTER_RE = re.compile(r"(?i)[a-z]")
_URL_WWW_RE = re.compile(r"(?i)www\.")
_URL_WWW_START_RE = re.compile(r"(?i)(?<![A-Za-z0-9.])www\.")
_URL_QUERY_MARK_RE = re.compile(r"[?#]")
_URL_PART_RE = re.compile(r"[^/=]+")
# In a query or fragment a value is one part, "/" and all: base64 has "/" in its alphabet.
_URL_QUERY_PART_RE = re.compile(r"[^=]+")
_URL_LOOKBACK = 2048
# A reset link, a magic link and an OAuth code are 22 to 31 characters; in a URL a part that long
# and random is cut out too. Outside a URL the 32-character threshold stands.
_MIN_URL_PART_CHARS = 20
_WORD_RUN_RE = re.compile(r"[a-z]{4,}|\d{4,}")
# A name ("getUserProfileById2Handler") is mostly runs of lower-case letters or of digits; in a random
# token such runs are under a third of the characters 19 times in 20.
_MAX_WORD_SHARE = 0.4
# "/channel/<id>", "/user/<id>": a public id, random by design, that names a page.
_PUBLIC_ID_SEGMENTS = frozenset(
    {"channel", "channels", "user", "users", "video", "videos", "playlist", "embed", "shorts", "profile", "profiles"}
)


class _UrlHeads:
    """Where the URLs of one text start, found in one pass, to ask of each token whether it continues one."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._breaks = [m.start() for m in _URL_BREAK_RE.finditer(text)]
        self._marks = [m.start() for m in _URL_QUERY_MARK_RE.finditer(text)]
        self._www = [m.start() for m in _URL_WWW_START_RE.finditer(text)]
        # Each run of scheme characters that a "://" follows: where the run starts, where its "://"
        # is, and the first letter in it (a scheme starts with a letter), -1 when it has none.
        self._runs: list[int] = []
        self._colons: list[int] = []
        self._letters: list[int] = []
        for run in _URL_SCHEME_RUN_RE.finditer(text):
            if text.startswith("://", run.end()):
                letter = _URL_SCHEME_LETTER_RE.search(text, run.start(), run.end())
                self._runs.append(run.start())
                self._colons.append(run.end())
                self._letters.append(letter.start() if letter else -1)
        # For each run, the first run at or after it that has a letter.
        self._lettered = [len(self._runs)] * (len(self._runs) + 1)
        for i in range(len(self._runs) - 1, -1, -1):
            self._lettered[i] = i if self._letters[i] >= 0 else self._lettered[i + 1]

    def start(self, token: int) -> int | None:
        """Where the URL that the token at ``token`` continues starts; ``None`` when it continues none."""
        text = self._text
        window = max(0, token - _URL_LOOKBACK)
        if token > window and text[token - 1] == "\n":
            token -= 1  # the pattern's "$" also matches before a newline that ends the stretch
        b = bisect.bisect_left(self._breaks, token)
        low = max(window, self._breaks[b - 1] + 1) if b else window
        best: int | None = None
        # A "www." with no letter, digit or dot before it; at the start of the window, any "www.".
        w = bisect.bisect_left(self._www, low)
        if low == window and _URL_WWW_RE.match(text, low):
            best = low
        elif w < len(self._www):
            best = self._www[w]
        if best is not None and best + 4 > token:
            best = None
        # The first "://" after ``low`` whose run of scheme characters has a letter at or after ``low``.
        i = bisect.bisect_right(self._colons, low)
        if i < len(self._colons) and self._runs[i] < low:
            letter = _URL_SCHEME_LETTER_RE.search(text, low, self._colons[i])
            scheme = letter.start() if letter else -1
            if scheme < 0:
                i = self._lettered[i + 1]
        else:
            i = self._lettered[i]
            scheme = -1
        if scheme < 0 and i < len(self._colons):
            scheme = self._letters[i]
        if scheme >= 0 and self._colons[i] + 3 <= token and (best is None or scheme < best):
            best = scheme
        return best

    def in_query(self, start: int, token: int) -> bool:
        """True when the URL from ``start`` up to the token at ``token`` holds a ``?`` or a ``#``."""
        m = bisect.bisect_left(self._marks, token)
        return m > 0 and self._marks[m - 1] >= start


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


# "TMPDIR=/Volumes/T7/x", "--output=/tmp/x", "--test-name-pattern=Checkout2Flow_x": the token
# alphabet holds "=", "-" and "/", so a shell word that assigns a value is matched whole.
_ASSIGN_PREFIX_RE = re.compile(r"^-{0,2}[A-Za-z_][A-Za-z0-9_-]*=(?=[^=])")
_NAME_SPLIT_RE = re.compile(r"[/_=\-]+")
# Fewer class changes per character than this and the token is words, not random characters.
_MAX_NAME_BREAKS = 0.15
_LOWER_WORD_RE = re.compile(r"[a-z]{3,}")
_LONG_CASE_RUN_RE = re.compile(r"[a-z]{21,}|[A-Z]{21,}")


def _reads_like_names(token: str) -> bool:
    """True for a path, branch or test name written by a person: ``T7/Codex-Worktrees/bth-ai-setup-20261107``,
    ``feature/Add-OAuth2-Login-For-Enterprise-SSO``, ``Checkout2Flow_handlesRetries_3x``.

    Such a token is several words joined by ``/``, ``-`` or ``_``, and inside a word the character
    class seldom changes (``Codex``, ``OAuth2``, ``20261107``). In a random token it changes at
    about four characters in ten, so a generated credential stays flagged.
    """
    parts = [part for part in _NAME_SPLIT_RE.split(token) if part]
    if len(parts) < 2:
        return False
    # Two of the parts hold a word, and no part is one long run of a single case
    # ("sess_ABCDEFGHJKLMNPQRSTUVWXYZABCDEFGHJK9" is a generated token with a prefix).
    if sum(1 for part in parts if _LOWER_WORD_RE.search(part)) < 2 or _LONG_CASE_RUN_RE.search(token):
        return False
    breaks = 0
    for part in parts:
        for prev, cur in zip(part, part[1:], strict=False):
            if prev.isdigit() != cur.isdigit() or (prev.islower() and cur.isupper()):
                breaks += 1
    return breaks <= _MAX_NAME_BREAKS * sum(len(part) for part in parts)


def _is_short_url_token(part: str) -> bool:
    """True for a 20 to 31 character URL part that is a generated value, not a name or an id in hex."""
    if _HEX_RE.fullmatch(part) or _UUID_RE.fullmatch(part):
        return False
    if _looks_like_path(part) or _reads_like_names(part):
        return False
    classes = (any(c.islower() for c in part), any(c.isupper() for c in part), any(c.isdigit() for c in part))
    wordy = sum(len(run) for run in _WORD_RUN_RE.findall(part))
    return all(classes) and wordy < _MAX_WORD_SHARE * len(part)


def _url_part_spans(in_query: bool, token: str) -> list[tuple[int, int]]:
    """The parts of ``token`` (a stretch of a URL) that are credentials, as offsets in it.

    ``in_query`` says the URL before the token already holds a ``?`` or a ``#``.
    """

    def credential(value: str, public_id: bool = False) -> bool:
        if len(value) >= _MIN_TOKEN_CHARS:
            return _is_high_entropy_token(value)
        return len(value) >= _MIN_URL_PART_CHARS and not public_id and _is_short_url_token(value)

    found: list[tuple[int, int]] = []
    previous = ""
    for part in (_URL_QUERY_PART_RE if in_query else _URL_PART_RE).finditer(token):
        value = part.group()
        if in_query and _looks_like_path(value):
            # "?next=/docs/setup" is a path; so, by its shape, is a base64 value that starts with "/".
            segments = [(m.start(), m.end()) for m in _URL_PART_RE.finditer(value) if credential(m.group())]
            found.extend((part.start() + start, part.start() + end) for start, end in segments)
        elif credential(value, public_id=not in_query and previous.lower() in _PUBLIC_ID_SEGMENTS):
            found.append((part.start(), part.end()))
        previous = value
    return found


def _is_high_entropy_token(token: str) -> bool:
    if _HEX_RE.fullmatch(token) or _UUID_RE.fullmatch(token):
        return False  # commit SHAs, digests, ids
    if _looks_like_path(token):
        return False  # file paths (/Volumes/T7/..., ~/Projects/...) are not credentials
    assigned = _ASSIGN_PREFIX_RE.match(token)
    if assigned:
        # The value decides: a path or a name is not a credential, a random value still is
        # (a named secret variable is caught by the labelled rules before this fallback).
        value = token[assigned.end() :]
        if _looks_like_path(value) or _reads_like_names(value):
            return False
    elif _reads_like_names(token):
        return False
    stripped = token.strip("=")
    classes = sum(
        (
            any(c.islower() for c in stripped),
            any(c.isupper() for c in stripped),
            any(c.isdigit() for c in stripped),
        )
    )
    return classes == 3 and _entropy(stripped) >= 4.2


# Hex after a credential word: "datadog key <hex>", "Twilio auth token <hex>", "the api key is <hex>",
# "ENCRYPTION_KEY=<hex>", '"key": "<hex>"', "private key 0x<hex>". The word before the label is kept
# in ``qual`` so a key that names a lookup or a public value keeps its hex (see _NOT_SECRET_KEY).
_LABELLED_HEX_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:(?P<qual>[A-Za-z0-9]{1,40})[ _\-])?"
    r"(?P<label>keys?|tokens?|secrets?|credentials?|passwords?|passphrase)"
    r"[\"']?(?:[ \t]*[:=][ \t]*|[ \t]+(?:(?:is|was)[ \t]+)?)[\"']?"
    r"(?:0x)?(?P<value>[0-9a-fA-F]{32,})(?![0-9A-Za-z_])"
)
# "cache key <sha256>", "public key <hex>", "GPG key <fingerprint>": the hex is an id, a digest or a public value.
_NOT_SECRET_KEY = frozenset(
    {
        "cache",
        "primary",
        "foreign",
        "partition",
        "sort",
        "hash",
        "idempotency",
        "dedup",
        "dedupe",
        "lookup",
        "index",
        "row",
        "object",
        "routing",
        "shard",
        "public",
        "pub",
        "verify",
        "verification",
        "gpg",
        "pgp",
        "ssh",
        "host",
    }
)


def _labelled_hex_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _LABELLED_HEX_RE.finditer(text):
        qual = (match.group("qual") or "").lower()
        if match.group("label").lower().startswith("key") and qual in _NOT_SECRET_KEY:
            continue
        spans.append((*match.span("value"), "secret"))
    return spans


# Encoded credentials: a token of base64 (standard or URL-safe) or hex characters is decoded, and
# redacted when the text it decodes to holds a credential. Digests, commit ids, nonces and other
# random bytes never decode to text, so this finds nothing in them. Bounded: a longer token is not
# decoded (the unlabelled fallback still applies to it).
_ENCODED_RE = re.compile(r"(?<![A-Za-z0-9_+/=\-])[A-Za-z0-9_+/\-]{24,}={0,2}(?![A-Za-z0-9_+/=\-])")
_MAX_ENCODED_CHARS = 4096


def _decoded_text(token: str) -> str | None:
    """The text ``token`` encodes (hex, ``0x`` hex, or base64), or None when it is not encoded text."""
    body = token[2:] if token[:2] in ("0x", "0X") and _HEX_RE.fullmatch(token[2:]) else token
    if _HEX_RE.fullmatch(body):
        if len(body) % 2:
            return None
        raw = bytes.fromhex(body)
    else:
        stripped = body.rstrip("=")
        if len(stripped) % 4 == 1:
            return None
        padded = stripped.replace("-", "+").replace("_", "/") + "=" * (-len(stripped) % 4)
        try:
            raw = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not decoded.strip() or not all(c.isprintable() or c in "\t\n\r" for c in decoded):
        return None
    return decoded


def _encoded_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _ENCODED_RE.finditer(text):
        token = match.group()
        if len(token) > _MAX_ENCODED_CHARS or _looks_like_path(token):
            continue
        decoded = _decoded_text(token)
        if decoded is None:
            continue
        inner = sorted(_spans(decoded, decode=False))
        if not inner:
            continue
        # The first named credential in the decoded text names the span; else the fallback's kind.
        kind = next((k for _, _, k in inner if k != "high_entropy_token"), inner[0][2])
        spans.append((match.start(), match.end(), kind))
    return spans


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.counts)


def _spans(text: str, decode: bool = True, fallback: bool = True) -> list[tuple[int, int, str]]:
    """Every credential span in ``text``. ``decode=False`` skips encoded tokens (one level of
    decoding only); ``fallback=False`` skips the unlabelled high-entropy fallback."""
    spans: list[tuple[int, int, str]] = []
    for rule in _RULES:
        found = rule.finder(text) if rule.finder else (m.span(rule.group) for m in rule.pattern.finditer(text))
        for start, end in found:
            if start < 0 or end <= start:
                continue
            value = text[start:end]
            if _PLACEHOLDER_RE.fullmatch(value):
                continue
            if rule.check is not None and not rule.check(value):
                continue
            spans.append((start, end, rule.kind))
    spans.extend(_labelled_hex_spans(text))
    # Before the fallback: a decoded span covering the same token keeps the decoded credential's kind.
    if decode:
        spans.extend(_encoded_spans(text))
    if fallback:
        urls: _UrlHeads | None = None
        for match in _TOKEN_RE.finditer(text):
            urls = urls or _UrlHeads(text)
            head = urls.start(match.start())
            if head is not None:
                # "com/channel/<id>" is not one token: each path segment and query value is its own.
                for start, end in _url_part_spans(urls.in_query(head, match.start()), match.group()):
                    spans.append((match.start() + start, match.start() + end, "high_entropy_token"))
            elif _is_high_entropy_token(match.group()):
                spans.append((match.start(), match.end(), "high_entropy_token"))
    return spans


# No rule matches fewer characters than this ("PASS=abc", "pwd=abcd"): shorter text is returned as is.
_MIN_SECRET_TEXT = 8


def redact_secrets(text: str, *, fallback: bool = True) -> RedactionResult:
    """Replace every detected credential in ``text`` with ``[REDACTED:<kind>]``.

    ``fallback=False`` applies the named formats (provider keys, JWTs, PEM keys,
    labelled assignments, encoded credentials) but not the unlabelled
    high-entropy fallback: for identifiers such as a random session id.
    """
    if not text or len(text) < _MIN_SECRET_TEXT:
        return RedactionResult(text=text)
    spans = _spans(text, fallback=fallback)
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


# Metadata values that identify things (the relay row key, session and agent ids,
# commit shas, checksums, project fingerprints): a long random session id reads
# like a token, and rewriting it would break the lookups that use it. Their
# strings (and the strings in a list under them) are checked against the named
# credential formats only, never the unlabelled high-entropy fallback: a
# provider key is never an id. Dict keys are names, checked the same way.
IDENTIFIER_KEYS = frozenset(
    {
        "relay_key",
        "session_id",
        "agent_id",
        "user_id",
        "project_id",
        "memory_id",
        "source_id",
        "id",
        "sha",
        "head_commit",
        "checksum",
        "content_checksum",
        "fingerprints",
    }
)


def _redact_counted(text: str, counts: dict[str, int], fallback: bool) -> str:
    result = redact_secrets(text, fallback=fallback)
    for kind, n in result.counts.items():
        counts[kind] = counts.get(kind, 0) + n
    return result.text


def redact_value(value: Any, counts: dict[str, int] | None = None, *, identifier: bool = False) -> Any:
    """``value`` with credentials redacted from every string inside it.

    Walks dicts (keys and values), lists and tuples (returned as lists) at any
    depth; numbers, booleans and None are kept as they are. The input is not
    modified. ``counts`` (when given) is incremented per redacted kind. Values
    under :data:`IDENTIFIER_KEYS` skip the high-entropy fallback.
    """
    if counts is None:
        counts = {}
    if isinstance(value, str):
        return _redact_counted(value, counts, fallback=not identifier)
    if isinstance(value, dict):
        return {
            (_redact_counted(k, counts, fallback=False) if isinstance(k, str) else k): redact_value(
                v, counts, identifier=k in IDENTIFIER_KEYS
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v, counts, identifier=identifier) for v in value]
    return value


def scrub_value(value: Any) -> Any:
    """:func:`redact_value` if redaction is enabled (default); ``value`` unchanged otherwise."""
    if not redaction_enabled():
        return value
    return redact_value(value)


def _scrub_metadata_field(metadata: Any) -> Any:
    """A metadata dict, or its JSON text as a raw row stores it, with credentials redacted."""
    if isinstance(metadata, str) and metadata:
        try:
            parsed = json.loads(metadata)
        except (TypeError, ValueError):
            return scrub(metadata)
        if not isinstance(parsed, dict | list):
            return scrub(metadata)
        cleaned = scrub_value(parsed)
        return metadata if cleaned == parsed else json.dumps(cleaned)
    return scrub_value(metadata)


def scrub_memory_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a raw memory row/dict with credentials redacted from its content,
    extracted facts and metadata."""
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
    if cleaned.get("metadata") is not None:
        cleaned["metadata"] = _scrub_metadata_field(cleaned["metadata"])
    return cleaned
