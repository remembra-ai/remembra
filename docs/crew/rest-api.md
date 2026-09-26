# REST API stubs (§6)

Generated file: [openapi.json](openapi.json) (OpenAPI 3.1) from `remembra.crew.schemas.ROUTES`
and `REQUEST_SHAPES`. Do not edit it by hand; a test compares it with the generator.

Every operation carries extension fields the implementing work packages must honour:

| Field | Meaning |
|---|---|
| `x-access` | `crew` → `load_crew(crew_id, user, perm)`; `entity` → `load_crew_entity(x-entity, id, user, perm)` returning **404** on ACL failure; `resolve` → `resolve_project_access` (read-only; only join may create a crew); `user` → the caller's own crews/notifications; `host` → host token; `session` → session token of the named session; `existing` → an existing relay route that gains crew behaviour |
| `x-entity` | the kind passed to `load_crew_entity` |
| `x-permission` | `crew:read`, `crew:write`, `crew:claim`, `crew:override`, `crew:admin` |
| `x-human-only` | (H): JWT dashboard login plus crew role owner/admin (D27); API keys get 403 |
| `x-step-up` | login within 15 minutes: settings, override, unfreeze, waive, bypass codes, zone-change approval |
| `x-release` | `L0` or `L1` (L1 routes are listed but not built at launch) |
| `x-rate-bucket` | the §11 limiter bucket (`guard`, `claims`, `adopt`, `messages`, `tasks`, `events`, `heartbeat`, `join`, `snapshot`, `events_poll`, `default`) |
| tags | the owning work package |

Invariants the route-table tests enforce (and WP-14's route test should reuse):

* every route with `{crew_id}` uses `crew` access; every entity route names its entity kind and
  its first path parameter is that entity's id;
* `x-human-only` ⇔ permission `crew:override` or `crew:admin` (these two are excluded from
  `ROLE_PERMISSIONS[ADMIN]` for API keys);
* step-up implies human-only;
* operation ids are unique;
* static `/crews/…` paths (`resolve`, `join`, `inbox/overview`) are registered before
  `/crews/{crew_id}`.

Mutations accept `Idempotency-Key`; `If-Match` is required where listed (412 on mismatch).
Errors use `CrewError {error, message, blockers[]?, retry_after_s?}` with 404, 409, 412, 422, 423
and 429 as in §6. Responses that wrote an event carry `seq`.

Request bodies with a fixed shape: `HostRegister`, `Join`, `Heartbeat`, `ClientEvents`, `Leave`,
`Stall`, `Reason`, `Freeze`, `ZonesFile`, `Tree`, `Match`, `Claim`, `Override`, `Guard`,
`BypassIssue`, `BypassRedeem`, `TaskCreate`, `Report`, `Checkpoint`, `Message`. Other bodies are
left to the owning work package and must follow the same closed-object rule.
