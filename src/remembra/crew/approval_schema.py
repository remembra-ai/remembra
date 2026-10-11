"""Read-only schema contract for the unregistered approval adapter.

These immutable fingerprints identify the exact reviewed SQLite schema objects.
Installable DDL exists only in the synthetic test scaffold in this increment.
Production migration, retention and erasure review must precede installation.
There is no runtime DDL builder, setup function or migration registration here.
"""

import hashlib
from types import MappingProxyType
from typing import Final


def schema_fingerprint(sql: str) -> str:
    """Match SQLite's stored definition with the existing whitespace normalization."""
    return hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()


SCHEMA_FINGERPRINTS: Final = MappingProxyType(
    {
        "crew_approval_records": "e27e008a1c666e67d753531ccbe0b21ea4ab8daff0f82b77a1de9ab423c99fa0",
        "crew_approval_versions": "1b5d5d97feb10600275fe23a90d348e058f34ea36531366213359638218d8c7c",
        "crew_approval_records_immutable": "9b452530be84c362ddba20bbe9b48e1300705d86e3394f16e94c16881f2caa16",
        "crew_approval_versions_no_update": "9c7a6e4edbc076a93122f74f579a572b92bd255ceab32f170b80b2ac6088b3b9",
        "crew_approval_versions_no_delete": "d4367e48813b8c4cd3bf447e8f3592e13f2d2bf77cd6cd71eff8be22e585e411",
    }
)
