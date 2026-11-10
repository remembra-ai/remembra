"""
PII Detection and Redaction.

Scans content for Personally Identifiable Information (PII) patterns
before storage, supporting OWASP ASI06 (Memory Poisoning) compliance.

Modes:
- detect: Log warnings but allow storage
- redact: Replace PII with [REDACTED_TYPE] placeholders
- block: Reject content containing PII
"""

import bisect
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


# ============================================================================
# PII Patterns
# ============================================================================

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    # US Social Security Number
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Credit Card Numbers (major formats)
    "credit_card": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    # Email Addresses
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    # US Phone Numbers
    "phone_us": re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    # International Phone (generic)
    "phone_intl": re.compile(r"\b\+\d{1,3}[-.\s]?\d{6,14}\b"),
    # API Keys / Secrets (common patterns)
    "api_key": re.compile(r"\b(?:sk|pk|api|key|token|secret)[-_][A-Za-z0-9]{16,}\b", re.IGNORECASE),
    # Password values after common labels
    # Catches: "password: xxx", "password=xxx", "password is xxx", "my password: xxx"
    # Matches the full pattern; password value is in group 1
    "password": re.compile(
        r"(?:password|passwd|pwd)\s*(?:[:\=\*]+|is|are|was)\s*(\S{6,})",
        re.IGNORECASE,
    ),
    # AWS Access Keys
    "aws_key": re.compile(r"\b(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}\b"),
    # NOTE: IPv4 address detection removed — too many false positives for legitimate
    # user-owned infra config (deploy IPs, server addresses) which Remembra must
    # preserve verbatim for recall. Passwords/SSN/keys/etc. are still redacted.
    # US Passport
    "passport_us": re.compile(r"\b[A-Z]\d{8}\b"),
    # Driver's License (generic patterns)
    "drivers_license": re.compile(r"\b[A-Z]{1,2}\d{6,8}\b"),
    # Bank Account (generic)
    # Very generic, use with caution. Compact YYYYMMDD dates (20260925, as in
    # backup/folder names) are excluded: they were being redacted as accounts.
    "bank_account": re.compile(r"\b(?!(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b)\d{8,17}\b"),
    # Date of Birth patterns
    "dob": re.compile(r"\b(?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01])[-/](?:19|20)\d{2}\b"),
}

_EMAIL_PATTERN = PII_PATTERNS["email"]
_EMAIL_LOCAL_REVERSED = re.compile(r"[A-Za-z0-9._%+-]*")
_EMAIL_LOCAL_START = re.compile(r"\b[A-Za-z0-9._%+-]")


def _email_matches(content: str) -> Iterator[re.Match[str]]:
    """The matches ``_EMAIL_PATTERN.finditer(content)`` gives, in time linear in the content.

    ``finditer`` tries the pattern at every word boundary, and each try reads the run of
    local-part characters to its end: a long dotted run with no ``@`` costs the square of its
    length. A local part can only end at an ``@``, so each ``@`` is taken once: ``finditer`` would
    start at the earliest word boundary in the run of local-part characters before it, and what
    follows the ``@`` does not depend on that start.
    """
    cursor = 0  # the end of the last match: the next one starts at or after it
    floor = 0  # no local part reaches back past the last "@" or into the last match
    at = content.find("@")
    while at != -1:
        run = _EMAIL_LOCAL_REVERSED.match(content[floor:at][::-1])
        begin = _EMAIL_LOCAL_START.search(content, at - (run.end() if run else 0), at)
        match = _EMAIL_PATTERN.match(content, begin.start()) if begin else None
        if match:
            yield match
            cursor = match.end()
        floor = max(cursor, at + 1)
        at = content.find("@", floor)


