"""Security module for Remembra - sanitization, audit, encryption, and protection.

The names below load on first use, not at package import. Client-side entry
points (``remembra-relay``, ``remembra-mcp``) import
``remembra.security.secrets`` and ``remembra.security.error_sanitizer``; an
eager import here would drag in the audit logger and the server's storage
layer, which need ``aiosqlite`` and ``structlog`` -- packages a plain
``pipx install remembra`` does not have.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from remembra.security.anomaly_detector import AnomalyDetector, AnomalyReport, AnomalyResult
    from remembra.security.audit import AuditLogger
    from remembra.security.encryption import FieldEncryptor
    from remembra.security.pii_detector import PIIDetector, PIIScanResult, redact_pii, scan_for_pii
    from remembra.security.sanitizer import ContentSanitizer, SanitizationResult

_LAZY: dict[str, str] = {
    "AuditLogger": "remembra.security.audit",
    "ContentSanitizer": "remembra.security.sanitizer",
    "SanitizationResult": "remembra.security.sanitizer",
    "FieldEncryptor": "remembra.security.encryption",
    "PIIDetector": "remembra.security.pii_detector",
    "PIIScanResult": "remembra.security.pii_detector",
    "scan_for_pii": "remembra.security.pii_detector",
    "redact_pii": "remembra.security.pii_detector",
    "AnomalyDetector": "remembra.security.anomaly_detector",
    "AnomalyReport": "remembra.security.anomaly_detector",
    "AnomalyResult": "remembra.security.anomaly_detector",
}

__all__ = [
    # Audit
    "AuditLogger",
    # Content Sanitization (prompt injection)
    "ContentSanitizer",
    "SanitizationResult",
    # Encryption
    "FieldEncryptor",
    # PII Detection (OWASP ASI06)
    "PIIDetector",
    "PIIScanResult",
    "scan_for_pii",
    "redact_pii",
    # Anomaly Detection (OWASP ASI06)
    "AnomalyDetector",
    "AnomalyReport",
    "AnomalyResult",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
