"""One content policy for every write path (SEC-11 / SEC-13).

Single-memory store already ran PII detection and the prompt-injection
sanitizer; batch, bulk import, file/JSON import and ingest paths did not (or
discarded the result). ``prepare_content`` applies the same steps everywhere:

1. PII policy from ``REMEMBRA_PII_MODE`` (detect / redact / block).
2. Prompt-injection sanitizer (when ``sanitization_enabled``): sanitized text,
   trust score and checksum are all kept and must be passed to storage.

Credential redaction (SEC-23) happens earlier, in the request models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from remembra.config import get_settings


@dataclass
class PreparedContent:
    content: str
    trust_score: float = 1.0
    checksum: str | None = None
    blocked_pii_types: list[str] | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_pii_types)


def prepare_content(
    app_state: Any,
    content: str,
    source: str,
    *,
    sanitization_enabled: bool | None = None,
) -> PreparedContent:
    """Apply PII + sanitizer policy to ``content`` using the app's configured detectors.

    ``sanitization_enabled`` should come from the request's injected settings;
    when omitted the global settings are used.
    """
    if sanitization_enabled is None:
        sanitization_enabled = bool(getattr(get_settings(), "sanitization_enabled", True))
    pii_detector = getattr(app_state, "pii_detector", None)
    if pii_detector is not None:
        pii_result = pii_detector.scan(content, source=source)
        if pii_result.has_pii:
            if pii_result.blocked:
                return PreparedContent(
                    content=content,
                    blocked_pii_types=sorted({m.type for m in pii_result.matches}),
                )
            if pii_result.redacted_content:
                content = pii_result.redacted_content

    prepared = PreparedContent(content=content)
    sanitizer = getattr(app_state, "sanitizer", None)
    if sanitizer is not None and sanitization_enabled:
        analysis = sanitizer.analyze(content, source=source)
        prepared.content = analysis.content
        prepared.trust_score = float(analysis.trust_score)
        prepared.checksum = getattr(analysis, "checksum", None)
    return prepared