# A hyphen/dot-joined identifier around a digit run: "629134551556-abc.apps.googleusercontent.com".
_IDENTIFIER_RUN = re.compile(r"[A-Za-z0-9._-]+")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]*")
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
# A random-looking part: letters AND digits, at least 6 characters ("8v3kq0abcd", "a1b2c3").
_MIXED_PART = re.compile(r"(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}")
# "-<label>.<label>.<tld>...": the digits are the first label of a host name. Used as a yes/no test of
# what follows the digits, so two dotted parts are matched and no more (the rest is not read).
_HOST_TAIL = re.compile(r"-[A-Za-z0-9]+(?:\.[A-Za-z0-9-]+){2}")
# An account label shortly before the number means it is one, whatever follows it.
_ACCOUNT_LABEL = re.compile(r"(?i)(?:\b|_)(?:acct|account|a/c|iban|acc(?:t)?\s*(?:no|num|number|#))(?:\b|_)")
_LABEL_WINDOW = 24
# A word that names a machine-issued number, then the digits: "CI run 40233615394",
# "migrations 590000000-599999", "epoch 1762735701", "App Store Connect app ID 1234567890". Between
# the two only "id"/"number" or a gh verb ("gh run view 42664995945"), never a free word: "run card
# <digits>", "PR contact <digits>" and "release funds <digits>" are personal. A bare "id" or "#" is
# not a label either: "Tax ID 123456789" and "check # 12345678" are personal.
_DEVELOPER_LABEL = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:(?:runs?|jobs?|workflows?|builds?|pipelines?|deploy(?:s|ment|ments)?|migrations?|commits?|artifacts?|"
    r"epoch|timestamp|unix\s+time)"
    r"(?:[\s_-]*(?:ids?|numbers?|num|no\.?)|\s+(?:view|watch|rerun|cancel|download|delete|logs?))?"
    r"|(?:app|project|team|org|repo|installation|suite|message|thread|channel|chat|trace|span)[\s_-]*ids?)"
    r"[\s:=#(]*$"
)
_URL_START = re.compile(r"(?i)^[\"'(<\[]*(?:[a-z][a-z0-9+.-]*://|www\.)")
# A word in the URL or path that says the number is a person's: "tel://…", "wa.me/…", "phone/fax/…".
_PERSONAL_HINT = re.compile(
    r"(?i)(?<![a-z])(?:tel|sms|fax|phone|mobile|cell|whatsapp|wa\.me|card|cc|pay(?:ments?)?|charge|billing|"
    r"bank|wire|ach|routing|iban|ssn|acct|accounts?)(?![a-z])"
)
_NAMED_SEGMENT = re.compile(r"[A-Za-z]{2,}")
_CARD_DIGITS = 16
_WORD_RUN = re.compile(r"\S+")
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"
# An account label that starts inside a word is at most 12 characters ("_acctnumber_"): a search
# that starts this far before the end of the last one misses no label the last one could not see.
_LABEL_OVERLAP = 16


class _Runs:
    """The maximal runs of one character class in a text, found in one pass on first use."""

    def __init__(self, content: str, pattern: re.Pattern[str]) -> None:
        self._content = content
        self._pattern = pattern
        self._starts: list[int] | None = None
        self._ends: list[int] = []

    def around(self, start: int, end: int) -> tuple[int, int]:
        """[start, end) widened to the run that reaches ``start`` from the left and the run at ``end``."""
        if self._starts is None:
            spans = [m.span() for m in self._pattern.finditer(self._content)]
            self._starts = [a for a, _ in spans]
            self._ends = [b for _, b in spans]
        left, right = start, end
        if start > 0:
            i = bisect.bisect_right(self._starts, start - 1) - 1
            if i >= 0 and start - 1 < self._ends[i]:
                left = self._starts[i]
        if end < len(self._content):
            i = bisect.bisect_right(self._starts, end) - 1
            if i >= 0 and end < self._ends[i]:
                right = self._ends[i]
        return left, right


class _Word:
    """One whitespace-delimited word, with what the developer-number check asks of it worked out once."""

    __slots__ = ("text", "left", "has_slash", "personal", "is_url", "named", "stripped", "_region", "_origin", "_seen", "_label")

    def __init__(self, content: str, left: int, right: int) -> None:
        text = content[left:right]
        self.text = text
        self.left = left
        self.has_slash = "/" in text
        self.personal = self.has_slash and bool(_PERSONAL_HINT.search(text))
        self.is_url = bool(_URL_START.match(text))
        # The segments with a name in them. The digits asked about are a segment without one.
        self.named = sum(1 for seg in text.split("/") if _NAMED_SEGMENT.search(seg)) if self.has_slash else 0
        self.stripped = len(text.rstrip(_TRAILING_PUNCTUATION))
        # The label window reaches back from the start of the word: a URL can put the digits far from it.
        self._origin = max(0, left - _LABEL_WINDOW)
        self._region = content[self._origin : right]
        self._seen = 0  # where the last label search ended, in the region
        self._label = False  # a label that every later (longer) search finds too

    def account_label_before(self, start: int) -> bool:
        """``_ACCOUNT_LABEL.search(content[word start - window : start])``, each stretch of the word read once.

        The numbers of one word are asked about left to right. A label found short of the end of
        the text searched is found by every longer search; one that ends the text searched may
        rest on that end being a word boundary ("account" in "account12345678"), so it is not kept.
        """
        end = start - self._origin
        if end <= self._seen:
            self._seen, self._label = 0, False
        if self._label:
            return True
        begin = self._seen - _LABEL_OVERLAP
        if begin < self.left - self._origin:
            begin = 0  # a label that starts before the word can hold any amount of white space
        self._seen = end
        found = _ACCOUNT_LABEL.search(self._region, begin, end)
        if found is None:
            return False
        self._label = found.end() < end or self._region[found.end() - 1] == "_"
        return True


