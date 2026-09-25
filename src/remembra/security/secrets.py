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


def _is_high_entropy_token(token: str) -> bool:
    if _HEX_RE.fullmatch(token) or _UUID_RE.fullmatch(token):
        return False  # commit SHAs, digests, ids
    if "/" in token and token.count("/") >= 2 and not any(c.isdigit() for c in token):
        return False  # paths
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
