"""The single outbound redaction choke point for Crew mode (spec §11 "Redaction", docs/crew/redaction.md).

Every crew payload that leaves a host or goes to memory passes through
:func:`outbound`: events, presence frames, heartbeat ``last_action``,
snapshots, checkpoint facts, reports, stall payloads and memory promotions.
It returns a new JSON value and never mutates its input.

Rules, applied recursively to every value:

* **Command metadata only.** A ``command`` / ``cmd`` / ``command_line`` value
  becomes its ``verb`` (the program name); the raw string is dropped. The
  exceptions are test-runner fingerprints needed for acceptance-criteria
  matching: ``command``/``cmd`` inside a ``tests`` list and any ``fingerprint``
  value are kept, redacted.
* **No command output.** ``stdout``, ``stderr``, ``output`` and raw
  environment dumps are dropped whole, so the output of a command that read an
  ``.env`` or key file never leaves.
* **No raw hostnames.** ``hostname``-style keys are dropped (``member_key``
  already carries a salted host label).
* **Strings** go through: absolute paths under ``repo_root`` → repo-relative;
  other paths under ``home`` → ``~/…`` (without ``home``, as on the server, any
  ``/Users/<name>`` or ``/home/<name>`` prefix → ``~``); env assignments whose name has a
  word ending in ``KEY|TOKEN|SECRET|PASS|PWD|DSN|URL|AUTH`` (``DB_PASS``,
  ``dbUrl``, ``PGPASSWORD``; not ``passed``, ``author``, ``max_tokens=4096``),
  and ``export X=<≥32 random chars>``, lose their value; ``Authorization``-style headers, ``-u user:pass``
  and URL credentials lose their secret part; then
  :func:`remembra.security.secrets.redact_secrets` (provider keys, JWTs, PEM
  keys, high-entropy tokens) and, when given, the per-request PII scrubber.

Ownership note: §14 assigns ``crew/redact.py`` to no work package; WP-6 wrote it
because checkpoint facts and reports are the first payloads that need it.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from remembra.security.secrets import redact_secrets

PAYLOAD_TYPES: Final = ("event", "presence", "heartbeat", "snapshot", "checkpoint", "report", "stall", "promotion")

# Keys whose whole value is dropped.
_DROP_KEYS: Final = frozenset(
    {
        "stdout",
        "stderr",
        "output",
        "combined_output",
        "env",
        "environ",
        "environment",
        "hostname",
        "host_name",
        "raw_hostname",
        "machine_name",
    }
)
# Keys holding a raw command line: reduced to a verb outside a ``tests`` list.
_COMMAND_KEYS: Final = frozenset({"command", "cmd", "command_line", "raw_command"})
# Keys holding a test-runner fingerprint: kept, redacted.
_FINGERPRINT_KEYS: Final = frozenset({"fingerprint"})
# Lists whose items are test results (their command is a fingerprint).
_TEST_LIST_KEYS: Final = frozenset({"tests"})

# A name is a secret's when one of its words ENDS in a credential word: ``DB_PASS``, ``PGPASSWORD``,
# ``REDISURL``, ``encryptionKey``, ``authToken``. A substring test also hit ``passed`` (the server's own
# ``passed=41 failed=0`` test line), ``author``, ``bypass``, ``curl`` and ``max_tokens``.
_ENV_SECRET_WORD: Final = re.compile(
    r"(?:keys?|tokens?|secrets?|pass|passwords?|passwd|passphrase|passcode|pwd|dsn|urls?|auth|authorization|authentication)$"
)
# Words that end like a credential word and are not one.
_ENV_PLAIN_WORDS: Final = frozenset(
    {
        "bypass",
        "compass",
        "surpass",
        "trespass",
        "overpass",
        "underpass",
        "encompass",
        "monkey",
        "monkeys",
        "hotkey",
        "hotkeys",
        "turkey",
        "donkey",
        "hockey",
        "jockey",
        "whiskey",
        "curl",
        "curls",
        "furl",
        "unfurl",
        "hurl",
        "purl",
    }
)
# A count as often as a credential: ``PASS=120``, ``TESTS_PASS=128``, ``max_tokens=4096``, ``keys=3``.
_ENV_COUNT_WORD: Final = re.compile(r"(?:pass|keys|tokens|secrets|passwords|urls)$")
_ENV_NOT_A_VALUE: Final = frozenset({"none", "null", "nil", "true", "false", "yes", "no", "on", "off"})
_CAMEL_WORD: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_ENV_ASSIGN: Final = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|'\"`]+)")
_EXPORT_ASSIGN: Final = re.compile(r"\bexport\s+([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|'\"`]+)")
_HEADER: Final = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-auth-token|cookie|set-cookie)(\s*:\s*)([^'\"\n]+)"
)
_BASIC_USER: Final = re.compile(r"(?<![A-Za-z0-9_-])(-u|--user)(\s+|=)([^\s:'\"]+):([^\s'\"]+)")
_URL_CREDS: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s:/@]+):([^\s@/]+)@")
_REDACTED: Final = "[REDACTED:{kind}]"

Scrubber = Callable[[str], str]


def _looks_random(value: str) -> bool:
    stripped = value.strip("'\"")
    if len(stripped) < 32:
        return False
    classes = sum((any(c.islower() for c in stripped), any(c.isupper() for c in stripped), any(c.isdigit() for c in stripped)))
    return classes >= 2


def _name_words(name: str) -> list[str]:
    """The lower-case words of an assignment name: ``_``-separated parts, and each part's camelCase words."""
    words: list[str] = []
    for part in name.split("_"):
        if not part:
            continue
        words.append(part.lower())
        words.extend(w.lower() for w in _CAMEL_WORD.findall(part))
    return words