class _ScanContext:
    """The words and identifier tokens of one text, each worked out once for all the numbers in it."""

    def __init__(self, content: str) -> None:
        self.content = content
        self._word_runs = _Runs(content, _WORD_RUN)
        self._token_runs = _Runs(content, _IDENTIFIER_RUN)
        self._words: dict[tuple[int, int], _Word] = {}
        self._uuids: dict[tuple[int, int], bool] = {}

    def word(self, start: int, end: int) -> _Word:
        """The whitespace-delimited word containing [start, end)."""
        span = self._word_runs.around(start, end)
        word = self._words.get(span)
        if word is None:
            word = self._words[span] = _Word(self.content, *span)
        return word

    def token(self, start: int, end: int) -> tuple[int, bool]:
        """Where the run of identifier characters (letters, digits, ``.``, ``_``, ``-``) containing
        [start, end) ends, and whether the whole run is a UUID."""
        span = self._token_runs.around(start, end)
        is_uuid = self._uuids.get(span)
        if is_uuid is None:
            is_uuid = self._uuids[span] = bool(_UUID.fullmatch(self.content[span[0] : span[1]].strip("._-")))
        return span[1], is_uuid


def is_identifier_fragment(content: str, start: int, end: int, _context: _ScanContext | None = None) -> bool:
    """True when the digits at [start, end) are one part of a machine identifier, not a number on their own.

    Only these shapes (the false positives seen), and never with an account
    label (``acct``, ``account``, ``a/c``, ``iban``, ``acc no``) just before:

    * a UUID (``12345678-1234-5678-1234-567812345678``);
    * digits, ``-`` and a random-looking part of letters AND digits (6+):
      ``20260926123456-a1b2c3``, an OAuth client id
      ``629134551556-8v3kq0abc...``;
    * digits that start a dotted host name: ``629134551556-x.apps.googleusercontent.com``.

    A plain word or code after the number (``-checking``, ``-SAV``, ``-JMD``,
    ``-01``) does not make it an identifier: bank accounts are written that way.
    """
    if _ACCOUNT_LABEL.search(content[max(0, start - _LABEL_WINDOW) : start]):
        return False
    token_end, is_uuid = (_context or _ScanContext(content)).token(start, end)
    if is_uuid:
        return True
    if not content.startswith("-", end, token_end):
        return False
    # The part after the "-", up to the next "-", "." or "_" of the token.
    following = _ALNUM_RUN.match(content, end + 1, token_end)
    return bool((following and _MIXED_PART.fullmatch(following.group())) or _HOST_TAIL.match(content, end, token_end))


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _is_path_segment(word: _Word, offset: int, length: int) -> bool:
    """True when the digits are one whole segment of a URL, or of a path with two named segments."""
    if not word.has_slash or word.personal:
        return False
    # The character before the digits, and the one after them unless only punctuation is left.
    before = word.text[offset - 1] if offset else ""
    after = word.text[offset + length] if offset + length < word.stripped else ""
    if word.is_url:
        return before in ("/", "=") and after in ("", "/", "?", "#", "&")
    return before in ("", "/") and after in ("", "/") and word.named >= 2


def is_developer_number(content: str, start: int, end: int, _context: _ScanContext | None = None) -> bool:
    """True when the bare digit run at [start, end) is a machine-issued number, not a personal one.

    Only these contexts (the false positives seen), and never with an account
    label (``acct``, ``account``, ``a/c``, ``iban``, ``acc no``) just before:

    * a whole segment of a URL: ``www.facebook.com/100063512345678``,
      ``.../actions/runs/42664995945/job/7``; or of a slash path that has two segments with a
      name in them: ``artifacts/20261110/1234567890123/report.html``. Not ``12345678901/021000021``
      or ``2035551234/ext``, and not when a segment says phone, card, pay, bank and the like;
    * straight after a word naming such a number (run, job, workflow, build, deploy, migration,
      commit, artifact, epoch, ``<thing> id``): ``gh run view 42664995945``,
      ``migrations 590000000-599999``, ``app ID 1234567890``.

    Sixteen digits are the shape of a card number: kept only as a URL segment, and only when the
    number fails the card checksum.
    """
    word = (_context or _ScanContext(content)).word(start, end)
    if word.account_label_before(start):
        return False
    in_path = _is_path_segment(word, start - word.left, end - start)
    if end - start == _CARD_DIGITS:
        return in_path and word.is_url and not _luhn_valid(content[start:end])
    return in_path or bool(_DEVELOPER_LABEL.search(content[max(0, start - 2 * _LABEL_WINDOW) : start]))


