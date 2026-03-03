"""
PII Detection and Redaction.

Scans content for Personally Identifiable Information (PII) patterns
before storage, supporting OWASP ASI06 (Memory Poisoning) compliance.

Modes:
- detect: Log warnings but allow storage
- redact: Replace PII with [REDACTED_TYPE] placeholders
- block: Reject content containing PII
"""

import hashlib
import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


# ============================================================================
# PII Patterns
# ============================================================================

PII_PATTERNS: dict[str, re.Pattern] = {
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
    "api_key": re.compile(r"\b(?:sk|pk|api|key|token|secret|password)[-_][A-Za-z0-9]{16,}\b", re.IGNORECASE),
    
    # AWS Access Keys
    "aws_key": re.compile(r"\b(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}\b"),
    
    # IP Addresses (v4)
    "ip_address": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
    
    # US Passport
    "passport_us": re.compile(r"\b[A-Z]\d{8}\b"),
    
    # Driver's License (generic patterns)
    "drivers_license": re.compile(r"\b[A-Z]{1,2}\d{6,8}\b"),
    
    # Bank Account (generic)
    "bank_account": re.compile(r"\b\d{8,17}\b"),  # Very generic, use with caution
    
    # Date of Birth patterns
    "dob": re.compile(r"\b(?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01])[-/](?:19|20)\d{2}\b"),
}

# Severity levels for PII types
PII_SEVERITY: dict[str, str] = {
    "ssn": "critical",
    "credit_card": "critical",
    "api_key": "critical",
    "aws_key": "critical",
    "passport_us": "high",
    "bank_account": "high",
    "drivers_license": "high",
    "email": "medium",
    "phone_us": "medium",
    "phone_intl": "medium",
    "ip_address": "low",
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
    ):
        self.enabled = enabled
        self.mode = mode
        self.exclusions = set(exclusions or [])
        
        # Build active patterns (excluding user exclusions)
        self.patterns = {
            name: pattern
            for name, pattern in PII_PATTERNS.items()
            if name not in self.exclusions
        }
        
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
        
        # Scan for each PII pattern
        for pii_type, pattern in self.patterns.items():
            for match in pattern.finditer(content):
                original = match.group()
                severity = PII_SEVERITY.get(pii_type, "medium")
                
                matches.append(PIIMatch(
                    type=pii_type,
                    severity=severity,
                    start=match.start(),
                    end=match.end(),
                    original=original,
                    redacted=self._redact_value(original, pii_type),
                ))
                
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
        # Sort matches by start position in reverse to replace from end
        sorted_matches = sorted(matches, key=lambda m: m.start, reverse=True)
        
        result = content
        for match in sorted_matches:
            placeholder = f"[REDACTED_{match.type.upper()}]"
            result = result[:match.start] + placeholder + result[match.end:]
        
        return result
    
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
        
        sorted_matches = sorted(result.matches, key=lambda m: m.start, reverse=True)
        
        hashed = content
        for match in sorted_matches:
            # Create deterministic hash
            hash_value = hashlib.sha256(match.original.encode()).hexdigest()[:12]
            placeholder = f"[HASH_{match.type.upper()}:{hash_value}]"
            hashed = hashed[:match.start] + placeholder + hashed[match.end:]
        
        return hashed


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
