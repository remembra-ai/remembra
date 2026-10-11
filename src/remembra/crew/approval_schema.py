"""Unregistered approval DDL proposal; importing this module changes nothing.

Only synthetic tests install these objects in this increment. Production
installation needs the Crew migration, retention and erasure owners' review.
There is intentionally no setup, migration runner, startup hook or fallback.
"""

from types import MappingProxyType
from typing import Final

SCHEMA_OBJECTS: Final = MappingProxyType(
    {
        "crew_approval_records": """CREATE TABLE crew_approval_records (
            id TEXT PRIMARY KEY,
            crew_id TEXT NOT NULL REFERENCES crews(id),
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
            body TEXT NOT NULL CHECK(json_valid(body)),
            CHECK(json_extract(body, '$.id') IS id),
            CHECK(json_extract(body, '$.version') IS version),
            CHECK(json_extract(body, '$.request.boundary.crew_id') IS crew_id),
            CHECK(json_extract(body, '$.request.boundary.tenant_id') IS tenant_id),
            CHECK(json_extract(body, '$.request.boundary.project_id') IS project_id)
        )""",
        "crew_approval_versions": """CREATE TABLE crew_approval_versions (
            approval_id TEXT NOT NULL REFERENCES crew_approval_records(id),
            version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
            crew_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            outbox_id TEXT NOT NULL UNIQUE REFERENCES crew_outbox(id),
            body TEXT NOT NULL CHECK(json_valid(body)),
            PRIMARY KEY(approval_id, version),
            UNIQUE(crew_id, event_seq),
            FOREIGN KEY(crew_id, event_seq) REFERENCES crew_events(crew_id, seq),
            CHECK(json_extract(body, '$.id') IS approval_id),
            CHECK(json_extract(body, '$.version') IS version)
        )""",
        "crew_approval_records_immutable": """CREATE TRIGGER crew_approval_records_immutable
            BEFORE UPDATE ON crew_approval_records
            WHEN NEW.id IS NOT OLD.id OR NEW.crew_id IS NOT OLD.crew_id
              OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.project_id IS NOT OLD.project_id
              OR NEW.request_key IS NOT OLD.request_key OR NEW.request_digest IS NOT OLD.request_digest
              OR NEW.version != OLD.version + 1
              OR json_extract(NEW.body, '$.request') IS NOT json_extract(OLD.body, '$.request')
              OR (json_extract(OLD.body, '$.decision') IS NOT NULL AND
                  json_extract(NEW.body, '$.decision') IS NOT json_extract(OLD.body, '$.decision'))
              OR json_extract(OLD.body, '$.invalidation') IS NOT NULL
              OR json_extract(OLD.body, '$.consumption') IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'immutable approval'); END""",
        "crew_approval_versions_no_update": """CREATE TRIGGER crew_approval_versions_no_update
            BEFORE UPDATE ON crew_approval_versions
            BEGIN SELECT RAISE(ABORT, 'immutable approval history'); END""",
        "crew_approval_versions_no_delete": """CREATE TRIGGER crew_approval_versions_no_delete
            BEFORE DELETE ON crew_approval_versions
            BEGIN SELECT RAISE(ABORT, 'immutable approval history'); END""",
    }
)

APPROVAL_DDL: Final = tuple(SCHEMA_OBJECTS.values())