# The patterns that also match a bare run of digits: a CI run id is 11 digits, an epoch or an
# App Store id 10 (the shape of a phone number), a long id in a URL 16 (the shape of a card number).
_BARE_DIGIT_TYPES = frozenset({"bank_account", "phone_us", "credit_card"})


# Severity levels for PII types
PII_SEVERITY: dict[str, str] = {
    "ssn": "critical",
    "credit_card": "critical",
    "api_key": "critical",
    "aws_key": "critical",
    "password": "critical",
    "passport_us": "high",
    "bank_account": "high",
    "drivers_license": "high",
    "email": "medium",
    "phone_us": "medium",
    "phone_intl": "medium",
    "dob": "medium",
}


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class PIIMatch:
    """A detected PII pattern match."""

    type: str
    severity: str
    start: int
    end: int
    original: str
    redacted: str


@dataclass
class PIIScanResult:
    """Result of PII scan."""

    has_pii: bool = False
    matches: list[PIIMatch] = field(default_factory=list)
    redacted_content: str | None = None
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False

    @property
    def critical_count(self) -> int:
        return sum(1 for m in self.matches if m.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for m in self.matches if m.severity == "high")


_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1}


def _merge_overlaps(matches: list[PIIMatch], content: str = "") -> list[tuple[int, int, PIIMatch]]:
    """Overlapping matches as one span each, in text order: ``(start, end, naming match)``.

    Two patterns often match the same digits (a 10-digit run is a phone number and a bank
    account). Replacing them one by one spliced the placeholders into each other
    (``[REDACTED_BANK_ACCOUNT]PHONE_US]``). The span is the union; the most severe match names
    it, then the longest, then the earliest. On the very same span the specific pattern (phone,
    card) names it rather than the generic digit run, unless ``content`` has an account label
    just before.
    """

    def rank(m: PIIMatch) -> tuple[int, int, int]:
        return (_SEVERITY_RANK.get(m.severity, 0), m.end - m.start, -m.start)

    def better(new: PIIMatch, named: PIIMatch) -> bool:
        if (new.start, new.end) == (named.start, named.end) and "bank_account" in (new.type, named.type):
            labelled = bool(_ACCOUNT_LABEL.search(content[max(0, new.start - _LABEL_WINDOW) : new.start]))
            return (new.type == "bank_account") == labelled and new.type != named.type
        return rank(new) > rank(named)

    merged: list[tuple[int, int, PIIMatch]] = []
    for match in sorted(matches, key=lambda m: (m.start, -(m.end - m.start))):
        if match.end <= match.start:
            continue
        if merged and match.start < merged[-1][1]:
            start, end, named = merged[-1]
            merged[-1] = (start, max(end, match.end), match if better(match, named) else named)
            continue
        merged.append((match.start, match.end, match))
    return merged


# ============================================================================
# PII Detector
# ============================================================================


