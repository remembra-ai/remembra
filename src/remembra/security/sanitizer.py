"""Input sanitization for memory content - defense against prompt injection and XSS."""

import hashlib
import html
import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# ============================================================================
# XSS SANITIZATION PATTERNS
# ============================================================================

# HTML tags to completely remove (with content)
DANGEROUS_TAGS_WITH_CONTENT = re.compile(
    r"<\s*(script|style|iframe|object|embed|form|input|button|textarea|select|frame|frameset|applet|meta|link|base)\b[^>]*>.*?</\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Self-closing or unclosed dangerous tags
DANGEROUS_TAGS_SELF_CLOSING = re.compile(
    r"<\s*(script|style|iframe|object|embed|form|input|button|textarea|select|frame|frameset|applet|meta|link|base|img|svg|math)\b[^>]*/?\s*>",
    re.IGNORECASE,
)

# Event handlers (onclick, onload, onerror, etc.)
EVENT_HANDLERS = re.compile(
    r"\s+on\w+\s*=\s*['\"][^'\"]*['\"]|\s+on\w+\s*=\s*\S+",
    re.IGNORECASE,
)

# javascript: and data: URLs
DANGEROUS_URLS = re.compile(
    r"(href|src|action|formaction|data|poster|background)\s*=\s*['\"]?\s*(javascript|data|vbscript)\s*:",
    re.IGNORECASE,
)

# Expression/eval patterns in CSS
CSS_EXPRESSIONS = re.compile(
    r"expression\s*\(|behavior\s*:|binding\s*:|moz-binding\s*:",
    re.IGNORECASE,
)

# HTML comments (can hide XSS)
HTML_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)

# SVG/Math with embedded scripts
SVG_MATH_TAGS = re.compile(
    r"<\s*(svg|math)\b[^>]*>.*?</\s*(svg|math)\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Suspicious patterns that may indicate prompt injection attempts
# Each pattern has a weight for trust score reduction
SUSPICIOUS_PATTERNS: list[tuple[str, float, str]] = [
    # Direct instruction overrides
    (r"ignore\s+(all\s+)?previous\s+(instructions?|context)", 0.35, "instruction_override"),
    (r"disregard\s+(all\s+)?(previous\s+)?instructions?", 0.35, "instruction_override"),
    (r"forget\s+(all\s+)?previous\s+(context|instructions?)", 0.35, "instruction_override"),
    (r"ignore\s+all\s+previous", 0.35, "instruction_override"),
    # Variants the four above miss ("ignore the above instructions", "disregard prior
    # instructions", "override your previous instructions"). Each needs an
    # instruction/prompt noun aimed at the reader: honest developer text says
    # "override existing rules", "forget earlier context", "bypass system messages",
    # and a handoff scored below 1.0 is withheld whole, so those must score 1.0.
    # The verb/determiner splits keep each phrase counted once (no overlap with the
    # four above).
    (
        r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|the|your)\s+)*"
        r"(?:prior|above|earlier|preceding)\s+(?:instructions?|prompts?|directives?)\b",
        0.35,
        "instruction_override",
    ),
    (
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:the|your|any)\s+previous\s+(?:instructions?|prompts?|directives?)\b",
        0.35,
        "instruction_override",
    ),
    (
        r"\b(?:override|bypass)\s+(?:(?:all|any|the|your)\s+)*previous\s+(?:instructions?|prompts?|directives?)\b",
        0.35,
        "instruction_override",
    ),
    # "SYSTEM OVERRIDE. ..." / "New system instructions: ..." headers. A bare "new
    # instructions:" is honest docs text ("docs: new instructions: run make setup").
    (r"\bsystem\s+override\s*[.:!]", 0.35, "instruction_override"),
    (r"\bnew\s+system\s+(?:instructions?|prompt)\s*:", 0.35, "instruction_override"),
    # Concealment: an imperative asking the reading agent to keep something from the
    # user. Not "never reveal to the user which field was wrong" / "don't tell the
    # user whether the email exists" (honest auth and UX text).
    (
        r"\b(?:do\s*n[o']?t|do\s+not|never)\s+(?:tell|mention|inform)\s+(?:(?:it|this|that|anything)\s+)?"
        r"(?:to\s+)?(?:the\s+)?(?:user|human|owner|operator)\b"
        r"(?!\s+(?:which|what|whether|why|how|when|where|if|who)\b)",
        0.35,
        "concealment",
    ),
    (
        r"\bwithout\s+telling\s+(?:the\s+)?(?:user|human|owner|operator)\s+about\s+(?:it|this|that)\b",
        0.35,
        "concealment",
    ),
    (
        r"\bkeep\s+(?:this|it|that)\s+(?:secret|hidden|quiet)\s+from\s+(?:the\s+)?(?:user|human|owner|operator)\b",
        0.35,
        "concealment",
    ),
    # Role manipulation
    (r"you\s+are\s+now\s+a?", 0.30, "role_manipulation"),
    (r"act\s+as\s+if\s+you", 0.25, "role_manipulation"),
    (r"pretend\s+(that\s+)?you", 0.25, "role_manipulation"),
    (r"your\s+new\s+instructions?\s+(are|is)", 0.35, "role_manipulation"),
    (r"from\s+now\s+on\s+you", 0.25, "role_manipulation"),
    # System prompt extraction
    (r"(what|show|reveal|tell|repeat)\s+(is\s+|me\s+)?your\s+(system\s+)?(prompt|instructions?)", 0.30, "prompt_extraction"),
    (r"ignore\s+the\s+system\s+prompt", 0.35, "prompt_extraction"),
    (r"show\s+me\s+your\s+instructions?", 0.30, "prompt_extraction"),
    # Delimiter manipulation
    (r"```\s*system", 0.20, "delimiter_injection"),
    (r"\[SYSTEM\]", 0.20, "delimiter_injection"),
    (r"<\|im_start\|>", 0.30, "delimiter_injection"),
    (r"<\|im_end\|>", 0.30, "delimiter_injection"),
    # Output manipulation
    (r"output\s+only", 0.15, "output_manipulation"),
    (r"respond\s+with\s+only", 0.15, "output_manipulation"),
    (r"do\s+not\s+add\s+any", 0.15, "output_manipulation"),
    # Memory manipulation specific
    (r"insert\s+this\s+into\s+(your\s+)?memory", 0.25, "memory_manipulation"),
    (r"remember\s+that\s+you\s+must", 0.25, "memory_manipulation"),
    (r"store\s+this\s+as\s+a?\s*fact", 0.20, "memory_manipulation"),
]