def _env_secret_name(name: str, value: str) -> bool:
    """True when ``name=value`` assigns a credential: a word of the name ends in a credential word.

    A short number or a literal such as ``None``/``true`` after a word that also names a count
    (``PASS=120``, ``max_tokens=4096``) is a count, not a credential.
    """
    hits = [w for w in _name_words(name) if w not in _ENV_PLAIN_WORDS and _ENV_SECRET_WORD.search(w)]
    if not hits:
        return False
    bare = value.strip("'\"")
    if (bare.isdigit() and len(bare) < 8) or bare.lower() in _ENV_NOT_A_VALUE:
        return not all(_ENV_COUNT_WORD.search(w) for w in hits)
    return True


def _env_sub(match: re.Match[str]) -> str:
    name, value = match.group(1), match.group(2)
    if value.strip("'\"") and not value.startswith("[REDACTED:") and _env_secret_name(name, value):
        return f"{name}={_REDACTED.format(kind='env')}"
    return match.group(0)


def _export_sub(match: re.Match[str]) -> str:
    name, value = match.group(1), match.group(2)
    if _looks_random(value):
        return f"export {name}={_REDACTED.format(kind='env')}"
    return match.group(0)


# A home directory the caller could not name (the server never knows the host's $HOME).
_GENERIC_HOME: Final = re.compile(r"(?<![A-Za-z0-9._~/-])(?:/Users|/home)/[^/\s'\"`:;]+")


def _rewrite_paths(text: str, repo_root: str | None, home: str | None) -> str:
    if repo_root:
        root = repo_root.rstrip("/")
        if root:
            text = text.replace(root + "/", "")
            text = re.sub(re.escape(root) + r"(?![A-Za-z0-9._-])", ".", text)
    if home:
        h = home.rstrip("/")
        if h:
            text = text.replace(h + "/", "~/")
            text = re.sub(re.escape(h) + r"(?![A-Za-z0-9._-])", "~", text)
    else:
        # server side: no host context, so any /Users/<name> or /home/<name> prefix becomes ~
        text = _GENERIC_HOME.sub("~", text)
    return text


def redact_text(text: str, *, repo_root: str | None = None, home: str | None = None, pii: Scrubber | None = None) -> str:
    """Redact one string (paths, env assignments, headers, credentials, secrets, PII)."""
    if not text:
        return text
    out = _EXPORT_ASSIGN.sub(_export_sub, text)
    out = _ENV_ASSIGN.sub(_env_sub, out)
    out = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED.format(kind='header')}", out)
    out = _BASIC_USER.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}:{_REDACTED.format(kind='password')}", out)
    out = _URL_CREDS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{_REDACTED.format(kind='password')}@", out)
    out = redact_secrets(out).text
    out = _rewrite_paths(out, repo_root, home)
    if pii is not None:
        out = pii(out)
    return out


_ASSIGN_WORD: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS: Final = frozenset({"sudo", "env", "time", "nice", "nohup", "exec", "command", "export", "builtin"})


def command_verb(command: str) -> str:
    """The program name of a command line (``npx vitest run`` → ``npx``); never the arguments."""
    try:
        from remembra.crew.gatecore import parse_bash_full

        for _cwd, argv in parse_bash_full(command).argvs:
            if argv and str(argv[0]) not in _WRAPPERS:
                return posixpath.basename(str(argv[0]))[:32] or "command"
    except Exception:  # the parser never raises by contract; stay safe regardless
        pass
    for token in command.strip().split():
        if _ASSIGN_WORD.match(token) or token in _WRAPPERS:
            continue
        verb = posixpath.basename(token.strip("'\"()`$"))
        return re.sub(r"[^A-Za-z0-9._+-]", "", verb)[:32] or "command"
    return "command"


def _walk(
    value: Any,
    *,
    in_tests: bool,
    repo_root: str | None,
    home: str | None,
    pii: Scrubber | None,
) -> Any:
    if isinstance(value, str):
        return redact_text(value, repo_root=repo_root, home=home, pii=pii)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            low = key.lower()
            if low in _DROP_KEYS:
                continue
            if low in _COMMAND_KEYS and not in_tests:
                if isinstance(item, str):
                    if "verb" not in value:
                        out["verb"] = command_verb(item)
                    continue
                if item is None:
                    continue
            if low in _FINGERPRINT_KEYS or (low in _COMMAND_KEYS and in_tests):
                out[key] = _walk(item, in_tests=in_tests, repo_root=repo_root, home=home, pii=pii)
                continue
            out[key] = _walk(item, in_tests=low in _TEST_LIST_KEYS, repo_root=repo_root, home=home, pii=pii)
        return out
    if isinstance(value, (list, tuple)):
        return [_walk(v, in_tests=in_tests, repo_root=repo_root, home=home, pii=pii) for v in value]
    return value


def outbound(
    payload_type: str,
    payload: Any,
    *,
    repo_root: str | None = None,
    home: str | None = None,
    pii: Scrubber | None = None,
) -> Any:
    """Redact ``payload`` of ``payload_type`` (one of :data:`PAYLOAD_TYPES`) for leaving the host or going to memory."""
    if payload_type not in PAYLOAD_TYPES:
        raise ValueError(f"unknown outbound payload type {payload_type!r}; expected one of {', '.join(PAYLOAD_TYPES)}")
    return _walk(payload, in_tests=False, repo_root=repo_root, home=home, pii=pii)
