"""Health-check helpers."""

from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def check_qdrant(target: Any) -> dict[str, Any]:
    """Ping Qdrant and return status details.

    ``target`` is either the app's ``QdrantStore`` — checked through its own
    client, i.e. the transport actually used for traffic (gRPC) — or a base
    URL string, checked over HTTP ``/healthz`` (used before the store exists).

    Note: Internal URLs are not exposed in the response for security.
    """
    if not isinstance(target, str) and hasattr(target, "health_check"):
        ok = bool(await target.health_check())
        return {"status": "ok" if ok else "degraded"}

    import httpx

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{target}/healthz")
            ok = r.status_code == 200
    except Exception as exc:
        log.warning("qdrant_health_check_failed", error=str(exc))
        ok = False

    # Don't expose internal service URLs - security best practice
    return {"status": "ok" if ok else "degraded"}


def build_health_response(
    version: str,
    qdrant: dict[str, Any],
    encryption_enabled: bool = False,
    build_sha: str | None = None,
) -> dict[str, Any]:
    overall = "ok" if qdrant["status"] == "ok" else "degraded"

    response = {
        "status": overall,
        "version": version,
        "dependencies": {"qdrant": qdrant},
    }

    if build_sha:
        response["build_sha"] = build_sha

    if encryption_enabled:
        response["encryption"] = "AES-256-GCM"

    return response