def sanitize_xss(content: str) -> tuple[str, list[str]]:
    """
    Remove XSS attack vectors from content.

    Args:
        content: Raw input content

    Returns:
        Tuple of (sanitized_content, list_of_removed_patterns)

    Security note: This is defense-in-depth. Even though Remembra stores
    text for AI context (not rendered as HTML), we sanitize because:
    1. Content may be displayed in dashboards/UIs
    2. Prevents stored XSS if content is ever rendered
    3. Strips malicious payloads from polluting memory
    """
    if not content:
        return content, []

    removed: list[str] = []
    sanitized = content

    # Remove dangerous tags with their content (script, style, iframe, etc.)
    if DANGEROUS_TAGS_WITH_CONTENT.search(sanitized):
        removed.append("dangerous_tags_with_content")
        sanitized = DANGEROUS_TAGS_WITH_CONTENT.sub("", sanitized)

    # Remove SVG/Math tags (can contain embedded scripts)
    if SVG_MATH_TAGS.search(sanitized):
        removed.append("svg_math_tags")
        sanitized = SVG_MATH_TAGS.sub("", sanitized)

    # Remove self-closing dangerous tags
    if DANGEROUS_TAGS_SELF_CLOSING.search(sanitized):
        removed.append("dangerous_self_closing_tags")
        sanitized = DANGEROUS_TAGS_SELF_CLOSING.sub("", sanitized)

    # Remove event handlers (onclick, onload, onerror, etc.)
    if EVENT_HANDLERS.search(sanitized):
        removed.append("event_handlers")
        sanitized = EVENT_HANDLERS.sub("", sanitized)

    # Remove javascript:/data:/vbscript: URLs
    if DANGEROUS_URLS.search(sanitized):
        removed.append("dangerous_urls")
        sanitized = DANGEROUS_URLS.sub(r"\1=''", sanitized)

    # Remove CSS expressions
    if CSS_EXPRESSIONS.search(sanitized):
        removed.append("css_expressions")
        sanitized = CSS_EXPRESSIONS.sub("", sanitized)

    # Remove HTML comments
    if HTML_COMMENTS.search(sanitized):
        removed.append("html_comments")
        sanitized = HTML_COMMENTS.sub("", sanitized)

    # Escape remaining HTML-like tags, but be precise about what we escape:
    # - Must look like an actual HTML tag: <tagname, <tagname/, </tagname
    # - Preserve text that happens to have angle brackets like "a > b" or "<no tags>"
    # Known dangerous tags we haven't caught yet
    known_html_tags = (
        r"a|abbr|address|area|article|aside|audio|b|bdi|bdo|blockquote|body|br|"
        r"canvas|caption|cite|code|col|colgroup|data|datalist|dd|del|details|dfn|"
        r"dialog|div|dl|dt|em|fieldset|figcaption|figure|footer|h[1-6]|head|header|"
        r"hgroup|hr|html|i|ins|kbd|label|legend|li|main|map|mark|menu|meter|nav|"
        r"noscript|ol|optgroup|option|output|p|picture|pre|progress|q|rp|rt|ruby|"
        r"s|samp|section|small|source|span|strong|sub|summary|sup|table|tbody|td|"
        r"template|tfoot|th|thead|time|title|tr|track|u|ul|var|video|wbr"
    )
    remaining_tags = re.compile(
        rf"<\s*/?\s*({known_html_tags})\b[^>]*>",
        re.IGNORECASE,
    )
    if remaining_tags.search(sanitized):
        removed.append("remaining_html_tags")
        sanitized = remaining_tags.sub(lambda m: html.escape(m.group(0)), sanitized)

    # Clean up extra whitespace from removals
    sanitized = re.sub(r"\n\s*\n", "\n\n", sanitized)
    sanitized = sanitized.strip()

    if removed:
        log.warning(
            "xss_content_sanitized",
            removed_patterns=removed,
            original_length=len(content),
            sanitized_length=len(sanitized),
        )

    return sanitized, removed


