"""Encrypt the plaintext text fields left in existing Qdrant payloads.

Until 2026-09-27 a point's ``extracted_facts`` and ``entities``, and every
string inside a metadata list, were written in plaintext even with
``REMEMBRA_ENCRYPTION_KEY`` set; only ``content`` and the other metadata
strings were encrypted. Points written while no key was set are plaintext
throughout. New writes encrypt all of ``ENCRYPTED_FIELDS`` (storage/qdrant.py).

This scrolls one collection (payloads only, no vectors) and finds every point
whose text fields still hold a plaintext string. Dry run by default: it reports
counts per field and sample ids, never text. With ``apply=True`` it re-reads
each such point just before writing and overwrites only its text fields that
still hold plaintext (``set_payload``). Vectors and filter fields are not
touched, nothing is deleted, and strings that are already encrypted are kept
as they are, so a re-run finds nothing to do.

A point whose text fields changed between the scan and that re-read was
rewritten by the app, which encrypts on write, so it is skipped and counted as
``changed_during_scan``. The app can still rewrite a point in the moment
between the re-read and the write; run this with the new code deployed, and
re-run it until ``needs_update`` is 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from qdrant_client.http import models as qmodels

from remembra.storage.qdrant import ENCRYPTED_FIELDS, FIELD_USER_ID, encrypt_text_fields

log = structlog.get_logger(__name__)


@dataclass
class ReencryptReport:
    apply: bool
    collection: str
    scanned: int = 0
    up_to_date: int = 0
    needs_update: int = 0
    updated: int = 0
    changed_during_scan: int = 0
    errors: int = 0
    fields: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "apply" if self.apply else "dry_run",
            "collection": self.collection,
            "scanned": self.scanned,
            "up_to_date": self.up_to_date,
            "needs_update": self.needs_update,
            "updated": self.updated,
            "changed_during_scan": self.changed_during_scan,
            "errors": self.errors,
            "fields": dict(sorted(self.fields.items())),
            "samples": self.samples,
        }


def _text_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in ENCRYPTED_FIELDS if key in payload}


def _plaintext_fields(encryptor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """The text fields of ``payload`` that still hold plaintext, encrypted (empty when none do).

    ``encrypt`` keeps an already-encrypted string as it is and gives a new
    ciphertext for any plaintext one, so a field changes only if it holds plaintext.
    """
    current = _text_fields(payload)
    wanted = encrypt_text_fields(encryptor, current)
    return {key: value for key, value in wanted.items() if value != current[key]}


async def reencrypt_payloads(
    qdrant: Any,
    *,
    apply: bool = False,
    user_id: str | None = None,
    batch_size: int = 256,
    sample_limit: int = 10,
) -> ReencryptReport:
    """Report (and with ``apply`` encrypt) plaintext text fields in the collection ``qdrant`` points at.

    Raises:
        ValueError: encryption is off (no ``REMEMBRA_ENCRYPTION_KEY``), so there is no key to encrypt with.
    """
    encryptor = qdrant._encryptor
    if not encryptor.enabled:
        raise ValueError("REMEMBRA_ENCRYPTION_KEY is not set: there is no key to encrypt with.")

    report = ReencryptReport(apply=apply, collection=qdrant.collection_name)
    client = await qdrant._get_client()
    scroll_filter = None
    if user_id:
        scroll_filter = qmodels.Filter(must=[qmodels.FieldCondition(key=FIELD_USER_ID, match=qmodels.MatchValue(value=user_id))])

    offset: Any = None
    while True:
        points, offset = await client.scroll(
            collection_name=qdrant.collection_name,
            scroll_filter=scroll_filter,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        scanned: dict[str, dict[str, Any]] = {}
        for point in points:
            report.scanned += 1
            payload = dict(point.payload or {})
            stale = _plaintext_fields(encryptor, payload)
            if not stale:
                report.up_to_date += 1
                continue
            memory_id = str(point.id)
            report.needs_update += 1
            for key in stale:
                report.fields[key] = report.fields.get(key, 0) + 1
            if len(report.samples) < sample_limit:
                report.samples.append({"id": memory_id, "fields": sorted(stale)})
            scanned[memory_id] = _text_fields(payload)

        if apply and scanned:
            await _write(qdrant, client, encryptor, scanned, report)
        if offset is None:
            return report


async def _write(qdrant: Any, client: Any, encryptor: Any, scanned: dict[str, dict[str, Any]], report: ReencryptReport) -> None:
    fresh = await qdrant.get_raw_payloads(list(scanned))
    for memory_id, seen in scanned.items():
        current = fresh.get(memory_id)
        if current is None or _text_fields(current) != seen:
            report.changed_during_scan += 1  # rewritten (or deleted) by the app since the scan
            continue
        try:
            await client.set_payload(
                collection_name=qdrant.collection_name,
                payload=_plaintext_fields(encryptor, current),
                points=[memory_id],
            )
            report.updated += 1
        except Exception as e:  # noqa: BLE001 - keep going, report the count
            report.errors += 1
            log.warning("payload_reencrypt_failed", memory_id=memory_id, error=str(e))
