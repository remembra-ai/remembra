"""
Smart Auto-Forgetting via Temporal Phrase Detection.

This module detects temporal phrases in content and suggests
appropriate TTLs for automatic memory expiration.

Examples:
    - "meeting tomorrow at 2pm" → expires in ~36 hours
    - "remember for next week" → expires in 7 days
    - "annual review" → expires in 1 year
    - "call me in 30 minutes" → expires in ~1 hour

Usage:
    parser = TemporalParser()

    result = parser.detect("Meeting with John tomorrow at 3pm")
    if result:
        print(f"Suggested TTL: {result.ttl_seconds}s ({result.reason})")
        # Suggested TTL: 129600s (expires after 'tomorrow' reference)

The parser is intentionally lightweight - uses regex patterns with
optional dateparser fallback for complex cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class TemporalGranularity(Enum):
    """Granularity of detected temporal reference."""

    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"
    YEARS = "years"
    UNKNOWN = "unknown"


@dataclass
class TemporalDetection:
    """Result of temporal phrase detection."""

    phrase: str
    """The matched temporal phrase from content."""

    ttl_seconds: int
    """Suggested TTL in seconds."""

    ttl_string: str
    """Human-readable TTL (e.g., '36h', '7d')."""

    granularity: TemporalGranularity
    """Time granularity of the reference."""

    confidence: float
    """Detection confidence (0.0 - 1.0)."""

    reason: str
    """Explanation of why this TTL was suggested."""

    buffer_hours: int = 12
    """Hours of buffer added after the temporal reference."""


# Pre-compiled regex patterns for efficiency
# Order matters - more specific patterns first

TEMPORAL_PATTERNS: list[tuple[re.Pattern[str], int, TemporalGranularity, float, str]] = [
    # Explicit "remember for X" patterns - highest confidence
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(\d+)\s*(?:min(?:ute)?s?)\b", re.I),
        lambda m: int(m.group(1)) * 60 + 1800,  # +30min buffer
        TemporalGranularity.MINUTES,
        0.95,
        "explicit remember duration",
    ),
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(\d+)\s*(?:hour?s?|hr?s?)\b", re.I),
        lambda m: int(m.group(1)) * 3600 + 7200,  # +2hr buffer
        TemporalGranularity.HOURS,
        0.95,
        "explicit remember duration",
    ),
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(\d+)\s*(?:day?s?)\b", re.I),
        lambda m: int(m.group(1)) * 86400 + 43200,  # +12hr buffer
        TemporalGranularity.DAYS,
        0.95,
        "explicit remember duration",
    ),
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(?:a\s+|next\s+)?week\b", re.I),
        lambda m: 7 * 86400 + 43200,
        TemporalGranularity.WEEKS,
        0.95,
        "explicit remember duration (1 week)",
    ),
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(\d+)\s*(?:week?s?)\b", re.I),
        lambda m: int(m.group(1)) * 7 * 86400 + 43200,
        TemporalGranularity.WEEKS,
        0.95,
        "explicit remember duration",
    ),
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(?:a\s+|next\s+)?month\b", re.I),
        lambda m: 30 * 86400 + 86400,
        TemporalGranularity.MONTHS,
        0.95,
        "explicit remember duration (1 month)",
    ),
    (
        re.compile(r"\bremember\s+(?:this\s+)?for\s+(\d+)\s*(?:month?s?)\b", re.I),
        lambda m: int(m.group(1)) * 30 * 86400 + 86400,
        TemporalGranularity.MONTHS,
        0.95,
        "explicit remember duration",
    ),
    # "until X" patterns
    (
        re.compile(r"\buntil\s+tomorrow\b", re.I),
        lambda m: 36 * 3600,  # ~36 hours
        TemporalGranularity.DAYS,
        0.90,
        "expires after tomorrow",
    ),
    (
        re.compile(
            r"\buntil\s+(?:next\s+)?(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|"
            r"thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
            re.I,
        ),
        lambda m: 7 * 86400 + 43200,  # ~7.5 days
        TemporalGranularity.WEEKS,
        0.85,
        "expires after next weekday",
    ),
    (
        re.compile(r"\buntil\s+(?:next\s+)?week(?:end)?\b", re.I),
        lambda m: 7 * 86400 + 43200,
        TemporalGranularity.WEEKS,
        0.85,
        "expires after next week",
    ),
    (
        re.compile(r"\buntil\s+(?:next\s+)?month\b", re.I),
        lambda m: 30 * 86400 + 86400,
        TemporalGranularity.MONTHS,
        0.85,
        "expires after next month",
    ),
    # Relative time - "in X minutes/hours/days"
    (
        re.compile(r"\bin\s+(\d+)\s*(?:min(?:ute)?s?)\b", re.I),
        lambda m: int(m.group(1)) * 60 + 1800,
        TemporalGranularity.MINUTES,
        0.80,
        "relative time reference",
    ),
    (
        re.compile(r"\bin\s+(\d+)\s*(?:hour?s?|hr?s?)\b", re.I),
        lambda m: int(m.group(1)) * 3600 + 3600,
        TemporalGranularity.HOURS,
        0.80,
        "relative time reference",
    ),
    (
        re.compile(r"\bin\s+(\d+)\s*(?:day?s?)\b", re.I),
        lambda m: int(m.group(1)) * 86400 + 43200,
        TemporalGranularity.DAYS,
        0.80,
        "relative time reference",
    ),
    (
        re.compile(r"\bin\s+(\d+)\s*(?:week?s?)\b", re.I),
        lambda m: int(m.group(1)) * 7 * 86400 + 86400,
        TemporalGranularity.WEEKS,
        0.80,
        "relative time reference",
    ),
    (
        re.compile(r"\bin\s+(\d+)\s*(?:month?s?)\b", re.I),
        lambda m: int(m.group(1)) * 30 * 86400 + 86400,
        TemporalGranularity.MONTHS,
        0.80,
        "relative time reference",
    ),
    # Common temporal keywords
    (
        re.compile(r"\btomorrow\b(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?", re.I),
        lambda m: 36 * 3600,  # 36 hours
        TemporalGranularity.DAYS,
        0.75,
        "reference to tomorrow",
    ),
    (
        re.compile(r"\btonight\b", re.I),
        lambda m: 18 * 3600,  # 18 hours
        TemporalGranularity.HOURS,
        0.75,
        "reference to tonight",
    ),
    (
        re.compile(r"\bthis\s+(?:afternoon|evening|morning)\b", re.I),
        lambda m: 12 * 3600,  # 12 hours
        TemporalGranularity.HOURS,
        0.70,
        "reference to today",
    ),
    (
        re.compile(r"\bnext\s+week\b", re.I),
        lambda m: 10 * 86400,  # 10 days
        TemporalGranularity.WEEKS,
        0.75,
        "reference to next week",
    ),
    (
        re.compile(r"\bthis\s+week(?:end)?\b", re.I),
        lambda m: 7 * 86400,  # 7 days
        TemporalGranularity.WEEKS,
        0.70,
        "reference to this week",
    ),
    (
        re.compile(
            r"\bnext\s+(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|"
            r"thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
            re.I,
        ),
        lambda m: 10 * 86400,  # 10 days
        TemporalGranularity.WEEKS,
        0.75,
        "reference to next weekday",
    ),
    (
        re.compile(
            r"\bthis\s+(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|"
            r"thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
            re.I,
        ),
        lambda m: 5 * 86400,  # 5 days
        TemporalGranularity.DAYS,
        0.70,
        "reference to this weekday",
    ),
    (
        re.compile(r"\bnext\s+month\b", re.I),
        lambda m: 45 * 86400,  # 45 days
        TemporalGranularity.MONTHS,
        0.75,
        "reference to next month",
    ),
    (
        re.compile(r"\bthis\s+month\b", re.I),
        lambda m: 30 * 86400,  # 30 days
        TemporalGranularity.MONTHS,
        0.70,
        "reference to this month",
    ),
    (
        re.compile(r"\bnext\s+year\b", re.I),
        lambda m: 400 * 86400,  # ~13 months
        TemporalGranularity.YEARS,
        0.75,
        "reference to next year",
    ),
    (
        re.compile(r"\bthis\s+year\b", re.I),
        lambda m: 365 * 86400,  # 1 year
        TemporalGranularity.YEARS,
        0.70,
        "reference to this year",
    ),
    # Meeting/event patterns
    (
        re.compile(r"\bmeeting\s+(?:is\s+)?(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:today)?\b", re.I),
        lambda m: 12 * 3600,  # 12 hours
        TemporalGranularity.HOURS,
        0.65,
        "meeting today",
    ),
    (
        re.compile(r"\bmeeting\s+(?:is\s+)?(?:tomorrow|tmrw)\b", re.I),
        lambda m: 36 * 3600,
        TemporalGranularity.DAYS,
        0.70,
        "meeting tomorrow",
    ),
    (
        re.compile(r"\bcall\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+today)?\b", re.I),
        lambda m: 12 * 3600,
        TemporalGranularity.HOURS,
        0.65,
        "call today",
    ),
    (
        re.compile(r"\b(?:deadline|due)\s+(?:is\s+)?(?:tomorrow|tmrw)\b", re.I),
        lambda m: 36 * 3600,
        TemporalGranularity.DAYS,
        0.75,
        "deadline tomorrow",
    ),
    (
        re.compile(
            r"\b(?:deadline|due)\s+(?:is\s+)?(?:next\s+)?(?:mon(?:day)?|tue(?:sday)?|"
            r"wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
            re.I,
        ),
        lambda m: 10 * 86400,
        TemporalGranularity.WEEKS,
        0.75,
        "deadline this/next week",
    ),
    (
        re.compile(r"\b(?:deadline|due)\s+(?:is\s+)?(?:in\s+)?(\d+)\s*(?:day?s?)\b", re.I),
        lambda m: int(m.group(1)) * 86400 + 43200,
        TemporalGranularity.DAYS,
        0.80,
        "deadline in N days",
    ),
    # Recurring/periodic patterns - longer TTLs
    (
        re.compile(r"\b(?:weekly|every\s+week)\b", re.I),
        lambda m: 14 * 86400,  # 2 weeks
        TemporalGranularity.WEEKS,
        0.60,
        "weekly event",
    ),
    (
        re.compile(r"\b(?:monthly|every\s+month)\b", re.I),
        lambda m: 60 * 86400,  # 2 months
        TemporalGranularity.MONTHS,
        0.60,
        "monthly event",
    ),
    (
        re.compile(r"\b(?:annual(?:ly)?|yearly|every\s+year)\b", re.I),
        lambda m: 400 * 86400,  # ~13 months
        TemporalGranularity.YEARS,
        0.60,
        "annual event",
    ),
    (
        re.compile(r"\b(?:quarterly|every\s+quarter)\b", re.I),
        lambda m: 120 * 86400,  # 4 months
        TemporalGranularity.MONTHS,
        0.60,
        "quarterly event",
    ),
    # Specific date patterns (MM/DD or Month Day)
    (
        re.compile(
            r"\bon\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
            r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?\b",
            re.I,
        ),
        lambda m: 365 * 86400,  # 1 year (could be this or next year)
        TemporalGranularity.YEARS,
        0.55,
        "specific date reference",
    ),
]


class TemporalParser:
    """
    Parser for detecting temporal phrases and suggesting TTLs.

    This parser uses a combination of regex patterns to detect
    temporal references in text content. When a match is found,
    it suggests an appropriate TTL that expires after the temporal
    reference (with a buffer).

    The parser is designed to be lightweight and fast - no external
    NLP dependencies required. For complex parsing needs, you can
    optionally enable dateparser integration.

    Args:
        use_dateparser: Try dateparser for unmatched content (requires install)
        min_confidence: Minimum confidence threshold for detection
        default_buffer_hours: Default buffer hours added after reference

    Example:
        >>> parser = TemporalParser()
        >>> result = parser.detect("Meeting tomorrow at 3pm")
        >>> if result:
        ...     print(f"TTL: {result.ttl_string}")
        TTL: 36h
    """

    def __init__(
        self,
        use_dateparser: bool = False,
        min_confidence: float = 0.5,
        default_buffer_hours: int = 12,
    ) -> None:
        self._use_dateparser = use_dateparser
        self._min_confidence = min_confidence
        self._default_buffer_hours = default_buffer_hours
        self._dateparser = None

        if use_dateparser:
            try:
                import dateparser

                self._dateparser = dateparser
            except ImportError:
                pass

    def detect(self, content: str) -> TemporalDetection | None:
        """
        Detect temporal phrases in content and suggest TTL.

        Scans the content for temporal references and returns
        the highest-confidence match with a suggested TTL.

        Args:
            content: Text content to analyze

        Returns:
            TemporalDetection with suggested TTL, or None if no match

        Example:
            >>> parser = TemporalParser()
            >>> result = parser.detect("Remember for 3 days: buy groceries")
            >>> result.ttl_seconds
            302400  # 3.5 days with buffer
        """
        if not content or not isinstance(content, str):
            return None

        best_match: TemporalDetection | None = None
        best_confidence = 0.0

        for pattern, ttl_calc, granularity, confidence, reason in TEMPORAL_PATTERNS:
            if confidence < self._min_confidence:
                continue

            match = pattern.search(content)
            if match:
                # Calculate TTL (pattern may have lambda or int)
                if callable(ttl_calc):
                    ttl_seconds = ttl_calc(match)
                else:
                    ttl_seconds = ttl_calc

                if confidence > best_confidence:
                    best_confidence = confidence
                    best_match = TemporalDetection(
                        phrase=match.group(0),
                        ttl_seconds=ttl_seconds,
                        ttl_string=self._format_ttl(ttl_seconds),
                        granularity=granularity,
                        confidence=confidence,
                        reason=reason,
                        buffer_hours=self._default_buffer_hours,
                    )

        # Try dateparser as fallback for complex cases
        if best_match is None and self._dateparser is not None:
            best_match = self._try_dateparser(content)

        return best_match

    def detect_all(self, content: str) -> list[TemporalDetection]:
        """
        Detect all temporal phrases in content.

        Unlike detect(), this returns all matches found,
        not just the highest confidence one.

        Args:
            content: Text content to analyze

        Returns:
            List of TemporalDetection objects, sorted by confidence (highest first)
        """
        if not content or not isinstance(content, str):
            return []

        matches: list[TemporalDetection] = []

        for pattern, ttl_calc, granularity, confidence, reason in TEMPORAL_PATTERNS:
            if confidence < self._min_confidence:
                continue

            for match in pattern.finditer(content):
                if callable(ttl_calc):
                    ttl_seconds = ttl_calc(match)
                else:
                    ttl_seconds = ttl_calc

                matches.append(
                    TemporalDetection(
                        phrase=match.group(0),
                        ttl_seconds=ttl_seconds,
                        ttl_string=self._format_ttl(ttl_seconds),
                        granularity=granularity,
                        confidence=confidence,
                        reason=reason,
                        buffer_hours=self._default_buffer_hours,
                    )
                )

        # Sort by confidence (highest first)
        matches.sort(key=lambda x: x.confidence, reverse=True)
        return matches

    def _try_dateparser(self, content: str) -> TemporalDetection | None:
        """Fallback to dateparser for complex temporal expressions."""
        if self._dateparser is None:
            return None

        try:
            # Try to parse the content as a date
            parsed = self._dateparser.parse(
                content,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": datetime.now(),
                },
            )

            if parsed:
                delta = parsed - datetime.now()
                if delta.total_seconds() > 0:
                    # Add buffer
                    ttl_seconds = int(delta.total_seconds()) + (self._default_buffer_hours * 3600)

                    return TemporalDetection(
                        phrase="(dateparser)",
                        ttl_seconds=ttl_seconds,
                        ttl_string=self._format_ttl(ttl_seconds),
                        granularity=self._infer_granularity(delta),
                        confidence=0.50,  # Lower confidence for dateparser
                        reason="parsed by dateparser",
                        buffer_hours=self._default_buffer_hours,
                    )
        except Exception:
            pass

        return None

    def _infer_granularity(self, delta: timedelta) -> TemporalGranularity:
        """Infer granularity from timedelta."""
        seconds = delta.total_seconds()

        if seconds < 3600:
            return TemporalGranularity.MINUTES
        elif seconds < 86400:
            return TemporalGranularity.HOURS
        elif seconds < 604800:
            return TemporalGranularity.DAYS
        elif seconds < 2592000:
            return TemporalGranularity.WEEKS
        elif seconds < 31536000:
            return TemporalGranularity.MONTHS
        else:
            return TemporalGranularity.YEARS

    def _format_ttl(self, seconds: int) -> str:
        """Format seconds as human-readable TTL string."""
        if seconds < 3600:
            return f"{seconds // 60}m"
        elif seconds < 86400:
            return f"{seconds // 3600}h"
        elif seconds < 604800:
            days = seconds / 86400
            if days == int(days):
                return f"{int(days)}d"
            return f"{days:.1f}d"
        elif seconds < 2592000:
            weeks = seconds / 604800
            if weeks == int(weeks):
                return f"{int(weeks)}w"
            return f"{weeks:.1f}w"
        elif seconds < 31536000:
            months = seconds / 2592000
            if months == int(months):
                return f"{int(months)}mo"
            return f"{months:.1f}mo"
        else:
            years = seconds / 31536000
            if years == int(years):
                return f"{int(years)}y"
            return f"{years:.1f}y"


# Singleton for convenience
_default_parser: TemporalParser | None = None


def detect_temporal(
    content: str,
    min_confidence: float = 0.5,
) -> TemporalDetection | None:
    """
    Convenience function to detect temporal phrases.

    Uses a singleton parser instance for efficiency.

    Args:
        content: Text content to analyze
        min_confidence: Minimum confidence threshold

    Returns:
        TemporalDetection or None

    Example:
        >>> from remembra.client.temporal_parser import detect_temporal
        >>> result = detect_temporal("Call me tomorrow at 3pm")
        >>> result.ttl_string
        '36h'
    """
    global _default_parser

    if _default_parser is None:
        _default_parser = TemporalParser(min_confidence=min_confidence)

    return _default_parser.detect(content)


def suggest_ttl(content: str, min_confidence: float = 0.5) -> str | None:
    """
    Convenience function to get TTL suggestion for content.

    Args:
        content: Text content to analyze
        min_confidence: Minimum confidence threshold

    Returns:
        TTL string (e.g., "36h", "7d") or None

    Example:
        >>> from remembra.client.temporal_parser import suggest_ttl
        >>> suggest_ttl("Meeting next week")
        '10d'
    """
    result = detect_temporal(content, min_confidence)
    return result.ttl_string if result else None