@dataclass
class SanitizationResult:
    """Result of content sanitization."""

    content: str  # Sanitized content (XSS removed)
    original_content: str  # Original content before sanitization
    trust_score: float  # 0.0 to 1.0 (1.0 = fully trusted)
    checksum: str  # SHA-256 hash for integrity verification
    flagged_patterns: list[str]  # List of detected suspicious pattern types
    xss_removed: list[str]  # List of XSS patterns that were removed
    is_suspicious: bool  # True if trust_score < threshold
    source: str  # Content source (user_input, agent_generated, etc.)
    was_sanitized: bool  # True if content was modified


class ContentSanitizer:
    """
    Sanitizes and scores content before storage.

    Defense-in-depth against memory injection attacks (MINJA):
    1. Pattern detection for known injection techniques
    2. Trust scoring based on detected patterns
    3. Integrity checksums for tamper detection
    4. Logging of suspicious content (without storing actual content)

    Note: This does NOT block content - it flags it and reduces trust.
    The application decides how to handle low-trust memories.
    """

    def __init__(
        self,
        trust_threshold: float = 0.5,
        log_suspicious: bool = True,
    ) -> None:
        """
        Initialize sanitizer.

        Args:
            trust_threshold: Score below which content is flagged as suspicious
            log_suspicious: Whether to log warnings for suspicious content
        """
        self.trust_threshold = trust_threshold
        self.log_suspicious = log_suspicious

        # Compile patterns for efficiency
        self._compiled_patterns = [
            (re.compile(pattern, re.IGNORECASE), weight, name) for pattern, weight, name in SUSPICIOUS_PATTERNS
        ]

    @staticmethod
    def compute_checksum(content: str) -> str:
        """Compute SHA-256 checksum of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def analyze(
        self,
        content: str,
        source: str = "user_input",
        sanitize: bool = True,
    ) -> SanitizationResult:
        """
        Analyze and sanitize content for XSS and suspicious patterns.

        Args:
            content: The text content to analyze
            source: Origin of the content (user_input, agent_generated, external_api)
            sanitize: Whether to sanitize XSS (default: True)

        Returns:
            SanitizationResult with sanitized content, trust score, and detected patterns
        """
        original_content = content

        # Step 1: XSS sanitization (remove dangerous HTML)
        xss_removed: list[str] = []
        if sanitize:
            content, xss_removed = sanitize_xss(content)

        was_sanitized = bool(xss_removed)

        # Step 2: Prompt injection analysis (on sanitized content)
        trust_score = 1.0
        flagged_patterns: list[str] = []

        # Check each suspicious pattern
        for pattern, weight, pattern_name in self._compiled_patterns:
            if pattern.search(content):
                trust_score -= weight
                if pattern_name not in flagged_patterns:
                    flagged_patterns.append(pattern_name)

        # XSS content reduces trust score
        if xss_removed:
            trust_score -= 0.2 * len(xss_removed)

        # Clamp trust score to [0.0, 1.0]
        trust_score = max(0.0, min(1.0, trust_score))

        # Compute checksum of sanitized content
        checksum = self.compute_checksum(content)

        # Determine if suspicious
        is_suspicious = trust_score < self.trust_threshold or was_sanitized

        # Log if suspicious (SECURITY: Never log content, only hash for correlation)
        if is_suspicious and self.log_suspicious:
            content_hash = hashlib.sha256(original_content.encode()).hexdigest()[:16]
            log.warning(
                "suspicious_content_detected",
                trust_score=round(trust_score, 2),
                flagged_patterns=flagged_patterns,
                xss_removed=xss_removed,
                content_length=len(original_content),
                sanitized_length=len(content),
                content_hash=content_hash,  # Safe hash instead of content preview
                source=source,
            )

        return SanitizationResult(
            content=content,
            original_content=original_content,
            trust_score=trust_score,
            checksum=checksum,
            flagged_patterns=flagged_patterns,
            xss_removed=xss_removed,
            is_suspicious=is_suspicious,
            source=source,
            was_sanitized=was_sanitized,
        )

    def verify_integrity(self, content: str, expected_checksum: str) -> bool:
        """
        Verify content integrity against stored checksum.

        Args:
            content: The content to verify
            expected_checksum: The expected SHA-256 checksum

        Returns:
            True if checksum matches, False if content was tampered with
        """
        actual_checksum = self.compute_checksum(content)
        matches = actual_checksum == expected_checksum

        if not matches:
            log.error(
                "content_integrity_violation",
                expected=expected_checksum[:16] + "...",
                actual=actual_checksum[:16] + "...",
            )

        return matches