class PIIDetector:
    """
    Detects and optionally redacts PII from content.

    Usage:
        detector = PIIDetector(settings)
        result = detector.scan("My SSN is 123-45-6789")
        if result.has_pii:
            print(f"Found {len(result.matches)} PII patterns")
            print(f"Redacted: {result.redacted_content}")
    """

    def __init__(
        self,
        enabled: bool = True,
        mode: str = "detect",  # detect | redact | block
        exclusions: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.mode = mode
        self.exclusions = set(exclusions or [])

        # Build active patterns (excluding user exclusions)
        self.patterns = {name: pattern for name, pattern in PII_PATTERNS.items() if name not in self.exclusions}

        log.info(
            "pii_detector_initialized",
            enabled=enabled,
            mode=mode,
            patterns=len(self.patterns),
            exclusions=list(self.exclusions),
        )

    def scan(self, content: str, source: str = "user_input") -> PIIScanResult:
        """
        Scan content for PII patterns.

        Args:
            content: Text content to scan
            source: Source identifier for logging

        Returns:
            PIIScanResult with matches and optional redacted content
        """
        if not self.enabled or not content:
            return PIIScanResult(has_pii=False)

        matches: list[PIIMatch] = []
        warnings: list[str] = []

        # The words and tokens around the bare digit runs, worked out once for the whole text.
        context = _ScanContext(content)

        # Scan for each PII pattern
        for pii_type, pattern in self.patterns.items():
            found = _email_matches(content) if pattern is _EMAIL_PATTERN else pattern.finditer(content)
            for match in found:
                # For password pattern, extract just the password value (group 1)
                if pii_type == "password" and match.lastindex and match.lastindex >= 1:
                    original = match.group(1)
                    # Calculate start/end for just the password value
                    full_match = match.group()
                    pw_start = match.start() + full_match.index(original)
                    pw_end = pw_start + len(original)
                    start, end = pw_start, pw_end
                else:
                    original = match.group()
                    start, end = match.start(), match.end()
                    if pii_type == "bank_account" and is_identifier_fragment(content, start, end, context):
                        continue
                    if pii_type in _BARE_DIGIT_TYPES and original.isdigit() and is_developer_number(content, start, end, context):
                        continue

                severity = PII_SEVERITY.get(pii_type, "medium")

                matches.append(
                    PIIMatch(
                        type=pii_type,
                        severity=severity,
                        start=start,
                        end=end,
                        original=original,
                        redacted=self._redact_value(original, pii_type),
                    )
                )

                warnings.append(f"Found {pii_type} ({severity})")

        if not matches:
            return PIIScanResult(has_pii=False)

        # Log detection
        log.warning(
            "pii_detected",
            source=source,
            match_count=len(matches),
            types=list(set(m.type for m in matches)),
            critical=sum(1 for m in matches if m.severity == "critical"),
        )

        # Build result based on mode
        result = PIIScanResult(
            has_pii=True,
            matches=matches,
            warnings=warnings,
        )

        if self.mode == "redact":
            result.redacted_content = self._redact_all(content, matches)
        elif self.mode == "block":
            result.blocked = True

        return result

    def _redact_value(self, value: str, pii_type: str) -> str:
        """Create a redacted version of a PII value."""
        # Keep first and last chars for context, redact middle
        if len(value) <= 4:
            return f"[{pii_type.upper()}]"

        return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"

    def _redact_all(self, content: str, matches: list[PIIMatch]) -> str:
        """Replace all PII matches with redaction placeholders."""
        # Built left to right in one pass: splicing each placeholder into the text copies it every time.
        parts: list[str] = []
        cursor = 0
        for start, end, match in _merge_overlaps(matches, content):
            parts.append(content[cursor:start])
            parts.append(f"[REDACTED_{match.type.upper()}]")
            cursor = end
        parts.append(content[cursor:])
        return "".join(parts)

    def redact(self, content: str) -> str:
        """
        Convenience method to redact all PII from content.

        Returns redacted content regardless of mode setting.
        """
        result = self.scan(content)
        if result.has_pii:
            return self._redact_all(content, result.matches)
        return content

    def hash_pii(self, content: str) -> str:
        """
        Hash PII values for safe storage/comparison.

        Replaces PII with deterministic hashes so:
        - Same PII always produces same hash
        - Original value cannot be recovered
        - Can still detect if same PII appears again
        """
        result = self.scan(content)
        if not result.has_pii:
            return content

        parts: list[str] = []
        cursor = 0
        for start, end, match in _merge_overlaps(result.matches, content):
            # Create deterministic hash (of the whole replaced span: overlapping matches are one value)
            hash_value = hashlib.sha256(content[start:end].encode()).hexdigest()[:12]
            parts.append(content[cursor:start])
            parts.append(f"[HASH_{match.type.upper()}:{hash_value}]")
            cursor = end
        parts.append(content[cursor:])
        return "".join(parts)


# ============================================================================
# Convenience Functions
# ============================================================================


def scan_for_pii(
    content: str,
    mode: str = "detect",
    exclusions: list[str] | None = None,
) -> PIIScanResult:
    """
    Quick scan for PII in content.

    Args:
        content: Text to scan
        mode: 'detect', 'redact', or 'block'
        exclusions: PII types to ignore

    Returns:
        PIIScanResult with matches
    """
    detector = PIIDetector(enabled=True, mode=mode, exclusions=exclusions)
    return detector.scan(content)


def redact_pii(content: str, exclusions: list[str] | None = None) -> str:
    """
    Redact all PII from content.

    Args:
        content: Text to redact
        exclusions: PII types to ignore

    Returns:
        Content with PII replaced by [REDACTED_TYPE] placeholders
    """
    detector = PIIDetector(enabled=True, mode="redact", exclusions=exclusions)
    return detector.redact(content)
